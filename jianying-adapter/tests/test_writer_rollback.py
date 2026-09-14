from __future__ import annotations

from pathlib import Path

import pytest

from jianying_adapter.writer_rollback import WriterRollbackError, rollback_new_draft


def test_failed_new_writer_draft_is_quarantined_and_root_meta_restored(tmp_path: Path) -> None:
    root = tmp_path / "logical"
    root.mkdir()
    draft = root / "NEW_DRAFT"
    draft.mkdir()
    (draft / "draft_content.json").write_text("broken", encoding="utf-8")
    root_meta = root / "root_meta_info.json"
    root_meta.write_text("after", encoding="utf-8")
    backup = tmp_path / "backup" / "root_meta_info.json"
    backup.parent.mkdir()
    backup.write_text("before", encoding="utf-8")

    report = rollback_new_draft(
        logical_root=root,
        draft_name="NEW_DRAFT",
        root_meta_backup=backup,
        quarantine_root=tmp_path / "quarantine",
        failure=RuntimeError("post-write timeline mismatch"),
    )

    assert report["status"] == "rolled_back"
    assert root_meta.read_text(encoding="utf-8") == "before"
    assert not draft.exists()
    assert Path(report["failed_draft_quarantine"]).is_dir()
    assert report["permanent_delete_performed"] is False


def test_rollback_failure_restores_failed_draft_to_live_location(tmp_path: Path) -> None:
    root = tmp_path / "logical"
    root.mkdir()
    draft = root / "NEW_DRAFT"
    draft.mkdir()
    root_meta = root / "root_meta_info.json"
    root_meta.write_text("after", encoding="utf-8")
    backup = tmp_path / "backup" / "root_meta_info.json"
    backup.parent.mkdir()
    backup.write_text("before", encoding="utf-8")

    def fail_restore(_source: Path, _target: Path) -> None:
        raise OSError("injected restore failure")

    with pytest.raises(WriterRollbackError):
        rollback_new_draft(
            logical_root=root,
            draft_name="NEW_DRAFT",
            root_meta_backup=backup,
            quarantine_root=tmp_path / "quarantine",
            failure=RuntimeError("post-write failure"),
            _restore_root_meta=fail_restore,
        )

    assert draft.is_dir()
    assert root_meta.read_text(encoding="utf-8") == "after"


def test_rollback_restores_through_physical_root_when_logical_writes_fail(tmp_path, draft_root_alias):
    from jianying_adapter.writer_rollback import restore_file_atomic
    logical, physical = draft_root_alias
    draft = physical / "NEW_DRAFT"
    draft.mkdir()
    (draft / "draft_content.json").write_text("failed", encoding="utf-8")
    root_meta = physical / "root_meta_info.json"
    root_meta.write_text("after", encoding="utf-8")
    backup = tmp_path / "root_meta.before.json"
    backup.write_text("before", encoding="utf-8")

    def physical_only(source, target):
        if not target.is_relative_to(physical):
            raise FileExistsError("C alias does not accept writes")
        restore_file_atomic(source, target)

    report = rollback_new_draft(
        logical_root=logical, physical_root=physical, draft_name="NEW_DRAFT",
        root_meta_backup=backup, quarantine_root=tmp_path / "quarantine",
        failure=RuntimeError("post-write check failed"), _restore_root_meta=physical_only,
    )
    assert report["physical_root"] == str(physical)
    assert report["logical_root"] == str(logical)
    assert root_meta.read_text(encoding="utf-8") == "before"
    assert not (logical / "NEW_DRAFT").exists()
    assert (Path(report["failed_draft_quarantine"]) / "draft_content.json").read_text(encoding="utf-8") == "failed"


def test_rollback_rejects_mismatched_physical_root_before_mutation(tmp_path):
    logical, physical = tmp_path / "logical", tmp_path / "physical"
    logical.mkdir()
    physical.mkdir()
    with pytest.raises(WriterRollbackError, match="same directory"):
        rollback_new_draft(
            logical_root=logical, physical_root=physical, draft_name="NEW_DRAFT",
            root_meta_backup=tmp_path / "backup", quarantine_root=tmp_path / "quarantine",
            failure=RuntimeError("failed"),
        )
    assert list(logical.iterdir()) == []
    assert list(physical.iterdir()) == []
    assert not (tmp_path / "quarantine").exists()
