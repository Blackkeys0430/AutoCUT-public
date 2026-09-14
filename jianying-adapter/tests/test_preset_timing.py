import copy
import json
from types import SimpleNamespace

import pytest

from jianying_adapter.preset_timing import retime_preset_text_tracks
from jianying_adapter.shared_operations import shared_operation_handlers


@pytest.fixture
def inputs(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"current_authorizations": [{"option": "visual_packaging_revision",
                     "enabled": True, "source": "current"}]}), encoding="utf-8")
    name = "JY_PRESET_A__NODE__question_01"
    row = {"track_name": name, "start_us": 1190000, "end_us": 2600000}
    context = SimpleNamespace(plan={"project_state": str(state), "presets": [{
        "template_id": "A", "node_id": "question", "start_us": 0, "end_us": 2600000,
        "actual_text_tracks": [{**row, "segment_count": 1}]}]})
    spec = {"template_id": "A", "node_id": "question", "authorization_source": "current",
            "design_reason": "align second clause", "tracks": [row]}
    draft = {"tracks": [{"type": "text", "name": name, "segments": [{"id": "text",
        "target_timerange": {"start": 1433333, "duration": 1100000},
        "source_timerange": None, "extra_material_refs": ["motion"], "common_keyframes": []}]},
        {"type": "audio", "name": "dialogue", "segments": [{"id": "audio"}]}],
        "materials": {"material_animations": [{"id": "motion", "animations": [{"type": "in",
        "start": 0, "duration": 500000, "resource_id": "original", "path": "local-native"}]}]}}
    return draft, spec, context


def test_aligns_speech_without_retiming_native_entrance_or_other_tracks(inputs):
    draft, spec, context = inputs
    before = copy.deepcopy(draft)
    result, report = retime_preset_text_tracks(draft, spec, context)
    assert result["tracks"][0]["segments"][0]["target_timerange"] == {"start": 1190000, "duration": 1410000}
    assert result["materials"] == before["materials"]
    assert result["tracks"][1] == before["tracks"][1]
    assert result["tracks"][0]["segments"][0]["source_timerange"] is None
    assert draft == before
    assert report["native_visual_verified"] is False
    assert shared_operation_handlers()["retime_preset_text_tracks"] is retime_preset_text_tracks


def _native_inline_motion():
    return [{"property_type": prop, "material_id": "", "keyframe_list": [
        {"time_offset": 633333, "values": first, "curveType": "Line", "graphID": "", "string_value": ""},
        {"time_offset": 800000, "values": last, "curveType": "Line", "graphID": "", "string_value": ""},
    ]} for prop, first, last in [
        ("KFTypeScaleX", [1.2], [0.5]),
        ("KFTypePositionX", [-0.02], [-0.567]),
        ("KFTypePositionY", [0.708], [0.630]),
        ("KFTypeTextColor", [1, 0.75, 0.09, 1], [1, 1, 1, 1]),
    ]]


def test_moves_and_extends_inline_motion_without_changing_native_offsets(inputs):
    draft, spec, context = inputs
    segment = draft["tracks"][0]["segments"][0]
    segment["common_keyframes"] = _native_inline_motion()
    before = copy.deepcopy(draft)
    result, report = retime_preset_text_tracks(draft, spec, context)
    actual = result["tracks"][0]["segments"][0]
    assert actual["common_keyframes"] == segment["common_keyframes"]
    assert actual["target_timerange"] == {"start": 1190000, "duration": 1410000}
    assert result["materials"] == before["materials"]
    assert report["tracks"][0]["keyframe_end_offset_us"] == 800000
    assert draft == before


@pytest.mark.parametrize("fault", ["target_bounds", "source_bounds", "negative", "external_ref", "graph_ref", "material_ref"])
def test_rejects_unbounded_or_external_inline_keyframes(inputs, fault):
    draft, spec, context = inputs
    segment = draft["tracks"][0]["segments"][0]
    segment["common_keyframes"] = _native_inline_motion()
    group = segment["common_keyframes"][0]
    if fault == "target_bounds":
        spec["tracks"][0]["end_us"] = 1900000
        context.plan["presets"][0]["actual_text_tracks"][0]["end_us"] = 1900000
    elif fault == "source_bounds":
        group["keyframe_list"][-1]["time_offset"] = 1200000
    elif fault == "negative":
        group["keyframe_list"][0]["time_offset"] = -1
    elif fault == "external_ref":
        segment["keyframe_refs"] = ["unknown-motion"]
    elif fault == "graph_ref":
        group["keyframe_list"][0]["graphID"] = "unknown-curve"
    else:
        group["material_id"] = "unknown-material"
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        retime_preset_text_tracks(draft, spec, context)
    assert draft == before


@pytest.mark.parametrize("mutation", ["loop", "short", "keyframes", "declaration", "duplicate", "authorization"])
def test_rejects_unsafe_timing_without_partial_changes(inputs, mutation):
    draft, spec, context = inputs
    if mutation == "loop":
        draft["materials"]["material_animations"][0]["animations"][0]["type"] = "loop"
    elif mutation == "short":
        spec["tracks"][0]["end_us"] = 1300000
        context.plan["presets"][0]["actual_text_tracks"][0]["end_us"] = 1300000
    elif mutation == "keyframes":
        draft["tracks"][0]["segments"][0]["common_keyframes"] = [{"property_type": "KFTypeScaleX"}]
    elif mutation == "declaration":
        spec["tracks"][0]["start_us"] = 1000000
    elif mutation == "duplicate":
        spec["tracks"].append(copy.deepcopy(spec["tracks"][0]))
    else:
        spec["authorization_source"] = "old"
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        retime_preset_text_tracks(draft, spec, context)
    assert draft == before
