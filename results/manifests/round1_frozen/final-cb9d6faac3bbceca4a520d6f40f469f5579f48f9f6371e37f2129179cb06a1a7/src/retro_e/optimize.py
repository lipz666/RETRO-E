"""E0 -> E* context optimization.

This module holds the reusable primitives for the Stage-4 optimizer described in
reports/EXPERIMENT_PLAN.md and HANDOFF.md section 9. It deliberately does not run
dspy.GEPA's own autoloop: the plan requires that "GEPA may choose candidate edits,
but it may not choose the evaluation targets, sample counts, or stopping rule"
(EXPERIMENT_PLAN.md Stage 4), and GEPA's internal Pareto/budget scheduler cannot be
made to respect an externally predeclared three-tier successive-halving budget
(24x40x1x1 -> 6x120x1x3 -> 2x40x3x3). Instead this module implements the GEPA idea
directly -- reflective mutation: critique a candidate's losing/tying cases with the
judge-family model, then rewrite with the generator-family model -- and leaves tier
scheduling, candidate selection, and promotion to scripts/optimize_e_star.py, which
calls these functions under an externally fixed schedule.

Frozen for the whole optimization stage, exactly as in the main experiment:
foundation model, model version, system prompt, sampling parameters, judge model,
judge prompt. The only thing that changes between candidates is experience context
text. See optimization_contract() below.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tiktoken

from .api import OpenAICompatibleClient
from .config import ExperimentConfig
from .io import append_jsonl, assert_protocol, existing_keys, read_jsonl
from .judge import parse_decision, source_winner
from .prompts import compose_generation_prompt_from_text, compose_judge_prompt
from .validation import validate_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locked tokenizer for context-budget enforcement (config/experiment.toml:
# context.tokenizer). Neither Gemini nor GPT expose a public tokenizer through
# this gateway, so a tiktoken encoding is locked as the deterministic, reproducible
# proxy. Locked once, before the first optimizer candidate is scored, and never
# changed mid-run -- changing it would silently move the E*/E0 ratio ceiling.
# ---------------------------------------------------------------------------
LOCKED_TOKENIZER_ENCODING = "o200k_base"
_encoding = None


def locked_token_count(text: str) -> int:
    global _encoding
    if _encoding is None:
        _encoding = tiktoken.get_encoding(LOCKED_TOKENIZER_ENCODING)
    return len(_encoding.encode(text))


@dataclass(frozen=True)
class ContextCandidate:
    candidate_id: str
    path: Path | None
    approximate_tokens: int
    source_parent: str | None
    training_reward: float | None = None


def inspect_candidate_text(text: str, source_parent: str | None = None) -> ContextCandidate:
    text = text.strip()
    digest = hashlib.sha256(text.encode()).hexdigest()
    return ContextCandidate(digest[:16], None, locked_token_count(text), source_parent)


def inspect_candidate(path: Path, source_parent: str | None = None) -> ContextCandidate:
    candidate = inspect_candidate_text(path.read_text(encoding="utf-8"), source_parent)
    return ContextCandidate(
        candidate.candidate_id, path, candidate.approximate_tokens, source_parent
    )


def enforce_candidate_budget(
    config: ExperimentConfig,
    candidate: ContextCandidate,
    baseline: ContextCandidate,
) -> None:
    maximum = baseline.approximate_tokens * config.context.max_e_star_ratio
    if candidate.approximate_tokens > maximum:
        raise ValueError(
            f"Candidate {candidate.candidate_id} has {candidate.approximate_tokens} locked "
            f"tokens ({LOCKED_TOKENIZER_ENCODING}), above the allowed {maximum:.0f} "
            f"({config.context.max_e_star_ratio:.0%} of E0's {baseline.approximate_tokens})."
        )


def optimization_contract() -> dict[str, object]:
    """Immutable boundary the optimizer harness must respect."""
    return {
        "mutable": ["experience_context_text"],
        "frozen": [
            "foundation_model",
            "model_version",
            "system_prompt",
            "target_set",
            "sampling_parameters",
            "judge_model",
            "judge_prompt",
        ],
        "reward": {"win": 1.0, "tie": 0.5, "loss": 0.0},
        "test_set_access": False,
        "candidate_artifacts": [
            "text",
            "parent_id",
            "mutation_rationale",
            "reward",
            "token_count",
            "target_batch",
            "hash",
            "generations_file",
            "judgments_file",
            "api_usage",
        ],
    }


# ---------------------------------------------------------------------------
# Leakage guard: an optimizer candidate must never quote a specific molecule or
# source identifier (EXPERIMENT_PLAN.md Stage 4 safeguards; HANDOFF.md section 13).
# ---------------------------------------------------------------------------
def leak_scan(text: str, forbidden_smiles: set[str], forbidden_ids: set[str]) -> list[str]:
    hits = [smiles for smiles in forbidden_smiles if len(smiles) >= 6 and smiles in text]
    hits += [identifier for identifier in forbidden_ids if identifier and identifier in text]
    return hits


# ---------------------------------------------------------------------------
# Candidate route generation / judging against a cached baseline route. These
# mirror generate._generate_one / judge._judge_one but take raw candidate text
# instead of a frozen named condition, and are fully isolated from the four
# frozen CLI conditions (baseline/E0/E_star/control) and their files: nothing
# here writes to, or reads experience text from, contexts/experience_e0.md,
# contexts/experience_E_star.md, or contexts/experience_control.md.
# ---------------------------------------------------------------------------
OPTIMIZATION_ORDER_SEED = 20260910


def _stable_random(seed: int, *parts: object) -> random.Random:
    payload = "\0".join(str(part) for part in (seed, *parts))
    value = int(hashlib.sha256(payload.encode()).hexdigest()[:16], 16)
    return random.Random(value)


def _candidate_record_id(candidate_id: str, target_id: str, sample_id: int) -> str:
    value = f"candidate\0{candidate_id}\0{target_id}\0{sample_id}"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def generate_candidate_route(
    config: ExperimentConfig,
    target: dict[str, Any],
    candidate_id: str,
    candidate_text: str,
    sample_id: int,
    protocol_hash: str,
) -> dict[str, Any]:
    target_id = str(target["target_id"])
    target_smiles = str(target["target_smiles"])
    prompt = compose_generation_prompt_from_text(config, target_smiles, candidate_text)
    with OpenAICompatibleClient(config) as client:
        completion = client.complete(
            prompt,
            temperature=config.generation.temperature,
            top_p=config.generation.top_p,
            max_tokens=config.generation.max_tokens,
            model=config.generator_model,
        )
    validation = validate_response(
        completion.content, target_smiles, expected_routes=config.generation.n_routes
    )
    return {
        "record_id": _candidate_record_id(candidate_id, target_id, sample_id),
        "target_id": target_id,
        "target_smiles": target_smiles,
        "condition": "optimizer_candidate",
        "candidate_id": candidate_id,
        "sample_id": sample_id,
        "model": config.generator_model,
        "response_model": completion.response_model,
        "temperature": config.generation.temperature,
        "top_p": config.generation.top_p,
        "max_tokens": config.generation.max_tokens,
        "n_routes": config.generation.n_routes,
        "system_prompt_version": config.protocol.system_prompt_version,
        "experience_version": hashlib.sha256(candidate_text.encode()).hexdigest(),
        "protocol_sha256": protocol_hash,
        "route_text": (
            json.dumps(
                validation.normalized or validation.parsed,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if validation.parsed is not None
            else ""
        ),
        "raw_response": completion.content,
        "valid": validation.valid,
        "validation_errors": list(validation.errors),
        "validation_warnings": list(validation.warnings),
        "usage": completion.usage,
        "request_id": completion.request_id,
        "latency_seconds": completion.latency_seconds,
        "created_at": datetime.now(UTC).isoformat(),
    }


def judge_candidate_vs_baseline(
    config: ExperimentConfig,
    candidate_id: str,
    candidate_route: dict[str, Any],
    baseline_route: dict[str, Any],
    judge_run: int,
    protocol_hash: str,
) -> dict[str, Any]:
    target_id = candidate_route["target_id"]
    sample_id = candidate_route["sample_id"]
    pair_id = hashlib.sha256(
        f"optimizer\0{candidate_id}\0{target_id}\0{sample_id}\0{protocol_hash}".encode()
    ).hexdigest()[:24]
    rng = _stable_random(OPTIMIZATION_ORDER_SEED, pair_id, judge_run)
    swapped = bool(rng.getrandbits(1))
    if swapped:
        route_a, route_b = baseline_route, candidate_route
        source_a, source_b = "baseline", "candidate"
    else:
        route_a, route_b = candidate_route, baseline_route
        source_a, source_b = "candidate", "baseline"

    a_valid = bool(route_a.get("valid"))
    b_valid = bool(route_b.get("valid"))
    if a_valid and b_valid:
        prompt = compose_judge_prompt(
            config,
            str(candidate_route["target_smiles"]),
            str(route_a["route_text"]),
            str(route_b["route_text"]),
        )
        with OpenAICompatibleClient(config) as client:
            completion = client.complete(
                prompt,
                temperature=config.judge.temperature,
                top_p=config.judge.top_p,
                max_tokens=config.judge.max_tokens,
                model=config.judge_model,
            )
        decision = parse_decision(completion.content)
        adjudication = "llm_judge"
        response_model = completion.response_model
        raw_response = completion.content
        usage = completion.usage
        request_id = completion.request_id
        latency_seconds = completion.latency_seconds
    else:
        decision = "A" if a_valid else ("B" if b_valid else "Tie")
        adjudication = "predeclared_validity_rule"
        response_model = None
        raw_response = ""
        usage = {}
        request_id = None
        latency_seconds = 0.0

    return {
        "pair_id": pair_id,
        "target_id": target_id,
        "target_smiles": candidate_route["target_smiles"],
        "sample_id": sample_id,
        "candidate_id": candidate_id,
        "comparison": "optimizer_candidate_vs_baseline",
        "route_A_source": source_a,
        "route_B_source": source_b,
        "route_A_record_id": route_a["record_id"],
        "route_B_record_id": route_b["record_id"],
        "route_A_valid": a_valid,
        "route_B_valid": b_valid,
        "judge_model": config.judge_model,
        "response_model": response_model,
        "decision": decision,
        "adjudication": adjudication,
        "raw_response": raw_response,
        "temperature": config.judge.temperature,
        "usage": usage,
        "request_id": request_id,
        "latency_seconds": latency_seconds,
        "order_randomized": True,
        "order_swapped": swapped,
        "judge_run": judge_run,
        "protocol_sha256": protocol_hash,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _sum_usage(records: list[dict[str, Any]]) -> dict[str, int | float]:
    totals: dict[str, int | float] = {}
    for record in records:
        usage = record.get("usage") or {}
        for key, value in usage.items():
            if isinstance(value, int | float):
                totals[key] = totals.get(key, 0) + value
    return totals


def outcomes_from_judgments(
    judgment_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Majority-vote each pair and map the winning source to a reward. Pure
    function over already-loaded rows so it is unit-testable without any API
    or filesystem access."""
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for row in judgment_rows:
        by_pair.setdefault(row["pair_id"], []).append(row)
    outcomes = []
    for pair_id, rows in by_pair.items():
        winner_source = source_winner(rows)
        reward = {"candidate": 1.0, "baseline": 0.0, "Tie": 0.5}[winner_source]
        outcomes.append(
            {
                "pair_id": pair_id,
                "target_id": rows[0]["target_id"],
                "sample_id": rows[0]["sample_id"],
                "winner": winner_source,
                "reward": reward,
            }
        )
    return outcomes


