"""Does generation temperature explain the dominant variance component?

The nested decomposition of round-1 Stage 5 puts 68-75% of measurement variance in generation
sampling (sigma^2_sample = 0.619) against 18-22% for the judge and 7-10% for genuine
molecule-level heterogeneity. Since we measure a mean effect rather than best-of-k, generation
diversity is pure noise here, and temperature is the one precision lever that costs nothing.

The 0.3 arm already exists: round-1 Stage 5 judged E0 vs baseline on 100 targets with 3
samples and 3 votes. This probe generates only the temperature-0 arm, on the same targets,
with the same samples and votes, so the two sigma^2_sample estimates are directly comparable.

    uv run python scripts/probe_temperature.py --protocol-hash HASH --targets 20

Budget: 120 generations and 180 judge calls. This measures the instrument, not a hypothesis:
no context is trained or selected, so reusing round-1 test targets exposes them to nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retro_e.api import OpenAICompatibleClient
from retro_e.config import ExperimentConfig, load_config
from retro_e.io import append_jsonl, assert_protocol, existing_keys, read_jsonl
from retro_e.judge import run_judging
from retro_e.prompts import compose_generation_prompt
from retro_e.validation import validate_response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONDITIONS = ("baseline", "E0")


def generate_one(
    config: ExperimentConfig, target: dict[str, Any], condition: str,
    sample_id: int, protocol_hash: str,
) -> dict[str, Any]:
    smiles = str(target["target_smiles"])
    prompt = compose_generation_prompt(config, smiles, condition)
    with OpenAICompatibleClient(config) as client:
        completion = client.complete(
            prompt, temperature=config.generation.temperature, top_p=config.generation.top_p,
            max_tokens=config.generation.max_tokens, model=config.generator_model,
        )
    validation = validate_response(
        completion.content, smiles, expected_routes=config.generation.n_routes
    )
    return {
        "record_id": f"temp0:{condition}:{target['target_id']}:{sample_id}:{protocol_hash[:12]}",
        "target_id": str(target["target_id"]), "target_smiles": smiles,
        "condition": condition, "sample_id": sample_id,
        "model": config.generator_model, "response_model": completion.response_model,
        "temperature": config.generation.temperature, "top_p": config.generation.top_p,
        "max_tokens": config.generation.max_tokens, "n_routes": config.generation.n_routes,
        "system_prompt_version": config.protocol.system_prompt_version,
        "experience_version": condition, "protocol_sha256": protocol_hash,
        "route_text": json.dumps(
            validation.normalized or validation.parsed, ensure_ascii=False,
            separators=(",", ":"),
        ) if validation.parsed is not None else "",
        "raw_response": completion.content, "valid": validation.valid,
        "validation_errors": list(validation.errors),
        "validation_warnings": list(validation.warnings),
        "usage": completion.usage, "request_id": completion.request_id,
        "latency_seconds": completion.latency_seconds,
        "created_at": datetime.now(UTC).isoformat(),
    }


def sigma2_sample(path: Path, favored: str, keep: set[str]) -> tuple[float, int, float]:
    """Within-target variance of pair-level majority scores. Mirrors the round-1 decomposition."""
    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(path):
        if str(row["target_id"]) in keep:
            by_pair[row["pair_id"]].append(row)

    def majority(rows: list[dict[str, Any]]) -> float:
        counts = {1.0: 0, 0.0: 0, -1.0: 0}
        for row in rows:
            d = row["decision"]
            s = 0.0 if d == "Tie" else (1.0 if row[f"route_{d}_source"] == favored else -1.0)
            counts[s] += 1
        best = max(counts.values())
        winners = [k for k, n in counts.items() if n == best]
        return winners[0] if len(winners) == 1 else 0.0

    within_pair = [
        statistics.pvariance([
            0.0 if r["decision"] == "Tie"
            else (1.0 if r[f"route_{r['decision']}_source"] == favored else -1.0)
            for r in rows
        ])
        for rows in by_pair.values()
        if len(rows) > 1 and rows[0]["adjudication"] == "llm_judge"
    ]
    by_target: dict[str, list[float]] = defaultdict(list)
    for rows in by_pair.values():
        by_target[str(rows[0]["target_id"])].append(majority(rows))
    spreads = [statistics.pvariance(v) for v in by_target.values() if len(v) > 1]
    return (
        statistics.mean(spreads) if spreads else float("nan"),
        len(by_target),
        statistics.mean(within_pair) if within_pair else float("nan"),
    )


def route_identity(path: Path, keep: set[str]) -> dict[str, Any]:
    """How different are independent generations for one (target, condition)? Free diagnostic."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(path):
        if str(row["target_id"]) in keep:
            groups[(str(row["target_id"]), row["condition"])].append(row)
    identical = distinct = 0
    step_spreads = []
    for rows in groups.values():
        texts = [r.get("route_text") or "" for r in rows]
        if len(texts) < 2:
            continue
        identical += 1 if len(set(texts)) == 1 else 0
        distinct += len(set(texts))
        steps = []
        for t in texts:
            # An unparseable or invalid route contributes no step count; the record itself is
            # retained elsewhere with its validation errors, so nothing is being hidden here.
            try:
                steps.append(len(json.loads(t)["routes"][0]["steps"]))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        if len(steps) > 1:
            step_spreads.append(max(steps) - min(steps))
    n = sum(1 for rows in groups.values() if len(rows) > 1)
    return {
        "groups": n,
        "all_three_identical_rate": identical / n if n else None,
        "mean_distinct_routes_per_group": distinct / n if n else None,
        "mean_step_count_spread": statistics.mean(step_spreads) if step_spreads else None,
    }


