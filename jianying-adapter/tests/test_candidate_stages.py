from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path

import pytest
from test_render_candidate_audio import cue

from jianying_adapter import candidate_plan as module
from jianying_adapter import shared_operations
from jianying_adapter.candidate_plan import (
    CandidateAssembler,
    SHARED_OPERATION_REGISTRY,
    get_shared_operation_registry,
    validate_writer_contract,
)


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _plan(tmp_path: Path) -> Path:
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"rough")
    rough_hash = hashlib.sha256(rough.read_bytes()).hexdigest()
    evidence_a = tmp_path / "whisper.json"
    evidence_b = tmp_path / "funasr.json"
    for path, source in ((evidence_a, "whisper"), (evidence_b, "funasr")):
        _write(path, {
            "source": source,
            "rough_cut_sha256": rough_hash,
            "segments": [{"start": 0, "end": 1, "text": "完整语音"}],
        })
    state = tmp_path / "project_state.json"
    _write(state, {
        "schema": "huoke_video_project_state_v1",
        "project_id": "STAGE-TEST",
        "status": "planning",
        "confirmed": ["test"],
        "prohibited": [],
        "pending": [],
        "artifacts": {},
    })
    base = tmp_path / "base.json"
    _write(base, {
        "duration": 1_000_000,
            "tracks": [{"id": "caption", "name": "JY_ZH_SUBTITLES", "type": "text", "segments": [{
            "id": "s1", "material_id": "caption-text",
            "target_timerange": {"start": 0, "duration": 1_000_000},
        }]}],
        "materials": {"texts": [{
            "id": "caption-text",
            "content": json.dumps({"text": "完整语音", "styles": [{"size": 14}]}, ensure_ascii=False),
        }]},
    })
    gate = tmp_path / "semantic.json"
    _write(gate, {
        "status": "passed",
        "rough_cut": {"sha256": rough_hash.upper()},
        "evidence_paths": [str(evidence_a), str(evidence_b)],
        "checks": {"no_repeat": True},
        "content_gate": {
            "final_retained_speech": [{"id": "s1", "start": 0, "end": 1, "text": "完整语音"}],
            "ordinary_subtitles": [{"id": "c1", "start": 0, "end": 1, "text": "完整语音"}],
            "preset_replacement_windows": [],
        },
    })
    registry = tmp_path / "usage.json"
    _write(registry, {"records": []})
    exe = tmp_path / "8.8.0" / "JianyingPro.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"synthetic")
    plan = tmp_path / "plan.json"
    _write(plan, {
        "schema": "jianying-adapter.candidate-plan.v1",
        "project_format": {
            "separates_creative_plan_from_writer": True,
            "style_normalization": False,
            "writer_adapter": "jianying-8.8",
            "candidate_builder": "CandidateAssembler",
            "per_video_build_script": False,
            "operation_registry": SHARED_OPERATION_REGISTRY,
        },
        "project_state": str(state), "base_draft": str(base), "rough_cut": str(rough),
        "semantic_gate": str(gate), "usage_registry": str(registry),
        "target": {"width": 1080, "height": 1920, "jianying_version": "8.8.0", "expected_exe": str(exe)},
        "options": {"bgm": {"enabled": False}},
        "subject_clarity_preflight": {"status": "passed", "checks": {
            "primary_subject_defined": True, "unused_headroom_checked": True,
            "background_distraction_checked": True, "perspective_checked": True,
            "caption_subject_clearance_checked": True,
        }},
        "text_style_budget": {"max_display_families": 2, "max_palette_roles": 3, "full_screen_text": False},
        "transition_requires_real_before_after_state": True,
        "presets": [], "brolls": [],
        "operations": [{"id": "captions", "kind": "reflow_ordinary_captions", "safe_width_ratio": 0.82}],
        "draft_name": "STAGE_TEST",
        "preview_output": str(tmp_path / "preview" / "candidate.json"),
        "preview_report": str(tmp_path / "preview" / "report.json"),
        "preview_manifest": str(tmp_path / "preview" / "manifest.json"),
        "output": str(tmp_path / "sealed" / "candidate.json"),
        "report": str(tmp_path / "sealed" / "report.json"),
        "manifest": str(tmp_path / "sealed" / "manifest.json"),
    })
    return plan


