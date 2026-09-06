"""Build scaffold-disjoint RETRO-E splits from the official PaRoutes 2.0 n1 set."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold

SOURCE_URL = "https://zenodo.org/records/7341155"
SOURCE_DOI = "10.5281/zenodo.7341155"
SOURCE_TITLE = "PaRoutes 2.0 n1 reference routes"
SEED = 20260902


def kekule(smiles: str) -> str | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        return Chem.MolToSmiles(mol, canonical=True, kekuleSmiles=True)
    except Chem.KekulizeException:
        return None


def scaffold(smiles: str) -> str | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    value = MurckoScaffold.GetScaffoldForMol(mol)
    if value.GetNumAtoms() == 0:
        return None
    return Chem.MolToSmiles(value, canonical=True, isomericSmiles=False)


def flatten_route(root: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], int]:
    steps: list[dict[str, Any]] = []
    patents: set[str] = set()
    max_depth = 0

    def visit_molecule(node: dict[str, Any], depth: int) -> None:
        nonlocal max_depth
        max_depth = max(max_depth, depth)
        product = kekule(str(node.get("smiles") or ""))
        if product is None:
            raise ValueError("invalid product molecule")
        reactions = node.get("children") or []
        for reaction in reactions:
            metadata = reaction.get("metadata") or {}
            reaction_id = str(metadata.get("ID") or "")
            if reaction_id:
                patents.add(reaction_id.split(";", 1)[0])
            precursors = []
            for child in reaction.get("children") or []:
                precursor = kekule(str(child.get("smiles") or ""))
                if precursor is None:
                    raise ValueError("invalid precursor molecule")
                precursors.append(precursor)
            if not precursors:
                raise ValueError("reaction without precursor molecules")
            steps.append(
                {
                    "step_id": len(steps) + 1,
                    "product_smiles": product,
                    "precursor_smiles": precursors,
                    "reaction_class": None,
                    "conditions": None,
                    "yield_pct": None,
                    "yield_type": None,
                    "evidence_text": str(metadata.get("rsmi") or metadata.get("smiles") or ""),
                    "source_reaction_id": reaction_id or None,
                    "ring_breaker": bool(metadata.get("RingBreaker")),
                }
            )
            for child in reaction.get("children") or []:
                visit_molecule(child, depth + 1)

    visit_molecule(root, 0)
    return steps, sorted(patents), max_depth


def target_record(candidate: dict[str, Any], split: str, serial: int) -> dict[str, Any]:
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
        "quality_notes": [
            "Target comes from a route mechanically extracted from USPTO patent reactions.",
            "This is a benchmark route, not a literature- or laboratory-validated synthesis.",
        ],
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    raw_routes = json.loads(args.routes.read_text(encoding="utf-8"))
    candidates = []
    rejected = Counter()
    for source_index, root in enumerate(raw_routes):
        target = kekule(str(root.get("smiles") or ""))
        target_scaffold = scaffold(str(root.get("smiles") or ""))
        if target is None or target_scaffold is None:
            rejected["invalid_or_acyclic_target"] += 1
            continue
        mol = Chem.MolFromSmiles(target)
        heavy_atoms = mol.GetNumHeavyAtoms() if mol is not None else 0
        if not 18 <= heavy_atoms <= 65:
            rejected["target_size_outside_18_65"] += 1
            continue
        try:
            steps, patents, max_depth = flatten_route(root)
        except ValueError:
            rejected["invalid_route_structure"] += 1
            continue
        leaves = sorted(
            {
                precursor
                for step in steps
                for precursor in step["precursor_smiles"]
                if precursor not in {entry["product_smiles"] for entry in steps}
            }
        )
        if len(steps) < 5 or len(leaves) < 2:
            rejected["route_below_complexity_threshold"] += 1
            continue
        candidates.append(
            {
                "source_index": source_index,
                "target_smiles": target,
                "scaffold_smiles": target_scaffold,
                "steps": steps,
                "patents": patents,
                "max_depth": max_depth,
                "leaf_count": len(leaves),
                "heavy_atoms": heavy_atoms,
            }
        )

    rng = random.Random(SEED)
    rng.shuffle(candidates)
    chosen = []
    used_scaffolds: set[str] = set()
    for candidate in candidates:
        if candidate["scaffold_smiles"] in used_scaffolds:
            continue
        used_scaffolds.add(candidate["scaffold_smiles"])
        chosen.append(candidate)
        if len(chosen) == 500:
            break
    if len(chosen) < 500:
        raise RuntimeError(f"Only {len(chosen)} unique eligible scaffolds; need 500")

    source_candidates = chosen[:200]
    train_candidates = chosen[200:400]
    test_candidates = chosen[400:500]

    source_routes = []
    for serial, candidate in enumerate(source_candidates, 1):
        patents = candidate["patents"]
        source_routes.append(
            {
                "route_id": f"paroutes-n1-experience-{serial:04d}",
                "target_smiles": candidate["target_smiles"],
                "steps": candidate["steps"],
                "source_type": "compiled_patent_route_dataset",
                "source_id": f"PaRoutes2-n1-index-{candidate['source_index']}",
                "source_title": SOURCE_TITLE,
                "source_doi": SOURCE_DOI,
                "source_url": SOURCE_URL,
                "source_entry": (
                    f"n1-route-index:{candidate['source_index']};patents:{','.join(patents)}"
                ),
                "route_depth": candidate["max_depth"],
                "leaf_count": candidate["leaf_count"],
                "target_heavy_atoms": candidate["heavy_atoms"],
                "scaffold_smiles": candidate["scaffold_smiles"],
                "smiles_validated": True,
                "extraction_confidence": "medium",
                "needs_human_review": True,
                "quality_notes": [
                    "Mechanically extracted from reaction records belonging to one USPTO patent.",
                    "Reaction class, isolated yield, and complete conditions are unavailable.",
                    "Use for strategic experience distillation, not as an executable literature procedure.",
                ],
            }
        )

    train_targets = [
        target_record(candidate, "train", serial)
        for serial, candidate in enumerate(train_candidates, 1)
    ]
    test_targets = [
        target_record(candidate, "test", serial)
        for serial, candidate in enumerate(test_candidates, 1)
    ]

    # Scaffold grouping was already enforced across all 500 records. These are fixed views of train.
    optimizer_targets = train_targets[:160]
    selection_targets = train_targets[160:]
    pilot_targets = optimizer_targets[:5]
    h1_targets = optimizer_targets[:30]

    out = args.output_dir
    write_jsonl(out / "experience_source_routes.jsonl", source_routes)
    write_jsonl(out / "train_targets.jsonl", train_targets)
    write_jsonl(out / "test_targets.jsonl", test_targets)
    write_jsonl(out / "optimizer_targets.jsonl", optimizer_targets)
    write_jsonl(out / "selection_targets.jsonl", selection_targets)
    write_jsonl(out / "pilot_targets.jsonl", pilot_targets)
    write_jsonl(out / "h1_screen_targets.jsonl", h1_targets)

    report = {
        "source": {
            "title": SOURCE_TITLE,
            "doi": SOURCE_DOI,
            "url": SOURCE_URL,
            "raw_file": str(args.routes),
            "raw_sha256": hashlib.sha256(args.routes.read_bytes()).hexdigest(),
        },
        "seed": SEED,
        "selection": {
            "target_heavy_atoms": [18, 65],
            "minimum_steps": 5,
            "minimum_leaves": 2,
            "scaffold_disjoint_across_source_train_test": True,
        },
        "raw_routes": len(raw_routes),
        "eligible_candidates": len(candidates),
        "selected_unique_scaffolds": len(chosen),
        "rejected": dict(rejected),
        "output_counts": {
            "experience_source_routes": len(source_routes),
            "train_targets": len(train_targets),
            "test_targets": len(test_targets),
            "optimizer_targets": len(optimizer_targets),
            "selection_targets": len(selection_targets),
            "pilot_targets": len(pilot_targets),
            "h1_screen_targets": len(h1_targets),
        },
        "limitations": [
            "PaRoutes contains patent-derived benchmark routes, not independently validated procedures.",
            "Reaction names, yields, and full conditions are absent in the route artifact.",
            "All selected records require human review before chemical execution.",
        ],
    }
    (out / "dataset_quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
