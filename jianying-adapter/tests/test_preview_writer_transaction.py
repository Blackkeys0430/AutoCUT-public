"""Exercise the real Writer transaction with only external Jianying IO stubbed."""
import importlib.util
import json
import shutil
from types import SimpleNamespace
from argparse import Namespace
from pathlib import Path

import pytest


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_editor_stopped_uses_shared_discovery_and_fails_closed(tmp_path, monkeypatch):
    from jianying_adapter.jianying_environment import ProcessRecord
    writer, _ = _gate_args(tmp_path)
    monkeypatch.setattr(writer, "collect_jianying_processes", lambda: [])
    writer.ensure_editor_stopped()
    monkeypatch.setattr(writer, "collect_jianying_processes", lambda: [
        ProcessRecord(88, "JianyingPro.exe", None, "AccessDenied")])
    with pytest.raises(RuntimeError, match="剪映仍在运行"):
        writer.ensure_editor_stopped()

    def failed():
        raise OSError("snapshot denied")

    monkeypatch.setattr(writer, "collect_jianying_processes", failed)
    with pytest.raises(OSError, match="snapshot denied"):
        writer.ensure_editor_stopped()


def _transaction(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "write_candidate_plan.py"
    spec = importlib.util.spec_from_file_location("preview_transaction_writer", script)
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    root = tmp_path / "test-root"
    root.mkdir()
    root_meta = root / "root_meta_info.json"
    _write(root_meta, {"all_draft_store": []})
    before_meta = root_meta.read_bytes()
    state_path = tmp_path / "project_state.json"
    _write(state_path, {"project_id": "current-video", "status": "planning", "pending": ["native visual QA"]})
    before_state = state_path.read_bytes()
    candidate = tmp_path / "preview" / "draft_content.json"
    content = {"id": "test-id", "duration": 1000000, "tracks": [],
               "platform": {"app_version": "8.8.0"}, "last_modified_platform": {"app_version": "8.8.0"}}
    _write(candidate, content)
    report, manifest = candidate.with_name("report.json"), candidate.with_name("manifest.json")
    _write(report, {"ok": True})
    _write(manifest, {"status": "preview_only", "writer_allowed": False, "registered_live_draft": False})
    before_manifest = manifest.read_bytes()
    install = tmp_path / "install"
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"test media")
    plan_path = tmp_path / "candidate_plan.json"
    _write(plan_path, {"schema": "jianying-adapter.candidate-plan.v1", "project_state": str(state_path),
                      "target": {"expected_exe": str(install / "JianyingPro.exe")}, "rough_cut": str(rough)})
    args = Namespace(plan=plan_path, draft_root=root, physical_root=root, install_dir=install,
                     user_data=tmp_path, profiles_root=tmp_path, profile_kind="test", preview_test=True,
                     environment_report=tmp_path / "environment.json", backup_dir=tmp_path / "backup",
                     compatibility_reference_path=candidate, registration=tmp_path / "registration.json")
    _write(args.environment_report, {"pre_write": {"ok": True}})
    monkeypatch.setattr(writer, "parse_args", lambda: args)
    monkeypatch.setattr(writer, "ensure_writer_gate", lambda **kw: (candidate, report, manifest, "TEST_ONLY"))
    monkeypatch.setattr(writer, "audit_double_8_8_reference", lambda *a, **kw: {"path": str(candidate)})
    monkeypatch.setattr(writer.materializer, "_codec", lambda *a: None)
    monkeypatch.setattr(writer.materializer, "load_json_object_with_codec", lambda path, **kw: (json.loads(path.read_text(encoding="utf-8")), None))
    monkeypatch.setattr(writer, "inspect_jianying_environment", lambda **kw: {"ok": True})
    monkeypatch.setattr(writer, "validate_timeline_equivalence", lambda *a, **kw: {"ok": True, "errors": []})
    monkeypatch.setattr(writer, "validate_actual_content", lambda *a, **kw: {"ok": True, "errors": []})
    monkeypatch.setattr(writer, "advance_state_after_writer", lambda *a, **kw: pytest.fail("preview must not advance production state"))

    def create_base(operation):
        assert operation.draft_root == args.draft_root
        assert operation.physical_root == args.physical_root
        shutil.copy2(root_meta, args.backup_dir / "root_meta_info.json")
        (root / "TEST_ONLY").mkdir()
        _write(root_meta, {"all_draft_store": [{"draft_name": "TEST_ONLY"}]})
        return {"ok": True}

    def apply_candidate(operation):
        shutil.copy2(candidate, root / "TEST_ONLY" / "draft_content.json")
        return {"backup": str(args.backup_dir), "content_mirrors_deep_equal": True}

    monkeypatch.setattr(writer.materializer, "create_base", create_base)
    monkeypatch.setattr(writer.materializer, "apply_candidate", apply_candidate)
    return writer, args, state_path, manifest, before_state, before_manifest, before_meta


