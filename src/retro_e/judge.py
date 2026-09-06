from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .api import OpenAICompatibleClient
from .config import ExperimentConfig
from .io import append_jsonl, assert_protocol, existing_keys, read_jsonl
from .prompts import compose_judge_prompt

logger = logging.getLogger(__name__)


class JudgmentError(ValueError):
    pass


_DECISION_ANCHOR = re.compile(r"decision\s*:\s*[`*_\s]*\(?(a|b|tie)\)?", re.IGNORECASE)


def parse_decision(text: str) -> str:
    """Strict whole-response match first (judge_v1, gpt-5.6-sol: a clean single token).
    Falls back to a `DECISION: <token>` anchor (judge_v2: some models -- observed with
    gemini-3.7-flash-high -- prepend prose before or after the verdict despite being told
    not to; the anchor is parseable regardless of where in the response it lands)."""
    compact = re.sub(r"[\s.`*_:-]+", "", text).lower()
    if compact == "a":
        return "A"
    if compact == "b":
        return "B"
    if compact == "tie":
        return "Tie"
    anchored = _DECISION_ANCHOR.search(text)
    if anchored:
        token = anchored.group(1).lower()
        return "Tie" if token == "tie" else token.upper()
    raise JudgmentError(f"Judge did not return exactly A, B, or Tie: {text[:100]!r}")


