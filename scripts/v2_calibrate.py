"""v2_pilot startup calibration (plan v2 section 3.1).

Measures what candidate models actually cost and how stable they are, on real Stage-5 route
pairs and the real judge prompt, before any v2 experiment call is made. Generates no new
routes: it re-judges frozen round-1 generations.

    uv run python scripts/v2_calibrate.py --probe cost      # hidden reasoning tokens per judge call
    uv run python scripts/v2_calibrate.py --probe order     # A/B position bias and flip consistency
    uv run python scripts/v2_calibrate.py --probe judge_v3  # structured-prompt parse rate

Results are recorded in results/v2/calibration/startup_calibration.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from concurrent import futures
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retro_e.api import OpenAICompatibleClient
from retro_e.config import ExperimentConfig, load_config
from retro_e.judge import JudgmentError, parse_decision, parse_judgment
from retro_e.prompts import compose_judge_prompt

COST_PROBE_MODELS = [
    "gemini-3.8-flash-high",
    "gemini-3.7-flash-high",
    "gemini-3.6-flash-high",
    "gpt-5.4-mini",
    "gpt-5.5",
    "claude-haiku-4-5-20251001",
    "gpt-5.6-luna",
]
ORDER_PROBE_MODELS = [
    "claude-haiku-4-5-20251001",
    "gemini-3.7-flash-high",
    "gemini-3.8-flash-high",
]
LEFT = Path("results/generations/final_E_star.jsonl")
RIGHT = Path("results/generations/final_baseline.jsonl")


def valid_rows(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        row = json.loads(line)
        if row.get("valid"):
            rows[(row["target_id"], row["sample_id"])] = row
    return rows


def shared_pairs(count: int) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    left, right = valid_rows(LEFT), valid_rows(RIGHT)
    keys = [key for key in sorted(left) if key in right and key[1] == 0][:count]
    return [(left[key], right[key]) for key in keys]


def call(
    config: ExperimentConfig, model: str, prompt: str, max_tokens: int
) -> dict[str, Any]:
    with OpenAICompatibleClient(config) as client:
        completion = client.complete(
            prompt, temperature=0.0, top_p=1.0, max_tokens=max_tokens, model=model
        )
    usage = completion.usage or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "content": completion.content,
        "reasoning_tokens": details.get("reasoning_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "latency_seconds": completion.latency_seconds,
        "response_model": completion.response_model,
    }


def run_all(jobs: list[Any], work: Any, workers: int) -> list[Any]:
    with futures.ThreadPoolExecutor(workers) as pool:
        return list(pool.map(work, jobs))


def probe_cost(config: ExperimentConfig, pairs: int) -> None:
    """Hidden reasoning tokens dominate judge cost and are invisible without measuring."""
    prompts = [
        compose_judge_prompt(config, a["target_smiles"], a["route_text"], b["route_text"])
        for a, b in shared_pairs(pairs)
    ]

    def work(job: tuple[str, str]) -> dict[str, Any]:
        model, prompt = job
        try:
            result = call(config, model, prompt, 150)
        except Exception as exc:  # noqa: BLE001 -- one dead model must not kill the sweep
            return {"model": model, "error": str(exc)[:160]}
        try:
            decision = parse_decision(result["content"])
        except JudgmentError:
            decision = None
        return {"model": model, "decision": decision, **result}

    rows = run_all([(m, p) for m in COST_PROBE_MODELS for p in prompts], work, 8)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["model"]].append(row)

    header = f"{'model':<28}{'ok':>4}{'parsed':>8}{'reason':>8}{'out':>7}{'total':>8}{'lat':>7}"
    print(header)
    for model in COST_PROBE_MODELS:
        ok = [row for row in grouped[model] if "error" not in row]
        if not ok:
            print(f"{model:<28} ERROR {grouped[model][0]['error']}")
            continue
        parsed = sum(row["decision"] is not None for row in ok)

        def mean(key: str, rows: list[dict[str, Any]] = ok) -> float:
            return statistics.mean(row[key] for row in rows)

        print(
            f"{model:<28}{len(ok):>4}{parsed:>5}/{len(ok):<2}{mean('reasoning_tokens'):>8.0f}"
            f"{mean('completion_tokens'):>7.0f}{mean('total_tokens'):>8.0f}"
            f"{mean('latency_seconds'):>7.1f}"
        )


def probe_order(config: ExperimentConfig, pairs: int) -> None:
    """A judge that flips its verdict when A and B swap is reading position, not chemistry."""
    pair_rows = shared_pairs(pairs)

    def work(job: tuple[str, int, bool]) -> dict[str, Any]:
        model, index, swapped = job
        e_star, baseline = pair_rows[index]
        first, second = (baseline, e_star) if swapped else (e_star, baseline)
        prompt = compose_judge_prompt(
            config, e_star["target_smiles"], first["route_text"], second["route_text"]
        )
        result = call(config, model, prompt, 150)
        try:
            decision = parse_decision(result["content"])
        except JudgmentError:
            return {"model": model, "index": index, "decision": None, "winner": "unparsed"}
        winner = {
            "A": "baseline" if swapped else "E_star",
            "B": "E_star" if swapped else "baseline",
            "Tie": "Tie",
        }[decision]
        return {"model": model, "index": index, "decision": decision, "winner": winner}

    jobs = [
        (model, index, swapped)
        for model in ORDER_PROBE_MODELS
        for index in range(len(pair_rows))
        for swapped in (False, True)
    ]
    rows = run_all(jobs, work, 10)
    by_model_pair: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_model_pair[(row["model"], row["index"])].append(row)

    print(f"{'model':<28}{'A-rate':>9}{'E*-win':>9}{'flip-consistent':>18}")
    for model in ORDER_PROBE_MODELS:
        subset = [row for row in rows if row["model"] == model]
        a_rate = sum(row["decision"] == "A" for row in subset) / len(subset)
        e_rate = sum(row["winner"] == "E_star" for row in subset) / len(subset)
        consistent = sum(
            len({row["winner"] for row in by_model_pair[(model, i)]}) == 1
            for i in range(len(pair_rows))
        ) / len(pair_rows)
        print(f"{model:<28}{a_rate:>9.0%}{e_rate:>9.0%}{consistent:>18.0%}")


def probe_judge_v3(config: ExperimentConfig, pairs: int) -> None:
    """A new judge prompt must prove its parse rate on real data before it is frozen.

    Round 1 learned this the hard way: judge_v1 hit a 50% parse failure rate after the judge
    model changed, because the model wrote prose before the verdict and was truncated.
    """
    bound = config.with_roles(generator="generator", judge="judge")

    def work(pair: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        e_star, baseline = pair
        prompt = compose_judge_prompt(
            bound, e_star["target_smiles"], e_star["route_text"], baseline["route_text"]
        )
        result = call(bound, bound.judge_model, prompt, bound.judge.max_tokens)
        try:
            result["verdict"] = parse_judgment(result["content"])
        except JudgmentError as exc:
            result["verdict"] = None
            result["parse_failure"] = str(exc)[:120]
        return result

    rows = run_all(shared_pairs(pairs), work, 6)
    parsed = [row for row in rows if row["verdict"]]
    structured = [row for row in parsed if row["verdict"]["structured"]]
    def mean(key: str) -> float:
        return statistics.mean(row[key] for row in rows)

    print(f"judge model  {bound.judge_model}   prompt {bound.paths.judge_prompt.name}")
    print(f"parsed       {len(parsed)}/{len(rows)}")
    print(f"structured   {len(structured)}/{len(rows)}")
    print(
        f"tokens       reasoning {mean('reasoning_tokens'):.0f} | visible "
        f"{mean('completion_tokens'):.0f} | total {mean('total_tokens'):.0f} | "
        f"latency {mean('latency_seconds'):.1f}s"
    )
    print("decisions   ", dict(Counter(row["verdict"]["decision"] for row in parsed)))
    print(
        "fatal flagged",
        sum(row["verdict"]["fatal_A"] or row["verdict"]["fatal_B"] for row in structured),
        f"/ {len(structured)}",
    )
    print(
        "decisive step",
        sum(bool(row["verdict"]["decisive_step"]) for row in structured),
        f"/ {len(structured)}",
    )
    tags = Counter(tag for row in structured for tag in row["verdict"]["error_tags"])
    unknown = Counter(tag for row in structured for tag in row["verdict"]["unknown_error_tags"])
    print("error tags  ", dict(tags))
    print("UNKNOWN tags", dict(unknown) or "none")
    for row in rows:
        if not row["verdict"] or not row["verdict"]["structured"]:
            reason = row.get("parse_failure") or row["verdict"]["parse_error"]
            print("  DEGRADED:", reason, "|", row["content"][:110].replace("\n", "|"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", choices=["cost", "order", "judge_v3"], required=True)
    parser.add_argument("--pairs", type=int, default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    defaults = {"cost": ("config/experiment.toml", 5), "order": ("config/experiment.toml", 10)}
    config_path, pairs = defaults.get(args.probe, ("config/v2_pilot.toml", 12))
    config = load_config(args.config or config_path)
    pairs = args.pairs or pairs
    {"cost": probe_cost, "order": probe_order, "judge_v3": probe_judge_v3}[args.probe](
        config, pairs
    )


if __name__ == "__main__":
    main()