def test_preview_is_non_writable_and_seal_binds_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _plan(tmp_path)
    monkeypatch.setattr(module, "exact_process_identifier", lambda _path: {"ok": True})
    assembler = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True})
    preview = assembler.preview()
    manifest = json.loads(Path(preview["manifest"]).read_text(encoding="utf-8"))
    assert manifest["writer_allowed"] is False
    assert manifest["phase"] == "preview"
    assert manifest["blockers"] == ["current_video_evidence_pending_seal"]
    assert manifest["candidate_sha256"] == hashlib.sha256(Path(preview["candidate"]).read_bytes()).hexdigest().upper()

    sealed = assembler.seal()
    sealed_manifest = json.loads(Path(sealed["manifest"]).read_text(encoding="utf-8"))
    assert sealed_manifest["writer_allowed"] is True
    state = json.loads((tmp_path / "project_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "candidate_ready"
    assert validate_writer_contract(json.loads(plan_path.read_text(encoding="utf-8")), plan_path=plan_path)["ok"]


@pytest.mark.parametrize('background_volume, risk', [(1.0, True), (.1, False)])
def test_real_audio_risk_blocks_assembly_and_missing_measurement_blocks_writer(tmp_path, monkeypatch, cue, background_volume, risk):
    ffmpeg, sound = cue
    monkeypatch.setenv('REVIEW_FFMPEG', ffmpeg)
    monkeypatch.setattr(module, 'exact_process_identifier', lambda _path: {'ok': True})
    plan_path = _plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    base = json.loads(Path(plan['base_draft']).read_text(encoding='utf-8'))
    voice = sound['tracks'][0]
    voice['name'] = 'JY_ROUGH_CUT_VIDEO'
    background = copy.deepcopy(voice)
    background['name'] = 'background_fixture'
    background['segments'][0]['id'] = 'background'
    background['segments'][0]['volume'] = background_volume
    base['tracks'].extend([voice, background])
    base['materials']['audios'] = sound['materials']['audios']
    _write(Path(plan['base_draft']), base)
    assembler = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda *_: {'ok': True})
    if risk:
        with pytest.raises(RuntimeError, match='候选最终验证失败'):
            assembler.preview()
        report = json.loads(Path(plan['preview_report']).read_text(encoding='utf-8'))
        assert report['final_validation']['audio_measurement']['voice_balance']['intervals']
        assert not Path(plan['preview_manifest']).exists()
        return
    assembler.preview()
    # Seal must reuse the report already bound to this immutable candidate.
    from jianying_adapter import action_audio
    monkeypatch.setattr(action_audio, 'measure_candidate_audio', lambda *a, **kw: pytest.fail('same candidate measured twice'))
    assembler.seal()
    assert validate_writer_contract(plan, plan_path=plan_path)['ok']
    report_path = Path(plan['report'])
    report = json.loads(report_path.read_text(encoding='utf-8'))
    report['final_validation'].pop('audio_measurement')
    _write(report_path, report)
    rejected = validate_writer_contract(plan, plan_path=plan_path)
    assert not rejected['ok']
    assert any('音量测量' in error for error in rejected['errors'])


def test_seal_allows_only_late_evidence_additions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _plan(tmp_path)
    monkeypatch.setattr(module, "exact_process_identifier", lambda _path: {"ok": True})
    CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True}).preview()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["current_video_ready"] = True
    plan["frontend_proof"] = {"captured": True}
    _write(plan_path, plan)
    sealed = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True}).seal()
    manifest = json.loads(Path(sealed["manifest"]).read_text(encoding="utf-8"))
    assert manifest["writer_allowed"] is True
    assert manifest["plan_sha256"] == hashlib.sha256(plan_path.read_bytes()).hexdigest().upper()


