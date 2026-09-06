"""Stage 4 harness: E0 -> E* context optimization.

Runs the three predeclared tiers from reports/EXPERIMENT_PLAN.md Stage 4 /
HANDOFF.md section 9, using the reflective-mutation primitives in
retro_e.optimize. Every phase is resumable: rerunning a phase that already has
ledger rows / generation / judgment files for a given candidate does no new API
work for that candidate.

    tier1   up to --max-candidates (default 24) candidates, explored by reflective
            mutation from E0, evaluated on a fixed 40-target batch, 1 sample, 1 vote.
    tier2   top --top-k (default 6) tier1 candidates by reward, re-evaluated
            unchanged on a fixed 120-target batch, 1 sample, 3 votes.
    tier3   top --top-k (default 2) tier2 candidates by reward, re-evaluated
            unchanged on all 40 selection_targets, 3 samples, 3 votes.
    promote writes the tier3 winner to contexts/experience_E_star.md.

GEPA only ever proposes candidate text inside tier1 (via optimize.propose_mutation);
it never controls which targets, how many samples/votes, or when to stop -- that
schedule is fixed by this script, per EXPERIMENT_PLAN.md Stage 4: "GEPA may choose
candidate edits, but it may not choose the evaluation targets, sample counts, or
stopping rule."

Baseline routes are NOT generated here. Cache them first with the ordinary CLI:

    uv run retro-e generate --targets data/processed/optimizer_targets.jsonl \\
      --condition baseline --protocol-hash HASH \\
      --output results/generations/optimizer_pool_baseline.jsonl --workers 8
    uv run retro-e generate --targets data/processed/selection_targets.jsonl \\
      --condition baseline --protocol-hash HASH \\
      --output results/generations/selection_pool_baseline.jsonl --workers 8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from retro_e.config import load_config
from retro_e.io import append_jsonl, read_jsonl, write_json
from retro_e.optimize import (
    build_reflection_examples,
    compress_candidate,
    enforce_candidate_budget,
    evaluate_candidate,
    inspect_candidate_text,
    leak_scan,
    locked_token_count,
    outcomes_from_judgments,
    propose_mutation,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TIER_PARTITION_SEED = 20260911
TIER1_BATCH_SIZE = 40


def _partition_optimizer_targets(
    targets: list[dict[str, Any]], seed: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(targets, key=lambda t: str(t["target_id"]))
    rng = random.Random(seed)
    shuffled = ordered[:]
    rng.shuffle(shuffled)
    return shuffled[:TIER1_BATCH_SIZE], shuffled[TIER1_BATCH_SIZE:]


def _load_baseline(
    path: Path, restrict_sample_id: int | None = None
) -> dict[tuple[str, int], dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Baseline cache missing: {path}. Run `retro-e generate --condition baseline` on "
            "the matching target file first; this script never generates baseline routes."
        )
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    for row in read_jsonl(path):
        if restrict_sample_id is not None and int(row["sample_id"]) != restrict_sample_id:
            continue
        rows[(str(row["target_id"]), int(row["sample_id"]))] = row
    return rows


def _require_baseline_coverage(
    targets: list[dict[str, Any]],
    samples: int,
    baseline_by_target: dict[tuple[str, int], dict[str, Any]],
    label: str,
) -> None:
    missing = [
        (str(target["target_id"]), sample_id)
        for target in targets
        for sample_id in range(samples)
        if (str(target["target_id"]), sample_id) not in baseline_by_target
    ]
    if missing:
        raise ValueError(
            f"{label}: missing baseline for {len(missing)} (target,sample) pairs, e.g. "
            f"{missing[:5]}."
        )


def _ledger_rows(ledger_path: Path, tier: str | None = None) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []
    rows = list(read_jsonl(ledger_path))
    return [row for row in rows if tier is None or row["tier"] == tier]


def _log_candidate(
    ledger_path: Path,
    pool: dict[str, dict[str, Any]],
    *,
    tier: str,
    candidate_id: str,
    parent_id: str | None,
    rationale: str,
    text: str,
    target_batch: str,
    target_count: int,
    samples: int,
    votes: int,
    result: dict[str, Any],
    generations_path: Path,
    judgments_path: Path,
    e0_tokens: int,
    generator_model: str,
    judge_model: str,
) -> dict[str, Any]:
    if candidate_id in pool:
        return pool[candidate_id]
    row = {
        "tier": tier,
        "candidate_id": candidate_id,
        "parent_id": parent_id,
        "mutation_rationale": rationale,
        "text": text,
        "locked_tokens": locked_token_count(text),
        "e0_locked_tokens": e0_tokens,
        "target_batch": target_batch,
        "target_count": target_count,
        "samples": samples,
        "votes": votes,
        "reward": result["reward"],
        "pairs": result["pairs"],
        "wins": result["wins"],
        "losses": result["losses"],
        "ties": result["ties"],
        "generations_file": str(generations_path),
        "judgments_file": str(judgments_path),
        "api_usage": result["api_usage"],
        "generator_model": generator_model,
        "judge_model": judge_model,
        "created_at": datetime.now(UTC).isoformat(),
    }
    append_jsonl(ledger_path, row)
    pool[candidate_id] = row
    return row


def run_tier1(
    config,
    protocol_hash: str,
    e0_text: str,
    targets: list[dict[str, Any]],
    baseline_by_target: dict[tuple[str, int], dict[str, Any]],
    ledger_path: Path,
    *,
    max_candidates: int,
    workers: int,
    results_dir: Path,
    all_target_smiles: set[str],
    all_ids: set[str],
) -> None:
    _require_baseline_coverage(targets, 1, baseline_by_target, "tier1")
    tier_dir = results_dir / "optimization" / "tier1"
    tier_dir.mkdir(parents=True, exist_ok=True)
    e0_tokens = locked_token_count(e0_text)
    e0_candidate = inspect_candidate_text(e0_text)
    e0_id = e0_candidate.candidate_id

    pool = {row["candidate_id"]: row for row in _ledger_rows(ledger_path, "tier1")}
    outcomes_cache: dict[str, list[dict[str, Any]]] = {}
    routes_cache: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    text_cache: dict[str, str] = {}

    def _load_from_disk(candidate_id: str, text: str) -> None:
        """Resume support: repopulate the in-memory caches for a candidate that was
        already evaluated (in this run or an earlier, interrupted one). evaluate_candidate
        is idempotent, so any candidate here -- not just the E0 seed -- may end up chosen
        as a parent later in the loop and needs its outcomes/routes available."""
        text_cache[candidate_id] = text
        gen_path = tier_dir / f"{candidate_id}_generations.jsonl"
        judge_path = tier_dir / f"{candidate_id}_judgments.jsonl"
        routes_cache[candidate_id] = {
            (r["target_id"], r["sample_id"]): r for r in read_jsonl(gen_path)
        }
        outcomes_cache[candidate_id] = outcomes_from_judgments(list(read_jsonl(judge_path)))

    for candidate_id, row in pool.items():
        _load_from_disk(candidate_id, row["text"])

    def _evaluate(candidate_id: str, text: str, parent_id: str | None, rationale: str) -> None:
        gen_path = tier_dir / f"{candidate_id}_generations.jsonl"
        judge_path = tier_dir / f"{candidate_id}_judgments.jsonl"
        result = evaluate_candidate(
            config,
            candidate_id,
            text,
            targets,
            baseline_by_target,
            protocol_hash,
            samples=1,
            votes=1,
            workers=workers,
            generations_path=gen_path,
            judgments_path=judge_path,
        )
        _log_candidate(
            ledger_path,
            pool,
            tier="tier1",
            candidate_id=candidate_id,
            parent_id=parent_id,
            rationale=rationale,
            text=text,
            target_batch="tier1_40",
            target_count=len(targets),
            samples=1,
            votes=1,
            result=result,
            generations_path=gen_path,
            judgments_path=judge_path,
            e0_tokens=e0_tokens,
            generator_model=config.generator_model,
            judge_model=config.judge_model,
        )
        outcomes_cache[candidate_id] = result["outcomes"]
        routes_cache[candidate_id] = result["candidate_routes"]
        text_cache[candidate_id] = text
        logger.info(
            "tier1 candidate %s reward=%.3f (%d/%d/%d w/l/t) parent=%s",
            candidate_id,
            result["reward"],
            result["wins"],
            result["losses"],
            result["ties"],
            parent_id,
        )

    if e0_id not in pool:
        _evaluate(e0_id, e0_text, None, "seed: frozen E0")

    attempts = 0
    max_attempts = max_candidates * 3
    while len(pool) < max_candidates and attempts < max_attempts:
        attempts += 1
        ranked = sorted(pool.values(), key=lambda r: r["reward"], reverse=True)
        parent_row = ranked[1] if (attempts % 4 == 0 and len(ranked) > 1) else ranked[0]
        parent_id = parent_row["candidate_id"]
        parent_text = text_cache[parent_id]

        examples = build_reflection_examples(
            outcomes_cache[parent_id], routes_cache[parent_id], baseline_by_target
        )
        if not examples:
            logger.info("parent %s has no losses/ties in this batch; stopping tier1", parent_id)
            break

        try:
            new_text, critique, _usage = propose_mutation(
                config, parent_text, examples, e0_tokens
            )
        except Exception:
            # otherwise-successful multi-hour tier1 run; skip this attempt and try again.
            logger.exception("mutation proposal from %s failed; retrying", parent_id)
            continue
        leaks = leak_scan(new_text, all_target_smiles, all_ids)
        if leaks:
            logger.warning(
                "mutation from %s leaked %d identifier(s); discarding candidate", parent_id, len(leaks)
            )
            continue
        candidate = inspect_candidate_text(new_text, parent_id)
        try:
            enforce_candidate_budget(config, candidate, e0_candidate)
        except ValueError:
            try:
                new_text, _compress_usage = compress_candidate(config, new_text, e0_tokens)
            except Exception:
                logger.exception("compression for mutation from %s failed; discarding", parent_id)
                continue
            leaks = leak_scan(new_text, all_target_smiles, all_ids)
            candidate = inspect_candidate_text(new_text, parent_id)
            over_budget = candidate.approximate_tokens > e0_tokens * config.context.max_e_star_ratio
            if leaks or over_budget:
                logger.warning(
                    "mutation from %s still over budget or leaked after compression; discarding",
                    parent_id,
                )
                continue

        if candidate.candidate_id in pool:
            logger.info("mutation from %s converged to an already-seen candidate; retrying", parent_id)
            continue

        rationale = critique[:4000]
        try:
            _evaluate(candidate.candidate_id, new_text, parent_id, rationale)
        except Exception:
            logger.exception("evaluation of mutation from %s failed; retrying", parent_id)
            continue

    logger.info("tier1 complete: %d candidates evaluated", len(pool))


def run_confirmation_tier(
    config,
    protocol_hash: str,
    tier_name: str,
    source_tier: str,
    top_k: int,
    targets: list[dict[str, Any]],
    baseline_by_target: dict[tuple[str, int], dict[str, Any]],
    ledger_path: Path,
    results_dir: Path,
    *,
    samples: int,
    votes: int,
    workers: int,
) -> None:
    _require_baseline_coverage(targets, samples, baseline_by_target, tier_name)
    source_rows = _ledger_rows(ledger_path, source_tier)
    if not source_rows:
        raise ValueError(f"No {source_tier} ledger rows found; run --phase {source_tier} first")
    ranked = sorted(source_rows, key=lambda r: r["reward"], reverse=True)[:top_k]

    tier_dir = results_dir / "optimization" / tier_name
    tier_dir.mkdir(parents=True, exist_ok=True)
    pool = {row["candidate_id"]: row for row in _ledger_rows(ledger_path, tier_name)}

    for parent_row in ranked:
        candidate_id = parent_row["candidate_id"]
        if candidate_id in pool:
            logger.info("%s candidate %s already evaluated; skipping", tier_name, candidate_id)
            continue
        gen_path = tier_dir / f"{candidate_id}_generations.jsonl"
        judge_path = tier_dir / f"{candidate_id}_judgments.jsonl"
        result = evaluate_candidate(
            config,
            candidate_id,
            parent_row["text"],
            targets,
            baseline_by_target,
            protocol_hash,
            samples=samples,
            votes=votes,
            workers=workers,
            generations_path=gen_path,
            judgments_path=judge_path,
        )
        _log_candidate(
            ledger_path,
            pool,
            tier=tier_name,
            candidate_id=candidate_id,
            parent_id=parent_row.get("parent_id"),
            rationale=f"advanced from {source_tier} (reward={parent_row['reward']:.3f})",
            text=parent_row["text"],
            target_batch=tier_name,
            target_count=len(targets),
            samples=samples,
            votes=votes,
            result=result,
            generations_path=gen_path,
            judgments_path=judge_path,
            e0_tokens=parent_row["e0_locked_tokens"],
            generator_model=config.generator_model,
            judge_model=config.judge_model,
        )
        logger.info(
            "%s candidate %s reward=%.3f (%d/%d/%d w/l/t)",
            tier_name,
            candidate_id,
            result["reward"],
            result["wins"],
            result["losses"],
            result["ties"],
        )


def run_promote(config, ledger_path: Path, output_path: Path, promotion_path: Path) -> dict[str, Any]:
    tier3 = _ledger_rows(ledger_path, "tier3")
    if len(tier3) < 2:
        raise ValueError(
            f"tier3 has {len(tier3)} evaluated candidate(s); need both final candidates "
            "evaluated before promotion"
        )
    winner = max(tier3, key=lambda r: r["reward"])
    output_path.write_text(winner["text"].rstrip() + "\n", encoding="utf-8")
    record = {
        "promoted_candidate_id": winner["candidate_id"],
        "promoted_from_tier": "tier3",
        "reward": winner["reward"],
        "locked_tokens": winner["locked_tokens"],
        "e0_locked_tokens": winner["e0_locked_tokens"],
        "sha256": hashlib.sha256(winner["text"].encode()).hexdigest(),
        "tier3_candidates": [
            {"candidate_id": r["candidate_id"], "reward": r["reward"]} for r in tier3
        ],
        "output_path": str(output_path),
        "promoted_at": datetime.now(UTC).isoformat(),
    }
    write_json(promotion_path, record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/experiment.toml"))
    parser.add_argument("--protocol-hash", required=True)
    parser.add_argument("--phase", required=True, choices=["tier1", "tier2", "tier3", "promote"])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-candidates", type=int, default=24)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--target-limit", type=int, help="Debug/smoke-test only: truncate batches")
    parser.add_argument("--ledger", type=Path, help="Override ledger path (smoke-testing only)")
    parser.add_argument("--results-dir", type=Path, help="Override results dir (smoke-testing only)")
    parser.add_argument(
        "--output", type=Path, help="Override E* output path (promote phase; smoke-testing only)"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    results_dir = args.results_dir or config.paths.results_dir
    ledger_path = args.ledger or (results_dir / "optimization" / "ledger.jsonl")

    if args.phase == "promote":
        output_path = args.output or config.paths.experience_e_star
        promotion_path = (args.results_dir or config.root / "results") / "optimization" / "promotion.json"
        record = run_promote(config, ledger_path, output_path, promotion_path)
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return

    optimizer_targets = list(read_jsonl(config.paths.optimizer_targets))
    selection_targets = list(read_jsonl(config.paths.selection_targets))
    train_targets = list(read_jsonl(config.paths.train_targets))
    test_targets = list(read_jsonl(config.paths.test_targets))
    all_target_smiles = {str(t["target_smiles"]) for t in train_targets + test_targets}
    all_ids = {str(t.get("source_id") or "") for t in train_targets + test_targets}
    all_ids |= {str(t["target_id"]) for t in train_targets + test_targets}

    tier1_batch, tier2_batch = _partition_optimizer_targets(optimizer_targets, TIER_PARTITION_SEED)
    if args.target_limit:
        tier1_batch = tier1_batch[: args.target_limit]
        tier2_batch = tier2_batch[: args.target_limit]
        selection_targets = selection_targets[: args.target_limit]

    e0_text = config.paths.experience_e0.read_text(encoding="utf-8").strip()

    if args.phase == "tier1":
        baseline_by_target = _load_baseline(
            config.paths.results_dir / "generations" / "optimizer_pool_baseline.jsonl",
            restrict_sample_id=0,
        )
        run_tier1(
            config,
            args.protocol_hash,
            e0_text,
            tier1_batch,
            baseline_by_target,
            ledger_path,
            max_candidates=args.max_candidates,
            workers=args.workers,
            results_dir=results_dir,
            all_target_smiles=all_target_smiles,
            all_ids=all_ids,
        )
    elif args.phase == "tier2":
        baseline_by_target = _load_baseline(
            config.paths.results_dir / "generations" / "optimizer_pool_baseline.jsonl",
            restrict_sample_id=0,
        )
        run_confirmation_tier(
            config,
            args.protocol_hash,
            "tier2",
            "tier1",
            args.top_k or 6,
            tier2_batch,
            baseline_by_target,
            ledger_path,
            results_dir,
            samples=1,
            votes=3,
            workers=args.workers,
        )
    elif args.phase == "tier3":
        baseline_by_target = _load_baseline(
            config.paths.results_dir / "generations" / "selection_pool_baseline.jsonl"
        )
        run_confirmation_tier(
            config,
            args.protocol_hash,
            "tier3",
            "tier2",
            args.top_k or 2,
            selection_targets,
            baseline_by_target,
            ledger_path,
            results_dir,
            samples=3,
            votes=3,
            workers=args.workers,
        )


if __name__ == "__main__":
    main()
