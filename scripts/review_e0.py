"""Perform content-only QA on E0 without using target performance or reward."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from retro_e.api import OpenAICompatibleClient
from retro_e.config import load_config
from retro_e.io import read_jsonl
from retro_e.prompts import approximate_token_count


def clean(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = "\n".join(value.splitlines()[1:-1]).strip()
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/experiment.toml"))
    parser.add_argument("--context", type=Path, default=Path("contexts/experience_E0.md"))
    parser.add_argument(
        "--routes", type=Path, default=Path("data/processed/experience_source_routes.jsonl")
    )
    args = parser.parse_args()

    config = load_config(args.config)
    original = args.context.read_text(encoding="utf-8").strip()
    routes = list(read_jsonl(args.routes))
    with OpenAICompatibleClient(config) as client:
        audit = client.complete(
            f"""Audit this E0 for a frozen-LLM retrosynthesis experiment. It was distilled only from patent-derived routes. Focus on whether it expresses transferable route-global experience rather than a list of reaction tricks. Flag overgeneralization, contradictions, narrow scaffold bias, duplicated lessons, missing route-level decisions, and target memorization. Do not rewrite it.

E0
{original}""",
            temperature=0.0,
            top_p=1.0,
            max_tokens=1800,
            model=config.judge_model,
        )
        rewrite = client.complete(
            f"""Rewrite E0 using the audit. This is content-only quality control: no target reward or test data has been observed.

Output exactly:
SYNTHETIC EXPERIENCE
followed by exactly 16 numbered one-sentence entries, each 25-40 English words.

Entries 1-8 must be route-global strategic experience: disconnection choice by downstream simplification, convergence, longest linear sequence, starting-material accessibility, ordering, chemoselectivity risk propagation, stereochemical planning, and fallback/robustness.
Entries 9-16 must be conditional tactical experience distilled from the source: protection, heteroaryl/cross-coupling order, unstable or hazardous intermediates, oxidation-state planning, symmetry differentiation, and telescoping/purification. State boundary conditions rather than universal prescriptions.

Do not include a source target, route, patent ID, SMILES, concrete molecule example, tool instruction, workflow, generic textbook definition, or claim of experimental validation. Named reaction classes may appear only when they clarify a transferable conditional lesson. Output only the final context.

CURRENT E0
{original}

AUDIT
{audit.content}""",
            temperature=0.1,
            top_p=1.0,
            max_tokens=950,
            model=config.generator_model,
        )
    revised = clean(rewrite.content)
    entries = re.findall(r"(?m)^\d+\.\s+", revised)
    token_count = approximate_token_count(revised)
    exact_leaks = [
        route["route_id"]
        for route in routes
        if route["route_id"] in revised or route["target_smiles"] in revised
    ]
    if len(entries) != 16:
        raise ValueError(f"Expected 16 entries, found {len(entries)}")
    if not 750 <= token_count <= 1200:
        raise ValueError(f"Approximate token count {token_count} is outside 750-1200")
    if exact_leaks:
        raise ValueError(f"Exact source identifiers leaked: {exact_leaks[:10]}")

    candidate_dir = config.root / "contexts/candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    original_hash = hashlib.sha256(original.encode()).hexdigest()
    (candidate_dir / f"experience_E0_before_review_{original_hash[:12]}.md").write_text(
        original.rstrip() + "\n", encoding="utf-8"
    )
    args.context.write_text(revised.rstrip() + "\n", encoding="utf-8")
    report = {
        "original_sha256": original_hash,
        "revised_sha256": hashlib.sha256(revised.encode()).hexdigest(),
        "approximate_tokens": token_count,
        "entries": len(entries),
        "exact_source_leaks": exact_leaks,
        "generator_model": config.generator_model,
        "judge_model": config.judge_model,
        "audit": audit.content,
    }
    (candidate_dir / "e0_content_review.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: report[key] for key in report if key != "audit"}, indent=2))


if __name__ == "__main__":
    main()
