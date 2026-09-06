from __future__ import annotations

import hashlib
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
        **job,
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
