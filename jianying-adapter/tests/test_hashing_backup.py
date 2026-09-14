from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jianying_adapter.backup import create_complete_backup
from jianying_adapter.hashing import object_sha256, tree_fingerprint


def test_object_hash_key_order_stable():
    assert object_sha256({"b": 2, "a": 1}) == object_sha256({"a": 1, "b": 2})


def test_tree_hash_stable_ignores_mtime(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    path = root / "synthetic.txt"
    path.write_text("SYNTH", encoding="utf-8")
    first = tree_fingerprint(root)
    os.utime(path, (path.stat().st_atime + 100, path.stat().st_mtime + 100))
    second = tree_fingerprint(root)
    assert first == second


def test_backup_manifest_and_restore_plan(synthetic_draft: Path, tmp_path: Path):
    destination = tmp_path / "SYNTH-BACKUP"
    result = create_complete_backup(synthetic_draft, destination)
    assert synthetic_draft.exists()
    assert result.source_fingerprint == result.backup_fingerprint
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    restore = json.loads(Path(result.restore_plan_path).read_text(encoding="utf-8"))
    assert manifest["fingerprint"]["tree_sha256"] == result.source_fingerprint.tree_sha256
    assert restore["delete_extraneous"] is False
    assert restore["strategy"] == "copy-overwrite-existing-files"


@pytest.mark.parametrize("existing_output", ["backup", "manifest", "restore_plan"])
def test_backup_refuses_any_existing_output_without_creating_backup(
    synthetic_draft: Path,
    tmp_path: Path,
    existing_output: str,
):
    destination = tmp_path / "SYNTH-BACKUP"
    manifest = tmp_path / "SYNTH-BACKUP.manifest.json"
    restore_plan = tmp_path / "SYNTH-BACKUP.restore-plan.json"
    outputs = {
        "backup": destination,
        "manifest": manifest,
        "restore_plan": restore_plan,
    }
    protected = outputs[existing_output]
    if existing_output == "backup":
        protected.mkdir()
        sentinel = protected / "sentinel.txt"
    else:
        sentinel = protected
    sentinel.write_bytes(b"DO NOT OVERWRITE")

    with pytest.raises(FileExistsError, match="Refusing overwrite"):
        create_complete_backup(synthetic_draft, destination)

    assert sentinel.read_bytes() == b"DO NOT OVERWRITE"
    if existing_output != "backup":
        assert not destination.exists()
