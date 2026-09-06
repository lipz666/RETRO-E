import json

from retro_e.dataset import audit_target_files


def test_audit_detects_cross_split_smiles_leak(tmp_path):
    path = tmp_path / "targets.jsonl"
    rows = [
        {
            "target_id": "a",
            "target_smiles": "CCO",
            "split": "train",
            "source_type": "paper",
            "source_id": "x",
        },
        {
            "target_id": "b",
            "target_smiles": "OCC",
            "split": "test",
            "source_type": "paper",
            "source_id": "y",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    report = audit_target_files([path])
    assert not report["passed"]
    assert report["canonical_smiles_leak_count"] == 1