def evaluate_candidate(
    config: ExperimentConfig,
    candidate_id: str,
    candidate_text: str,
    targets: list[dict[str, Any]],
    baseline_by_target: dict[tuple[str, int], dict[str, Any]],
    protocol_hash: str,
    *,
    samples: int,
    votes: int,
    workers: int,
    generations_path: Path,
    judgments_path: Path,
) -> dict[str, Any]:
    """Generate `samples` candidate routes per target and judge each against the
    matching cached baseline sample with `votes` independent votes. Resumable:
    skips (target, sample) / (pair, judge_run) keys already present on disk, so
    calling this again for an already-complete candidate does no new API work."""
    missing = [
        (str(target["target_id"]), sample_id)
        for target in targets
        for sample_id in range(samples)
        if (str(target["target_id"]), sample_id) not in baseline_by_target
    ]
    if missing:
        raise ValueError(
            f"Missing cached baseline for {len(missing)} (target,sample) pairs, e.g. "
            f"{missing[:5]}. Run `retro-e generate --condition baseline` on this target "
            "file first; the optimizer never generates its own baseline routes."
        )
    assert_protocol(generations_path, protocol_hash)
    assert_protocol(judgments_path, protocol_hash)

    completed_generations = existing_keys(generations_path, ["target_id", "sample_id"])
    gen_jobs = [
        (target, sample_id)
        for target in targets
        for sample_id in range(samples)
        if (str(target["target_id"]), sample_id) not in completed_generations
    ]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [
            executor.submit(
                generate_candidate_route,
                config,
                target,
                candidate_id,
                candidate_text,
                sample_id,
                protocol_hash,
            )
            for target, sample_id in gen_jobs
        ]
        generation_failures = 0
        for future in as_completed(futures):
            try:
                append_jsonl(generations_path, future.result())
            except Exception:
                generation_failures += 1
                logger.exception("candidate %s generation job failed after retries", candidate_id)

    candidate_routes = {
        (row["target_id"], row["sample_id"]): row for row in read_jsonl(generations_path)
    }

    completed_judgments = existing_keys(judgments_path, ["pair_id", "judge_run"])
    judge_jobs = []
    for target in targets:
        target_id = str(target["target_id"])
        for sample_id in range(samples):
            key = (target_id, sample_id)
            if key not in candidate_routes:
                continue
            pair_id = hashlib.sha256(
                f"optimizer\0{candidate_id}\0{target_id}\0{sample_id}\0{protocol_hash}".encode()
            ).hexdigest()[:24]
            for judge_run in range(votes):
                if (pair_id, judge_run) in completed_judgments:
                    continue
                judge_jobs.append((candidate_routes[key], baseline_by_target[key], judge_run))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [
            executor.submit(
                judge_candidate_vs_baseline,
                config,
                candidate_id,
                candidate_route,
                baseline_route,
                judge_run,
                protocol_hash,
            )
            for candidate_route, baseline_route, judge_run in judge_jobs
        ]
        judgment_failures = 0
        for future in as_completed(futures):
            try:
                append_jsonl(judgments_path, future.result())
            except Exception:
                judgment_failures += 1
                logger.exception("candidate %s judge job failed after retries", candidate_id)

    judgment_rows = list(read_jsonl(judgments_path))
    outcomes = outcomes_from_judgments(judgment_rows)
    generation_rows = list(read_jsonl(generations_path))
    reward = sum(o["reward"] for o in outcomes) / len(outcomes) if outcomes else 0.0
    return {
        "candidate_id": candidate_id,
        "reward": reward,
        "pairs": len(outcomes),
        "wins": sum(o["winner"] == "candidate" for o in outcomes),
        "losses": sum(o["winner"] == "baseline" for o in outcomes),
        "ties": sum(o["winner"] == "Tie" for o in outcomes),
        "outcomes": outcomes,
        "candidate_routes": candidate_routes,
        "generation_failures": generation_failures,
        "judgment_failures": judgment_failures,
        "api_usage": _sum_usage(generation_rows + judgment_rows),
    }


