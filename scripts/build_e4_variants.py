"""Build the module-deletion variants of E* for the E4 intervention (plan v2 section 9.2).

Module membership is the assignment frozen in reports/v2/e0_estar_semantic_diff.md before any
ablation was run, so it cannot be reshaped to fit an outcome. Each of the four modules
partitions the 16 entries exactly once.

Deleting a module also shortens the context. That length change is a confound the plan
acknowledges (section 9.2 keeps a length-matched neutral substitution for the strongest
module as a follow-up); token counts are recorded here so it can be accounted for rather
than forgotten.

    uv run python scripts/build_e4_variants.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retro_e.optimize import locked_token_count

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "contexts/experience_E_star.md"
OUT = ROOT / "contexts/v2/e4_ablation"

MODULES = {
    "M1_strategic_disconnection": {
        "entries": [1, 2, 3, 8, 10],
        "label": "战略断键与收敛",
        "error_tags": ["structural_or_stereochemistry_omission", "infeasible_key_transformation"],
    },
    "M2_selectivity_protection": {
        "entries": [5, 6, 7, 16],
        "label": "选择性与保护",
        "error_tags": ["chemoselectivity_conflict", "unnecessary_protection_loop"],
    },
    "M3_sequencing_reliability": {
        "entries": [11, 12, 13, 14, 15],
        "label": "步骤排序与可靠性",
        "error_tags": ["missing_step", "unresolved_intermediate", "graph_connectivity_error"],
    },
    "M4_materials_complexity": {
        "entries": [4, 9],
        "label": "原料与复杂度分配",
        "error_tags": ["starting_material_availability_assumption"],
    },
}


def parse_entries(text: str) -> tuple[str, dict[int, str]]:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    title = lines[0].strip()
    entries: dict[int, str] = {}
    for line in lines[1:]:
        match = re.match(r"^\s*(\d+)\.\s*(.+)$", line)
        if match:
            entries[int(match.group(1))] = match.group(2).strip()
    return title, entries


def render(title: str, entries: dict[int, str], keep: list[int]) -> str:
    body = "\n".join(f"{position}. {entries[key]}" for position, key in enumerate(keep, 1))
    return f"{title}\n\n{body}\n"


def main() -> None:
    title, entries = parse_entries(SOURCE.read_text(encoding="utf-8"))
    assigned = sorted(index for module in MODULES.values() for index in module["entries"])
    if assigned != sorted(entries):
        raise SystemExit(
            f"Module assignment does not partition the entries: assigned={assigned} "
            f"present={sorted(entries)}"
        )

    OUT.mkdir(parents=True, exist_ok=True)
    full_text = render(title, entries, sorted(entries))
    (OUT / "full_E_star.md").write_text(full_text, encoding="utf-8")
    full_tokens = locked_token_count(full_text)

    report = {
        "source": str(SOURCE.relative_to(ROOT)),
        "module_assignment_frozen_in": "reports/v2/e0_estar_semantic_diff.md",
        "entries_total": len(entries),
        "full": {"file": "contexts/v2/e4_ablation/full_E_star.md", "locked_tokens": full_tokens},
        "variants": {},
        "length_confound_note": (
            "Deleting a module shortens the context. A drop in quality therefore has two "
            "candidate explanations: the module's content, and the shorter prompt. The plan's "
            "follow-up for the strongest module is a length-matched neutral substitution."
        ),
    }
    for name, module in MODULES.items():
        keep = [index for index in sorted(entries) if index not in module["entries"]]
        text = render(title, entries, keep)
        path = OUT / f"drop_{name}.md"
        path.write_text(text, encoding="utf-8")
        tokens = locked_token_count(text)
        report["variants"][name] = {
            "file": str(path.relative_to(ROOT)),
            "label": module["label"],
            "dropped_entries": module["entries"],
            "entries_remaining": len(keep),
            "locked_tokens": tokens,
            "tokens_removed": full_tokens - tokens,
            "fraction_removed": round((full_tokens - tokens) / full_tokens, 4),
            "predicted_error_tags": module["error_tags"],
        }

    (OUT / "variants.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"full E*: {full_tokens} locked tokens, {len(entries)} entries\n")
    print(f"{'variant':32}{'entries':>8}{'tokens':>8}{'removed':>9}{'%':>7}  预测受影响的 error_tag")
    for name, info in report["variants"].items():
        print(
            f"{name:32}{info['entries_remaining']:>8}{info['locked_tokens']:>8}"
            f"{info['tokens_removed']:>9}{info['fraction_removed']:>7.1%}  "
            f"{','.join(info['predicted_error_tags'])}"
        )
    print(f"\n写入 {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
