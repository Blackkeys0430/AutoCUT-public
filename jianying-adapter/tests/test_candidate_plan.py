from __future__ import annotations

import hashlib
import json
import pytest
from pathlib import Path

from jianying_adapter.candidate_plan import (
    CandidateAssembler,
    OperationRegistry,
    SHARED_OPERATION_REGISTRY,
    get_shared_operation_registry,
    validate_candidate_plan,
    validate_frontend_proof,
    validate_timeline_equivalence,
    validate_writer_contract,
)


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_project_visual_planning_requirement_cannot_be_removed_from_plan(tmp_path):
    path = make_plan(tmp_path)
    plan = json.loads(path.read_text("utf-8"))
    state_path = Path(plan["project_state"])
    state = json.loads(state_path.read_text("utf-8"))
    state["visual_planning_required"] = True
    write(state_path, state)
    result = validate_candidate_plan(plan, plan_path=path)
    assert any("不得省略 visual_planning_version" in e for e in result.errors)


@pytest.mark.parametrize("version", [None, 1, 2])
def test_state_v2_minimum_cannot_be_downgraded_at_real_plan_entry(tmp_path, version):
    from test_visual_planning import v2_plan
    path = make_plan(tmp_path)
    plan = json.loads(path.read_text("utf-8"))
    state_path = Path(plan["project_state"])
    state = json.loads(state_path.read_text("utf-8"))
    state.update(visual_planning_required=True, visual_planning_min_version=2)
    write(state_path, state)
    design = v2_plan()
    for key in ("visual_events", "design_decisions"):
        plan[key] = design[key]
    plan['operations'].extend(design['operations'])
    plan["visual_events"][0].update(end_us=1000000, speech_ids=["s1"])
    if version is not None:
        plan["project_format"]["visual_planning_version"] = version
    result = validate_candidate_plan(plan, plan_path=path)
    assert any("不得降级" in error for error in result.errors) is (version != 2)
    if version == 2:
        # No copied plan.content_gate: the actual entry forwards the existing
        # semantic file's retained speech to the pure design validator.
        assert result.visual_rules["visual_events"]["ok"], result.visual_rules["visual_events"]


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_boolean_visual_requirement_accepts_both_supported_versions(tmp_path, version):
    path = make_plan(tmp_path)
    plan = json.loads(path.read_text("utf-8"))
    state_path = Path(plan["project_state"])
    state = json.loads(state_path.read_text("utf-8"))
    state["visual_planning_required"] = True
    write(state_path, state)
    plan["project_format"]["visual_planning_version"] = version
    result = validate_candidate_plan(plan, plan_path=path)
    assert not any("不得省略 visual_planning_version" in error for error in result.errors)


@pytest.mark.parametrize("defect", [None, "array_order", "segment_index", "track_index"])
def test_declared_render_order_checks_array_and_indexes(defect):
    def visual(name, kind, index):
        return {"name": name, "type": kind, "segments": [
            {"render_index": index, "track_render_index": 0}
        ]}
    tracks = [visual("base", "video", 0), {"type": "audio", "segments": []},
              visual("broll", "video", 2), visual("caption", "text", 3)]
    if defect == "array_order":
        tracks[2], tracks[3] = tracks[3], tracks[2]
    elif defect == "segment_index":
        tracks[2]["segments"][0]["render_index"] = 1
    elif defect == "track_index":
        tracks[2]["segments"][0]["track_render_index"] = 1
    result = validate_timeline_equivalence(
        {"declared_render_order": ["base", "broll", "caption"]}, {"tracks": tracks}
    )
    assert result["ok"] is (defect is None)
    assert result["render_order"]["ok"] is (defect is None)


