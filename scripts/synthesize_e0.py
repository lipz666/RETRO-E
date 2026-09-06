"""Distill E0 from all experience-source routes with resumable batch summaries."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from retro_e.api import OpenAICompatibleClient
from retro_e.config import load_config
from retro_e.io import append_jsonl, read_jsonl
from retro_e.prompts import approximate_token_count


def compact_route(route: dict[str, Any]) -> str:
    lines = [
        (
            f"ROUTE {route['route_id']} | patent provenance: {route['source_entry']} | "
            f"depth={route.get('route_depth')} leaves={route.get('leaf_count')}"
        ),
        f"TARGET {route['target_smiles']}",
    ]
    for step in route["steps"]:
        precursors = " + ".join(step["precursor_smiles"])
        lines.append(f"{step['step_id']}. {step['product_smiles']} <= {precursors}")
    return "\n".join(lines)


def load_completed(path: Path, source_hash: str, model: str) -> dict[int, str]:
    if not path.exists():
        return {}
    rows = {}
    for row in read_jsonl(path):
        if row.get("source_sha256") == source_hash and row.get("model") == model:
            rows[int(row["batch_id"])] = str(row["summary"])
    return rows


def clean_context(text: str) -> str:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1]).strip()
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/experiment.toml"))
    parser.add_argument(
        "--routes", type=Path, default=Path("data/processed/experience_source_routes.jsonl")
    )
    parser.add_argument("--output", type=Path, default=Path("contexts/experience_E0.md"))
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()

    config = load_config(args.config)
    routes = list(read_jsonl(args.routes))
    if len(routes) != 200:
        raise ValueError(f"Expected exactly 200 experience routes, found {len(routes)}")
    source_hash = hashlib.sha256(args.routes.read_bytes()).hexdigest()
    batch_path = config.root / "contexts/candidates/e0_batch_summaries.jsonl"
    completed = load_completed(batch_path, source_hash, config.generator_model)
    synthesis_prompt = config.paths.experience_synthesis_prompt.read_text(encoding="utf-8").strip()

    with OpenAICompatibleClient(config) as client:
        summaries = []
        for offset in range(0, len(routes), args.batch_size):
            batch_id = offset // args.batch_size
            if batch_id in completed:
                summaries.append(completed[batch_id])
                continue
            batch = routes[offset : offset + args.batch_size]
            route_text = "\n\n".join(compact_route(route) for route in batch)
            prompt = f"""{synthesis_prompt}

