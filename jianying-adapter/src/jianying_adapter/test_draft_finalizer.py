from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .jianying_environment import ProcessRecord, require_jianying_environment


class TestDraftFinalizerError(RuntimeError):
    __test__ = False

    pass


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def write_object_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _registered_name(entry: Mapping[str, Any]) -> str:
    raw = str(entry.get("draft_fold_path") or "").replace("\\", "/").rstrip("/")
    return raw.rsplit("/", 1)[-1] if raw else ""


def validate_acceptance_evidence(
    evidence: Mapping[str, Any], *, draft_name: str
) -> dict[str, Any]:
    if evidence.get("schema") != "jianying-adapter.test-draft-acceptance.v1":
        raise TestDraftFinalizerError("unsupported acceptance evidence schema")
    if evidence.get("status") != "completed":
        raise TestDraftFinalizerError("front-end acceptance evidence is not completed")
    if str(evidence.get("draft_name") or "") != draft_name:
        raise TestDraftFinalizerError("acceptance evidence draft_name mismatch")
    foreground = evidence.get("foreground_evidence")
    if not isinstance(foreground, Mapping) or foreground.get("status") != "completed":
        raise TestDraftFinalizerError("foreground_evidence is not completed")
    if str(foreground.get("observed_draft_name") or "") != draft_name:
        raise TestDraftFinalizerError("foreground evidence draft_name mismatch")
    required_true = (
        "full_playback_completed",
        "objective_audit_completed",
        "application_closed_normally",
    )
    missing_true = [key for key in required_true if foreground.get(key) is not True]
    if missing_true:
        raise TestDraftFinalizerError(
            "foreground evidence incomplete: " + ", ".join(missing_true)
        )
    if foreground.get("objective_result") != "passed":
        raise TestDraftFinalizerError("foreground objective_result is not passed")
    if foreground.get("objective_media_missing_count") != 0:
        raise TestDraftFinalizerError("foreground objective media missing count is not zero")
    if not str(foreground.get("observed_at") or "").strip():
        raise TestDraftFinalizerError("foreground evidence has no observed_at")
    files = foreground.get("files")
    if not isinstance(files, list) or not files:
        raise TestDraftFinalizerError("foreground evidence has no objective files")
    missing = [str(path) for path in files if not Path(str(path)).is_file()]
    if missing:
        raise TestDraftFinalizerError("foreground evidence files missing: " + ", ".join(missing))
    return {
        "schema": evidence["schema"],
        "status": evidence["status"],
        "draft_name": draft_name,
        "observed_at": foreground["observed_at"],
        "full_playback_completed": True,
        "objective_audit_completed": True,
        "application_closed_normally": True,
        "objective_result": "passed",
        "objective_media_missing_count": 0,
        "files": [str(path) for path in files],
    }


def build_test_draft_finalization_plan(
    *,
    draft_name: str,
    expected_exe: Path,
    logical_root: Path,
    physical_root: Path,
    profiles_root: Path,
    acceptance_evidence: Mapping[str, Any],
    backup_root: Path,
    quarantine_root: Path,
    report_path: Path,
    process_records: Sequence[ProcessRecord | Mapping[str, Any]] | None = None,
    cleanup_request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not draft_name or Path(draft_name).name != draft_name or draft_name in {".", ".."}:
        raise TestDraftFinalizerError("draft_name must be one exact folder name")
    logical_root = Path(os.path.abspath(logical_root))
    physical_root = Path(os.path.abspath(physical_root))
    environment = require_jianying_environment(
        stage="before_launch",
        profile_kind="test",
        expected_exe=expected_exe,
        logical_root=logical_root,
        physical_root=physical_root,
        profiles_root=profiles_root,
        computer_use_proof=None,
        require_computer_use_proof=False,
        target_draft_name=draft_name,
        registration_expectation="unique",
        lock_expectation="none",
        process_records=process_records,
    )
    evidence_summary = (validate_acceptance_evidence(acceptance_evidence, draft_name=draft_name)
                        if cleanup_request is None else None)
    draft_path = logical_root / draft_name
    if not draft_path.is_dir():
        raise FileNotFoundError(draft_path)
    root_meta_path = logical_root / "root_meta_info.json"
    root_meta = read_object(root_meta_path)
    entries = root_meta.get("all_draft_store")
    if not isinstance(entries, list):
        raise TestDraftFinalizerError("root_meta_info all_draft_store is not a list")
    matched = [
        row for row in entries if isinstance(row, dict) and _registered_name(row) == draft_name
    ]
    if len(matched) != 1:
        raise TestDraftFinalizerError("target must have exactly one C-path registration")
    if cleanup_request is not None:
        if (cleanup_request.get("draft_name") != draft_name or
                not cleanup_request.get("authorization_source") or
                not cleanup_request.get("draft_id") or
                cleanup_request["draft_id"] != matched[0].get("draft_id")):
            raise TestDraftFinalizerError("explicit cleanup requires matching draft name, ID and user authorization")
        evidence_summary = {"mode": "user_requested_cleanup", **dict(cleanup_request),
                            "visual_acceptance_claimed": False}
    remaining = [row for row in entries if row is not matched[0]]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    operation_root = Path(quarantine_root) / f"{stamp}_{draft_name}"
    return {
        "schema": "jianying-adapter.test-draft-finalization-plan.v1",
        "profile_kind": "test",
        "draft_name": draft_name,
        "logical_root": logical_root,
        "physical_root": physical_root,
        "draft_path": draft_path,
        "root_meta_path": root_meta_path,
        "root_meta": root_meta,
        "remaining_entries": remaining,
        "matched_entry": matched[0],
        "environment": environment,
        "acceptance_evidence": evidence_summary,
        "backup_root": Path(backup_root),
        "operation_root": operation_root,
        "report_path": Path(report_path),
    }


def finalize_test_draft(
    plan: Mapping[str, Any],
    *,
    _write_root_meta=write_object_atomic,
) -> dict[str, Any]:
    """Back up, quarantine and unregister one test draft with rollback on failure."""

    if plan.get("profile_kind") != "test":
        raise TestDraftFinalizerError("production profiles can never be quarantined")
    draft_name = str(plan["draft_name"])
    logical_root = Path(plan["logical_root"])
    draft_path = Path(plan["draft_path"])
    root_meta_path = Path(plan["root_meta_path"])
    operation_root = Path(plan["operation_root"])
    report_path = Path(plan["report_path"])
    backup_root = Path(plan["backup_root"]) / operation_root.name
    root_meta_backup = backup_root / "root_meta_info.before.json"
    draft_backup = backup_root / "draft"
    quarantined_draft = operation_root / "draft"

    if operation_root.exists() or backup_root.exists():
        raise FileExistsError("finalization output already exists")
    if draft_path.resolve().parent != Path(plan["physical_root"]).resolve():
        raise TestDraftFinalizerError("draft resolves outside the test profile")
    if operation_root.resolve().is_relative_to(Path(plan["physical_root"]).resolve()):
        raise TestDraftFinalizerError("quarantine must be outside the active profile")
    if read_object(root_meta_path) != plan["root_meta"]:
        raise TestDraftFinalizerError("draft index changed after planning; recheck before cleanup")
    backup_root.mkdir(parents=True, exist_ok=False)
    operation_root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(root_meta_path, root_meta_backup)
    shutil.copytree(draft_path, draft_backup)

    moved = False
    root_meta_changed = False
    try:
        shutil.move(str(draft_path), str(quarantined_draft))
        moved = True
        updated = dict(plan["root_meta"])
        updated["all_draft_store"] = list(plan["remaining_entries"])
        _write_root_meta(root_meta_path, updated)
        root_meta_changed = True

        report = {
            "schema": "jianying-adapter.test-draft-finalization.v1",
            "status": "completed",
            "profile_kind": "test",
            "draft_name": draft_name,
            "removed_registration_count": 1,
            "logical_root": str(logical_root),
            "backup": {
                "root_meta": str(root_meta_backup),
                "draft": str(draft_backup),
            },
            "quarantine": str(quarantined_draft),
            "environment_gate": plan["environment"],
            "acceptance_evidence": plan["acceptance_evidence"],
            "removed_registration": plan["matched_entry"],
            "permanent_delete_performed": False,
            "junction_changed": False,
            "recovery": {
                "requires_process_count_zero": True,
                "requires_lock_count_zero": True,
                "restore_root_meta_from": str(root_meta_backup),
                "restore_draft_from": str(draft_backup),
                "restore_draft_to": str(logical_root / draft_name),
            },
        }
        write_object_atomic(report_path, report)
        return report
    except Exception:
        if root_meta_changed or root_meta_backup.is_file():
            shutil.copy2(root_meta_backup, root_meta_path)
        if moved and quarantined_draft.exists() and not draft_path.exists():
            shutil.move(str(quarantined_draft), str(draft_path))
        raise