def make_plan(tmp_path: Path) -> Path:
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"rough cut")
    digest = hashlib.sha256(rough.read_bytes()).hexdigest().upper()
    state = tmp_path / "project_state.json"
    write(state, {
        "schema": "huoke_video_project_state_v1",
        "project_id": "TEST",
        "status": "candidate_ready",
        "confirmed": ["测试"],
        "prohibited": [],
        "pending": [],
        "artifacts": {},
    })
    base = tmp_path / "base.json"
    write(base, {"duration": 1000000, "tracks": [{"name": "JY_ZH_SUBTITLES", "type": "text", "segments": [{"material_id": "text1", "target_timerange": {"start": 0, "duration": 1000000}}]}], "materials": {"texts": [{"id": "text1", "content": json.dumps({"text": "完整语音", "styles": [{"size": 14}]}, ensure_ascii=False)}]}})
    evidence_paths = []
    for engine in ("whisper", "funasr"):
        evidence_path = tmp_path / (engine + ".json")
        write(evidence_path, {"source": engine, "rough_cut_sha256": digest, "segments": [{"start": 0, "end": 1, "text": "完整语音"}]})
        evidence_paths.append(str(evidence_path))
    gate = tmp_path / "semantic.json"
    write(gate, {
        "status": "passed",
        "rough_cut": {"sha256": digest},
        "evidence_paths": evidence_paths,
        "content_gate": {
            "final_retained_speech": [{"id": "s1", "start": 0, "end": 1, "text": "完整语音"}],
            "ordinary_subtitles": [{"id": "c1", "start": 0, "end": 1, "text": "完整语音"}],
            "preset_replacement_windows": [],
        },
        "checks": {"no_repeat": True},
    })
    registry = tmp_path / "registry.json"
    write(registry, {"records": []})
    expected_exe = tmp_path / "8.8.0.13328" / "JianyingPro.exe"
    expected_exe.parent.mkdir(parents=True)
    expected_exe.write_bytes(b"exe")
    plan = tmp_path / "plan.json"
    write(plan, {
        "schema": "jianying-adapter.candidate-plan.v1",
        "project_format": {
            "name": "获客口播自动剪辑管线统一工程格式",
            "separates_creative_plan_from_writer": True,
            "style_normalization": False,
            "writer_adapter": "jianying-8.8",
            "candidate_builder": "CandidateAssembler",
            "per_video_build_script": False,
            "operation_registry": SHARED_OPERATION_REGISTRY,
        },
        "project_state": str(state),
        "base_draft": str(base),
        "rough_cut": str(rough),
        "semantic_gate": str(gate),
        "usage_registry": str(registry),
        "target": {
            "width": 1080,
            "height": 1920,
            "jianying_version": "8.8.0",
            "expected_exe": str(expected_exe),
        },
        "options": {"bgm": {"enabled": False}},
        "subject_clarity_preflight": {
            "status": "passed",
            "checks": {
                "primary_subject_defined": True,
                "unused_headroom_checked": True,
                "background_distraction_checked": True,
                "perspective_checked": True,
                "caption_subject_clearance_checked": True,
            },
        },
        "text_style_budget": {
            "max_display_families": 2,
            "max_palette_roles": 3,
            "full_screen_text": False,
        },
        "transition_requires_real_before_after_state": True,
        "presets": [],
        "operations": [{"id": "captions", "kind": "reflow_ordinary_captions"}],
        "draft_name": "TEST_CANDIDATE",
        "preview_output": str(tmp_path / "preview_candidate.json"),
        "preview_report": str(tmp_path / "preview_report.json"),
        "preview_manifest": str(tmp_path / "preview_manifest.json"),
        "output": str(tmp_path / "candidate.json"),
        "report": str(tmp_path / "report.json"),
        "manifest": str(tmp_path / "manifest.json"),
    })
    return plan


