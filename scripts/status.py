"""Print the live state of the project by reading artifacts, not documentation.

Every number here is derived from files on disk, so it cannot drift from reality the way a
hand-maintained status section does. docs/STATUS.md carries the narrative and points here
for the current figures.

    uv run python scripts/status.py
    uv run python scripts/status.py --json
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TIERS = {"screen": 16, "advance": 4, "selection": 2}
METHODS = ("reflective", "search_only", "instruction_opt")
SEEDS = (11, 29, 47)


def jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def protocol_hashes() -> dict[str, Any]:
    found: dict[str, Any] = {}
    for label, directory in (("round1", "results/manifests"), ("v2", "results/v2/manifests")):
        entries = {}
        for path in sorted((ROOT / directory).glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if "protocol_sha256" not in payload:
                continue
            entries[path.stem.split("-", 1)[0]] = {
                "protocol_sha256": payload["protocol_sha256"],
                "resolved_models": payload.get("resolved_models", {}),
            }
        found[label] = entries
    return found


def splits() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, path in (
        ("round1 experience_source", "data/processed/experience_source_routes.jsonl"),
        ("round1 train", "data/processed/train_targets.jsonl"),
        ("round1 optimizer", "data/processed/optimizer_targets.jsonl"),
        ("round1 selection", "data/processed/selection_targets.jsonl"),
        ("round1 test", "data/processed/test_targets.jsonl"),
        ("v2 dev_new", "data/v2/dev_new_targets.jsonl"),
        ("v2 holdout_new", "data/v2/holdout_new_targets.jsonl"),
        ("v2 external_audit_panel", "data/v2/external_audit_panel.jsonl"),
    ):
        out[name] = jsonl_count(ROOT / path)
    audit = ROOT / "data/v2/splits.json"
    if audit.exists():
        payload = json.loads(audit.read_text(encoding="utf-8"))
        out["v2 isolation_passed"] = payload["isolation_audit"]["passed"]
        out["v2 reserve_scaffolds_left"] = payload["pool"]["reserve_left"]
    return out


def baselines() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("optimizer_pool_baseline", "selection_pool_baseline"):
        path = ROOT / "results/v2/generations" / f"{name}.jsonl"
        if not path.exists():
            out[name] = {"records": 0}
            continue
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        out[name] = {
            "records": len(rows),
            "sample_0": sum(row["sample_id"] == 0 for row in rows),
            "valid": sum(row["valid"] for row in rows),
            "models": sorted({row["response_model"] for row in rows}),
            "protocol": sorted({row["protocol_sha256"][:12] for row in rows}),
        }
    return out


def e1_runs() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for method in METHODS:
        for seed in SEEDS:
            ledger = ROOT / "results/v2/optimization" / method / str(seed) / "ledger.jsonl"
            key = f"{method}/{seed}"
            if not ledger.exists():
                out[key] = {"state": "not started"}
                continue
            rows = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
                if line
            ]
            tiers = collections.Counter(row["tier"] for row in rows)
            complete = all(tiers.get(tier) == count for tier, count in EXPECTED_TIERS.items())
            entry: dict[str, Any] = {
                "state": "complete" if complete else "partial",
                "tiers": {tier: tiers.get(tier, 0) for tier in EXPECTED_TIERS},
                "expected": EXPECTED_TIERS,
                "compression_passes_total": sum(
                    row.get("compression_passes", 0) for row in rows
                ),
                "candidates_rejected_total": sum(len(row.get("rejected", [])) for row in rows),
            }
            stop = ledger.parent / "screen_stop_reason.json"
            if stop.exists():
                entry["screen_stopped_early"] = json.loads(stop.read_text(encoding="utf-8"))[
                    "reason"
                ]
            promotion = ROOT / "contexts/v2" / method / str(seed) / "promotion.json"
            if promotion.exists():
                payload = json.loads(promotion.read_text(encoding="utf-8"))
                entry["winner"] = payload["winner_candidate_id"]
                entry["selection_reward"] = payload["selection_reward"]
                entry["winner_tokens"] = payload["locked_tokens"]
                entry["full_schedule"] = payload["full_schedule"]
            out[key] = entry
    return out


def downstream() -> dict[str, Any]:
    dev = ROOT / "results/v2/dev_eval"
    return {
        "dev_eval_generations": (
            sum(jsonl_count(p) for p in (dev / "generations").glob("*.jsonl"))
            if (dev / "generations").exists()
            else 0
        ),
        "dev_eval_judgments": (
            sum(jsonl_count(p) for p in (dev / "judgments").glob("*.jsonl"))
            if (dev / "judgments").exists()
            else 0
        ),
        "p0_reanalysis_done": (ROOT / "results/v2/p0_round1_reanalysis.json").exists(),
        "semantic_diff_done": (ROOT / "reports/v2/e0_estar_semantic_diff.md").exists(),
        "transfer_e3_started": (ROOT / "results/v2/transfer").exists(),
        "ablation_e4_started": (ROOT / "results/v2/ablation").exists(),
    }


def checks() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, command in (
        ("tests", [".venv/bin/python", "-m", "pytest", "-q", "--no-header", "-x"]),
        ("ruff", [".venv/bin/python", "-m", "ruff", "check", "src", "scripts", "tests"]),
    ):
        try:
            done = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=300, check=False)
            out[name] = "pass" if done.returncode == 0 else "FAIL"
        except Exception as exc:  # noqa: BLE001 -- status must never crash on a tool problem
            out[name] = f"could not run: {type(exc).__name__}"
    return out


def build(run_checks: bool) -> dict[str, Any]:
    report = {
        "protocol_hashes": protocol_hashes(),
        "splits": splits(),
        "baselines": baselines(),
        "e1_runs": e1_runs(),
        "downstream": downstream(),
    }
    if run_checks:
        report["checks"] = checks()
    return report


def render(report: dict[str, Any]) -> None:
    print("PROTOCOL HASHES")
    for phase, entries in report["protocol_hashes"].items():
        for stage, info in entries.items():
            models = info["resolved_models"]
            print(f"  {phase:7} {stage:14} {info['protocol_sha256'][:16]}…")
            if models:
                print(
                    f"  {'':7} {'':14} generator={models.get('generator', '?')} "
                    f"judge={models.get('judge', '?')}"
                )

    print("\nSPLITS")
    for name, value in report["splits"].items():
        print(f"  {name:28} {value}")

    print("\nBASELINE CACHES")
    for name, info in report["baselines"].items():
        if not info["records"]:
            print(f"  {name:26} MISSING")
            continue
        print(
            f"  {name:26} {info['records']} records, sample0={info['sample_0']}, "
            f"valid={info['valid']}, model={','.join(info['models'])}, "
            f"protocol={','.join(info['protocol'])}"
        )

    print("\nE1 RUNS (expected screen/advance/selection = 16/4/2)")
    for key, info in report["e1_runs"].items():
        if info["state"] == "not started":
            print(f"  {key:24} not started")
            continue
        tiers = info["tiers"]
        line = (
            f"  {key:24} {info['state']:8} "
            f"{tiers['screen']:2}/{tiers['advance']}/{tiers['selection']}"
        )
        if "winner" in info:
            line += (
                f"  winner={info['winner'][:12]} reward={info['selection_reward']:.3f} "
                f"tok={info['winner_tokens']}"
            )
        if info.get("screen_stopped_early"):
            line += f"  STOPPED:{info['screen_stopped_early']}"
        print(line)
        print(
            f"  {'':24} compression_passes={info['compression_passes_total']} "
            f"rejected={info['candidates_rejected_total']}"
        )

    print("\nDOWNSTREAM")
    for name, value in report["downstream"].items():
        print(f"  {name:26} {value}")

    if "checks" in report:
        print("\nCHECKS")
        for name, value in report["checks"].items():
            print(f"  {name:26} {value}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--no-checks", action="store_true", help="Skip running pytest and ruff")
    args = parser.parse_args()
    report = build(run_checks=not args.no_checks)
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        print()
    else:
        render(report)


if __name__ == "__main__":
    main()