def test_preview_writer_success_preserves_manifest_and_planning(tmp_path, monkeypatch):
    writer, args, state_path, manifest, _, before_manifest, _ = _transaction(tmp_path, monkeypatch)
    previous = json.loads(state_path.read_text(encoding="utf-8"))
    previous["artifacts"] = {"draft_id": "previous-draft", "registration": "old-registration.json"}
    previous["internal_review"] = {"delivery": {"draft_id": "previous-draft", "subjective_visual_qa": "accepted"}}
    _write(state_path, previous)
    assert writer.main() == 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    registration = json.loads(args.registration.read_text(encoding="utf-8"))
    assert state["status"] == "planning"
    assert state["validation"]["test_preview"]["profile_kind"] == "test"
    assert state["artifacts"]["draft_id"] == registration["draft_id"]
    assert state["artifacts"]["registration"] == str(args.registration)
    assert state["artifacts"]["native_test"] == registration["physical_draft"]
    assert state["internal_review"]["delivery"]["draft_id"] == registration["draft_id"]
    assert state["internal_review"]["delivery"]["subjective_visual_qa"] == "pending_user"
    assert "用户播放验收当前TEST" in state["pending"]
    assert registration["status"] == "test_preview_written_pending_native_qa"
    assert registration["production_writer_allowed"] is False
    assert manifest.read_bytes() == before_manifest
    assert (args.draft_root / "TEST_ONLY" / "draft_content.json").is_file()
    attempt = json.loads((args.backup_dir / "writer_attempt.json").read_text(encoding="utf-8"))
    assert attempt["physical_root"] == str(args.physical_root)


