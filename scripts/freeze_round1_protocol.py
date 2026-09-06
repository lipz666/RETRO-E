"""Snapshot every file that round-1 protocol hashes were computed over, and verify them.

The manifest in config.build_manifest hashes the whole `src/retro_e` tree, so any v2 code
change permanently destroys the ability to re-derive round-1 protocol hashes from the live
working tree. That would silently invalidate the reproducibility claim in
`reports/final_report_zh.md` section 8. This script copies the exact bytes into
`results/manifests/round1_frozen/` once, before v2 edits, and can re-verify afterwards.

    uv run python scripts/freeze_round1_protocol.py --mode freeze   # run once, pre-v2
    uv run python scripts/freeze_round1_protocol.py --mode verify   # run any time after
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = ROOT / "results/manifests"
SNAPSHOT = MANIFEST_DIR / "round1_frozen"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def manifests() -> list[Path]:
    return sorted(p for p in MANIFEST_DIR.glob("*.json") if p.is_file())


def freeze() -> int:
    """Snapshot per manifest, not as a flat union.

    Code legitimately evolved between stages (pilot -> h1_screen -> optimization -> final),
    so one file path can carry different bytes in different manifests. Anything whose live
    bytes no longer match its manifest is reported as unrecoverable rather than substituted.
    """
    SNAPSHOT.mkdir(parents=True, exist_ok=True)
    summary = {}
    exit_code = 0
    for manifest_path in manifests():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        stage_dir = SNAPSHOT / manifest_path.stem
        recovered, unrecoverable = [], []
        for relative, digest in sorted(payload["file_sha256"].items()):
            # An existing snapshot copy is authoritative. Re-running freeze after the working
            # tree has drifted must never downgrade a good snapshot to "unrecoverable".
            already = stage_dir / relative
            if already.exists() and sha256(already) == digest:
                recovered.append(relative)
                continue
            live = ROOT / relative
            if live.exists() and sha256(live) == digest:
                already.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live, already)
                recovered.append(relative)
            else:
                unrecoverable.append(relative)
        summary[manifest_path.stem] = {
            "protocol_sha256": payload["protocol_sha256"],
            "files_total": len(payload["file_sha256"]),
            "files_recovered": len(recovered),
            "files_unrecoverable": unrecoverable,
            "fully_recoverable": not unrecoverable,
        }
        if unrecoverable:
            exit_code = 1
        (stage_dir / "MANIFEST_FILES.json").parent.mkdir(parents=True, exist_ok=True)
        (stage_dir / "MANIFEST_FILES.json").write_text(
            json.dumps(payload["file_sha256"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    (SNAPSHOT / "FROZEN_FILES.json").write_text(
        json.dumps(
            {
                "purpose": "Byte-exact round-1 protocol inputs, frozen before v2 code changes.",
                "caveat": (
                    "Stages whose files_unrecoverable list is non-empty had already drifted before "
                    "this freeze ran, because the source tree kept evolving after those stages "
                    "executed. Their stored per-file hashes remain in results/manifests/, but their "
                    "aggregate protocol hash is no longer re-derivable from any local copy."
                ),
                "path_dependence": (
                    "build_manifest hashes config.public_dict(), which contains the absolute "
                    "repository root. Re-deriving an aggregate protocol hash therefore requires "
                    "restoring these files into the SAME absolute path they were hashed from "
                    "(verified: copying the snapshot to a different directory yields a different "
                    "aggregate hash even with byte-identical files). Per-file sha256 verification "
                    "is path-independent and is the check --mode verify performs."
                ),
                "stages": summary,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def verify() -> int:
    frozen = json.loads((SNAPSHOT / "FROZEN_FILES.json").read_text(encoding="utf-8"))
    report = {}
    failures = []
    for stage, info in sorted(frozen["stages"].items()):
        stage_dir = SNAPSHOT / stage
        expected = json.loads((stage_dir / "MANIFEST_FILES.json").read_text(encoding="utf-8"))
        bad = []
        for relative, digest in sorted(expected.items()):
            copy = stage_dir / relative
            if relative in info["files_unrecoverable"]:
                continue
            if not copy.exists() or sha256(copy) != digest:
                bad.append(relative)
        drifted = [
            relative
            for relative, digest in sorted(expected.items())
            if (ROOT / relative).exists() and sha256(ROOT / relative) != digest
        ]
        report[stage] = {
            "snapshot_intact": not bad,
            "snapshot_corrupted": bad,
            "never_recoverable": info["files_unrecoverable"],
            "working_tree_drifted_since": drifted,
        }
        failures.extend(bad)
    print(
        json.dumps(
            {
                "stages": report,
                "note": (
                    "Working-tree drift is expected once v2 edits land. Round-1 protocol hashes "
                    "stay re-derivable from this snapshot, not from the live tree."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["freeze", "verify"], default="verify")
    sys.exit(freeze() if parser.parse_args().mode == "freeze" else verify())
