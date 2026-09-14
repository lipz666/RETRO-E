"""Rerun round 1's E* vs E0 on its own targets with the current generator.

Experiment A's positive control came in at +0.060 [-0.037, +0.152] where round 1 measured
+0.140 and +0.130. Two things had changed at once: the target set (200 fresh scaffolds
instead of round 1's 100 test targets) and the generator (gemini-3.8 instead of 3.7). This
holds the targets and the whole design fixed at round 1's and varies only the generator.

    uv run python scripts/run_control_check.py --protocol-hash HASH --workers 40

A null here points at the generator, which is what the project's model-specificity reading
predicts: E* was optimised against 3.7's failure modes. A positive result points at the new
target set instead, and experiment A's control failure would then need another explanation.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from retro_e.api import OpenAICompatibleClient
from retro_e.config import ExperimentConfig, load_config
from retro_e.io import append_jsonl, assert_protocol, existing_keys, read_jsonl
from retro_e.judge import run_judging, source_winner
from retro_e.prompts import compose_generation_prompt
from retro_e.validation import validate_response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
CONDITIONS = ("E0", "E_star")


def generate_one(config, target, condition, sample_id, protocol_hash):
    smiles = str(target["target_smiles"])
    prompt = compose_generation_prompt(config, smiles, condition)
    with OpenAICompatibleClient(config) as client:
        c = client.complete(prompt, temperature=config.generation.temperature,
                            top_p=config.generation.top_p,
                            max_tokens=config.generation.max_tokens,
                            model=config.generator_model)
    v = validate_response(c.content, smiles, expected_routes=config.generation.n_routes)
    return {
        "record_id": f"ctrl:{condition}:{target['target_id']}:{sample_id}:{protocol_hash[:12]}",
        "target_id": str(target["target_id"]), "target_smiles": smiles,
        "condition": condition, "sample_id": sample_id,
        "model": config.generator_model, "response_model": c.response_model,
        "temperature": config.generation.temperature, "top_p": config.generation.top_p,
        "max_tokens": config.generation.max_tokens, "n_routes": config.generation.n_routes,
        "system_prompt_version": config.protocol.system_prompt_version,
        "experience_version": condition, "protocol_sha256": protocol_hash,
        "route_text": json.dumps(v.normalized or v.parsed, ensure_ascii=False,
                                 separators=(",", ":")) if v.parsed is not None else "",
        "raw_response": c.content, "valid": v.valid,
        "validation_errors": list(v.errors), "validation_warnings": list(v.warnings),
        "usage": c.usage, "request_id": c.request_id, "latency_seconds": c.latency_seconds,
        "created_at": datetime.now(UTC).isoformat(),
    }


def run(args):
    config: ExperimentConfig = load_config(args.config).with_roles(
        generator="generator", judge="judge")
    targets = list(read_jsonl(ROOT / "data/processed/test_targets.jsonl"))
    out = Path(config.paths.results_dir) / "control_check"
    gen, jud = out / "generations", out / "judgments"
    gen.mkdir(parents=True, exist_ok=True); jud.mkdir(parents=True, exist_ok=True)

    # Hard preflight: nothing reaches the API unless the design matches round 1's exactly.
    from retro_e.optimize import locked_token_count
    e0 = config.paths.experience_e0.read_text(encoding="utf-8").strip()
    es = config.paths.experience_e_star.read_text(encoding="utf-8").strip()
    print(f"  targets={len(targets)}  samples={config.generation.samples_per_target} "
          f"votes={config.judge.runs_per_pair}  temp={config.generation.temperature}")
    print(f"  E0={locked_token_count(e0)} tok  E_star={locked_token_count(es)} tok")
    print(f"  generator={config.generator_model}  judge={config.judge_model}")
    assert len(targets) == 100, f"expected round 1's 100 test targets, got {len(targets)}"
    assert config.generation.samples_per_target == 3 and config.judge.runs_per_pair == 3
    assert locked_token_count(e0) == 736 and locked_token_count(es) == 755
    assert "3.8" in config.generator_model and "3.7" in config.judge_model
    print("  preflight OK")

    if args.stage in ("all", "generate"):
        for condition in CONDITIONS:
            path = gen / f"{condition}.jsonl"
            assert_protocol(path, args.protocol_hash)
            done = existing_keys(path, ["target_id", "sample_id"])
            jobs = [(t, s) for t in targets
                    for s in range(config.generation.samples_per_target)
                    if (str(t["target_id"]), s) not in done]
            failed = 0
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                fs = [pool.submit(generate_one, config, t, condition, s, args.protocol_hash)
                      for t, s in jobs]
                for f in as_completed(fs):
                    try:
                        append_jsonl(path, f.result())
                    except Exception:
                        failed += 1
                        logger.exception("%s generation failed", condition)
            logger.info("generated %s: planned=%d failed=%d", condition, len(jobs), failed)

    if args.stage in ("all", "judge"):
        run_judging(config, gen / "E_star.jsonl", gen / "E0.jsonl", "E_star", "E0",
                    "E_star_vs_E0_control", jud / "E_star_vs_E0_control.jsonl",
                    args.protocol_hash, workers=args.workers)

    if args.stage in ("all", "metrics"):
        by_pair = defaultdict(list)
        for r in read_jsonl(jud / "E_star_vs_E0_control.jsonl"):
            by_pair[r["pair_id"]].append(r)
        by_t = defaultdict(list); rule = 0
        for rows in by_pair.values():
            w = source_winner(rows)
            by_t[str(rows[0]["target_id"])].append(
                1.0 if w == "E_star" else (0.0 if w == "Tie" else -1.0))
            rule += rows[0]["adjudication"] == "predeclared_validity_rule"
        means = [statistics.mean(v) for v in by_t.values()]
        rng = random.Random(20260914)
        d = sorted(statistics.mean(rng.choices(means, k=len(means))) for _ in range(10000))
        lo, hi = d[math.floor(.025*9999)], d[math.ceil(.975*9999)]
        report = {
            "stage": "control_check", "protocol_sha256": args.protocol_hash,
            "design": "round 1 Stage-5 exactly: 100 test targets, 3 samples, 3 votes",
            "generator": config.generator_model, "judge": config.judge_model,
            "pairs": len(by_pair), "targets": len(by_t),
            "net_win_rate": statistics.mean(means), "cluster_95ci": [lo, hi],
            "decided_by_validity_rule": rule,
            "round1_reference": {"stage5_gemini_judge": 0.140, "cross_judge_luna": 0.130},
            "reading": (
                "Reproducing +0.13 to +0.14 points at the new target set as the cause of "
                "experiment A's control failure. A null points at the generator, which is "
                "what the model-specificity reading predicts."
            ),
            "created_at": datetime.now(UTC).isoformat(),
        }
        (out / "control_metrics.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"\n  E* vs E0 (3.8 生成, round1 原 target): net={statistics.mean(means):+.3f} "
              f"CI=[{lo:+.3f},{hi:+.3f}]  pairs={len(by_pair)}  规则判决={rule}")
        print("  round1 参照: +0.140 (gemini judge) / +0.130 (luna judge)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--protocol-hash", required=True)
    p.add_argument("--config", default="config/v2_control.toml")
    p.add_argument("--workers", type=int, default=40)
    p.add_argument("--stage", choices=["all", "generate", "judge", "metrics"], default="all")
    run(p.parse_args())


if __name__ == "__main__":
    main()