def run(args: argparse.Namespace) -> None:
    config = load_config(args.config).with_roles(generator="generator", judge="judge")
    targets = sorted(
        read_jsonl(ROOT / "data/processed/test_targets.jsonl"),
        key=lambda t: str(t["target_id"]),
    )[: args.targets]
    keep = {str(t["target_id"]) for t in targets}
    out = Path(config.paths.results_dir) / "temperature_probe"
    gen, jud = out / "generations", out / "judgments"
    gen.mkdir(parents=True, exist_ok=True)
    jud.mkdir(parents=True, exist_ok=True)
    logger.info(
        "temp=%s targets=%d samples=%d votes=%d",
        config.generation.temperature, len(targets),
        config.generation.samples_per_target, config.judge.runs_per_pair,
    )

    if args.stage in ("all", "generate"):
        for condition in CONDITIONS:
            path = gen / f"{condition}.jsonl"
            assert_protocol(path, args.protocol_hash)
            done = existing_keys(path, ["target_id", "sample_id"])
            jobs = [
                (t, s) for t in targets
                for s in range(config.generation.samples_per_target)
                if (str(t["target_id"]), s) not in done
            ]
            failed = 0
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                fs = [
                    pool.submit(generate_one, config, t, condition, s, args.protocol_hash)
                    for t, s in jobs
                ]
                for f in as_completed(fs):
                    try:
                        append_jsonl(path, f.result())
                    except Exception:
                        failed += 1
                        logger.exception("%s generation failed", condition)
            logger.info("generated %s: planned=%d failed=%d", condition, len(jobs), failed)

    if args.stage in ("all", "judge"):
        run_judging(
            config, gen / "E0.jsonl", gen / "baseline.jsonl", "E0", "baseline",
            "E0_vs_baseline_temp0", jud / "E0_vs_baseline_temp0.jsonl",
            args.protocol_hash, workers=args.workers,
        )
        logger.info("judged E0_vs_baseline_temp0")

    if args.stage in ("all", "metrics"):
        t0_s2, t0_n, t0_judge = sigma2_sample(
            jud / "E0_vs_baseline_temp0.jsonl", "E0", keep
        )
        t3_s2, t3_n, t3_judge = sigma2_sample(
            ROOT / "results/judgments/final_E0_vs_baseline.jsonl", "E0", keep
        )
        ident0 = route_identity(gen / "baseline.jsonl", keep)
        ident0_e0 = route_identity(gen / "E0.jsonl", keep)
        ident3 = route_identity(ROOT / "results/generations/final_baseline.jsonl", keep)
        ident3_e0 = route_identity(ROOT / "results/generations/final_E0.jsonl", keep)

        report = {
            "phase": "v2_pilot", "stage": "temperature_probe",
            "question": "Is sigma^2_sample an artefact of temperature 0.3?",
            "protocol_sha256": args.protocol_hash,
            "targets": sorted(keep),
            "arms": {
                "temperature_0.0": {
                    "sigma2_sample": t0_s2, "sigma2_judge_within_pair": t0_judge,
                    "targets": t0_n,
                    "route_identity_baseline": ident0, "route_identity_E0": ident0_e0,
                },
                "temperature_0.3_round1": {
                    "sigma2_sample": t3_s2, "sigma2_judge_within_pair": t3_judge,
                    "targets": t3_n,
                    "route_identity_baseline": ident3, "route_identity_E0": ident3_e0,
                },
            },
            "note": (
                "Same targets, same samples per target, same votes per pair, same judge. The "
                "only difference is generation temperature. sigma^2_judge is reported as a "
                "control: the judge is unchanged, so it should not move."
            ),
            "created_at": datetime.now(UTC).isoformat(),
        }
        (out / "temperature_metrics.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\n{'arm':24}{'sigma2_sample':>15}{'sigma2_judge':>14}{'targets':>9}")
        for name, b in report["arms"].items():
            print(
                f"{name:24}{b['sigma2_sample']:>15.4f}{b['sigma2_judge_within_pair']:>14.4f}"
                f"{b['targets']:>9}"
            )
        print("\n路线同一性（同 target、同 condition 的 3 次独立生成）")
        for name, b in report["arms"].items():
            for cond in ("baseline", "E0"):
                d = b[f"route_identity_{cond}"]
                print(
                    f"  {name:22} {cond:9} 三次全同={d['all_three_identical_rate']}  "
                    f"不同路线数/组={d['mean_distinct_routes_per_group']}  "
                    f"步数极差={d['mean_step_count_spread']}"
                )
        if t3_s2 and not math.isnan(t0_s2):
            print(f"\nsigma2_sample 变化: {t3_s2:.4f} → {t0_s2:.4f}  ({t0_s2/t3_s2:.2f}x)")
        print("wrote", out / "temperature_metrics.json")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--protocol-hash", required=True)
    p.add_argument("--config", default="config/v2_temp0.toml")
    p.add_argument("--targets", type=int, default=20)
    p.add_argument("--workers", type=int, default=40)
    p.add_argument("--stage", choices=["all", "generate", "judge", "metrics"], default="all")
    run(p.parse_args())


if __name__ == "__main__":
    main()
