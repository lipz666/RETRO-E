"""E1 development evaluation on dev_new (plan v2 section 6.3).

Generates routes on the 80 dev_new targets under 11 conditions -- baseline, E0, and the nine
E1 winners (3 methods x 3 seeds) -- then runs the four per-seed comparisons:

    reflective vs baseline, vs E0, vs search_only, vs instruction_opt

    uv run python scripts/run_e1_dev_eval.py --protocol-hash HASH --workers 40
    uv run python scripts/run_e1_dev_eval.py --protocol-hash HASH --stage metrics

Budget: 11 x 80 x 2 = 1,760 generations and 3 x 4 x 80 x 2 = 1,920 judged pairs at one vote
each, matching the plan. Pairing is by (target_id, sample_id): never best-of-two.

Deliberately a script rather than a `retro-e generate --condition` extension. The nine winner
contexts are arbitrary files, and adding a CLI flag would edit src/retro_e, changing the
protocol hash that the nine E1 runs are pinned to. scripts/ is outside the hashed set.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retro_e.api import OpenAICompatibleClient
from retro_e.config import ExperimentConfig, load_config
from retro_e.io import append_jsonl, assert_protocol, existing_keys, read_jsonl
from retro_e.judge import run_judging, source_winner
from retro_e.optimize import MUTATION_METHODS
from retro_e.prompts import compose_generation_prompt, compose_generation_prompt_from_text
from retro_e.validation import validate_response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SEEDS = (11, 29, 47)
SAMPLES = 2
COMPARISONS = ("baseline", "E0", "search_only", "instruction_opt")


def conditions(config: ExperimentConfig) -> dict[str, str | None]:
    """Condition name -> experience text, or None for baseline."""
    found: dict[str, str | None] = {
        "baseline": None,
        "E0": config.paths.experience_e0.read_text(encoding="utf-8").strip(),
    }
    for method in MUTATION_METHODS:
        for seed in SEEDS:
            winner = config.root / "contexts/v2" / method / str(seed) / "winner.md"
            if not winner.exists():
                raise FileNotFoundError(
                    f"Missing E1 winner: {winner}. Run scripts/run_e1_matrix.py for "
                    f"--method {method} --seed {seed} first; dev evaluation never re-picks "
                    "a winner (plan v2 section 6.2)."
                )
            found[f"{method}_{seed}"] = winner.read_text(encoding="utf-8").strip()
    return found


def generate_one(
    config: ExperimentConfig,
    target: dict[str, Any],
    condition: str,
    text: str | None,
    sample_id: int,
    protocol_hash: str,
) -> dict[str, Any]:
    """Mirrors retro_e.generate._generate_one's record shape so run_judging can consume it."""
    target_id = str(target["target_id"])
    smiles = str(target["target_smiles"])
    prompt = (
        compose_generation_prompt(config, smiles, "baseline")
        if text is None
        else compose_generation_prompt_from_text(config, smiles, text)
    )
    with OpenAICompatibleClient(config) as client:
        completion = client.complete(
            prompt,
            temperature=config.generation.temperature,
            top_p=config.generation.top_p,
            max_tokens=config.generation.max_tokens,
            model=config.generator_model,
        )
    validation = validate_response(
        completion.content, smiles, expected_routes=config.generation.n_routes
    )
    return {
        "record_id": f"{condition}:{target_id}:{sample_id}:{protocol_hash[:12]}",
        "target_id": target_id,
        "target_smiles": smiles,
        "condition": condition,
        "sample_id": sample_id,
        "model": config.generator_model,
        "response_model": completion.response_model,
        "temperature": config.generation.temperature,
        "top_p": config.generation.top_p,
        "max_tokens": config.generation.max_tokens,
        "n_routes": config.generation.n_routes,
        "system_prompt_version": config.protocol.system_prompt_version,
        "experience_version": condition,
        "protocol_sha256": protocol_hash,
        "route_text": json.dumps(
            validation.normalized or validation.parsed, ensure_ascii=False, separators=(",", ":")
        )
        if validation.parsed is not None
        else "",
        "raw_response": completion.content,
        "valid": validation.valid,
        "validation_errors": list(validation.errors),
        "validation_warnings": list(validation.warnings),
        "usage": completion.usage,
        "request_id": completion.request_id,
        "latency_seconds": completion.latency_seconds,
        "created_at": datetime.now(UTC).isoformat(),
    }


def generate_condition(
    config: ExperimentConfig,
    targets: list[dict[str, Any]],
    condition: str,
    text: str | None,
    output: Path,
    protocol_hash: str,
    workers: int,
) -> dict[str, int]:
    assert_protocol(output, protocol_hash)
    done = existing_keys(output, ["target_id", "sample_id"])
    jobs = [
        (target, sample_id)
        for target in targets
        for sample_id in range(SAMPLES)
        if (str(target["target_id"]), sample_id) not in done
    ]
    counts = {"planned": len(targets) * SAMPLES, "skipped": len(done), "completed": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(generate_one, config, target, condition, text, sample_id, protocol_hash)
            for target, sample_id in jobs
        ]
        for future in as_completed(futures):
            try:
                append_jsonl(output, future.result())
                counts["completed"] += 1
            except Exception:  # one dead request must not lose the others
                counts["failed"] += 1
                logger.exception("%s generation failed after retries", condition)
    return counts


