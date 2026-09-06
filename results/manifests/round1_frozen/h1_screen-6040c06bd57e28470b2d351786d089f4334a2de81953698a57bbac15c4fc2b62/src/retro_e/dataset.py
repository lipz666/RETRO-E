from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .chemistry import canonicalize, murcko_scaffold
from .io import read_jsonl

REQUIRED_TARGET_FIELDS = {"target_id", "target_smiles", "split", "source_type", "source_id"}


def audit_target_files(paths: list[Path]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for path in paths:
        for line_index, row in enumerate(read_jsonl(path), 1):
            missing = REQUIRED_TARGET_FIELDS - set(row)
            if missing:
                errors.append(f"{path}:{line_index}: missing fields {sorted(missing)}")
            canonical = canonicalize(str(row.get("target_smiles") or ""))
            if canonical is None:
                errors.append(f"{path}:{line_index}: invalid target SMILES")
            enriched = dict(row)
            enriched["_canonical"] = canonical
            enriched["_scaffold"] = murcko_scaffold(canonical) if canonical else None
            enriched["_path"] = str(path)
            rows.append(enriched)

    id_counts = Counter(str(row.get("target_id")) for row in rows)
    duplicate_ids = sorted(target_id for target_id, count in id_counts.items() if count > 1)
    if duplicate_ids:
        errors.append(f"Duplicate target_id values: {duplicate_ids[:20]}")

    smiles_splits: dict[str, set[str]] = {}
    scaffold_splits: dict[str, set[str]] = {}
    for row in rows:
        split = str(row.get("split"))
        if row["_canonical"]:
            smiles_splits.setdefault(row["_canonical"], set()).add(split)
        if row["_scaffold"]:
            scaffold_splits.setdefault(row["_scaffold"], set()).add(split)
    smiles_leaks = {key: sorted(value) for key, value in smiles_splits.items() if len(value) > 1}
    scaffold_leaks = {
        key: sorted(value) for key, value in scaffold_splits.items() if len(value) > 1
    }
    if smiles_leaks:
        errors.append(f"Canonical target leakage across splits: {len(smiles_leaks)} molecules")
    if scaffold_leaks:
        warnings.append(f"Murcko scaffold overlap across splits: {len(scaffold_leaks)} scaffolds")

    provenance_missing = sum(
        not str(row.get("source_type") or "").strip() or not str(row.get("source_id") or "").strip()
        for row in rows
    )
    review_count = sum(bool(row.get("needs_human_review")) for row in rows)
    split_counts = Counter(str(row.get("split")) for row in rows)
    source_type_counts = Counter(str(row.get("source_type")) for row in rows)
    return {
        "rows": len(rows),
        "split_counts": dict(sorted(split_counts.items())),
        "source_type_counts": dict(sorted(source_type_counts.items())),
        "invalid_or_schema_errors": errors,
        "warnings": warnings,
        "duplicate_target_ids": duplicate_ids,
        "canonical_smiles_leak_count": len(smiles_leaks),
        "scaffold_overlap_count": len(scaffold_leaks),
        "provenance_missing_count": provenance_missing,
        "needs_human_review_count": review_count,
        "passed": not errors,
    }