def test_seal_rejects_build_contract_changes_after_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _plan(tmp_path)
    monkeypatch.setattr(module, "exact_process_identifier", lambda _path: {"ok": True})
    CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True}).preview()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["operations"][0]["safe_width_ratio"] = 0.81
    _write(plan_path, plan)
    with pytest.raises(RuntimeError, match="不可变构建合同"):
        CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True}).seal()


def test_seal_failure_preserves_recoverable_outputs_as_non_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _plan(tmp_path)
    monkeypatch.setattr(module, "exact_process_identifier", lambda _path: {"ok": True})
    CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True}).preview()
    monkeypatch.setattr(module, "_advance_state_after_seal", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("state write failed")))
    assembler = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True})
    with pytest.raises(RuntimeError, match="state write failed"):
        assembler.seal()
    manifest = json.loads((tmp_path / "sealed" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["writer_allowed"] is False
    assert manifest["status"] == "seal_failed"
    assert (tmp_path / "sealed" / "candidate.json").exists()
    state = json.loads((tmp_path / "project_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "planning"


def test_run_and_plain_registry_are_rejected(tmp_path: Path) -> None:
    plan_path = _plan(tmp_path)
    with pytest.raises(TypeError, match="只能由 get_shared_operation_registry"):
        module.OperationRegistry(SHARED_OPERATION_REGISTRY, {})
    assembler = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True})
    with pytest.raises(RuntimeError, match=r"preview\(\) -> seal\(\)"):
        assembler.run()


def test_preview_manifest_tampering_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _plan(tmp_path)
    monkeypatch.setattr(module, "exact_process_identifier", lambda _path: {"ok": True})
    assembler = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True})
    preview = assembler.preview()
    manifest_path = Path(preview["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["writer_allowed"] = True
    _write(manifest_path, manifest)
    with pytest.raises(RuntimeError, match="writer_allowed=false"):
        assembler.seal()


def test_shared_operation_false_result_writes_failure_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _plan(tmp_path)
    original_factory = shared_operations.shared_operation_handlers

    def failing_handler(_draft: object, _spec: object, _ctx: object) -> dict[str, bool]:
        return {"ok": False}

    def failing_factory() -> dict[str, object]:
        handlers = original_factory()
        handlers["reflow_ordinary_captions"] = failing_handler
        return handlers

    monkeypatch.setattr(shared_operations, "shared_operation_handlers", failing_factory)
    assembler = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True})
    with pytest.raises(RuntimeError, match="已写入报告"):
        assembler.preview()
    report = json.loads((tmp_path / "preview" / "report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["operations"][-1]["status"] == "failed"
    assert not (tmp_path / "preview" / "candidate.json").exists()


def test_measurement_operations_reject_unsupported_canvas() -> None:
    context = type("Context", (), {"plan": {"target": {"width": 720, "height": 1280}, "presets": []}})()
    with pytest.raises(ValueError, match="只支持 1080x1920"):
        shared_operations.fit_template_text_bounds({"tracks": [], "materials": {}}, {}, context)
    with pytest.raises(ValueError, match="只支持 1080x1920"):
        shared_operations.measure_preset_internal_collisions({"tracks": [], "materials": {}}, {}, context)


def test_replacement_requires_node_scope_before_removing_tracks() -> None:
    draft = {
        "tracks": [
            {"name": "JY_PRESET_OLD__NODE__node_01_01"},
            {"name": "JY_PRESET_OLD__NODE__node_02_01"},
        ],
        "materials": {"texts": []},
    }
    context = type("Context", (), {"plan": {"presets": []}})()
    with pytest.raises(ValueError, match="必须显式声明 node_id"):
        shared_operations.replace_preset_group(
            draft,
            {"old_template_id": "OLD", "new_template_id": "NEW"},
            context,
        )
    assert len(draft["tracks"]) == 2
