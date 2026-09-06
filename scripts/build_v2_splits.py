"""Build the v2_pilot splits (dev_new, holdout_new) from PaRoutes n1 scaffolds unused by round 1.

Round 1 consumed 500 unique Murcko scaffolds out of 967 eligible n1 routes. This script
re-derives that selection bit-for-bit (same SEED, same shuffle, same filters), removes it,
and draws the v2 splits from what is left, so the new targets are scaffold-disjoint from
every round-1 split by construction rather than by post-hoc filtering.

It does not build `ood_probe`: the leftover pool is distributionally identical to the pool
round 1 drew from, so a draw from it is more in-distribution data, not a distribution shift.
See the `ood_probe` entry in the emitted splits.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_paroutes_dataset import (
    SEED,
    SOURCE_DOI,
    SOURCE_TITLE,
    SOURCE_URL,
    flatten_route,
    kekule,
    scaffold,
    write_jsonl,
)
from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import rdFingerprintGenerator

RDLogger.DisableLog("rdApp.*")

V2_SEED = 20260906
DEV_NEW_SIZE = 80
HOLDOUT_NEW_SIZE = 60
AUDIT_PANEL_SIZE = 40

_FPGEN = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)


def fingerprint(smiles: str):
    mol = Chem.MolFromSmiles(smiles)
    return None if mol is None else _FPGEN.GetFingerprint(mol)


def eligible_candidates(raw_routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identical filters to build_paroutes_dataset.main; kept in sync deliberately."""
    candidates = []
    for source_index, root in enumerate(raw_routes):
        target = kekule(str(root.get("smiles") or ""))
        target_scaffold = scaffold(str(root.get("smiles") or ""))
        if target is None or target_scaffold is None:
            continue
        mol = Chem.MolFromSmiles(target)
        heavy_atoms = mol.GetNumHeavyAtoms() if mol is not None else 0
        if not 18 <= heavy_atoms <= 65:
            continue
        try:
            steps, patents, max_depth = flatten_route(root)
        except ValueError:
            continue
        products = {entry["product_smiles"] for entry in steps}
        leaves = sorted(
            {
                precursor
                for step in steps
                for precursor in step["precursor_smiles"]
                if precursor not in products
            }
        )
        if len(steps) < 5 or len(leaves) < 2:
            continue
        candidates.append(
            {
                "source_index": source_index,
                "target_smiles": target,
                "scaffold_smiles": target_scaffold,
                "step_count": len(steps),
                "patents": patents,
                "max_depth": max_depth,
                "leaf_count": len(leaves),
                "leaf_smiles": leaves,
                "heavy_atoms": heavy_atoms,
            }
        )
    return candidates


def round1_used_scaffolds(candidates: list[dict[str, Any]]) -> set[str]:
    """Replay round 1's selection exactly: same SEED, same shuffle, first 500 unique scaffolds."""
    replay = list(candidates)
    random.Random(SEED).shuffle(replay)
    used: set[str] = set()
    for candidate in replay:
        if candidate["scaffold_smiles"] in used:
            continue
        used.add(candidate["scaffold_smiles"])
        if len(used) == 500:
            break
    if len(used) < 500:
        raise RuntimeError(f"Round-1 replay produced {len(used)} scaffolds, expected 500")
    return used


def v2_record(candidate: dict[str, Any], split: str, serial: int) -> dict[str, Any]:
    return {
        "target_id": f"paroutes-n1-{split}-{serial:04d}",
        "target_smiles": candidate["target_smiles"],
        "split": split,
        "source_type": "compiled_patent_route_dataset",
        "source_id": f"PaRoutes2-n1-index-{candidate['source_index']}",
        "source_url": SOURCE_URL,
        "source_citation": f"{SOURCE_TITLE}; DOI {SOURCE_DOI}",
        "scaffold_smiles": candidate["scaffold_smiles"],
        "smiles_validated": True,
        "extraction_confidence": "medium",
        "needs_human_review": True,
        # v2 additions: needed for predeclared stratification and stock-based leaf auditing.
        "reference_route_depth": candidate["max_depth"],
        "reference_step_count": candidate["step_count"],
        "reference_leaf_count": candidate["leaf_count"],
        "reference_leaf_smiles": candidate["leaf_smiles"],
        "target_heavy_atoms": candidate["heavy_atoms"],
        "quality_notes": [
            "Target comes from a route mechanically extracted from USPTO patent reactions.",
            "This is a benchmark route, not a literature- or laboratory-validated synthesis.",
            "Reference route fields describe the PaRoutes route only; they are never shown to a generator.",
        ],
    }


