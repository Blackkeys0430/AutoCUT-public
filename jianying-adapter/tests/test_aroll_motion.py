import copy
import json
from pathlib import Path

import pytest

from jianying_adapter.aroll_motion import animate_aroll_transform, _native_keyframe_model
from jianying_adapter.candidate_plan import CandidateAssembler, get_shared_operation_registry
from jianying_adapter import candidate_plan
from jianying_adapter.shared_operations import shared_final_validator
from test_candidate_stages import _plan, _write


def draft_and_spec():
    segment = {
        "id": "aroll", "material_id": "video", "speed": 1.0, "volume": 0.7,
        "source_timerange": {"start": 0, "duration": 1_000_000},
        "target_timerange": {"start": 100_000, "duration": 1_000_000},
        "clip": {"scale": {"x": 1, "y": 1}, "transform": {"x": 0, "y": 0}, "rotation": 0, "alpha": 1},
        "uniform_scale": {"on": True, "value": 1.0},
        "common_keyframes": [{"property_type": "KFTypeVolume", "keyframe_list": [{"time_offset": 0, "values": [0.7]}]}],
        "extra_material_refs": ["speed"], "reverse": False,
    }
    draft = {"fps": 30, "duration": 1_100_000, "materials": {
        "videos": [{"id": "video", "path": "rough.mp4"}],
        "speeds": [{"id": "speed", "speed": 1.0, "curve_speed": None}],
    }, "tracks": [{"name": "JY_ROUGH_CUT_VIDEO", "type": "video", "segments": [segment]},
                   {"name": "AUDIO", "type": "audio", "segments": [{"volume": 0.4}]},
                   {"name": "JY_ZH_SUBTITLES", "type": "text", "segments": []}]}
    spec = {"id": "motion", "kind": "animate_aroll_transform", "track_name": "JY_ROUGH_CUT_VIDEO",
            "position_space": "normalized", "interpolation": "linear", "keyframes": [
                {"time_offset_us": t, "scale_x": s, "scale_y": s, "position_x": 0.0, "position_y": y}
                for t, s, y in [(0, 1, 0), (300_000, 1, 0), (500_000, 1.1, -0.04), (1_000_000, 1.1, -0.04)]]}
    return draft, spec


def test_native_model_and_unrelated_data_preserved():
    draft, spec = draft_and_spec()
    before = copy.deepcopy(draft)
    report = get_shared_operation_registry().handlers[spec["kind"]](draft, spec, None)
    segment = draft["tracks"][0]["segments"][0]
    assert segment["uniform_scale"] == {"on": False, "value": 1.0}
    assert segment["common_keyframes"][0] == before["tracks"][0]["segments"][0]["common_keyframes"][0]
    model = _native_keyframe_model()
    for prop, group in zip(("scale_x", "scale_y", "position_x", "position_y"), segment["common_keyframes"][1:]):
        expected = model.KeyframeList(getattr(model.KeyframeProperty, prop))
        for frame in spec["keyframes"]:
            expected.add_keyframe(frame["time_offset_us"], float(frame[prop]))
        expected = expected.export_json()
        group = copy.deepcopy(group)
        group.pop("id")
        expected.pop("id")
        for a, b in zip(group["keyframe_list"], expected["keyframe_list"]):
            a.pop("id")
            b.pop("id")
        assert group == expected
    for key in ("common_keyframes", "uniform_scale", "clip"):
        segment[key] = before["tracks"][0]["segments"][0][key]
    assert draft == before
    assert report["native_visual_qa"] == "pending"


@pytest.mark.parametrize("field,value", [
    ("scale_x", 0), ("scale_y", -1), ("scale_x", float("nan")),
    ("position_y", float("inf")), ("position_x", True), ("scale_x", "1"),
    ("time_offset_us", True), ("time_offset_us", 0), ("time_offset_us", -1),
    ("time_offset_us", 1_000_001), ("time_offset_us", 12.5),
])
def test_invalid_keyframe_rejected_without_mutation(field, value):
    draft, spec = draft_and_spec()
    before = copy.deepcopy(draft)
    spec["keyframes"][1][field] = value
    with pytest.raises(ValueError):
        animate_aroll_transform(draft, spec, None)
    assert draft == before


@pytest.mark.parametrize("case", ["unknown", "duplicate", "missing", "multi", "offset", "speed", "speed_material", "curve", "timing", "reverse", "visual_kf", "animation", "refs", "first", "space", "interpolation"])
def test_unsupported_or_conflicting_input_rejected(case):
    draft, spec = draft_and_spec()
    segment = draft["tracks"][0]["segments"][0]
    if case == "unknown": spec["track_name"] = "another"
    elif case == "duplicate": draft["tracks"].append(copy.deepcopy(draft["tracks"][0]))
    elif case == "missing": draft["tracks"].pop(0)
    elif case == "multi": draft["tracks"][0]["segments"].append(copy.deepcopy(segment))
    elif case == "offset": segment["source_timerange"]["start"] = 100
    elif case == "speed": segment["speed"] = 1.2
    elif case == "speed_material": draft["materials"]["speeds"][0]["speed"] = 1.2
    elif case == "curve": draft["materials"]["speeds"][0]["curve_speed"] = {"points": [1]}
    elif case == "timing": segment["source_timerange"]["duration"] -= 1
    elif case == "reverse": segment["reverse"] = True
    elif case == "visual_kf": segment["common_keyframes"].append({"property_type": "UNIFORM_SCALE"})
    elif case == "animation":
        segment["extra_material_refs"].append("anim")
        draft["materials"]["material_animations"] = [{"id": "anim", "animations": [{"type": "in"}]}]
    elif case == "refs": segment["keyframe_refs"] = ["motion"]
    elif case == "first": spec["keyframes"][0]["time_offset_us"] = 1
    elif case == "space": spec["position_space"] = "px"
    elif case == "interpolation": spec["interpolation"] = "bezier"
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError): animate_aroll_transform(draft, spec, None)
    assert draft == before


def test_candidate_assembler_executes_registered_motion_preview(tmp_path: Path, monkeypatch):
    plan_path = _plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    base_path = Path(plan["base_draft"])
    base = json.loads(base_path.read_text(encoding="utf-8"))
    ar, spec = draft_and_spec()
    ar["tracks"][0]["segments"][0]["target_timerange"]["start"] = 0
    base["tracks"].insert(0, ar["tracks"][0])
    base["materials"].update(ar["materials"])
    plan["operations"].insert(0, spec)
    _write(base_path, base)
    _write(plan_path, plan)
    monkeypatch.setattr(candidate_plan, "exact_process_identifier", lambda _path: {"ok": True})
    result = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=shared_final_validator).preview()
    built = json.loads(Path(result["candidate"]).read_text(encoding="utf-8"))
    assert len(built["tracks"][0]["segments"][0]["common_keyframes"]) == 5
    assert built["tracks"][0]["segments"][0]["volume"] == 0.7
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["writer_allowed"] is False