# ---------------------------------------------------------------------------
# Reflective mutation (the "GEPA idea"): critique losing/tying cases with the
# judge-family model, then rewrite with the generator-family model. Mirrors the
# critique-then-revise pattern already used in scripts/synthesize_e0.py and
# scripts/review_e0.py.
# ---------------------------------------------------------------------------
def _clean(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = "\n".join(value.splitlines()[1:-1]).strip()
    return value


def _summarize_route(record: dict[str, Any]) -> dict[str, Any]:
    if not record.get("valid") or not record.get("route_text"):
        return {"valid": False}
    try:
        route = json.loads(record["route_text"])["routes"][0]
    except (KeyError, IndexError, ValueError):
        return {"valid": False}
    return {
        "valid": True,
        "strategy_plan": route.get("strategy_plan", ""),
        "key_disconnection_class": route.get("key_disconnection_class", ""),
        "disconnection_type": route.get("disconnection_type", ""),
        "planned_num_steps": route.get("planned_num_steps"),
        "starting_material_count": len(route.get("starting_materials") or []),
    }


def build_reflection_examples(
    outcomes: list[dict[str, Any]],
    candidate_routes: dict[tuple[str, int], dict[str, Any]],
    baseline_routes: dict[tuple[str, int], dict[str, Any]],
    max_examples: int = 6,
) -> list[dict[str, Any]]:
    """Strategy-level (never full-SMILES) summaries of the parent's worst cases,
    so the reflection prompt cannot simply copy a training route into the
    revised context (HANDOFF.md section 13 forbids that)."""
    losses_and_ties = [o for o in outcomes if o["winner"] != "candidate"]
    losses_and_ties.sort(key=lambda o: 0 if o["winner"] == "baseline" else 1)
    examples = []
    for outcome in losses_and_ties[:max_examples]:
        key = (outcome["target_id"], outcome["sample_id"])
        if key not in candidate_routes or key not in baseline_routes:
            continue
        examples.append(
            {
                "outcome": outcome["winner"],
                "candidate_route": _summarize_route(candidate_routes[key]),
                "baseline_route": _summarize_route(baseline_routes[key]),
            }
        )
    return examples


def propose_mutation(
    config: ExperimentConfig,
    parent_text: str,
    examples: list[dict[str, Any]],
    target_tokens: int,
) -> tuple[str, str, dict[str, Any]]:
    """Returns (new_candidate_text, critique_text, api_usage)."""
    examples_text = json.dumps(examples, ensure_ascii=False, indent=2)
    critique_prompt = f"""You are auditing a synthetic-experience context used to help an LLM plan \
retrosynthetic routes. Below is the current context, followed by up to {len(examples)} training-set \
cases where a route generated under this context lost or tied against a baseline route (no context) \
in blind pairwise judging. Each case gives only strategy-level fields, never full step SMILES.

Identify concrete, general weaknesses in the CONTEXT (not the specific routes) that plausibly explain \
the losses: missing guidance, guidance that pushed toward a worse strategy, contradictions, or gaps. \
Do not propose replacement text yet. Do not name or describe the specific target molecules or infer \
what they are. Return a concise numbered critique of the context, at most 8 points.

CURRENT CONTEXT
{parent_text}

LOSING/TYING CASES
{examples_text}"""
    with OpenAICompatibleClient(config) as client:
        critique = client.complete(
            critique_prompt,
            temperature=0.3,
            top_p=1.0,
            max_tokens=1400,
            model=config.judge_model,
        )
        revision_prompt = f"""Revise the synthetic-experience context below using the audit. Keep \
everything that already works; change only what the audit supports. Preserve the numbered-entry \
format and the title line SYNTHETIC EXPERIENCE. Target approximately {target_tokens} tokens.

Hard constraints:
- general, transferable strategic retrosynthetic experience only;
- no SMILES, no target molecule, no source route or patent identifier, no claim of experimental \
validation, no mention of this optimization process or of "training", "reward", or "judge";
- do not shrink to fewer than 10 entries or grow past 20 entries.

CURRENT CONTEXT
{parent_text}

AUDIT
{critique.content}"""
        revision = client.complete(
            revision_prompt,
            temperature=0.4,
            top_p=1.0,
            max_tokens=1600,
            model=config.generator_model,
        )
    new_text = _clean(revision.content)
    usage = _sum_usage([{"usage": critique.usage}, {"usage": revision.usage}])
    return new_text, critique.content, usage


def compress_candidate(
    config: ExperimentConfig, text: str, target_tokens: int
) -> tuple[str, dict[str, Any]]:
    prompt = f"""Rewrite this synthetic-experience context to fit within approximately \
{target_tokens} tokens. Keep the title SYNTHETIC EXPERIENCE and numbered entries. Preserve the \
highest-value, most general lessons; remove repetition and filler. Add nothing new. No SMILES, \
targets, or meta-commentary.

CONTEXT
{text}"""
    with OpenAICompatibleClient(config) as client:
        completion = client.complete(
            prompt, temperature=0.0, top_p=1.0, max_tokens=1400, model=config.generator_model
        )
    return _clean(completion.content), completion.usage
