"""Does the effect of an experience context vary across predeclared structural subgroups?

The within-target consistency test (analyze_heterogeneity.py) has only three samples per
target and so can only see large molecule-level effects. A subgroup test pools many targets
per group and has far more power. If a context's advantage concentrates in the subgroups its
own entries claim to address, that is a usable routing signal. If every subgroup shows the
same effect, there is nothing for a router to condition on.

Subgroups are the ones frozen in reports/v2/e0_estar_semantic_diff.md before any ablation:

    M2 (selectivity and protection) applies to targets with >= 2 competing nucleophilic
       sites (free NH/OH) or >= 1 stereocentre.
    M4 (starting materials and complexity) applies to targets containing a heterocyclic or
       bicyclic core.

Also reported: continuous covariates (size, ring count, reference route depth), which were
not predeclared and are therefore exploratory.

    uv run python scripts/analyze_routing_signal.py
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rdkit import Chem, RDLogger

from retro_e.judge import source_winner

RDLogger.DisableLog("rdApp.*")

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = 10000
SEED = 20260913

COMPARISONS = {
    "E_star_vs_E0": ("E_star", "final_E_star_vs_E0.jsonl", "lunajudge_E_star_vs_E0.jsonl"),
    "E0_vs_baseline": ("E0", "final_E0_vs_baseline.jsonl", "lunajudge_E0_vs_baseline.jsonl"),
}


def features(smiles: str, route_depth: int | None) -> dict[str, Any]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {}
    free_nh_oh = sum(
        atom.GetSymbol() in ("N", "O") and atom.GetTotalNumHs() > 0 for atom in mol.GetAtoms()
    )
    stereo = len(Chem.FindMolChiralCenters(mol, includeUnassigned=True, useLegacyImplementation=False))
    ring_info = mol.GetRingInfo()
    hetero_ring = any(
        any(mol.GetAtomWithIdx(idx).GetSymbol() not in ("C",) for idx in ring)
        for ring in ring_info.AtomRings()
    )
    fused = sum(1 for ring in ring_info.AtomRings()) >= 2 and any(
        len(set(a) & set(b)) >= 2
        for i, a in enumerate(ring_info.AtomRings())
        for b in ring_info.AtomRings()[i + 1 :]
    )
    return {
        "heavy_atoms": mol.GetNumHeavyAtoms(),
        "rings": ring_info.NumRings(),
        "route_depth": route_depth,
        # Predeclared applicability rules.
        "M2_applies": free_nh_oh >= 2 or stereo >= 1,
        "M4_applies": bool(hetero_ring or fused),
    }


def per_target_effect(path: Path, favored: str) -> dict[str, float]:
    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            by_pair[row["pair_id"]].append(row)
    per_target: dict[str, list[float]] = defaultdict(list)
    for rows in by_pair.values():
        winner = source_winner(rows)
        score = 1.0 if winner == favored else (0.0 if winner == "Tie" else -1.0)
        per_target[str(rows[0]["target_id"])].append(score)
    return {t: statistics.mean(v) for t, v in per_target.items()}


def boot_ci(values: list[float], seed: int) -> list[float]:
    if len(values) < 2:
        return [float("nan"), float("nan")]
    rng = random.Random(seed)
    draws = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(BOOTSTRAP))
    return [
        draws[max(0, math.floor(0.025 * (len(draws) - 1)))],
        draws[min(len(draws) - 1, math.ceil(0.975 * (len(draws) - 1)))],
    ]


def diff_p(a: list[float], b: list[float], seed: int) -> float:
    """Permutation test on the difference of subgroup means."""
    if not a or not b:
        return float("nan")
    observed = abs(statistics.mean(a) - statistics.mean(b))
    pool = a + b
    rng = random.Random(seed)
    extreme = 0
    for _ in range(BOOTSTRAP):
        rng.shuffle(pool)
        if abs(statistics.mean(pool[: len(a)]) - statistics.mean(pool[len(a) :])) >= observed - 1e-12:
            extreme += 1
    return (extreme + 1) / (BOOTSTRAP + 1)


def main() -> None:
    targets = {}
    for line in (ROOT / "data/processed/test_targets.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            targets[row["target_id"]] = features(
                row["target_smiles"], row.get("reference_route_depth")
            )

    report: dict[str, Any] = {"bootstrap": BOOTSTRAP, "seed": SEED, "comparisons": {}}
    for name, (favored, gemini_file, luna_file) in COMPARISONS.items():
        block: dict[str, Any] = {}
        for judge, filename in (("gemini", gemini_file), ("luna", luna_file)):
            path = ROOT / "results/judgments" / filename
            if not path.exists():
                continue
            effects = per_target_effect(path, favored)
            judge_block: dict[str, Any] = {}
            for rule in ("M2_applies", "M4_applies"):
                inside = [v for t, v in effects.items() if targets.get(t, {}).get(rule)]
                outside = [v for t, v in effects.items() if targets.get(t) and not targets[t][rule]]
                judge_block[rule] = {
                    "n_inside": len(inside),
                    "n_outside": len(outside),
                    "mean_inside": statistics.mean(inside) if inside else None,
                    "mean_outside": statistics.mean(outside) if outside else None,
                    "ci_inside": boot_ci(inside, SEED),
                    "ci_outside": boot_ci(outside, SEED),
                    "difference": (
                        statistics.mean(inside) - statistics.mean(outside)
                        if inside and outside
                        else None
                    ),
                    "permutation_p": diff_p(inside[:], outside[:], SEED),
                }
            for covariate in ("heavy_atoms", "rings", "route_depth"):
                pairs = [
                    (targets[t][covariate], v)
                    for t, v in effects.items()
                    if targets.get(t) and targets[t].get(covariate) is not None
                ]
                if len(pairs) > 5:
                    try:
                        r = statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs])
                    except statistics.StatisticsError:
                        r = float("nan")
                    judge_block[f"corr_{covariate}"] = {"n": len(pairs), "pearson_r": r}
            block[judge] = judge_block
        report["comparisons"][name] = block

    out = ROOT / "results/v2/routing_signal_analysis.json"
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    for name, block in report["comparisons"].items():
        print(f"\n=== {name} ===")
        for judge, jb in block.items():
            print(f"  [{judge}]")
            for rule in ("M2_applies", "M4_applies"):
                b = jb[rule]
                if b["mean_inside"] is None or b["mean_outside"] is None:
                    print(f"    {rule}: 子群为空，无法检验")
                    continue
                print(
                    f"    {rule:12} 适用(n={b['n_inside']:3}) {b['mean_inside']:+.3f} "
                    f"[{b['ci_inside'][0]:+.3f},{b['ci_inside'][1]:+.3f}]   "
                    f"不适用(n={b['n_outside']:3}) {b['mean_outside']:+.3f} "
                    f"[{b['ci_outside'][0]:+.3f},{b['ci_outside'][1]:+.3f}]"
                )
                print(
                    f"    {'':12} 差值={b['difference']:+.3f}  置换 p={b['permutation_p']:.3f}"
                )
            cors = " ".join(
                f"{k.replace('corr_', '')} r={v['pearson_r']:+.3f}"
                for k, v in jb.items()
                if k.startswith("corr_")
            )
            print(f"    连续协变量相关: {cors}")
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
