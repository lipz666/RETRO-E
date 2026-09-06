"""E1: core training reproduction and method comparison (plan v2 section 6).

Runs one optimization run for a (method, optimizer_seed) pair under the predeclared v2
schedule. Nine runs make the full matrix: {reflective, search_only, instruction_opt}
x {11, 29, 47}.

    uv run python scripts/run_e1_matrix.py --method reflective --seed 11 \
        --protocol-hash HASH --workers 8

Schedule, fixed here and not negotiable by any method (plan v2 section 6.2):

    screen      16 candidate slots (E0 seed included) x 20 optimizer targets x 1 sample x 1 vote
    advance     top 4 by screen reward   x 40 optimizer targets x 1 sample x 1 vote
    selection   top 2 by advance reward  x 40 selection targets  x 1 sample x 1 vote

    560 route generations and 560 judge calls per run, plus mutation calls.

The three methods share, within one seed, the same target subsets, the same candidate slot
count, the same parent-selection rule (best reward so far) and the same tier cut-offs. The
only difference is what the rewriter is allowed to see when proposing a candidate, which is
what makes R vs S a test of reflective feedback and R vs I a test of chemical content.

This script never generates baseline routes: it consumes a cached baseline produced by
`retro-e generate --condition baseline`, so every method is scored against identical
baseline samples.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retro_e.config import load_config
from retro_e.io import append_jsonl, read_jsonl, write_json
from retro_e.optimize import (
    MUTATION_METHODS,
    PROMPT_TOKEN_HEADROOM,
    IncompleteEvaluationError,
    build_reflection_examples,
    compress_candidate,
    evaluate_candidate,
    inspect_candidate_text,
    leak_scan,
    locked_token_count,
    propose_candidate,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CANDIDATE_SLOTS = 16
SCREEN_TARGETS = 20
ADVANCE_TARGETS = 40
ADVANCE_KEEP = 4
SELECTION_KEEP = 2
SAMPLES = 1
VOTES = 1
MAX_MUTATION_ATTEMPTS = 5
MAX_EVALUATION_ATTEMPTS = 4


def partition_optimizer_targets(
    targets: list[dict[str, Any]], seed: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Screen and advance batches are disjoint and depend on the seed, never on the method."""
    ordered = sorted(targets, key=lambda target: str(target["target_id"]))
    shuffled = ordered[:]
    random.Random(seed).shuffle(shuffled)
    screen = shuffled[:SCREEN_TARGETS]
    advance = shuffled[SCREEN_TARGETS : SCREEN_TARGETS + ADVANCE_TARGETS]
    if len(advance) < ADVANCE_TARGETS:
        raise ValueError(
            f"Need {SCREEN_TARGETS + ADVANCE_TARGETS} optimizer targets, have {len(shuffled)}"
        )
    return screen, advance


def load_baseline(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Baseline cache missing: {path}. Run `retro-e generate --condition baseline` on "
            "the matching target file first; this script never generates baseline routes."
        )
    return {
        (str(row["target_id"]), int(row["sample_id"])): row
        for row in read_jsonl(path)
        if int(row["sample_id"]) < SAMPLES
    }


