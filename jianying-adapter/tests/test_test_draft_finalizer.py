from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_adapter.jianying_environment import JianyingEnvironmentError
from jianying_adapter.test_draft_finalizer import (
    TestDraftFinalizerError,
    build_test_draft_finalization_plan,
    finalize_test_draft,
)


def proof() -> dict:
    return {
        "node_repl_bridge_doctor": {
            "ok": True,
            "mode": "project-drift",
            "issues": [],
        },
        "runtime_observations": {
            "cua_get_state_called": True,
            "sky_window_enumeration_called": True,
            "observed_at": "2026-09-04T21:00:00+08:00",
            "full_playback_completed": True,
            "objective_audit_completed": True,
            "application_closed_normally": True,
            "objective_result": "passed",
            "objective_media_missing_count": 0,
            "agent_attested": True,
        },
    }


def fixture_paths(tmp_path: Path, *, profile_name: str = "8.8-tests") -> dict:
    root = tmp_path / "profiles" / profile_name / "com.lveditor.draft"
    root.mkdir(parents=True)
    draft_name = "accepted-probe"
    draft = root / draft_name
    draft.mkdir()
    (draft / "draft_content.json").write_text("{}", encoding="utf-8")
    root_meta = {
        "all_draft_store": [
            {"draft_id": "DRAFT-1", "draft_fold_path": str(root / draft_name)}
        ]
    }
    (root / "root_meta_info.json").write_text(json.dumps(root_meta), encoding="utf-8")
    exe = tmp_path / "JianyingPro-8.8" / "JianyingPro.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"8.8")
    image = tmp_path / "foreground.png"
    image.write_bytes(b"png")
    evidence = {
        "schema": "jianying-adapter.test-draft-acceptance.v1",
        "status": "completed",
        "draft_name": draft_name,
        "foreground_evidence": {
            "status": "completed",
            "observed_draft_name": draft_name,
            "observed_at": "2026-09-04T21:00:00+08:00",
            "full_playback_completed": True,
            "objective_audit_completed": True,
            "application_closed_normally": True,
            "objective_result": "passed",
            "objective_media_missing_count": 0,
            "files": [str(image)],
        },
    }
    return {
        "root": root,
        "draft": draft,
        "draft_name": draft_name,
        "exe": exe,
        "evidence": evidence,
        "backup": tmp_path / "backups",
        "quarantine": tmp_path / "workspace-quarantine",
        "report": tmp_path / "report.json",
    }


def build(paths: dict) -> dict:
    return build_test_draft_finalization_plan(
        draft_name=paths["draft_name"],
        expected_exe=paths["exe"],
        logical_root=paths["root"],
        physical_root=paths["root"],
        profiles_root=paths["root"].parents[1],
        acceptance_evidence=paths["evidence"],
        backup_root=paths["backup"],
        quarantine_root=paths["quarantine"],
        report_path=paths["report"],
        process_records=[],
    )


def test_production_profile_is_never_quarantined(tmp_path: Path) -> None:
    paths = fixture_paths(tmp_path, profile_name="8.8-production")
    with pytest.raises(JianyingEnvironmentError):
        build(paths)


def test_incomplete_frontend_evidence_is_rejected(tmp_path: Path) -> None:
    paths = fixture_paths(tmp_path)
    paths["evidence"]["status"] = "pending"
    with pytest.raises(TestDraftFinalizerError, match="not completed"):
        build(paths)


def test_media_missing_frontend_evidence_is_rejected(tmp_path: Path) -> None:
    paths = fixture_paths(tmp_path)
    paths["evidence"]["foreground_evidence"]["objective_media_missing_count"] = 1
    with pytest.raises(TestDraftFinalizerError, match="not zero"):
        build(paths)


def test_successfully_backs_up_quarantines_and_atomically_unregisters(tmp_path: Path) -> None:
    paths = fixture_paths(tmp_path)
    report = finalize_test_draft(build(paths))
    assert report["status"] == "completed"
    assert report["profile_kind"] == "test"
    assert report["permanent_delete_performed"] is False
    assert report["junction_changed"] is False
    assert not paths["draft"].exists()
    assert (Path(report["quarantine"]) / "draft_content.json").is_file()
    assert (Path(report["backup"]["draft"]) / "draft_content.json").is_file()
    assert Path(report["backup"]["root_meta"]).is_file()
    root = json.loads((paths["root"] / "root_meta_info.json").read_text(encoding="utf-8"))
    assert root["all_draft_store"] == []
    assert json.loads(paths["report"].read_text(encoding="utf-8"))["status"] == "completed"


def test_root_meta_failure_rolls_moved_draft_back(tmp_path: Path) -> None:
    paths = fixture_paths(tmp_path)
    plan = build(paths)
    before = (paths["root"] / "root_meta_info.json").read_bytes()

    def fail_write(_path, _value):
        raise OSError("injected root_meta failure")

    with pytest.raises(OSError, match="injected"):
        finalize_test_draft(plan, _write_root_meta=fail_write)
    assert paths["draft"].is_dir()
    assert (paths["draft"] / "draft_content.json").is_file()
    assert (paths["root"] / "root_meta_info.json").read_bytes() == before
    assert not paths["report"].exists()


def test_explicit_cleanup_does_not_claim_visual_acceptance(tmp_path: Path) -> None:
    paths = fixture_paths(tmp_path)
    args = dict(draft_name=paths['draft_name'], expected_exe=paths['exe'],
                logical_root=paths['root'], physical_root=paths['root'],
                profiles_root=paths['root'].parents[1], acceptance_evidence={},
                backup_root=paths['backup'], quarantine_root=paths['quarantine'],
                report_path=paths['report'], process_records=[])
    request = {'draft_name': paths['draft_name'], 'draft_id': 'WRONG', 'authorization_source': 'user requests old tests removed'}
    with pytest.raises(TestDraftFinalizerError, match='matching'):
        build_test_draft_finalization_plan(**args, cleanup_request=request)
    request['draft_id'] = 'DRAFT-1'
    plan = build_test_draft_finalization_plan(**args, cleanup_request=request)
    report = finalize_test_draft(plan)
    assert report['acceptance_evidence']['visual_acceptance_claimed'] is False
    assert report['removed_registration']['draft_id'] == 'DRAFT-1'
    assert report['permanent_delete_performed'] is False


def test_stale_cleanup_plan_does_not_remove_new_registration(tmp_path: Path) -> None:
    paths = fixture_paths(tmp_path)
    plan = build(paths)
    meta_path = paths['root'] / 'root_meta_info.json'
    meta = json.loads(meta_path.read_text())
    meta['all_draft_store'].append({'draft_id': 'new'})
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(TestDraftFinalizerError, match='index changed'):
        finalize_test_draft(plan)
    assert paths['draft'].is_dir()
    assert json.loads(meta_path.read_text()) == meta