def comparison_metrics(path: Path) -> dict[str, Any]:
    """Per-target mean paired score, then cluster on target (plan v2 section 5.2)."""
    rows = list(read_jsonl(path))
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_pair.setdefault(row["pair_id"], []).append(row)
    per_target: dict[str, list[float]] = {}
    wins = losses = ties = 0
    for pair_rows in by_pair.values():
        winner = source_winner(pair_rows)
        left = pair_rows[0]["left_source"]
        score = 1.0 if winner == left else (0.0 if winner == "Tie" else -1.0)
        if score > 0:
            wins += 1
        elif score < 0:
            losses += 1
        else:
            ties += 1
        per_target.setdefault(pair_rows[0]["target_id"], []).append(score)
    target_means = [sum(v) / len(v) for v in per_target.values()]
    return {
        "pairs": len(by_pair),
        "targets": len(per_target),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "net_win_rate": (wins - losses) / len(by_pair) if by_pair else 0.0,
        "target_mean_net": sum(target_means) / len(target_means) if target_means else 0.0,
        "target_means": target_means,
    }


def run(args: argparse.Namespace) -> None:
    config = load_config(args.config).with_roles(generator="generator", judge="judge")
    targets = list(read_jsonl(config.paths.h1_screen_targets))  # dev_new in config/v2_pilot.toml
    out_dir = Path(config.paths.results_dir) / "dev_eval"
    gen_dir = out_dir / "generations"
    judge_dir = out_dir / "judgments"
    gen_dir.mkdir(parents=True, exist_ok=True)
    judge_dir.mkdir(parents=True, exist_ok=True)
    texts = conditions(config)
    logger.info("dev_new targets=%d conditions=%d samples=%d", len(targets), len(texts), SAMPLES)

    if args.stage in ("all", "generate"):
        for condition, text in texts.items():
            counts = generate_condition(
                config, targets, condition, text,
                gen_dir / f"{condition}.jsonl", args.protocol_hash, args.workers,
            )
            logger.info("generated %s: %s", condition, counts)

    if args.stage in ("all", "judge"):
        for seed in SEEDS:
            for right in COMPARISONS:
                right_name = right if right in ("baseline", "E0") else f"{right}_{seed}"
                name = f"reflective_{seed}_vs_{right_name}"
                run_judging(
                    config,
                    gen_dir / f"reflective_{seed}.jsonl",
                    gen_dir / f"{right_name}.jsonl",
                    f"reflective_{seed}",
                    right_name,
                    name,
                    judge_dir / f"{name}.jsonl",
                    args.protocol_hash,
                    workers=args.workers,
                )
                logger.info("judged %s", name)

    if args.stage in ("all", "metrics"):
        report: dict[str, Any] = {
            "phase": "v2_pilot",
            "stage": "e1_dev_eval",
            "protocol_sha256": args.protocol_hash,
            "targets": len(targets),
            "samples_per_target": SAMPLES,
            "votes_per_pair": config.judge.runs_per_pair,
            "invalid_rate_by_condition": {},
            "comparisons": {},
            "note": (
                "Exploratory: plan v2 section 5.2 does not require p < 0.05 here. Each seed is "
                "reported separately; the pooled figure is not a substitute for showing all "
                "three, and cluster intervals cover target sampling only, not optimizer "
                "randomness across seeds."
            ),
            "created_at": datetime.now(UTC).isoformat(),
        }
        for condition in texts:
            rows = list(read_jsonl(gen_dir / f"{condition}.jsonl"))
            invalid = sum(not row["valid"] for row in rows)
            report["invalid_rate_by_condition"][condition] = {
                "generations": len(rows),
                "invalid": invalid,
                "rate": invalid / len(rows) if rows else None,
            }
        for path in sorted(judge_dir.glob("*.jsonl")):
            report["comparisons"][path.stem] = comparison_metrics(path)
            report["comparisons"][path.stem].pop("target_means")
        for right in COMPARISONS:
            per_seed = [
                report["comparisons"][f"reflective_{seed}_vs_"
                                     f"{right if right in ('baseline', 'E0') else f'{right}_{seed}'}"]
                for seed in SEEDS
                if f"reflective_{seed}_vs_"
                f"{right if right in ('baseline', 'E0') else f'{right}_{seed}'}"
                in report["comparisons"]
            ]
            if per_seed:
                nets = [entry["net_win_rate"] for entry in per_seed]
                report.setdefault("reflective_vs", {})[right] = {
                    "per_seed_net_win_rate": nets,
                    "mean_net_win_rate": sum(nets) / len(nets),
                    "seeds_positive": sum(net > 0 for net in nets),
                    "seeds_run": len(nets),
                }
        summary_path = out_dir / "dev_eval_metrics.json"
        summary_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report.get("reflective_vs", {}), indent=2, sort_keys=True))
        logger.info("wrote %s", summary_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol-hash", required=True)
    parser.add_argument("--config", default="config/v2_pilot.toml")
    parser.add_argument("--workers", type=int, default=40)
    parser.add_argument("--stage", choices=["all", "generate", "judge", "metrics"], default="all")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
