from __future__ import annotations

import tempfile
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "vendor" / "python-deps"))
sys.path.insert(0, str(REPO_ROOT / "vendor" / "pyJianYingDraft-source"))

from pyJianYingDraft import draft_codec


def test_write_bytes_atomic_uses_uuid_file_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "draft_content.json"
    target.write_bytes(b"old")

    def fail_named_temporary_file(*args, **kwargs):
        raise AssertionError("atomic writer must not use NamedTemporaryFile")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", fail_named_temporary_file)

    result = draft_codec._write_bytes_atomic(target, b"new")

    assert result == target
    assert target.read_bytes() == b"new"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []
