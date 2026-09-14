from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from jianying_adapter.project_state import (
    advance_state_after_writer,
    normalize_status,
    validate_current_authorizations,
    validate_project_state,
)


def state(status: str = "planning") -> dict:
    return {
        "schema": "huoke_video_project_state_v1",
        "project_id": "TEST",
        "status": status,
        "confirmed": ["规则A"],
        "prohibited": ["规则B"],
        "pending": [],
        "artifacts": {},
    }


def test_legacy_status_is_normalized_without_changing_source() -> None:
    value = state("v2_written_unopened_pending_user_visual_qa")
    report = validate_project_state(value)
    assert report.ok
    assert report.canonical_status == "written_pending_visual_qa"
    assert report.warnings
    assert value["status"] == "v2_written_unopened_pending_user_visual_qa"


def test_strict_status_rejects_legacy_alias() -> None:
    report = validate_project_state(state("waiting_user_visual_qa"), require_canonical_status=True)
    assert not report.ok
    assert report.canonical_status == "written_pending_visual_qa"


def test_artifact_validation_does_not_treat_draft_id_as_a_file(tmp_path: Path) -> None:
    value = state("written_pending_visual_qa")
    value["artifacts"] = {"draft_id": "NATIVE-DRAFT-ID", "live_draft": str(tmp_path)}
    assert validate_project_state(value, verify_artifacts=True).ok


def test_completed_cannot_keep_pending_work() -> None:
    value = state("completed")
    value["pending"] = ["仍待画面验收"]
    assert not validate_project_state(value).ok


def test_status_normalization_covers_candidate_and_rejection() -> None:
    assert normalize_status("workspace_only_candidate_ready") == "candidate_ready"
    assert normalize_status("visual_failed") == "visual_rejected"


def test_old_confirmed_strings_do_not_authorize_optional_feature() -> None:
    report = validate_current_authorizations(state(), {"bgm": {"enabled": True}})
    assert not report.ok


def test_structured_authorization_and_prohibition_precedence() -> None:
    value = state("candidate_ready")
    value["current_authorizations"] = [
        {"option": "bgm", "enabled": True, "status": "authorized", "source": "user_instruction"}
    ]
    assert validate_current_authorizations(
        value, {"bgm": {"enabled": True, "authorization_source": "user_instruction"}}
    ).ok
    value["prohibited"].append("bgm")
    assert not validate_current_authorizations(
        value, {"bgm": {"enabled": True, "authorization_source": "user_instruction"}}
    ).ok


def test_completed_requires_structure_and_user_visual_evidence(tmp_path: Path) -> None:
    value = state("completed")
    assert not validate_project_state(value).ok
    value["artifacts"]["draft_id"] = "draft-current"
    report = tmp_path / "structure.json"
    report.write_text(__import__("json").dumps({
        "ok": True, "project_id": "TEST", "draft_id": "draft-current"
    }), encoding="utf-8")
    value["completion_evidence"] = {
        "structure_validation": {"ok": True, "report_path": "structure.json"},
        "user_visual_qa": {"accepted": True, "source": "用户确认本草稿画面通过", "project_id": "TEST", "draft_id": "draft-current"},
    }
    assert validate_project_state(value, state_path=tmp_path / "project_state.json").ok
    value["completion_evidence"]["structure_validation"]["report_path"] = "missing.json"
    assert not validate_project_state(value, state_path=tmp_path / "project_state.json").ok


@pytest.mark.parametrize("field,bad", [("project_id", "OTHER"), ("draft_id", "old-draft"), ("ok", False)])
def test_completed_rejects_unbound_or_failed_report(tmp_path: Path, field: str, bad: object) -> None:
    value = state("completed")
    value["artifacts"]["draft_id"] = "current"
    report = {"ok": True, "project_id": "TEST", "draft_id": "current", field: bad}
    path = tmp_path / "structure.json"
    path.write_text(__import__("json").dumps(report), encoding="utf-8")
    value["completion_evidence"] = {
        "structure_validation": {"ok": True, "report_path": str(path)},
        "user_visual_qa": {"accepted": True, "source": "用户验收消息", "project_id": "TEST", "draft_id": "current"},
    }
    assert not validate_project_state(value).ok


def test_completed_rejects_visual_acceptance_for_other_draft(tmp_path: Path) -> None:
    value = state("completed")
    value["artifacts"]["draft_id"] = "current"
    path = tmp_path / "structure.json"
    path.write_text(__import__("json").dumps({"ok": True, "project_id": "TEST", "draft_id": "current"}), encoding="utf-8")
    value["completion_evidence"] = {
        "structure_validation": {"ok": True, "report_path": str(path)},
        "user_visual_qa": {"accepted": True, "source": "用户验收消息", "project_id": "TEST", "draft_id": "old"},
    }
    assert not validate_project_state(value).ok


def test_completed_requires_real_sources_for_both_evidence_records() -> None:
    value = state("completed")
    value["completion_evidence"] = {
        "structure_validation": {"ok": True},
        "user_visual_qa": {"accepted": True},
    }
    report = validate_project_state(value)
    assert not report.ok
    assert any("结构验收" in error for error in report.errors)
    assert any("source" in error for error in report.errors)


def test_state_advance_is_atomic_and_injectable_failure_keeps_old_state(tmp_path: Path) -> None:
    value = state("candidate_ready")
    state_path = tmp_path / "project_state.json"
    state_path.write_text(__import__("json").dumps(value), encoding="utf-8")
    registration = tmp_path / "registration.json"
    registration.write_text(
        __import__("json").dumps(
            {"draft_id": "draft-1", "live_draft": str(tmp_path / "live"), "backup": str(tmp_path / "backup"), "project_id": "TEST", "timeline_equivalence": {"ok": True}, "content_mirrors_deep_equal": True}
        ),
        encoding="utf-8",
    )
    backup = tmp_path / "backup"
    backup.mkdir()

    structure_report = tmp_path / "structure.json"
    structure_report.write_text('{"ok": true}', encoding="utf-8")

    def fail(_path: Path, _value: dict) -> None:
        raise OSError("injected state write failure")

    try:
        advance_state_after_writer(
            state_path,
            registration_path=registration,
            backup_path=backup,
            write_json=fail,
            structure_report_path=structure_report,
        )
    except OSError:
        pass
    else:
        raise AssertionError("injected state update must fail")
    assert __import__("json").loads(state_path.read_text(encoding="utf-8"))["status"] == "candidate_ready"


def test_state_advance_binds_writer_artifacts_and_preserves_visual_pending(tmp_path: Path) -> None:
    value = state("candidate_ready")
    value["pending"] = ["视觉验收记录", "其他待办"]
    state_path = tmp_path / "project_state.json"
    state_path.write_text(__import__("json").dumps(value), encoding="utf-8")
    backup = tmp_path / "backup"
    backup.mkdir()
    registration = tmp_path / "registration.json"
    registration.write_text(
        __import__("json").dumps(
            {
                "draft_id": "draft-1",
                "project_id": "TEST",
                "timeline_equivalence": {"ok": True},
                "content_mirrors_deep_equal": True,
                "live_draft": str(tmp_path / "live"),
                "backup": str(backup),
            }
        ),
        encoding="utf-8",
    )
    structure_report = tmp_path / "structure.json"
    structure_report.write_text('{"ok": true}', encoding="utf-8")
    result = advance_state_after_writer(
        state_path,
        registration_path=registration,
        backup_path=backup,
        candidate_plan_path=tmp_path / "plan.json",
        structure_report_path=structure_report,
    )
    after = result["after"]
    assert after["status"] == "written_pending_visual_qa"
    assert after["pending"] == ["视觉验收记录", "其他待办", "用户主观画面验收"]
    assert after["artifacts"]["live_draft"] == str(tmp_path / "live")
    assert after["artifacts"]["draft_id"] == "draft-1"
    assert after["artifacts"]["registration"] == str(registration)
    assert after["artifacts"]["writer_backup"] == str(backup)


def test_state_advance_rejects_empty_registration_binding(tmp_path: Path) -> None:
    state_path = tmp_path / "project_state.json"
    state_path.write_text(__import__("json").dumps(state("candidate_ready")), encoding="utf-8")
    registration = tmp_path / "registration.json"
    registration.write_text("{}", encoding="utf-8")
    backup = tmp_path / "backup"
    backup.mkdir()
    with pytest.raises(ValueError, match="draft_id"):
        advance_state_after_writer(
            state_path,
            registration_path=registration,
            backup_path=backup,
        )


def test_state_advance_rejects_registration_backup_mismatch(tmp_path: Path) -> None:
    state_path = tmp_path / "project_state.json"
    state_path.write_text(__import__("json").dumps(state("candidate_ready")), encoding="utf-8")
    registration = tmp_path / "registration.json"
    registration.write_text(
        __import__("json").dumps(
            {"draft_id": "draft-1", "live_draft": str(tmp_path / "live"), "backup": str(tmp_path / "wrong")}
        ),
        encoding="utf-8",
    )
    backup = tmp_path / "backup"
    backup.mkdir()
    with pytest.raises(ValueError, match="backup"):
        advance_state_after_writer(
            state_path,
            registration_path=registration,
            backup_path=backup,
        )


def test_authorization_source_must_match_plan_source() -> None:
    value = state("candidate_ready")
    value["current_authorizations"] = [
        {"option": "bgm", "enabled": True, "status": "authorized", "source": "user_instruction"}
    ]
    report = validate_current_authorizations(
        value, {"bgm": {"enabled": True, "authorization_source": "another_source"}}
    )
    assert not report.ok
    assert any("authorization_source" in error for error in report.errors)


def test_authorization_requires_explicit_current_status() -> None:
    value = state()
    value["current_authorizations"] = [{"option": "bgm", "enabled": True, "source": "user_instruction"}]
    assert not validate_current_authorizations(
        value, {"bgm": {"enabled": True, "authorization_source": "user_instruction"}}
    ).ok


def test_writer_failure_handler_uses_attempt_plan_and_restores_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-write failure must complete rollback without a scope NameError."""
    import sys

    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import write_candidate_plan as writer

    logical = tmp_path / "logical"
    logical.mkdir()
    draft = logical / "SYNTH-DRAFT"
    draft.mkdir()
    (logical / "root_meta_info.json").write_text("after", encoding="utf-8")
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "root_meta_info.json").write_text("before", encoding="utf-8")

    state_path = tmp_path / "project_state.json"
    before = state("candidate_ready")
    state_path.write_text(__import__("json").dumps(before), encoding="utf-8")
    state_snapshot = backup_dir / "project_state.before.json"
    state_snapshot.write_text(__import__("json").dumps(before), encoding="utf-8")
    state_path.write_text(
        __import__("json").dumps({**before, "status": "written_pending_visual_qa"}),
        encoding="utf-8",
    )

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        __import__("json").dumps(
            {"writer_allowed": True, "blockers": [], "registered_live_draft": True}
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "candidate_plan.json"
    plan.write_text(__import__("json").dumps({"manifest": str(manifest)}), encoding="utf-8")
    registration = tmp_path / "registration.json"
    attempt = backup_dir / "writer_attempt.json"
    attempt.write_text(
        __import__("json").dumps(
            {
                "schema": "jianying-adapter.writer-attempt.v1",
                "status": "create_started_after_absence_gate",
                "draft_name": draft.name,
                "logical_root": str(logical),
                "candidate_plan": str(plan),
                "project_state": str(state_path),
                "project_state_before": str(state_snapshot),
            }
        ),
        encoding="utf-8",
    )

    args = Namespace(backup_dir=backup_dir, registration=registration)
    monkeypatch.setattr(writer, "parse_args", lambda: args)
    monkeypatch.setattr(
        writer,
        "_run",
        lambda _args: (_ for _ in ()).throw(RuntimeError("synthetic post-write check")),
    )

    with pytest.raises(RuntimeError, match="Writer 失败") as raised:
        writer.main()
    assert "NameError" not in str(raised.value)
    assert __import__("json").loads(state_path.read_text(encoding="utf-8")) == before
    failure = __import__("json").loads(registration.read_text(encoding="utf-8"))
    assert failure["status"] == "writer_failed_rolled_back"
    updated_manifest = __import__("json").loads(manifest.read_text(encoding="utf-8"))
    assert updated_manifest["writer_allowed"] is False
    assert updated_manifest["registered_live_draft"] is False
    assert (logical / "root_meta_info.json").read_text(encoding="utf-8") == "before"
    assert not draft.exists()
    assert Path(failure["rollback"]["failed_draft_quarantine"]).is_dir()
