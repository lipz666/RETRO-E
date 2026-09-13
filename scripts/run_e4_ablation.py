"""E4: module intervention on a frozen experience context (plan v2 section 9.2).

Question Q6: does deleting an experience module change the specific chemical errors that
module addresses? This is the only question in the plan answered by a shift in error type
rather than by a win rate, which is why judge_v3 emits error tags at all.

Design: the frozen legacy E* plus four single-module deletions, on 40 fixed dev_new targets,
two routes per condition. Module membership was frozen in
reports/v2/e0_estar_semantic_diff.md before any ablation ran, and each module's predicted
error tags were declared with it, so a match cannot be chosen after seeing the outcome.

    uv run python scripts/run_e4_ablation.py --protocol-hash HASH --workers 20

Budget: 400 generations, 320 judged pairs.

Two confounds are recorded rather than assumed away. Deleting a module shortens the context
(M1/M2/M3 remove about 29% of tokens, M4 only 12%), so a quality drop has both a content and
a length explanation; the plan's follow-up is a length-matched neutral substitution for the
strongest module. And M4 is a much weaker intervention than the others, so a null there
carries less information.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import statistics
import sys
from collections import Counter, defaultdict
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
VARIANT_DIR = ROOT / "contexts/v2/e4_ablation"
TARGETS = 40
SAMPLES = 2


def load_variants() -> tuple[dict[str, str], dict[str, Any]]:
    spec = json.loads((VARIANT_DIR / "variants.json").read_text(encoding="utf-8"))
    texts = {"full": (VARIANT_DIR / "full_E_star.md").read_text(encoding="utf-8").strip()}
    for name, info in spec["variants"].items():
        texts[f"drop_{name}"] = (ROOT / info["file"]).read_text(encoding="utf-8").strip()
    return texts, spec


def generate_one(
    config: ExperimentConfig,
    target: dict[str, Any],
    condition: str,
    text: str,
    sample_id: int,
    protocol_hash: str,
) -> dict[str, Any]:
    smiles = str(target["target_smiles"])
    prompt = compose_generation_prompt_from_text(config, smiles, text)
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
        "record_id": f"e4:{condition}:{target['target_id']}:{sample_id}:{protocol_hash[:12]}",
        "target_id": str(target["target_id"]),
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
            validation.normalized or validation.parsed, ensure_ascii=False,
            separators=(",", ":"),
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
    text: str,
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
        submitted = [
            pool.submit(generate_one, config, target, condition, text, sample_id, protocol_hash)
            for target, sample_id in jobs
        ]
        for future in as_completed(submitted):
            try:
                append_jsonl(output, future.result())
                counts["completed"] += 1
            except Exception:  # one dead request must not lose the others
                counts["failed"] += 1
                logger.exception("%s generation failed after retries", condition)
    return counts


def analyse(path: Path, full_label: str, drop_label: str) -> dict[str, Any]:
    """Win rate plus error tags attributed to the condition that lost.

    judge_v3 defines error_tags as problems in the route the judge did NOT choose, so a tag
    belongs to the losing condition. Ties are excluded from attribution because the tag's
    owner is then undefined.
    """
    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(path):
        by_pair[row["pair_id"]].append(row)

    wins = losses = ties = 0
    tags_by_loser: dict[str, Counter[str]] = {full_label: Counter(), drop_label: Counter()}
    losses_by_condition: Counter[str] = Counter()
    unstructured = 0
    for rows in by_pair.values():
        winner = source_winner(rows)
        if winner == full_label:
            wins += 1
        elif winner == "Tie":
            ties += 1
        else:
            losses += 1
        for row in rows:
            verdict = row.get("structured_verdict") or {}
            if not verdict.get("structured"):
                unstructured += 1
                continue
            decision = row["decision"]
            if decision == "Tie":
                continue
            loser_side = "B" if decision == "A" else "A"
            loser = row.get(f"route_{loser_side}_source")
            if loser in tags_by_loser:
                losses_by_condition[loser] += 1
                for tag in verdict.get("error_tags") or []:
                    tags_by_loser[loser][tag] += 1

    total = len(by_pair)
    return {
        "pairs": total,
        "full_wins": wins,
        "full_losses": losses,
        "ties": ties,
        "net_win_rate_full_over_drop": (wins - losses) / total if total else 0.0,
        "votes_without_structured_verdict": unstructured,
        "tag_counts_when_condition_lost": {
            key: dict(value) for key, value in tags_by_loser.items()
        },
        "times_condition_lost": dict(losses_by_condition),
        "tag_rate_when_condition_lost": {
            key: {
                tag: round(count / losses_by_condition[key], 3)
                for tag, count in value.items()
            }
            if losses_by_condition[key]
            else {}
            for key, value in tags_by_loser.items()
        },
    }


def run(args: argparse.Namespace) -> None:
    config = load_config(args.config).with_roles(generator="generator", judge="judge")
    texts, spec = load_variants()
    all_targets = list(read_jsonl(ROOT / "data/v2/dev_new_targets.jsonl"))
    targets = sorted(all_targets, key=lambda t: str(t["target_id"]))[:TARGETS]

    out_dir = Path(config.paths.results_dir) / "ablation"
    gen_dir = out_dir / "generations"
    judge_dir = out_dir / "judgments"
    gen_dir.mkdir(parents=True, exist_ok=True)
    judge_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "E4 ablation: generator=%s judge=%s targets=%d conditions=%d samples=%d",
        config.generator_model, config.judge_model, len(targets), len(texts), SAMPLES,
    )

    if args.stage in ("all", "generate"):
        for condition, text in texts.items():
            counts = generate_condition(
                config, targets, condition, text,
                gen_dir / f"{condition}.jsonl", args.protocol_hash, args.workers,
            )
            logger.info("generated %s: %s", condition, counts)

    drops = [name for name in texts if name != "full"]
    if args.stage in ("all", "judge"):
        for drop in drops:
            name = f"full_vs_{drop}"
            run_judging(
                config,
                gen_dir / "full.jsonl", gen_dir / f"{drop}.jsonl",
                "full", drop, name,
                judge_dir / f"{name}.jsonl", args.protocol_hash, workers=args.workers,
            )
            logger.info("judged %s", name)

    if args.stage in ("all", "metrics"):
        report: dict[str, Any] = {
            "phase": "v2_pilot",
            "stage": "e4_module_intervention",
            "question": "Q6 -- does deleting a module change the errors that module addresses?",
            "protocol_sha256": args.protocol_hash,
            "generator": config.generator_model,
            "judge": config.judge_model,
            "targets": len(targets),
            "samples_per_target": SAMPLES,
            "votes_per_pair": config.judge.runs_per_pair,
            "module_spec": spec,
            # The protocol hash covers src, config, prompts and schemas, but not these variant
            # files or dev_new. Their digests are recorded here so provenance is complete even
            # though the aggregate hash does not reach them.
            "variant_file_sha256": {
                name: hashlib.sha256(
                    (VARIANT_DIR / f"{'full_E_star' if name == 'full' else name}.md")
                    .read_bytes()
                ).hexdigest()
                for name in texts
            },
            "targets_file_sha256": hashlib.sha256(
                (ROOT / "data/v2/dev_new_targets.jsonl").read_bytes()
            ).hexdigest(),
            "validity_by_condition": {},
            "comparisons": {},
            "predeclared_predictions": {
                f"drop_{name}": info["predicted_error_tags"]
                for name, info in spec["variants"].items()
            },
            "caveats": [
                (
                    "Deleting a module also shortens the context, so a quality drop has both "
                    "a content and a length explanation. The plan's follow-up is a "
                    "length-matched neutral substitution for the strongest module."
                ),
                (
                    "M4 removes only about 12% of tokens against about 29% for the others, so "
                    "it is a weaker intervention and a null result there says less."
                ),
                (
                    "Error tags are the judge's automatic labels, not expert annotations. "
                    "Plan section 5.3 requires expert spot-calibration before they carry "
                    "chemical weight."
                ),
            ],
            "created_at": datetime.now(UTC).isoformat(),
        }
        for condition in texts:
            rows = list(read_jsonl(gen_dir / f"{condition}.jsonl"))
            invalid = sum(not row["valid"] for row in rows)
            report["validity_by_condition"][condition] = {
                "generations": len(rows),
                "invalid": invalid,
                "invalid_rate": invalid / len(rows) if rows else None,
            }
        for drop in drops:
            path = judge_dir / f"full_vs_{drop}.jsonl"
            if path.exists():
                report["comparisons"][f"full_vs_{drop}"] = analyse(path, "full", drop)
        summary = out_dir / "ablation_metrics.json"
        summary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        print(f"\n{'comparison':34}{'net(full>drop)':>16}{'W/L/T':>14}")
        for name, block in report["comparisons"].items():
            wlt = f"{block['full_wins']}/{block['full_losses']}/{block['ties']}"
            print(f"{name:34}{block['net_win_rate_full_over_drop']:>+16.3f}{wlt:>14}")
        print("\n预先声明的预测 vs 实测 tag 率（该条件败诉时）")
        for name, block in report["comparisons"].items():
            drop = name.replace("full_vs_", "")
            predicted = report["predeclared_predictions"].get(drop, [])
            rates = block["tag_rate_when_condition_lost"].get(drop, {})
            hit = {tag: rates.get(tag, 0.0) for tag in predicted}
            other = {t: r for t, r in rates.items() if t not in predicted}
            print(f"  {drop}")
            print(f"    预测 tag: {hit}")
            print(f"    其他 tag: {dict(sorted(other.items(), key=lambda kv: -kv[1])[:4])}")
        mean_net = statistics.mean(
            b["net_win_rate_full_over_drop"] for b in report["comparisons"].values()
        ) if report["comparisons"] else float("nan")
        print(f"\n四个消融的平均 net(full>drop) = {mean_net:+.3f}")
        print("若删任何模块都无差别退化，则模块不可分离，路由缺乏机制基础。")
        logger.info("wrote %s", summary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol-hash", required=True)
    parser.add_argument("--config", default="config/v2_pilot.toml")
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--stage", choices=["all", "generate", "judge", "metrics"], default="all")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