ERROR_TAGS = frozenset(
    {
        "structural_or_stereochemistry_omission",
        "infeasible_key_transformation",
        "chemoselectivity_conflict",
        "unnecessary_protection_loop",
        "unresolved_intermediate",
        "starting_material_availability_assumption",
        "missing_step",
        "graph_connectivity_error",
    }
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
# A judge_v3 reply truncated mid-object has no closing brace, so _JSON_OBJECT cannot match and
# there is no DECISION: anchor either. The verdict itself is usually already on the wire --
# `decision` is the first field -- so salvage it rather than discarding the whole judgment.
# Observed at roughly 1% of calls: reasoning tokens can eat the output budget.
_TRUNCATED_DECISION = re.compile(r'"decision"\s*:\s*"(A|B|Tie)"', re.IGNORECASE)


def parse_judgment(text: str) -> dict[str, Any]:
    """Parse a judge_v3 structured verdict, degrading to a decision-only judge_v1/v2 reply.

    `structured` records whether the behavioural fields (fatal flags, error tags, decisive
    step) are real or absent, so plan section 5.3 metrics can never silently treat a
    decision-only reply as "no fatal problems found". A malformed envelope around a readable
    DECISION is a parse degradation, not a chemical finding -- section 15 requires the two to
    stay distinguishable, so `parse_error` is retained alongside the salvaged decision.
    """
    blob = _JSON_OBJECT.search(text)
    if blob is not None:
        try:
            payload = json.loads(blob.group(0))
        except json.JSONDecodeError as exc:
            payload = None
            parse_error = f"json_decode_error: {exc.msg}"
        else:
            parse_error = None
        if isinstance(payload, dict) and "decision" in payload:
            decision = str(payload.get("decision", "")).strip().lower()
            if decision not in {"a", "b", "tie"}:
                raise JudgmentError(f"Judge JSON has no usable decision: {text[:120]!r}")
            tags = payload.get("error_tags") or []
            tags = [str(tag) for tag in tags] if isinstance(tags, list) else []
            unknown = sorted(set(tags) - ERROR_TAGS)
            step = payload.get("decisive_step")
            return {
                "decision": "Tie" if decision == "tie" else decision.upper(),
                "structured": True,
                "insufficient_information": bool(payload.get("insufficient_information")),
                "main_reason": str(payload.get("main_reason") or "")[:400],
                "fatal_A": bool(payload.get("fatal_A")),
                "fatal_B": bool(payload.get("fatal_B")),
                "error_tags": [tag for tag in tags if tag in ERROR_TAGS],
                "unknown_error_tags": unknown,
                "decisive_step": None if step is None else str(step)[:32],
                "parse_error": None,
            }
    else:
        parse_error = "no_json_object_found"

    truncated = _TRUNCATED_DECISION.search(text)
    if truncated:
        token = truncated.group(1).lower()
        # Structured stays False: fields after the truncation point are absent, and a missing
        # fatal flag must never read as "the judge found no fatal problem".
        return {
            "decision": "Tie" if token == "tie" else token.upper(),
            "structured": False,
            "insufficient_information": None,
            "main_reason": None,
            "fatal_A": None,
            "fatal_B": None,
            "error_tags": [],
            "unknown_error_tags": [],
            "decisive_step": None,
            "parse_error": "truncated_json_decision_salvaged",
        }

    return {
        "decision": parse_decision(text),
        "structured": False,
        "insufficient_information": None,
        "main_reason": None,
        "fatal_A": None,
        "fatal_B": None,
        "error_tags": [],
        "unknown_error_tags": [],
        "decisive_step": None,
        "parse_error": parse_error,
    }


def _load_generations(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    rows = {}
    for row in read_jsonl(path):
        rows[(str(row["target_id"]), int(row["sample_id"]))] = row
    return rows


def _stable_random(seed: int, *parts: object) -> random.Random:
    payload = "\0".join(str(part) for part in (seed, *parts))
    value = int(hashlib.sha256(payload.encode()).hexdigest()[:16], 16)
    return random.Random(value)


def _judge_one(config: ExperimentConfig, job: dict[str, Any]) -> dict[str, Any]:
    route_a = job.pop("route_a")
    route_b = job.pop("route_b")
    a_valid = bool(route_a.get("valid"))
    b_valid = bool(route_b.get("valid"))
    if a_valid and b_valid:
        prompt = compose_judge_prompt(
            config,
            str(job["target_smiles"]),
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
        verdict = parse_judgment(completion.content)
        decision = verdict["decision"]
        adjudication = "llm_judge"
        response_model = completion.response_model
        raw_response = completion.content
        usage = completion.usage
        request_id = completion.request_id
        latency_seconds = completion.latency_seconds
    else:
        decision = "A" if a_valid else ("B" if b_valid else "Tie")
        # The validity rule is an operational adjudication, never a chemical opinion: it must
        # not contribute fatal flags or error tags to the section 5.3 behavioural metrics.
        verdict = {
            "structured": False,
            "insufficient_information": None,
            "main_reason": None,
            "fatal_A": None,
            "fatal_B": None,
            "error_tags": [],
            "unknown_error_tags": [],
            "decisive_step": None,
            "parse_error": None,
        }
        adjudication = "predeclared_validity_rule"
        response_model = None
        raw_response = ""
        usage = {}
        request_id = None
        latency_seconds = 0.0
    return {
        **job,
        "route_A_record_id": route_a["record_id"],
        "route_B_record_id": route_b["record_id"],
        "route_A_valid": a_valid,
        "route_B_valid": b_valid,
        "judge_model": config.judge_model,
        "response_model": response_model,
        "decision": decision,
        "adjudication": adjudication,
        "judge_prompt_version": config.protocol.judge_prompt_version,
        "structured_verdict": {key: value for key, value in verdict.items() if key != "decision"},
        "raw_response": raw_response,
        "temperature": config.judge.temperature,
        "usage": usage,
        "request_id": request_id,
        "latency_seconds": latency_seconds,
        "created_at": datetime.now(UTC).isoformat(),
    }


def run_judging(
    config: ExperimentConfig,
    left_path: Path,
    right_path: Path,
    left_source: str,
    right_source: str,
    comparison: str,
    output_path: Path,
    protocol_hash: str,
    *,
    limit: int | None = None,
    workers: int = 1,
) -> dict[str, int]:
    left = _load_generations(left_path)
    right = _load_generations(right_path)
    assert_protocol(left_path, protocol_hash)
    assert_protocol(right_path, protocol_hash)
    assert_protocol(output_path, protocol_hash)
    pair_keys = sorted(set(left) & set(right))
    if limit is not None:
        pair_keys = pair_keys[:limit]
    completed = existing_keys(output_path, ["pair_id", "judge_run"])
    counts = {
        "pairs": len(pair_keys),
        "planned": 0,
        "skipped": 0,
        "completed": 0,
        "failed": 0,
    }

    jobs = []
    for target_id, sample_id in pair_keys:
        left_row, right_row = left[(target_id, sample_id)], right[(target_id, sample_id)]
        if left_row.get("target_smiles") != right_row.get("target_smiles"):
            raise ValueError(
                f"Target mismatch for {target_id} sample {sample_id}: generation files disagree"
            )
        pair_id = hashlib.sha256(
            f"{comparison}\0{target_id}\0{sample_id}\0{protocol_hash}".encode()
        ).hexdigest()[:24]
        for judge_run in range(config.judge.runs_per_pair):
            counts["planned"] += 1
            if (pair_id, judge_run) in completed:
                counts["skipped"] += 1
                continue
            rng = _stable_random(config.judge.seed, pair_id, judge_run)
            swapped = bool(rng.getrandbits(1))
            if swapped:
                route_a, route_b = right_row, left_row
                source_a, source_b = right_source, left_source
            else:
                route_a, route_b = left_row, right_row
                source_a, source_b = left_source, right_source
            jobs.append(
                {
                    "pair_id": pair_id,
                    "target_id": target_id,
                    "target_smiles": left_row["target_smiles"],
                    "sample_id": sample_id,
                    "comparison": comparison,
                    "left_source": left_source,
                    "right_source": right_source,
                    "route_A_source": source_a,
                    "route_B_source": source_b,
                    "route_a": route_a,
                    "route_b": route_b,
                    "order_randomized": True,
                    "order_swapped": swapped,
                    "judge_run": judge_run,
                    "protocol_sha256": protocol_hash,
                }
            )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(_judge_one, config, job) for job in jobs]
        for future in as_completed(futures):
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001 -- preserve other independent API results
                counts["failed"] += 1
                logger.error("judge job failed after retries: %s", exc)
                continue
            append_jsonl(output_path, record)
            counts["completed"] += 1
    return counts


def majority_vote(rows: list[dict[str, Any]]) -> str:
    votes = {"A": 0, "B": 0, "Tie": 0}
    for row in rows:
        votes[str(row["decision"])] += 1
    best = max(votes.values())
    winners = [decision for decision, count in votes.items() if count == best]
    return winners[0] if len(winners) == 1 else "Tie"


def source_winner(rows: list[dict[str, Any]]) -> str:
    source_votes: dict[str, int] = {"Tie": 0}
    for row in rows:
        decision = str(row["decision"])
        winner = "Tie" if decision == "Tie" else str(row[f"route_{decision}_source"])
        source_votes[winner] = source_votes.get(winner, 0) + 1
    best = max(source_votes.values())
    winners = [source for source, count in source_votes.items() if count == best]
    return winners[0] if len(winners) == 1 else "Tie"
