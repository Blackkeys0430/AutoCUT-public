import json
from pathlib import Path

import pytest

from test_candidate_stages import _plan, _write
from jianying_adapter import candidate_plan as module
from jianying_adapter.candidate_plan import CandidateAssembler, get_shared_operation_registry, validate_writer_contract


def _preview(tmp_path, monkeypatch):
    path = _plan(tmp_path)
    monkeypatch.setattr(module, "exact_process_identifier", lambda _path: {"ok": True})
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["test_preview"] = {"enabled": True, "draft_name": "TEST_PREVIEW", "authorization_source": "user-approved-test"}
    _write(path, plan)
    state_path = Path(plan["project_state"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["test_preview_authorization"] = {"enabled": True, "source": "user-approved-test"}
    _write(state_path, state)
    CandidateAssembler(path, get_shared_operation_registry(), final_validator=lambda *_: {"ok": True}).preview()
    return path, plan


def test_preview_can_only_enter_explicit_test_profile(tmp_path, monkeypatch):
    path, plan = _preview(tmp_path, monkeypatch)
    report = validate_writer_contract(plan, plan_path=path, preview_test=True, profile_kind="test")
    assert report["ok"], report["errors"]
    assert report["manifest"]["writer_allowed"] is False
    assert json.loads(Path(plan["project_state"]).read_text())["status"] == "planning"
    assert not validate_writer_contract(plan, plan_path=path)["ok"]
    rejected = validate_writer_contract(plan, plan_path=path, preview_test=True, profile_kind="production")
    assert not rejected["ok"]
    assert any("禁止生产登记" in e for e in rejected["errors"])


def test_test_preview_requires_current_user_authorization(tmp_path, monkeypatch):
    path, plan = _preview(tmp_path, monkeypatch)
    state_path = Path(plan["project_state"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["test_preview_authorization"]["enabled"] = False
    _write(state_path, state)
    result = validate_writer_contract(plan, plan_path=path, preview_test=True, profile_kind="test")
    assert not result["ok"]
    assert any("授权" in e for e in result["errors"])


@pytest.mark.parametrize("tamper", ["candidate", "manifest", "plan"])
def test_test_preview_rejects_changed_artifacts(tmp_path, monkeypatch, tamper):
    path, plan = _preview(tmp_path, monkeypatch)
    if tamper == "candidate":
        target = Path(plan["preview_output"])
        data = json.loads(target.read_text(encoding="utf-8"))
        data["duration"] += 1
    elif tamper == "manifest":
        target = Path(plan["preview_manifest"])
        data = json.loads(target.read_text(encoding="utf-8"))
        data["writer_allowed"] = True
    else:
        target, data = path, plan
        data["test_preview"]["draft_name"] = plan["draft_name"]
    _write(target, data)
    assert not validate_writer_contract(plan, plan_path=path, preview_test=True, profile_kind="test")["ok"]


def test_retained_text_must_match_validated_adjudication(tmp_path, monkeypatch):
    path = _plan(tmp_path)
    plan = json.loads(path.read_text(encoding="utf-8"))
    gate_path = Path(plan["semantic_gate"])
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(module, "validate_semantic_evidence", lambda *a, **kw: {
        "ok": True, "errors": [], "adjudication_final_text": "经过裁决的不同原文",
    })
    report = module.validate_semantic_gate(gate, Path(plan["rough_cut"]), gate_path=gate_path)
    assert not report["ok"]
    assert any("实际裁决后的文字不一致" in e for e in report["errors"])
