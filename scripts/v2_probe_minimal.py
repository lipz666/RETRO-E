"""Measure what minimal thinking effort costs in route quality (v2_pilot).

The gateway exposes a no-thinking variant, `<model>(minimal)`. Hidden reasoning tokens
dominate this project's spend (about 16k per route generation), so the variant would change
what is affordable. It is only usable if route quality holds up, which cannot be assumed.

This probe generates baseline routes on optimizer targets with minimal effort and judges them
head to head against the cached high-effort baselines for the same targets and sample index.
A win rate near 0.5 means the two are interchangeable; well below 0.5 means minimal is worse.

    uv run python scripts/v2_probe_minimal.py --targets 20 --workers 20
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from concurrent import futures
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retro_e.api import OpenAICompatibleClient
from retro_e.config import load_config
from retro_e.judge import JudgmentError, parse_judgment
from retro_e.prompts import compose_generation_prompt, compose_judge_prompt
from retro_e.validation import validate_response

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "results/v2/generations/optimizer_pool_baseline.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", type=int, default=20)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--config", default="config/v2_pilot.toml")
    args = parser.parse_args()

    config = load_config(args.config).with_roles(generator="generator", judge="judge")
    high_model = config.generator_model
    minimal_model = f"{high_model}(minimal)"
    judge_model = config.judge_model

    cached = {}
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        row = json.loads(line)
        if row["sample_id"] == 0 and row["valid"]:
            cached[row["target_id"]] = row
    chosen = [cached[key] for key in sorted(cached)[: args.targets]]
    print(f"targets={len(chosen)}  high={high_model}  minimal={minimal_model}")

    def generate(row: dict[str, Any]) -> dict[str, Any]:
        prompt = compose_generation_prompt(config, row["target_smiles"], "baseline")
        with OpenAICompatibleClient(config) as client:
            completion = client.complete(
                prompt,
                temperature=config.generation.temperature,
                top_p=config.generation.top_p,
                max_tokens=config.generation.max_tokens,
                model=minimal_model,
            )
        validation = validate_response(
            completion.content, row["target_smiles"], expected_routes=config.generation.n_routes
        )
        usage = completion.usage or {}
        details = usage.get("completion_tokens_details") or {}
        return {
            "target_id": row["target_id"],
            "target_smiles": row["target_smiles"],
            "valid": validation.valid,
            "route_text": json.dumps(
                validation.normalized or validation.parsed, ensure_ascii=False,
                separators=(",", ":"),
            )
            if validation.parsed is not None
            else "",
            "reasoning_tokens": details.get("reasoning_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "latency": completion.latency_seconds,
        }

    with futures.ThreadPoolExecutor(args.workers) as pool:
        produced = list(pool.map(generate, chosen))

    high_tokens = [row["usage"]["total_tokens"] for row in chosen]
    high_reasoning = [
        (row["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        for row in chosen
    ]
    print("\nCOST")
    print(
        f"  high     reasoning {statistics.mean(high_reasoning):7.0f}  "
        f"total {statistics.mean(high_tokens):7.0f}  latency "
        f"{statistics.mean(row['latency_seconds'] for row in chosen):5.1f}s"
    )
    print(
        f"  minimal  reasoning {statistics.mean(r['reasoning_tokens'] for r in produced):7.0f}  "
        f"total {statistics.mean(r['total_tokens'] for r in produced):7.0f}  latency "
        f"{statistics.mean(r['latency'] for r in produced):5.1f}s"
    )
    print(
        f"  valid    high {sum(r['valid'] for r in chosen)}/{len(chosen)}   "
        f"minimal {sum(r['valid'] for r in produced)}/{len(produced)}"
    )

    # Head to head, each pair judged in both display orders to cancel position bias.
    pairs = [
        (minimal_row, cached[minimal_row["target_id"]])
        for minimal_row in produced
        if minimal_row["valid"]
    ]

    def judge(job: tuple[dict[str, Any], dict[str, Any], bool]) -> str:
        minimal_row, high_row, swapped = job
        first, second = (high_row, minimal_row) if swapped else (minimal_row, high_row)
        prompt = compose_judge_prompt(
            config, minimal_row["target_smiles"], first["route_text"], second["route_text"]
        )
        with OpenAICompatibleClient(config) as client:
            completion = client.complete(
                prompt, temperature=0.0, top_p=1.0,
                max_tokens=config.judge.max_tokens, model=judge_model,
            )
        try:
            decision = parse_judgment(completion.content)["decision"]
        except JudgmentError:
            return "unparsed"
        return {
            "A": "high" if swapped else "minimal",
            "B": "minimal" if swapped else "high",
            "Tie": "Tie",
        }[decision]

    jobs = [(m, h, s) for m, h in pairs for s in (False, True)]
    with futures.ThreadPoolExecutor(args.workers) as pool:
        verdicts = list(pool.map(judge, jobs))

    counts = Counter(verdicts)
    decided = counts["minimal"] + counts["high"]
    print("\nHEAD TO HEAD (minimal vs high, each pair judged in both orders)")
    print(f"  {dict(counts)}")
    if decided:
        print(f"  minimal win rate excluding ties: {counts['minimal'] / decided:.1%}")
    score = counts["minimal"] + 0.5 * counts["Tie"]
    print(f"  minimal mean score (win 1 / tie 0.5 / loss 0): {score / max(1, len(verdicts)):.3f}")
    print("\n  0.5 means interchangeable. Well below 0.5 means minimal degrades route quality.")


if __name__ == "__main__":
    main()