This is batch {batch_id + 1} of {(len(routes) + args.batch_size - 1) // args.batch_size}.
Extract only transferable strategic lessons supported by repeated route patterns. Explicitly distinguish a robust lesson from a limitation of patent-derived data. Do not copy a target, route, patent ID, or SMILES into the summary. Produce 12-20 concise candidate lessons for later aggregation; this is not the final E0.

ROUTES
{route_text}"""
            completion = client.complete(
                prompt,
                temperature=0.2,
                top_p=1.0,
                max_tokens=5000,
                model=config.generator_model,
            )
            summary = clean_context(completion.content)
            append_jsonl(
                batch_path,
                {
                    "batch_id": batch_id,
                    "source_sha256": source_hash,
                    "route_ids": [route["route_id"] for route in batch],
                    "model": config.generator_model,
                    "summary": summary,
                    "usage": completion.usage,
                    "request_id": completion.request_id,
                },
            )
            summaries.append(summary)
            print(f"completed batch {batch_id + 1}", flush=True)

        summary_text = "\n\n".join(
            f"BATCH {index + 1}\n{summary}" for index, summary in enumerate(summaries)
        )
        aggregation = f"""{synthesis_prompt}

Below are independent summaries distilled from all 200 source routes. Produce the final E0 as a numbered section titled SYNTHETIC EXPERIENCE.

Requirements:
- 650-750 English words (approximately 850-1050 tokens);
- strategic, operational retrosynthetic judgment rather than textbook facts;
- prioritize route-global decisions, precursor accessibility, convergence, ordering, selectivity, unstable intermediates, stereocontrol, and downstream consequences;
- preserve useful conditional nuance and avoid absolute claims;
- no target, patent, route ID, SMILES, named example from the source, tool instruction, workflow, or statement that the patent routes are experimentally validated;
- no discussion of this summarization process;
- output only the final context.

BATCH SUMMARIES
{summary_text}"""
        completion = client.complete(
            aggregation,
            temperature=0.2,
            top_p=1.0,
            max_tokens=1600,
            model=config.generator_model,
        )
        candidate = clean_context(completion.content)

        critique_prompt = f"""Audit this proposed synthetic-experience context for an experiment on unseen targets.
Identify target memorization, molecule-specific content, patent-data overgeneralization, contradictions, generic filler, non-actionable rules, and missing strategic lessons. Also assess whether its length is near 1000 tokens. Do not rewrite it. Return a concise numbered critique.

CONTEXT
{candidate}"""
        critique = client.complete(
            critique_prompt,
            temperature=0.0,
            top_p=1.0,
            max_tokens=2500,
            model=config.judge_model,
        )
        revision_prompt = f"""Revise the candidate context using the audit. Output only the final section titled SYNTHETIC EXPERIENCE with numbered entries.

Hard constraints:
- 650-750 English words (approximately 850-1050 tokens);
- general strategic experience only;
- no source target, patent, route ID, SMILES, concrete molecule example, tool/workflow instruction, or claim of experimental validation;
- do not mention the audit or the training experiment.

CANDIDATE
{candidate}

AUDIT
{critique.content}"""
        revision = client.complete(
            revision_prompt,
            temperature=0.1,
            top_p=1.0,
            max_tokens=1600,
            model=config.generator_model,
        )
        final_context = clean_context(revision.content)
        final_usage = revision.usage

        for compression_round in range(3):
            current_tokens = approximate_token_count(final_context)
            if current_tokens <= 1100:
                break
            compression_prompt = f"""Rewrite this synthetic-experience context as exactly 16 numbered entries. Each entry must contain 25-35 English words in one sentence. Preserve the highest-value route-planning lessons and conditional nuance; remove repetition, generic filler, and explanation. Keep the title SYNTHETIC EXPERIENCE. Add no examples, targets, SMILES, tools, or meta-commentary. Output only the title and 16 entries.

CONTEXT
{final_context}"""
            compressed = client.complete(
                compression_prompt,
                temperature=0.0,
                top_p=1.0,
                max_tokens=800,
                model=config.generator_model,
            )
            final_context = clean_context(compressed.content)
            final_usage = compressed.usage
            print(
                f"compression round {compression_round + 1}: "
                f"approximately {approximate_token_count(final_context)} tokens",
                flush=True,
            )

    exact_leaks = [
        route["route_id"]
        for route in routes
        if route["route_id"] in final_context or route["target_smiles"] in final_context
    ]
    if exact_leaks:
        raise ValueError(f"Exact source identifiers leaked into E0: {exact_leaks[:10]}")
    token_estimate = approximate_token_count(final_context)
    if not 750 <= token_estimate <= 1200:
        raise ValueError(f"E0 approximate token count {token_estimate} is outside safety bounds")

    args.output.write_text(final_context.rstrip() + "\n", encoding="utf-8")
    audit_path = config.root / "contexts/candidates/e0_final_audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "source_sha256": source_hash,
                "generator_model": config.generator_model,
                "judge_model": config.judge_model,
                "approximate_tokens": token_estimate,
                "final_completion_usage": final_usage,
                "exact_source_leaks": exact_leaks,
                "candidate_before_audit": candidate,
                "audit": critique.content,
                "final_sha256": hashlib.sha256(final_context.encode()).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "approximate_tokens": token_estimate,
                "sha256": hashlib.sha256(final_context.encode()).hexdigest(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
