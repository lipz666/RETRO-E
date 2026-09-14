"""Experiment A: is the useful experience self-derived, or transcribed human chemistry?

Round 1 supports the self-derived reading only indirectly, through the H1/H2 asymmetry and
the E0 -> E* semantic diff. This tests it directly by optimising a context from an empty
seed and comparing the result against both the human-distilled starting point and the
human-seeded optimised context.

    R_empty vs E0       does learning from nothing beat 200 distilled patent routes?
    R_empty vs E_star   does the human seed add anything on top of optimisation?
    E_star vs E0        positive control -- reproduces round-1 H2 (+0.13/+0.14) on fresh
                        targets at known power. If this does not reproduce, neither of the
                        other two comparisons can be trusted.

Design comes from the variance components measured on round-1 Stage 5
(sigma^2_target 0.073, sigma^2_sample 0.619, sigma^2_judge 0.174): 200 targets x 2 samples
x 1 vote gives MDE about 0.136, below the 0.130-0.140 effects this domain produces, and
spending on targets rather than votes is the cheaper route to that precision.

    uv run python scripts/run_expA.py --protocol-hash HASH --workers 40

Budget: 1200 generations and 1200 judged pairs.

These are artifact claims about three specific texts, which n=1 supports legitimately. They
are not method claims: "optimisation from any empty seed beats any human seed" would need
many independent runs and is not what this measures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import random
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
from retro_e.judge import run_judging, source_winner
from retro_e.prompts import compose_generation_prompt_from_text
from retro_e.validation import validate_response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TARGETS_FILE = ROOT / "data/v2/expA_eval_targets.jsonl"
R_EMPTY_DIR = ROOT / "contexts/v2/reflective/11_empty"
R_E0_DIR = ROOT / "contexts/v2/reflective/11_e0match"
SAMPLES = 2

# R_empty vs R_E0 is the primary comparison: same operator, same 16-4-2 schedule, same cap,
# same optimizer seed, same length-drift rule. Only the seed context differs, so it is the
# one contrast with a single variable.
#
# E_star vs E0 is a positive control, not a hypothesis. It should reproduce round-1 H2 at
# +0.13 to +0.14 on these fresh targets. If it does not, the apparatus is not measuring what
# it measured in round 1 and nothing else here carries weight.
#
# R_empty vs E0 is descriptive. E0 was never optimised and is 736 tokens against R_empty's
# 318, so a difference confounds seed provenance with optimisation and with length.
#
# R_empty vs E_star was dropped: E_star came from round 1's older operator and 24-6-2
# schedule, adding two more differences on top of those, for no question the others do not
# already answer better.
COMPARISONS = (("R_empty", "R_E0"), ("E_star", "E0"), ("R_empty", "E0"))
BOOTSTRAP = 10000
SEED = 20260914


def _optimized_context(directory: Path, label: str, relaunch: str) -> str:
    """Load an optimizer winner, refusing one promoted from an incomplete candidate pool."""
    winner = directory / "winner.md"
    promotion = directory / "promotion.json"
    if not winner.exists():
        raise SystemExit(f"Missing {winner}. Run the {label} optimization first:\n  {relaunch}")
    meta = json.loads(promotion.read_text(encoding="utf-8"))
    if not meta.get("full_schedule"):
        raise SystemExit(
            f"{label} ran a short screen ({meta.get('screen_candidates_evaluated')} slots). "
            "A winner promoted from an incomplete pool is not a valid artifact; fix the "
            "cause, delete the run directory, and rerun before comparing anything to it."
        )
    return winner.read_text(encoding="utf-8").strip()


def contexts(config: ExperimentConfig) -> dict[str, str]:
    return {
        "R_empty": _optimized_context(
            R_EMPTY_DIR, "R-empty",
            "uv run python scripts/run_e1_matrix.py --method reflective --seed 11 "
            "--run-tag _empty --config config/v2_rempty.toml --protocol-hash <hash>",
        ),
        "R_E0": _optimized_context(
            R_E0_DIR, "R-E0 matched",
            "uv run python scripts/run_e1_matrix.py --method reflective --seed 11 "
            "--run-tag _e0match --config config/v2_pilot.toml --protocol-hash <hash>",
        ),
        "E0": config.paths.experience_e0.read_text(encoding="utf-8").strip(),
        "E_star": config.paths.experience_e_star.read_text(encoding="utf-8").strip(),
    }


def generate_one(
    config: ExperimentConfig, target: dict[str, Any], condition: str, text: str,
    sample_id: int, protocol_hash: str,
) -> dict[str, Any]:
    smiles = str(target["target_smiles"])
    prompt = compose_generation_prompt_from_text(config, smiles, text)
    with OpenAICompatibleClient(config) as client:
        completion = client.complete(
            prompt, temperature=config.generation.temperature, top_p=config.generation.top_p,
            max_tokens=config.generation.max_tokens, model=config.generator_model,
        )
    v = validate_response(completion.content, smiles, expected_routes=config.generation.n_routes)
    return {
        "record_id": f"expA:{condition}:{target['target_id']}:{sample_id}:{protocol_hash[:12]}",
        "target_id": str(target["target_id"]), "target_smiles": smiles,
        "condition": condition, "sample_id": sample_id,
        "model": config.generator_model, "response_model": completion.response_model,
        "temperature": config.generation.temperature, "top_p": config.generation.top_p,
        "max_tokens": config.generation.max_tokens, "n_routes": config.generation.n_routes,
        "system_prompt_version": config.protocol.system_prompt_version,
        "experience_version": condition, "protocol_sha256": protocol_hash,
        "route_text": json.dumps(
            v.normalized or v.parsed, ensure_ascii=False, separators=(",", ":"),
        ) if v.parsed is not None else "",
        "raw_response": completion.content, "valid": v.valid,
        "validation_errors": list(v.errors), "validation_warnings": list(v.warnings),
        "usage": completion.usage, "request_id": completion.request_id,
        "latency_seconds": completion.latency_seconds,
        "created_at": datetime.now(UTC).isoformat(),
    }


def generate_condition(
    config: ExperimentConfig, targets: list[dict[str, Any]], condition: str, text: str,
    output: Path, protocol_hash: str, workers: int,
) -> dict[str, int]:
    assert_protocol(output, protocol_hash)
    done = existing_keys(output, ["target_id", "sample_id"])
    jobs = [
        (t, s) for t in targets for s in range(SAMPLES)
        if (str(t["target_id"]), s) not in done
    ]
    counts = {"planned": len(targets) * SAMPLES, "skipped": len(done), "completed": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        fs = [
            pool.submit(generate_one, config, t, condition, text, s, protocol_hash)
            for t, s in jobs
        ]
        for f in as_completed(fs):
            try:
                append_jsonl(output, f.result())
                counts["completed"] += 1
            except Exception:  # one dead request must not lose the others
                counts["failed"] += 1
                logger.exception("%s generation failed after retries", condition)
    return counts


def analyse(path: Path, favored: str) -> dict[str, Any]:
    """Net win rate with a target-clustered bootstrap interval."""
    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(path):
        by_pair[row["pair_id"]].append(row)
    by_target: dict[str, list[float]] = defaultdict(list)
    wins = losses = ties = rule = 0
    for rows in by_pair.values():
        w = source_winner(rows)
        score = 1.0 if w == favored else (0.0 if w == "Tie" else -1.0)
        by_target[str(rows[0]["target_id"])].append(score)
        wins += score > 0
        losses += score < 0
        ties += score == 0
        rule += rows[0]["adjudication"] == "predeclared_validity_rule"
    means = [statistics.mean(v) for v in by_target.values()]
    rng = random.Random(SEED)
    draws = sorted(
        statistics.mean(rng.choices(means, k=len(means))) for _ in range(BOOTSTRAP)
    ) if len(means) > 1 else [float("nan")]
    lo = draws[max(0, math.floor(0.025 * (len(draws) - 1)))]
    hi = draws[min(len(draws) - 1, math.ceil(0.975 * (len(draws) - 1)))]
    return {
        "pairs": len(by_pair), "targets": len(by_target),
        "wins": wins, "losses": losses, "ties": ties,
        "net_win_rate": statistics.mean(means) if means else float("nan"),
        "target_cluster_95ci": [lo, hi],
        "decided_by_validity_rule": rule,
    }


def run(args: argparse.Namespace) -> None:
    config = load_config(args.config).with_roles(generator="generator", judge="judge")
    texts = contexts(config)
    targets = list(read_jsonl(TARGETS_FILE))
    out = Path(config.paths.results_dir) / "expA"
    gen, jud = out / "generations", out / "judgments"
    gen.mkdir(parents=True, exist_ok=True)
    jud.mkdir(parents=True, exist_ok=True)
    logger.info(
        "expA: targets=%d conditions=%d samples=%d votes=%d",
        len(targets), len(texts), SAMPLES, config.judge.runs_per_pair,
    )

    if args.stage in ("all", "generate"):
        for condition, text in texts.items():
            counts = generate_condition(
                config, targets, condition, text, gen / f"{condition}.jsonl",
                args.protocol_hash, args.workers,
            )
            logger.info("generated %s: %s", condition, counts)

    if args.stage in ("all", "judge"):
        for left, right in COMPARISONS:
            name = f"{left}_vs_{right}"
            run_judging(
                config, gen / f"{left}.jsonl", gen / f"{right}.jsonl", left, right, name,
                jud / f"{name}.jsonl", args.protocol_hash, workers=args.workers,
            )
            logger.info("judged %s", name)

    if args.stage in ("all", "metrics"):
        report: dict[str, Any] = {
            "phase": "v2_pilot", "stage": "experiment_A",
            "question": "Is the useful experience self-derived, or transcribed human chemistry?",
            "protocol_sha256": args.protocol_hash,
            "generator": config.generator_model, "judge": config.judge_model,
            "targets": len(targets), "samples_per_target": SAMPLES,
            "votes_per_pair": config.judge.runs_per_pair,
            # The protocol hash covers config, prompts, schemas and src, but not the context
            # texts or the target file, so their digests are recorded here.
            "context_sha256": {
                k: hashlib.sha256(v.encode()).hexdigest() for k, v in texts.items()
            },
            "context_locked_tokens": {},
            "targets_file_sha256": hashlib.sha256(TARGETS_FILE.read_bytes()).hexdigest(),
            "validity_by_condition": {}, "comparisons": {},
            "interpretation": {
                "positive_control": (
                    "E_star vs E0 should reproduce round-1 H2 at roughly +0.13 to +0.14. If it "
                    "does not, the apparatus is not measuring what it measured in round 1 and "
                    "the other two comparisons carry no weight."
                ),
                "R_empty_vs_R_E0": (
                    "Primary. Same operator, schedule, cap and optimizer seed; only the seed "
                    "context differs. A null means the human-distilled starting point added "
                    "nothing once both were optimised, which is the direct form of the "
                    "project's central reading."
                ),
                "R_empty_vs_E0": (
                    "Descriptive only. E0 was never optimised and is 736 tokens against "
                    "R_empty's 318, so any difference confounds seed provenance with "
                    "optimisation and with length."
                ),
                "scope": (
                    "Artifact claims about three specific texts. Not a method claim: that would "
                    "need many independent optimization runs."
                ),
            },
            "created_at": datetime.now(UTC).isoformat(),
        }
        from retro_e.optimize import locked_token_count
        for k, v in texts.items():
            report["context_locked_tokens"][k] = locked_token_count(v)
        for condition in texts:
            rows = list(read_jsonl(gen / f"{condition}.jsonl"))
            invalid = sum(not r["valid"] for r in rows)
            report["validity_by_condition"][condition] = {
                "generations": len(rows), "invalid": invalid,
                "invalid_rate": invalid / len(rows) if rows else None,
            }
        for left, right in COMPARISONS:
            p = jud / f"{left}_vs_{right}.jsonl"
            if p.exists():
                report["comparisons"][f"{left}_vs_{right}"] = analyse(p, left)
        (out / "expA_metrics.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\n{'comparison':26}{'net':>9}{'cluster 95% CI':>22}{'W/L/T':>14}{'规则判决':>9}")
        for name, b in report["comparisons"].items():
            ci = b["target_cluster_95ci"]
            wlt = f"{b['wins']}/{b['losses']}/{b['ties']}"
            print(
                f"{name:26}{b['net_win_rate']:>+9.3f}"
                f"{f'[{ci[0]:+.3f}, {ci[1]:+.3f}]':>22}{wlt:>14}"
                f"{b['decided_by_validity_rule']:>9}"
            )
        print("\ncontext tokens:", report["context_locked_tokens"])
        logger.info("wrote %s", out / "expA_metrics.json")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--protocol-hash", required=True)
    p.add_argument("--config", default="config/v2_pilot.toml")
    p.add_argument("--workers", type=int, default=40)
    p.add_argument("--stage", choices=["all", "generate", "judge", "metrics"], default="all")
    run(p.parse_args())


if __name__ == "__main__":
    main()
