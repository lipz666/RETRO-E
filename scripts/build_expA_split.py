"""Draw the 200-target evaluation set for experiment A (R-empty vs E0 vs E*).

Experiment A is an artifact comparison: three fixed context texts, judged head to head. The
variance components measured on round-1 Stage 5 put the efficient allocation at many targets
with few samples, so this draws 200 targets for a 200 x 2 x 1 design (MDE about 0.136,
comfortably below the 0.130-0.140 effect sizes this domain produces).

The draw comes from the reserve of PaRoutes n1 scaffolds that no earlier split touched: not
round 1's 500, not dev_new (already spent on E4), and not holdout_new (kept for a single
final reveal). Scaffold disjointness is a property of the construction, not a filter applied
afterwards.

    uv run python scripts/build_expA_split.py
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

from build_paroutes_dataset import write_jsonl
from build_v2_splits import (
    DEV_NEW_SIZE,
    HOLDOUT_NEW_SIZE,
    V2_SEED,
    eligible_candidates,
    fingerprint,
    round1_used_scaffolds,
    v2_record,
)
from rdkit import DataStructs, RDLogger

RDLogger.DisableLog("rdApp.*")

ROOT = Path(__file__).resolve().parent.parent
EXPA_SEED = 20260914
EXPA_SIZE = 200


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", type=Path, default=ROOT / "data/raw/paroutes_v2/ref_routes_n1.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/v2")
    args = parser.parse_args()

    candidates = eligible_candidates(json.loads(args.routes.read_text(encoding="utf-8")))
    used = round1_used_scaffolds(candidates)

    # Rebuild the v2 pool exactly as build_v2_splits.py does, then replay its draw so the
    # already-spent dev_new and holdout_new scaffolds are removed rather than re-offered.
    pool: dict[str, dict[str, Any]] = {}
    for candidate in sorted(candidates, key=lambda c: c["source_index"]):
        key = candidate["scaffold_smiles"]
        if key in used or key in pool:
            continue
        pool[key] = candidate
    available = sorted(pool.values(), key=lambda c: c["source_index"])
    random.Random(V2_SEED).shuffle(available)
    spent = {c["scaffold_smiles"] for c in available[: DEV_NEW_SIZE + HOLDOUT_NEW_SIZE]}
    reserve = [c for c in available if c["scaffold_smiles"] not in spent]
    if len(reserve) < EXPA_SIZE:
        raise SystemExit(f"Reserve holds {len(reserve)} scaffolds; need {EXPA_SIZE}")

    random.Random(EXPA_SEED).shuffle(reserve)
    chosen = reserve[:EXPA_SIZE]
    rows = [v2_record(c, "expA_eval", i) for i, c in enumerate(chosen, 1)]

    # Isolation audit against every split that already exists.
    prior_smiles: set[str] = set()
    prior_scaffolds: set[str] = set()
    prior_rows: list[dict[str, Any]] = []
    for path in (
        "data/processed/train_targets.jsonl",
        "data/processed/test_targets.jsonl",
        "data/processed/experience_source_routes.jsonl",
        "data/v2/dev_new_targets.jsonl",
        "data/v2/holdout_new_targets.jsonl",
    ):
        for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            prior_smiles.add(row["target_smiles"])
            if row.get("scaffold_smiles"):
                prior_scaffolds.add(row["scaffold_smiles"])
            prior_rows.append(row)

    smiles_leaks = [r["target_id"] for r in rows if r["target_smiles"] in prior_smiles]
    scaffold_leaks = [r["target_id"] for r in rows if r["scaffold_smiles"] in prior_scaffolds]
    internal = len(rows) - len({r["scaffold_smiles"] for r in rows})

    old_fps = [fp for fp in (fingerprint(r["target_smiles"]) for r in prior_rows) if fp is not None]
    nearest = []
    for row in rows:
        fp = fingerprint(row["target_smiles"])
        if fp is None:
            continue
        sims = DataStructs.BulkTanimotoSimilarity(fp, old_fps)
        nearest.append(max(sims))
    nearest.sort()

    write_jsonl(args.output_dir / "expA_eval_targets.jsonl", rows)
    report = {
        "purpose": "Experiment A evaluation set: R-empty vs E0 vs E*, 200 x 2 x 1.",
        "expA_seed": EXPA_SEED,
        "n": len(rows),
        "drawn_from": "PaRoutes n1 scaffolds unused by round 1, dev_new and holdout_new",
        "reserve_before_draw": len(reserve),
        "reserve_left": len(reserve) - EXPA_SIZE,
        "source_raw_sha256": hashlib.sha256(args.routes.read_bytes()).hexdigest(),
        "isolation_audit": {
            "canonical_smiles_leaks": smiles_leaks,
            "murcko_scaffold_leaks": scaffold_leaks,
            "internal_scaffold_overlap": internal,
            "passed": not smiles_leaks and not scaffold_leaks and internal == 0,
        },
        "nearest_neighbour_audit": {
            "metric": "Morgan r=2, 2048 bits, max Tanimoto against every earlier split",
            "median": round(statistics.median(nearest), 4),
            "p90": round(nearest[int(0.9 * (len(nearest) - 1))], 4),
            "max": round(nearest[-1], 4),
            "count_above_0.7": sum(v > 0.7 for v in nearest),
        },
        "limitation": (
            "Scaffold disjointness does not bound analog similarity, and public patent routes "
            "may sit in the frozen model's pretraining data; structural isolation from earlier "
            "splits establishes neither."
        ),
    }
    (args.output_dir / "expA_split.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