def ledger_rows(path: Path, tier: str | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = list(read_jsonl(path))
    return [row for row in rows if tier is None or row["tier"] == tier]


def record(ledger_path: Path, pool: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    append_jsonl(ledger_path, row)
    pool[row["candidate_id"]] = row


def evaluate(
    config: Any,
    run_dir: Path,
    tier: str,
    candidate_id: str,
    text: str,
    targets: list[dict[str, Any]],
    baseline: dict[tuple[str, int], dict[str, Any]],
    protocol_hash: str,
    workers: int,
) -> dict[str, Any]:
    tier_dir = run_dir / tier
    tier_dir.mkdir(parents=True, exist_ok=True)
    # evaluate_candidate refuses to score a partial batch and is idempotent, so a transient
    # API failure is recovered by calling it again: completed work is skipped and only the
    # missing generations/judgments are retried. Without this a nine-run matrix dies on one
    # dropped request. The guard itself is never relaxed -- a run that cannot complete its
    # planned pairs after these attempts fails loudly rather than scoring what it has.
    last: IncompleteEvaluationError | None = None
    for attempt in range(MAX_EVALUATION_ATTEMPTS):
        try:
            return evaluate_candidate(
                config,
                candidate_id,
                text,
                targets,
                baseline,
                protocol_hash,
                samples=SAMPLES,
                votes=VOTES,
                workers=workers,
                generations_path=tier_dir / f"{candidate_id}_generations.jsonl",
                judgments_path=tier_dir / f"{candidate_id}_judgments.jsonl",
            )
        except IncompleteEvaluationError as exc:
            last = exc
            logger.warning(
                "%s candidate %s incomplete on attempt %d/%d, resuming: %s",
                tier, candidate_id, attempt + 1, MAX_EVALUATION_ATTEMPTS, exc,
            )
    raise last


def enforce_shared_budget(text: str, cap_tokens: int, candidate_id: str) -> None:
    """One absolute length cap shared by all three methods (plan v2 section 6.1).

    Round 1 capped E* at 110% of E0. E1 instead matches maximum length across methods, so a
    method cannot win by being allowed more words than its controls.
    """
    tokens = locked_token_count(text)
    if tokens > cap_tokens:
        raise ValueError(
            f"Candidate {candidate_id} has {tokens} locked tokens, above the shared cap of "
            f"{cap_tokens}. Rejected and accounted for; not silently trimmed."
        )


def run(args: argparse.Namespace) -> None:
    config = load_config(args.config).with_roles(generator="generator", judge="judge")
    if args.method not in MUTATION_METHODS:
        raise ValueError(f"Unknown method {args.method!r}")

    # A smoke run must not share a directory with the real run: its advance/selection rows
    # would be resumed as complete, and a candidate that made the smoke top-4 but not the real
    # top-4 would leave a stale row that the selection tier would then rank.
    run_name = f"{args.seed}{args.run_tag}"
    run_dir = Path(config.paths.results_dir) / "optimization" / args.method / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = run_dir / "ledger.jsonl"
    context_dir = config.root / "contexts/v2" / args.method / run_name
    context_dir.mkdir(parents=True, exist_ok=True)

    optimizer_targets = list(read_jsonl(config.paths.optimizer_targets))
    selection_targets = list(read_jsonl(config.paths.selection_targets))
    screen_targets, advance_targets = partition_optimizer_targets(optimizer_targets, args.seed)

    optimizer_baseline = load_baseline(args.optimizer_baseline)
    selection_baseline = load_baseline(args.selection_baseline)

    seed_text = config.paths.experience_e0.read_text(encoding="utf-8").strip()
    cap_tokens = config.context.target_tokens
    forbidden_smiles = {
        str(target["target_smiles"])
        for target in optimizer_targets + selection_targets
        if target.get("target_smiles")
    }
    forbidden_ids = {
        str(target[key])
        for target in optimizer_targets + selection_targets
        for key in ("target_id", "source_id")
        if target.get(key)
    }

    protocol = args.protocol_hash
    common = {
        "method": args.method,
        "optimizer_seed": args.seed,
        "generator_model": config.generator_model,
        "judge_model": config.judge_model,
        "protocol_sha256": protocol,
    }

    # ---------------- screen ----------------
    pool = {row["candidate_id"]: row for row in ledger_rows(ledger_path, "screen")}
    texts: dict[str, str] = {row["candidate_id"]: row["text"] for row in pool.values()}
    routes_cache: dict[str, Any] = {}
    outcomes_cache: dict[str, Any] = {}

    if not pool:
        seed_id = inspect_candidate_text(seed_text).candidate_id
        enforce_shared_budget(seed_text, cap_tokens, seed_id)
        result = evaluate(
            config, run_dir, "screen", seed_id, seed_text, screen_targets,
            optimizer_baseline, protocol, args.workers,
        )
        texts[seed_id] = seed_text
        routes_cache[seed_id] = result["candidate_routes"]
        outcomes_cache[seed_id] = result["outcomes"]
        record(ledger_path, pool, {
            **common, "tier": "screen", "candidate_id": seed_id, "parent_id": None,
            "mutation_rationale": "E0 seed, unmodified", "saw_task_feedback": None,
            "method_audit": {}, "text": seed_text, "locked_tokens": locked_token_count(seed_text),
            "target_batch": f"screen_{args.seed}", "target_count": len(screen_targets),
            "samples": SAMPLES, "votes": VOTES, "reward": result["reward"],
            "pairs": result["pairs"], "wins": result["wins"], "losses": result["losses"],
            "ties": result["ties"], "rejected": [],
            "expected_pairs": result["expected_pairs"],
            "generation_failures": result["generation_failures"],
            "judgment_failures": result["judgment_failures"],
            "api_usage": result["api_usage"], "created_at": datetime.now(UTC).isoformat(),
        })

    slots = min(args.max_screen_candidates, CANDIDATE_SLOTS)
    while len(pool) < slots:
        parent = max(pool.values(), key=lambda row: (row["reward"], row["candidate_id"]))
        parent_id = parent["candidate_id"]
        parent_text = texts[parent_id]
        if parent_id not in outcomes_cache:
            result = evaluate(
                config, run_dir, "screen", parent_id, parent_text, screen_targets,
                optimizer_baseline, protocol, args.workers,
            )
            routes_cache[parent_id] = result["candidate_routes"]
            outcomes_cache[parent_id] = result["outcomes"]
        examples = build_reflection_examples(
            outcomes_cache[parent_id], routes_cache[parent_id], optimizer_baseline
        )
        if args.method != "search_only" and not examples:
            stop_reason = {
                "reason": "feedback_exhausted",
                "parent_id": parent_id,
                "slots_filled": len(pool),
                "created_at": datetime.now(UTC).isoformat(),
            }
            write_json(run_dir / "screen_stop_reason.json", stop_reason)
            logger.info("screen stopped: %s", stop_reason)
            break

        rejected: list[dict[str, Any]] = []
        proposal = None
        compressed_count = 0
        for attempt in range(MAX_MUTATION_ATTEMPTS):
            trial = propose_candidate(
                config, args.method, parent_text, examples, cap_tokens,
                variation_seed=args.seed * 1000 + len(pool) * 10 + attempt,
            )
            proposed_tokens = locked_token_count(trial.text)
            if proposed_tokens > cap_tokens:
                # Shared fallback, identical for all three methods, but how often each method
                # needs it is itself a method difference, so the ledger records the count.
                #
                # One pass is not enough for the reflective arm. Its revision prompt asks the
                # model to keep everything that already works and change only what the audit
                # supports, which biases toward accretion: observed proposals landing at
                # 824-1032 locked tokens against an 800 cap even after a single compression
                # aimed at 736. search_only and instruction_opt clear the cap in one pass.
                # Compress with a progressively harder target instead, and record the passes.
                usage: dict[str, Any] = {}
                for ratio in (PROMPT_TOKEN_HEADROOM, 0.80, 0.70):
                    compressed_count += 1
                    text, pass_usage = compress_candidate(
                        config, trial.text, int(cap_tokens * ratio)
                    )
                    usage = {**usage, **pass_usage}
                    trial = dataclasses.replace(
                        trial, text=text, usage={**trial.usage, **usage},
                        audit={**trial.audit, "compressed_from_tokens": proposed_tokens},
                    )
                    if locked_token_count(trial.text) <= cap_tokens:
                        break
            candidate_id = inspect_candidate_text(trial.text, parent_id).candidate_id
            leaks = leak_scan(trial.text, forbidden_smiles, forbidden_ids)
            over = locked_token_count(trial.text) > cap_tokens
            if leaks or over or candidate_id in pool:
                rejected.append({
                    "attempt": attempt, "candidate_id": candidate_id, "leaks": leaks[:5],
                    "over_budget": over, "duplicate": candidate_id in pool,
                    "locked_tokens": locked_token_count(trial.text),
                })
                logger.warning("rejected candidate %s: %s", candidate_id, rejected[-1])
                continue
            proposal = trial
            break
        if proposal is None:
            stop_reason = {
                "reason": "no_admissible_candidate",
                "attempts": MAX_MUTATION_ATTEMPTS,
                "parent_id": parent_id,
                "slots_filled": len(pool),
                "rejected": rejected,
                "created_at": datetime.now(UTC).isoformat(),
            }
            write_json(run_dir / "screen_stop_reason.json", stop_reason)
            logger.error("screen stopped: %s", stop_reason)
            break

        candidate_id = inspect_candidate_text(proposal.text, parent_id).candidate_id
        result = evaluate(
            config, run_dir, "screen", candidate_id, proposal.text, screen_targets,
            optimizer_baseline, protocol, args.workers,
        )
        texts[candidate_id] = proposal.text
        routes_cache[candidate_id] = result["candidate_routes"]
        outcomes_cache[candidate_id] = result["outcomes"]
        record(ledger_path, pool, {
            **common, "tier": "screen", "candidate_id": candidate_id, "parent_id": parent_id,
            "mutation_rationale": proposal.rationale,
            "saw_task_feedback": proposal.saw_task_feedback, "method_audit": proposal.audit,
            "text": proposal.text, "locked_tokens": locked_token_count(proposal.text),
            "target_batch": f"screen_{args.seed}", "target_count": len(screen_targets),
            "samples": SAMPLES, "votes": VOTES, "reward": result["reward"],
            "pairs": result["pairs"], "wins": result["wins"], "losses": result["losses"],
            "ties": result["ties"], "rejected": rejected,
            "expected_pairs": result["expected_pairs"],
            "generation_failures": result["generation_failures"],
            "judgment_failures": result["judgment_failures"],
            "compression_passes": compressed_count,
            "api_usage": {**result["api_usage"], "mutation": proposal.usage},
            "created_at": datetime.now(UTC).isoformat(),
        })
        logger.info("screen %d/%d candidate %s reward=%.3f parent=%s",
                    len(pool), slots, candidate_id, result["reward"], parent_id)

    # ---------------- advance and selection ----------------
    def confirm(tier: str, source_tier: str, keep: int, targets, baseline, batch_name: str):
        existing = {row["candidate_id"]: row for row in ledger_rows(ledger_path, tier)}
        source = ledger_rows(ledger_path, source_tier)
        if not source:
            raise ValueError(f"No {source_tier} rows in {ledger_path}")
        ranked = sorted(source, key=lambda row: (-row["reward"], row["candidate_id"]))[:keep]
        for row in ranked:
            if row["candidate_id"] in existing:
                continue
            result = evaluate(
                config, run_dir, tier, row["candidate_id"], row["text"], targets,
                baseline, protocol, args.workers,
            )
            record(ledger_path, existing, {
                **common, "tier": tier, "candidate_id": row["candidate_id"],
                "parent_id": row["parent_id"], "mutation_rationale": "carried forward unchanged",
                "saw_task_feedback": row.get("saw_task_feedback"),
                "method_audit": row.get("method_audit", {}), "text": row["text"],
                "locked_tokens": row["locked_tokens"], "target_batch": batch_name,
                "target_count": len(targets), "samples": SAMPLES, "votes": VOTES,
                "reward": result["reward"], "pairs": result["pairs"], "wins": result["wins"],
                "losses": result["losses"], "ties": result["ties"], "rejected": [],
                "expected_pairs": result["expected_pairs"],
                "generation_failures": result["generation_failures"],
                "judgment_failures": result["judgment_failures"],
                "api_usage": result["api_usage"], "created_at": datetime.now(UTC).isoformat(),
            })
            logger.info("%s candidate %s reward=%.3f", tier, row["candidate_id"], result["reward"])
        return existing

    screen_filled = len(ledger_rows(ledger_path, "screen"))
    if screen_filled < slots:
        raise RuntimeError(
            f"Screen tier filled {screen_filled} of {slots} slots; see "
            f"{run_dir / 'screen_stop_reason.json'}. Refusing to run advance/selection or "
            "promote a winner from a short screen: the tier cut-offs assume a full candidate "
            "pool, and a promoted winner.md would be read downstream regardless of any flag. "
            "Fix the cause, delete this run directory, and start it again."
        )

    confirm("advance", "screen", ADVANCE_KEEP, advance_targets, optimizer_baseline,
            f"advance_{args.seed}")
    selection = confirm("selection", "advance", SELECTION_KEEP, selection_targets,
                        selection_baseline, "selection_40")

    winner = max(selection.values(), key=lambda row: (row["reward"], row["candidate_id"]))
    screen_count = len(ledger_rows(ledger_path, "screen"))
    (context_dir / "winner.md").write_text(winner["text"].strip() + "\n", encoding="utf-8")
    write_json(context_dir / "promotion.json", {
        **common,
        "winner_candidate_id": winner["candidate_id"],
        "winner_parent_id": winner["parent_id"],
        "selection_reward": winner["reward"],
        "locked_tokens": winner["locked_tokens"],
        "shared_cap_tokens": cap_tokens,
        "schedule": {
            "candidate_slots": CANDIDATE_SLOTS, "screen_targets": SCREEN_TARGETS,
            "advance_keep": ADVANCE_KEEP, "advance_targets": ADVANCE_TARGETS,
            "selection_keep": SELECTION_KEEP, "selection_targets": len(selection_targets),
            "samples": SAMPLES, "votes": VOTES,
        },
        "selection_candidates": {
            row["candidate_id"]: row["reward"] for row in selection.values()
        },
        "screen_candidates_evaluated": screen_count,
        "full_schedule": screen_count >= CANDIDATE_SLOTS,
        "note": (
            "Winner frozen on selection reward alone. dev_new was not consulted and must not "
            "be used to re-pick a winner (plan v2 section 6.2)."
            + (
                ""
                if screen_count >= CANDIDATE_SLOTS
                else f" SMOKE RUN: only {screen_count} of {CANDIDATE_SLOTS} screen slots were "
                "evaluated, so this winner is not a valid E1 result."
            )
        ),
        "created_at": datetime.now(UTC).isoformat(),
    })
    logger.info("run complete: winner %s reward=%.3f -> %s",
                winner["candidate_id"], winner["reward"], context_dir / "winner.md")
    print(json.dumps({
        "method": args.method, "seed": args.seed, "winner": winner["candidate_id"],
        "selection_reward": winner["reward"], "screen_candidates": len(ledger_rows(ledger_path, "screen")),
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", required=True, choices=list(MUTATION_METHODS))
    parser.add_argument("--seed", type=int, required=True, choices=[11, 29, 47])
    parser.add_argument("--protocol-hash", required=True)
    parser.add_argument("--config", default="config/v2_pilot.toml")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--run-tag", default="",
        help="Suffix for the run directory. Use for smoke runs so they cannot contaminate "
             "a real run's resumable ledger.",
    )
    parser.add_argument(
        "--max-screen-candidates", type=int, default=CANDIDATE_SLOTS,
        help=(
            "Stop the screen tier early. For smoke runs only: a run below the predeclared "
            f"{CANDIDATE_SLOTS} slots is not a valid E1 result. Resumable, so a smoke run can "
            "be continued to the full schedule later."
        ),
    )
    parser.add_argument("--optimizer-baseline", type=Path,
                        default=Path("results/v2/generations/optimizer_pool_baseline.jsonl"))
    parser.add_argument("--selection-baseline", type=Path,
                        default=Path("results/v2/generations/selection_pool_baseline.jsonl"))
    run(parser.parse_args())


if __name__ == "__main__":
    main()
