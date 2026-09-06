from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise TypeError(f"{path}:{line_number}: each JSONL record must be an object")
            yield value


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, encoded.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def existing_keys(path: Path, fields: Iterable[str]) -> set[tuple[Any, ...]]:
    if not path.exists():
        return set()
    names = tuple(fields)
    return {tuple(row.get(name) for name in names) for row in read_jsonl(path)}


def assert_protocol(path: Path, expected_hash: str) -> None:
    if not path.exists():
        return
    hashes = {str(row.get("protocol_sha256") or "") for row in read_jsonl(path)}
    if hashes != {expected_hash}:
        raise ValueError(
            f"Refusing to mix protocol hashes in {path}: found {sorted(hashes)}, "
            f"expected {expected_hash}. Use a new output file."
        )