def test_candidate_plan_binds_semantic_gate_to_current_roughcut(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert validate_candidate_plan(plan, plan_path=plan_path).ok
    Path(plan["rough_cut"]).write_bytes(b"changed")
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert not report.ok
    assert any("SHA256" in error for error in report.errors)


def test_candidate_plan_rejects_outputs_overwriting_each_other(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["report"] = plan["output"]
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert not report.ok
    assert any("六个输出路径必须互不相同" in error for error in report.errors)


def test_candidate_plan_rejects_executable_that_is_not_bound_to_8_8(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    wrong = tmp_path / "11.4.0.14403" / "JianyingPro.exe"
    wrong.parent.mkdir()
    wrong.write_bytes(b"not-a-real-pe")
    plan["target"]["expected_exe"] = str(wrong)
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert not report.ok
    assert any("版本门禁失败" in error for error in report.errors)


def test_candidate_plan_with_broll_does_not_require_frontend_proof(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    broll = tmp_path / "broll.mp4"
    broll.write_bytes(b"video")
    plan["brolls"] = [{
        "node_id": "broll_01",
        "track_name": "JY_BROLL_broll_01",
        "start_us": 100_000,
        "end_us": 900_000,
        "source_path": str(broll),
        "segment_count": 1,
        "clip": {"transform": {"x": 0.0, "y": 0.0}, "scale": 1.0},
    }]
    plan["operations"] = [
        {"id": "captions", "kind": "reflow_ordinary_captions"},
        {"id": "broll", "kind": "add_broll", "node_id": "broll_01"},
    ]
    plan.pop("frontend_proof", None)
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert report.ok is True
    assert report.frontend_proof == {}


def test_candidate_plan_bad_frontend_proof_is_diagnostic_warning(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    broll = tmp_path / "broll.mp4"
    broll.write_bytes(b"video")
    plan["brolls"] = [{
        "node_id": "broll_01",
        "track_name": "JY_BROLL_broll_01",
        "start_us": 100_000,
        "end_us": 900_000,
        "source_path": str(broll),
        "segment_count": 1,
        "clip": {"transform": {"x": 0.0, "y": 0.0}, "scale": 1.0},
    }]
    plan["operations"] = [
        {"id": "captions", "kind": "reflow_ordinary_captions"},
        {"id": "broll", "kind": "add_broll", "node_id": "broll_01"},
    ]
    proof_path = tmp_path / "old_frontend_proof.json"
    proof_path.write_text(json.dumps({"schema": "old-proof"}), encoding="utf-8")
    plan["frontend_proof"] = str(proof_path)
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert report.ok is True
    assert not report.errors
    assert any("frontend_proof (diagnostic)" in warning for warning in report.warnings)


def test_candidate_plan_malformed_frontend_proof_is_diagnostic_warning(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    broll = tmp_path / "broll.mp4"
    broll.write_bytes(b"video")
    plan["brolls"] = [{
        "node_id": "broll_01",
        "track_name": "JY_BROLL_broll_01",
        "start_us": 100_000,
        "end_us": 900_000,
        "source_path": str(broll),
        "segment_count": 1,
        "clip": {"transform": {"x": 0.0, "y": 0.0}, "scale": 1.0},
    }]
    plan["operations"] = [
        {"id": "captions", "kind": "reflow_ordinary_captions"},
        {"id": "broll", "kind": "add_broll", "node_id": "broll_01"},
    ]
    proof_path = tmp_path / "malformed_frontend_proof.json"
    proof_path.write_text("{not-json", encoding="utf-8")
    plan["frontend_proof"] = str(proof_path)
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert report.ok is True
    assert not report.errors
    assert any("无法读取或解析诊断证明" in warning for warning in report.warnings)


def test_candidate_plan_rejects_replacement_crossing_semantic_roles(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    registry_path = Path(plan["usage_registry"])
    write(registry_path, {
        "records": [
            {"template_id": "JIANYING-25-08", "primary_category": "question_hook"},
            {"template_id": "JIANYING-25-489", "primary_category": "parallel_list"},
        ],
    })
    plan["presets"] = [{
        "template_id": "JIANYING-25-489",
        "category": "parallel_list",
    }]
    plan["operations"] = [{
        "id": "replace_opening",
        "kind": "replace_preset_group",
        "old_template_id": "JIANYING-25-08",
        "new_template_id": "JIANYING-25-489",
    }]
    report = validate_candidate_plan(plan, plan_path=plan_path, verify_snapshot_files=False)
    assert not report.ok
    assert any("semantic_role mismatch" in error for error in report.errors)


def test_candidate_assembler_previews_explicit_shared_operations_once(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    state_path = tmp_path / "project_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "planning"
    write(state_path, state)
    assembler = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda _draft, _ctx: {"ok": True})
    result = assembler.preview()
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["writer_allowed"] is False
    assert manifest["registered_live_draft"] is False
    with pytest.raises(FileExistsError):
        assembler.preview()


def test_failed_operation_preserves_diagnostic_evidence(tmp_path, monkeypatch):
    from jianying_adapter import shared_operations
    plan_path = make_plan(tmp_path)
    state_path = tmp_path / "project_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "planning"
    write(state_path, state)
    evidence = {"ok": False, "collisions": [{"tracks": ["caption", "broll"], "overlap_px": 8}]}
    monkeypatch.setattr(shared_operations, "reflow_ordinary_captions", lambda *_: evidence)
    assembler = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=lambda *_: {"ok": True})
    with pytest.raises(RuntimeError, match="候选操作失败"):
        assembler.preview()
    report = json.loads((tmp_path / "preview_report.json").read_text(encoding="utf-8"))
    assert report["operations"][-1]["evidence"] == evidence
    assert not report["ok"]


def test_candidate_assembler_rejects_same_name_custom_registry(tmp_path: Path) -> None:
    make_plan(tmp_path)
    with pytest.raises(TypeError, match="只能由 get_shared_operation_registry"):
        OperationRegistry(SHARED_OPERATION_REGISTRY, {"mark": lambda *_args: {"ok": True}})
    assert not (tmp_path / "candidate.json").exists()


def test_enabled_optional_feature_requires_current_video_authorization(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["options"]["bgm"] = {"enabled": True}
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert not report.ok
    assert any("authorization_source" in error for error in report.errors)


def test_subject_clarity_preflight_requires_all_checks(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["subject_clarity_preflight"]["checks"]["perspective_checked"] = False
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert not report.ok
    assert any("perspective_checked" in error for error in report.errors)


def test_text_style_budget_accepts_current_video_specific_limits(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["text_style_budget"] = {
        "max_display_families": 5,
        "max_palette_roles": 8,
        "full_screen_text": True,
    }
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert report.ok, report.errors


def test_text_style_budget_rejects_malformed_values(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["text_style_budget"] = {
        "max_display_families": 0,
        "max_palette_roles": "three",
        "full_screen_text": "no",
    }
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert not report.ok
    assert any("max_display_families" in error for error in report.errors)
    assert any("max_palette_roles" in error for error in report.errors)
    assert any("full_screen_text" in error for error in report.errors)


def test_transition_requires_two_distinct_real_states(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["operations"] = [{
        "id": "transition-1",
        "kind": "mark",
        "visual_role": "transition",
        "before_state_id": "shot-1",
        "after_state_id": "shot-1",
        "boundary_kind": "shot_change",
    }]
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert not report.ok
    assert any("不能相同" in error for error in report.errors)


def test_transition_with_real_before_after_states_passes(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["operations"] = [{
        "id": "transition-1",
        "kind": "mark",
        "visual_role": "transition",
        "before_state_id": "shot-1",
        "after_state_id": "shot-2",
        "boundary_kind": "shot_change",
    }]
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert report.ok
    assert report.visual_rules["transition_operations"][0]["ok"] is True


def test_unified_project_format_separates_data_without_normalizing_style(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["project_format"] = {
        "name": "获客口播自动剪辑管线统一工程格式",
        "separates_creative_plan_from_writer": True,
        "style_normalization": False,
        "writer_adapter": "jianying-8.8",
        "candidate_builder": "CandidateAssembler",
        "per_video_build_script": False,
        "operation_registry": SHARED_OPERATION_REGISTRY,
    }
    assert validate_candidate_plan(plan, plan_path=plan_path).ok


def test_unified_project_format_rejects_forced_style_normalization(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["project_format"] = {
        "separates_creative_plan_from_writer": True,
        "style_normalization": True,
        "writer_adapter": "jianying-8.8",
        "candidate_builder": "CandidateAssembler",
        "per_video_build_script": False,
        "operation_registry": SHARED_OPERATION_REGISTRY,
    }
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert not report.ok
    assert any("禁止统一视觉模板" in error for error in report.errors)


def test_candidate_plan_rejects_non_ready_state_and_project_local_builder(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    state_path = Path(plan["project_state"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "planning"
    write(state_path, state)
    hidden = tmp_path / "helpers"
    hidden.mkdir()
    (hidden / "assemble_this_video.py").write_text("raise SystemExit\n", encoding="utf-8")
    report = validate_candidate_plan(plan, plan_path=plan_path)
    assert not report.ok
    assert any("candidate_ready" in error for error in report.errors)
    assert any("不得靠改名或放入子目录绕过" in error for error in report.errors)


def test_candidate_assembler_rejects_plain_callback_mapping(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)

    def mark(draft, spec, _context):
        draft["marker"] = spec["id"]

    try:
        CandidateAssembler(
            plan_path,
            {"mark": mark},  # type: ignore[arg-type]
            final_validator=lambda _draft, _context: {"ok": True},
        )
    except TypeError as error:
        assert "OperationRegistry" in str(error)
    else:
        raise AssertionError("plain per-video callback dictionaries must be rejected")


def test_writer_contract_rejects_manifest_false(tmp_path: Path) -> None:
    plan_path = make_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    write(Path(plan["output"]), json.loads(Path(plan["base_draft"]).read_text(encoding="utf-8")))
    write(Path(plan["report"]), {"ok": True, "final_validation": {"ok": True, "timeline_equivalence": {"ok": True}}})
    write(Path(plan["manifest"]), {
        "schema": "jianying-adapter.candidate-writer-manifest.v1",
        "status": "candidate_ready_for_single_writer", "writer_allowed": False,
        "blockers": [], "registered_live_draft": False,
        "candidate": plan["output"], "structure_report": plan["report"], "candidate_plan": str(plan_path),
    })
    report = validate_writer_contract(plan, plan_path=plan_path)
    assert not report["ok"]
    assert any("writer_allowed" in error for error in report["errors"])


def test_timeline_equivalence_rejects_preset_and_broll_drift(tmp_path: Path) -> None:
    broll = tmp_path / "broll.mp4"
    broll.write_bytes(b"video")
    plan = {
        "presets": [{
            "node_id": "node_01",
            "template_id": "T-01",
            "start_us": 1_000_000,
            "end_us": 3_000_000,
            "actual_text_tracks": [{
                "track_name": "JY_PRESET_T-01__NODE__node_01_01",
                "text": "重点",
                "start_us": 1_000_000,
                "end_us": 3_000_000,
                "segment_count": 1,
            }],
        }],
        "brolls": [{
            "node_id": "broll_01",
            "track_name": "JY_BROLL_broll_01",
            "start_us": 4_000_000,
            "end_us": 5_000_000,
            "source_path": str(broll),
            "segment_count": 1,
            "clip": {"transform": {"x": 0.0, "y": 0.2}, "scale": 0.5},
        }],
    }
    draft = {
        "tracks": [
            {"name": "JY_PRESET_T-01__NODE__node_01_01", "type": "text", "segments": [{
                "material_id": "text-1", "target_timerange": {"start": 1_000_000, "duration": 1_000_000},
            }]},
            {"name": "JY_BROLL_broll_01", "type": "video", "segments": [{
                "material_id": "video-1", "target_timerange": {"start": 4_200_000, "duration": 1_000_000},
                "clip": {"transform": {"x": 0.0, "y": 0.2}, "scale": 0.5},
            }]},
        ],
        "materials": {
            "texts": [{"id": "text-1", "content": json.dumps({"text": "重点"}, ensure_ascii=False)}],
            "videos": [{"id": "video-1", "path": str(broll)}],
        },
    }
    report = validate_timeline_equivalence(plan, draft)
    assert report["ok"] is False
    assert any("preset node_01 时点不等价" in error for error in report["errors"])
    assert any("broll broll_01 时点不等价" in error for error in report["errors"])


@pytest.mark.parametrize("mutation", [None, "reversed", "missing", "legacy", "legacy_changed"])
def test_timeline_equivalence_preserves_multisegment_text_order(mutation) -> None:
    name = "JY_PRESET_T-01__NODE__node_01_01"
    expected = {"track_name": name, "texts": ["整句", "重点"], "segment_count": 2,
                "start_us": 0, "end_us": 2_000_000}
    plan = {"presets": [{"template_id": "T-01", "node_id": "node_01",
                        "start_us": 0, "end_us": 2_000_000,
                        "actual_text_tracks": [expected]}]}
    segments = [{"material_id": "first", "target_timerange": {"start": 0, "duration": 1_000_000}},
                {"material_id": "second", "target_timerange": {"start": 1_000_000, "duration": 1_000_000}}]
    values = ["整句", "重点"]
    if mutation == "reversed":
        values.reverse()
    elif mutation == "missing":
        segments.pop()
    elif mutation in {"legacy", "legacy_changed"}:
        expected.pop("texts")
        expected["text"] = "整句"
        if mutation == "legacy":
            values[1] = "整句"
    draft = {"tracks": [{"type": "text", "name": name, "segments": segments}],
             "materials": {"texts": [{"id": key, "content": json.dumps({"text": value})}
                                      for key, value in zip(["first", "second"], values)]}}
    report = validate_timeline_equivalence(plan, draft)
    assert report["ok"] is (mutation in {None, "legacy"})
    if mutation not in {None, "legacy"}:
        assert any("文字不等价" in error for error in report["errors"])


def test_timeline_equivalence_rejects_inner_track_drift_and_undeclared_preset(tmp_path: Path) -> None:
    plan = {
        "presets": [{
            "node_id": "node_01",
            "template_id": "T-01",
            "start_us": 1_000_000,
            "end_us": 3_000_000,
            "actual_text_tracks": [
                {
                    "track_name": "JY_PRESET_T-01__NODE__node_01_01",
                    "text": "主句",
                    "start_us": 1_000_000,
                    "end_us": 3_000_000,
                    "segment_count": 1,
                },
                {
                    "track_name": "JY_PRESET_T-01__NODE__node_01_02",
                    "text": "副句",
                    "start_us": 1_000_000,
                    "end_us": 3_000_000,
                    "segment_count": 1,
                },
            ],
        }],
        "brolls": [],
    }
    draft = {
        "tracks": [
            {"name": "JY_PRESET_T-01__NODE__node_01_01", "type": "text", "segments": [{
                "material_id": "t1", "target_timerange": {"start": 1_000_000, "duration": 2_000_000},
            }]},
            {"name": "JY_PRESET_T-01__NODE__node_01_02", "type": "text", "segments": [{
                "material_id": "t2", "target_timerange": {"start": 1_500_000, "duration": 1_000_000},
            }]},
            {"name": "JY_PRESET_T-01__NODE__node_01_99", "type": "text", "segments": [{
                "material_id": "t3", "target_timerange": {"start": 1_000_000, "duration": 2_000_000},
            }]},
        ],
        "materials": {"texts": [
            {"id": "t1", "content": json.dumps({"text": "主句"}, ensure_ascii=False)},
            {"id": "t2", "content": json.dumps({"text": "副句"}, ensure_ascii=False)},
            {"id": "t3", "content": json.dumps({"text": "偷偷加的"}, ensure_ascii=False)},
        ]},
    }
    report = validate_timeline_equivalence(plan, draft)
    assert report["ok"] is False
    assert not any("preset node_01 时点不等价" in error for error in report["errors"])
    assert any("逐轨时点不等价" in error for error in report["errors"])
    assert any("未在 actual_text_tracks 声明" in error for error in report["errors"])


def test_timeline_equivalence_rejects_nested_broll_layout_drift(tmp_path: Path) -> None:
    broll = tmp_path / "broll.mp4"
    broll.write_bytes(b"video")
    plan = {
        "presets": [],
        "brolls": [{
            "node_id": "b1",
            "track_name": "JY_BROLL_b1",
            "start_us": 1_000_000,
            "end_us": 2_000_000,
            "source_path": str(broll),
            "segment_count": 1,
            "clip": {"transform": {"x": 0.1, "y": 0.2}, "scale": 0.5},
        }],
    }
    draft = {
        "tracks": [{"name": "JY_BROLL_b1", "type": "video", "segments": [{
            "material_id": "v1",
            "target_timerange": {"start": 1_000_000, "duration": 1_000_000},
            "clip": {"transform": {"x": 0.9, "y": 0.2}, "scale": 0.5},
        }]}],
        "materials": {"videos": [{"id": "v1", "path": str(broll)}]},
    }
    report = validate_timeline_equivalence(plan, draft)
    assert report["ok"] is False
    assert any("clip 布局参数" in error for error in report["errors"])


def test_frontend_proof_checks_binding_and_exact_8_8_window_without_runtime_attestations(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "8.8.0.13328" / "JianyingPro.exe"
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"exe")
    digest = "A" * 64
    proof = {
        "schema": "jianying-adapter.computer-use-proof.v1",
        "stage": "after_launch",
        "project_id": "PROJECT-1",
        "roughcut_sha256": digest,
        "runtime_observations": {
            "cua_state_window": {"app": f"process:{expected.resolve()}"},
            "blocking_popup": None,
        },
    }
    valid = validate_frontend_proof(
        proof,
        expected_exe=expected,
        project_id="PROJECT-1",
        roughcut_sha256=digest,
    )
    assert valid["ok"] is True
    assert valid["runtime_calls_machine_proven_by_this_json"] is False
    proof["runtime_observations"]["cua_state_window"]["app"] = (
        "process:E:\\JianyingPro\\Apps\\11.4.0.14403\\JianyingPro.exe"
    )
    report = validate_frontend_proof(
        proof,
        expected_exe=expected,
        project_id="PROJECT-1",
        roughcut_sha256=digest,
    )
    assert report["ok"] is False
    assert any("精确8.8" in error for error in report["errors"])