def stratum(record: dict[str, Any], size_cuts: tuple[float, float], depth_cut: float) -> str:
    heavy = record["target_heavy_atoms"]
    size = "S" if heavy <= size_cuts[0] else ("M" if heavy <= size_cuts[1] else "L")
    depth = "shallow" if record["reference_route_depth"] <= depth_cut else "deep"
    return f"{size}/{depth}"


def draw_audit_panel(dev_rows: list[dict[str, Any]]) -> list[str]:
    """Predeclared stratified draw on molecule size and route depth only (plan section 4.1).

    Deliberately blind to any method's outcome: this runs before a single v2 API call.
    """
    heavy = sorted(row["target_heavy_atoms"] for row in dev_rows)
    size_cuts = (
        heavy[len(heavy) // 3],
        heavy[2 * len(heavy) // 3],
    )
    depth_cut = statistics.median(row["reference_route_depth"] for row in dev_rows)
    strata: dict[str, list[dict[str, Any]]] = {}
    for row in dev_rows:
        strata.setdefault(stratum(row, size_cuts, depth_cut), []).append(row)

    rng = random.Random(V2_SEED + 1)
    picked: list[str] = []
    quotas = {
        key: round(AUDIT_PANEL_SIZE * len(rows) / len(dev_rows)) for key, rows in strata.items()
    }
    for key in sorted(strata):
        rows = sorted(strata[key], key=lambda row: row["target_id"])
        rng.shuffle(rows)
        picked.extend(row["target_id"] for row in rows[: quotas[key]])
    # Proportional rounding can miss or overshoot the panel size; settle deterministically.
    leftovers = [
        row["target_id"]
        for row in sorted(dev_rows, key=lambda row: row["target_id"])
        if row["target_id"] not in set(picked)
    ]
    rng.shuffle(leftovers)
    picked = sorted(set(picked[:AUDIT_PANEL_SIZE] + leftovers))[:AUDIT_PANEL_SIZE]
    if len(picked) != AUDIT_PANEL_SIZE:
        raise RuntimeError(f"Audit panel draw produced {len(picked)} targets")
    return sorted(picked)


def load_existing(paths: dict[str, Path]) -> dict[str, list[dict[str, Any]]]:
    existing = {}
    for name, path in paths.items():
        if not path.exists():
            raise FileNotFoundError(f"Round-1 split missing, refusing to build on top of it: {path}")
        existing[name] = [json.loads(line) for line in path.read_text().splitlines() if line]
    return existing


def similarity_audit(
    new_rows: list[dict[str, Any]], old_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Nearest-neighbour Tanimoto of every new target against every round-1 target.

    Scaffold disjointness does not bound analog similarity; this reports the distribution
    instead of silently deleting the closest pairs.
    """
    old_fps = [fingerprint(row["target_smiles"]) for row in old_rows]
    old_fps = [fp for fp in old_fps if fp is not None]
    nearest = []
    for row in new_rows:
        fp = fingerprint(row["target_smiles"])
        if fp is None:
            continue
        sims = DataStructs.BulkTanimotoSimilarity(fp, old_fps)
        best = max(range(len(sims)), key=sims.__getitem__)
        nearest.append(
            {
                "target_id": row["target_id"],
                "max_tanimoto": round(sims[best], 4),
                "nearest_round1_target": old_rows[best]["target_id"],
            }
        )
    values = sorted(entry["max_tanimoto"] for entry in nearest)
    return {
        "metric": "Morgan r=2, 2048 bits, Tanimoto; max over all 500 round-1 targets",
        "n": len(values),
        "median": round(statistics.median(values), 4),
        "p90": round(values[int(0.9 * (len(values) - 1))], 4),
        "max": round(values[-1], 4),
        "count_above_0.7": sum(value > 0.7 for value in values),
        "count_above_0.5": sum(value > 0.5 for value in values),
        "top10_nearest": sorted(nearest, key=lambda e: -e["max_tanimoto"])[:10],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", type=Path, default=Path("data/raw/paroutes_v2/ref_routes_n1.json"))
    parser.add_argument("--round1-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/v2"))
    args = parser.parse_args()

    raw_routes = json.loads(args.routes.read_text(encoding="utf-8"))
    candidates = eligible_candidates(raw_routes)
    used = round1_used_scaffolds(candidates)

    pool: dict[str, dict[str, Any]] = {}
    for candidate in sorted(candidates, key=lambda c: c["source_index"]):
        key = candidate["scaffold_smiles"]
        if key in used or key in pool:
            continue
        pool[key] = candidate
    available = sorted(pool.values(), key=lambda c: c["source_index"])
    needed = DEV_NEW_SIZE + HOLDOUT_NEW_SIZE
    if len(available) < needed:
        raise RuntimeError(
            f"Only {len(available)} unused scaffolds available; need {needed}. "
            "Reduce split sizes and report the reduction rather than relaxing isolation."
        )

    rng = random.Random(V2_SEED)
    rng.shuffle(available)
    dev_candidates = available[:DEV_NEW_SIZE]
    holdout_candidates = available[DEV_NEW_SIZE:needed]
    reserve = available[needed:]

    dev_rows = [v2_record(c, "dev_new", i) for i, c in enumerate(dev_candidates, 1)]
    holdout_rows = [v2_record(c, "holdout_new", i) for i, c in enumerate(holdout_candidates, 1)]
    panel_ids = draw_audit_panel(dev_rows)

    existing = load_existing(
        {
            "train": args.round1_dir / "train_targets.jsonl",
            "test": args.round1_dir / "test_targets.jsonl",
            "optimizer": args.round1_dir / "optimizer_targets.jsonl",
            "selection": args.round1_dir / "selection_targets.jsonl",
        }
    )
    source_routes = [
        json.loads(line)
        for line in (args.round1_dir / "experience_source_routes.jsonl").read_text().splitlines()
        if line
    ]

    old_smiles = {row["target_smiles"] for rows in existing.values() for row in rows}
    old_smiles |= {row["target_smiles"] for row in source_routes}
    old_scaffolds = {row.get("scaffold_smiles") for rows in existing.values() for row in rows}
    old_scaffolds |= {row.get("scaffold_smiles") for row in source_routes}
    new_rows = dev_rows + holdout_rows
    smiles_leaks = [r["target_id"] for r in new_rows if r["target_smiles"] in old_smiles]
    scaffold_leaks = [r["target_id"] for r in new_rows if r["scaffold_smiles"] in old_scaffolds]
    internal_scaffold_overlap = len(new_rows) - len({r["scaffold_smiles"] for r in new_rows})

    old_targets = [row for rows in existing.values() for row in rows]
    seen: set[str] = set()
    unique_old = []
    for row in old_targets:
        if row["target_id"] in seen:
            continue
        seen.add(row["target_id"])
        unique_old.append(row)

    out = args.output_dir
    write_jsonl(out / "dev_new_targets.jsonl", dev_rows)
    write_jsonl(out / "holdout_new_targets.jsonl", holdout_rows)
    write_jsonl(
        out / "external_audit_panel.jsonl",
        [row for row in dev_rows if row["target_id"] in set(panel_ids)],
    )

    def describe(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
        values = [row[key] for row in rows]
        return {"median": statistics.median(values), "min": min(values), "max": max(values)}

    splits = {
        "phase": "v2_pilot",
        "v2_seed": V2_SEED,
        "round1_seed_replayed": SEED,
        "source": {
            "title": SOURCE_TITLE,
            "doi": SOURCE_DOI,
            "url": SOURCE_URL,
            "raw_file": str(args.routes),
            "raw_sha256": hashlib.sha256(args.routes.read_bytes()).hexdigest(),
        },
        "pool": {
            "eligible_candidates": len(candidates),
            "round1_scaffolds_consumed": len(used),
            "unique_unused_scaffolds_available": len(available),
            "drawn": needed,
            "reserve_left": len(reserve),
        },
        "splits": {
            "dev_new": {
                "n": len(dev_rows),
                "file": "data/v2/dev_new_targets.jsonl",
                "role": "v2 method screening, module intervention (E4); may be looked at repeatedly",
                "target_ids": [row["target_id"] for row in dev_rows],
            },
            "holdout_new": {
                "n": len(holdout_rows),
                "file": "data/v2/holdout_new_targets.jsonl",
                "role": "frozen final comparison; max_reveals=1",
                "target_ids": [row["target_id"] for row in holdout_rows],
            },
            "external_audit_panel": {
                "n": len(panel_ids),
                "file": "data/v2/external_audit_panel.jsonl",
                "role": "subset of dev_new reserved for the non-Gemini audit and expert sampling",
                "drawn": "stratified on target_heavy_atoms tertile x reference_route_depth median",
                "drawn_before_any_v2_api_call": True,
                "target_ids": panel_ids,
            },
            "ood_probe": {
                "n": 0,
                "status": "not_built",
                "reason": (
                    "The unused PaRoutes n1 pool is distributionally identical to the pool round 1 "
                    "drew from (see distribution_check), so a draw from it would be additional "
                    "in-distribution data mislabelled as a distribution shift. An ood_probe needs a "
                    "genuinely different source (e.g. PaRoutes n5, or non-patent routes) and must "
                    "declare its own OOD definition."
                ),
            },
        },
        "distribution_check": {
            "note": "Round-1 pool vs unused pool, same filters; near-identical distributions are "
            "why ood_probe cannot be drawn here.",
            "round1_selected": {
                "heavy_atoms": describe(
                    [c for c in candidates if c["scaffold_smiles"] in used], "heavy_atoms"
                ),
                "max_depth": describe(
                    [c for c in candidates if c["scaffold_smiles"] in used], "max_depth"
                ),
            },
            "v2_available_pool": {
                "heavy_atoms": describe(list(pool.values()), "heavy_atoms"),
                "max_depth": describe(list(pool.values()), "max_depth"),
            },
        },
        "isolation_audit": {
            "canonical_smiles_leaks_vs_round1": smiles_leaks,
            "murcko_scaffold_leaks_vs_round1": scaffold_leaks,
            "internal_scaffold_overlap_within_v2": internal_scaffold_overlap,
            "dev_holdout_scaffold_overlap": len(
                {r["scaffold_smiles"] for r in dev_rows}
                & {r["scaffold_smiles"] for r in holdout_rows}
            ),
            "passed": not smiles_leaks and not scaffold_leaks and internal_scaffold_overlap == 0,
        },
        "nearest_neighbour_audit": similarity_audit(new_rows, unique_old),
        "limitations": [
            "Scaffold disjointness does not bound analog similarity; see nearest_neighbour_audit.",
            "PaRoutes routes are patent-derived benchmark routes, not validated procedures.",
            (
                "Public patent routes may sit inside the frozen model's pretraining data; "
                "structural isolation from round 1 does not establish freedom from "
                "pretraining contamination."
            ),
        ],
    }
    (out / "splits.json").write_text(
        json.dumps(splits, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in splits.items() if k != "splits"}, indent=2, sort_keys=True))
    print(
        json.dumps(
            {name: block["n"] for name, block in splits["splits"].items()}, indent=2, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
