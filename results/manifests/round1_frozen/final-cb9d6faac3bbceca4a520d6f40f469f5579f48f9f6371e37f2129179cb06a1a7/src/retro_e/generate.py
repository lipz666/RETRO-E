from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .api import OpenAICompatibleClient
from .config import ExperimentConfig
from .io import append_jsonl, assert_protocol, existing_keys, read_jsonl
from .prompts import compose_generation_prompt, context_path
from .validation import validate_response

logger = logging.getLogger(__name__)


def _record_id(target_id: str, condition: str, sample_id: int, protocol_hash: str) -> str:
    value = f"{target_id}\0{condition}\0{sample_id}\0{protocol_hash}"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _generate_one(
    config: ExperimentConfig,
    target: dict[str, Any],
    condition: str,
    sample_id: int,
    protocol_hash: str,
    experience_version: str,
) -> dict[str, Any]:
    target_id = str(target["target_id"])
    target_smiles = str(target["target_smiles"])
    prompt = compose_generation_prompt(config, target_smiles, condition)
    with OpenAICompatibleClient(config) as client:
        completion = client.complete(
            prompt,
            temperature=config.generation.temperature,
            top_p=config.generation.top_p,
            max_tokens=config.generation.max_tokens,
            model=config.generator_model,
        )
    validation = validate_response(
        completion.content,
        target_smiles,
        expected_routes=config.generation.n_routes,
    )
    return {
        "record_id": _record_id(target_id, condition, sample_id, protocol_hash),
        "target_id": target_id,
        "target_smiles": target_smiles,
        "condition": condition,
        "sample_id": sample_id,
        "model": config.generator_model,
        "response_model": completion.response_model,
        "temperature": config.generation.temperature,
        "top_p": config.generation.top_p,
        "max_tokens": config.generation.max_tokens,
        "n_routes": config.generation.n_routes,
        "system_prompt_version": config.protocol.system_prompt_version,
        "experience_version": experience_version,
        "protocol_sha256": protocol_hash,
        "route_text": json.dumps(
            validation.normalized or validation.parsed,
            ensure_ascii=False,
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


def run_generation(
    config: ExperimentConfig,
    targets_path: Path,
    condition: str,
    output_path: Path,
    protocol_hash: str,
    *,
    limit: int | None = None,
    workers: int = 1,
) -> dict[str, int]:
    context = context_path(config, condition)
    experience_version = "none"
    if context is not None:
        experience_version = hashlib.sha256(context.read_bytes()).hexdigest()
    assert_protocol(output_path, protocol_hash)
    completed = existing_keys(output_path, ["target_id", "condition", "sample_id"])
    counts = {
        "planned": 0,
        "skipped": 0,
        "completed": 0,
        "valid": 0,
        "invalid": 0,
        "failed": 0,
    }
    targets = list(read_jsonl(targets_path))
    if limit is not None:
        targets = targets[:limit]

    jobs = []
    for target in targets:
        target_id = str(target["target_id"])
        for sample_id in range(config.generation.samples_per_target):
            counts["planned"] += 1
            if (target_id, condition, sample_id) in completed:
                counts["skipped"] += 1
            else:
                jobs.append((target, sample_id))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [
            executor.submit(
                _generate_one,
                config,
                target,
                condition,
                sample_id,
                protocol_hash,
                experience_version,
            )
            for target, sample_id in jobs
        ]
        for future in as_completed(futures):
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001 -- preserve other independent API results
                counts["failed"] += 1
                logger.error("generation job failed after retries: %s", exc)
                continue
            append_jsonl(output_path, record)
            counts["completed"] += 1
            counts["valid" if record["valid"] else "invalid"] += 1
    return counts