def _gate_args(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "write_candidate_plan.py"
    spec = importlib.util.spec_from_file_location("writer_gate_no_config", script)
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    state = tmp_path / "state.json"
    _write(state, {"project_id": "current", "status": "planning"})
    plan_path = tmp_path / "plan.json"
    _write(plan_path, {"project_state": str(state), "draft_name": "NEW"})
    return writer, plan_path


def test_writer_gate_does_not_require_missing_config_when_editor_stopped(tmp_path, monkeypatch):
    writer, plan_path = _gate_args(tmp_path)
    monkeypatch.setattr(writer, "validate_writer_contract", lambda *a, **kw: {
        "ok": True, "paths": {"candidate": str(tmp_path / "candidate"), "report": str(tmp_path / "report"), "manifest": str(tmp_path / "manifest")}
    })
    monkeypatch.setattr(writer, "validate_current_authorizations", lambda *a, **kw: SimpleNamespace(ok=True, errors=[]))
    monkeypatch.setattr(writer, "inspect_jianying_environment", lambda **kw: {"ok": True})
    monkeypatch.setattr(writer, "ensure_editor_stopped", lambda: None)
    assert not hasattr(writer, "ensure_real_quit_enabled")
    result = writer.ensure_writer_gate(
        user_data=tmp_path / "missing-config-user-data", logical_root=tmp_path / "logical",
        physical_root=tmp_path / "physical", profiles_root=tmp_path, profile_kind="test",
        expected_exe=tmp_path / "JianyingPro.exe", environment_report_path=tmp_path / "env.json",
        plan={"project_state": str(tmp_path / "state.json"), "draft_name": "NEW"}, plan_path=plan_path,
        preview_test=False,
    )
    assert result[3] == "NEW"


def test_writer_gate_still_rejects_running_editor(tmp_path, monkeypatch):
    writer, plan_path = _gate_args(tmp_path)
    monkeypatch.setattr(writer, "validate_writer_contract", lambda *a, **kw: {
        "ok": True, "paths": {"candidate": str(tmp_path / "candidate"), "report": str(tmp_path / "report"), "manifest": str(tmp_path / "manifest")}
    })
    monkeypatch.setattr(writer, "validate_current_authorizations", lambda *a, **kw: SimpleNamespace(ok=True, errors=[]))
    monkeypatch.setattr(writer, "inspect_jianying_environment", lambda **kw: {"ok": True})
    monkeypatch.setattr(writer, "collect_jianying_processes", lambda: [SimpleNamespace(name="JianyingPro.exe")])
    with pytest.raises(RuntimeError, match="剪映仍在运行"):
        writer.ensure_writer_gate(
            user_data=tmp_path, logical_root=tmp_path / "logical", physical_root=tmp_path / "physical",
            profiles_root=tmp_path, profile_kind="test", expected_exe=tmp_path / "JianyingPro.exe",
            environment_report_path=tmp_path / "env.json", plan={"project_state": str(tmp_path / "state.json"), "draft_name": "NEW"},
            plan_path=plan_path, preview_test=False,
        )


@pytest.mark.parametrize("failure_stage", ["timeline", "captions", "layout", "state_commit"])
def test_preview_writer_failure_restores_index_state_and_preserves_preview(tmp_path, monkeypatch, failure_stage):
    writer, args, state_path, manifest, before_state, before_manifest, before_meta = _transaction(tmp_path, monkeypatch)
    if failure_stage == "timeline":
        monkeypatch.setattr(writer, "validate_timeline_equivalence", lambda *a, **kw: {"ok": False, "errors": ["injected mismatch"]})
    elif failure_stage == 'captions':
        monkeypatch.setattr(writer, 'validate_actual_content', lambda *a, **kw: {'ok': False, 'errors': ['injected missing qualifier']})
    elif failure_stage == "layout":
        import jianying_adapter.final_layout as layout
        monkeypatch.setattr(layout, "validate_final_layout", lambda *a, **kw: {"ok": False, "errors": ["injected platform collision"]})
    else:
        original_write = writer.write_object

        def fail_state_commit(path, value):
            if path == state_path and value.get("validation", {}).get("test_preview"):
                original_write(path, value)
                raise OSError("injected failure after state mutation")
            original_write(path, value)

        monkeypatch.setattr(writer, "write_object", fail_state_commit)
    with pytest.raises(RuntimeError, match="可恢复回滚"):
        writer.main()
    assert state_path.read_bytes() == before_state
    assert manifest.read_bytes() == before_manifest
    assert (args.draft_root / "root_meta_info.json").read_bytes() == before_meta
    assert not (args.draft_root / "TEST_ONLY").exists()
    failure = json.loads(args.registration.read_text(encoding="utf-8"))
    assert failure["status"] == "writer_failed_rolled_back"
    assert Path(failure["rollback"]["failed_draft_quarantine"]).is_dir()


@pytest.mark.parametrize("lose_qualifier", [False, True])
def test_writer_readback_validates_real_distributed_captions(tmp_path, monkeypatch, lose_qualifier):
    from test_caption_replacement import distributed_fixture
    from jianying_adapter.candidate_plan import validate_actual_content
    from jianying_adapter.caption_replacement import replace_ordinary_captions_with_presets

    writer, args, state_path, manifest, before_state, before_manifest, before_meta = _transaction(tmp_path, monkeypatch)
    draft, ctx, replacement = distributed_fixture(qualifier=True)
    replace_ordinary_captions_with_presets(draft, replacement, ctx)
    draft.update(id="test-id", duration=3_000_000,
                 platform={"app_version": "8.8.0"}, last_modified_platform={"app_version": "8.8.0"})
    candidate = args.plan.parent / "preview" / "draft_content.json"
    _write(candidate, draft)
    before_candidate = candidate.read_bytes()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    target = {**plan["target"], **ctx.plan["target"]}
    plan.update(ctx.plan)
    plan["target"] = target
    _write(args.plan, plan)
    monkeypatch.setattr(writer, "validate_actual_content", validate_actual_content)
    assert validate_actual_content(draft, plan, plan_path=args.plan)["ok"]

    if lose_qualifier:
        original_apply = writer.materializer.apply_candidate

        def corrupt_written_caption(operation):
            result = original_apply(operation)
            written = args.draft_root / "TEST_ONLY" / "draft_content.json"
            content = json.loads(written.read_text(encoding="utf-8"))
            material = content["materials"]["texts"][-1]
            payload = json.loads(material["content"])
            payload["text"] = "哪一条路更适合你"
            material["content"] = json.dumps(payload, ensure_ascii=False)
            _write(written, content)
            return result

        monkeypatch.setattr(writer.materializer, "apply_candidate", corrupt_written_caption)
        with pytest.raises(RuntimeError, match="可恢复回滚"):
            writer.main()
        registration = json.loads(args.registration.read_text(encoding="utf-8"))
        assert "写入后字幕分工/语义覆盖复核失败" in registration["failure"]
        assert registration["status"] == "writer_failed_rolled_back"
        assert state_path.read_bytes() == before_state
        assert (args.draft_root / "root_meta_info.json").read_bytes() == before_meta
        assert not (args.draft_root / "TEST_ONLY").exists()
    else:
        assert writer.main() == 0
        registration = json.loads(args.registration.read_text(encoding="utf-8"))
        assert registration["actual_caption_coverage"]["ok"]
        assert registration["status"] == "test_preview_written_pending_native_qa"
    assert candidate.read_bytes() == before_candidate
    assert manifest.read_bytes() == before_manifest
