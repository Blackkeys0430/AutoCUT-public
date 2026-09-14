import copy
import json
from types import SimpleNamespace

import pytest

from jianying_adapter.preset_motion import animate_preset_group_transform
from jianying_adapter.shared_operations import shared_operation_handlers
from jianying_adapter.visual_planning import TECHNIQUE_OPERATIONS


def fixture():
    tracks = []
    for i, start in enumerate((1_000_000, 1_500_000)):
        tracks.append({"name": f"JY_PRESET_T__NODE__n_{i+1:02}", "type": "text", "segments": [{
            "id": str(i), "material_id": str(i), "target_timerange": {"start": start, "duration": 4_000_000-start},
            "clip": {"scale": {"x": 1, "y": 1}, "transform": {"x": i*.2, "y": 0}},
            "uniform_scale": {"on": True, "value": 1}, "common_keyframes": [],
            "extra_material_refs": [f"a{i}"], "keyframe_refs": [],
        }]})
    draft = {"tracks": tracks, "materials": {
        "texts": [{"id": str(i), "content": json.dumps({"text": "文字", "styles": [{"size": 20+i*4}]})} for i in range(2)],
        "material_animations": [{"id": f"a{i}", "animations": [
            {"type": "in", "start": 0, "duration": 300_000},
            {"type": "out", "start": 2_700_000-i*500_000, "duration": 300_000},
        ]} for i in range(2)],
    }}
    context = SimpleNamespace(plan={"target": {"width": 1080, "height": 1920}, "presets": [{
        "template_id": "T", "node_id": "n", "start_us": 1_000_000, "end_us": 4_000_000,
    }]})
    spec = {"template_id": "T", "node_id": "n", "interpolation": "linear", "anchor_px": {"x": 540, "y": 960},
            "keyframes": [
                {"timeline_time_us": 1_800_000, "group_scale": 1, "shift_x_px": 0, "shift_y_px": 0},
                {"timeline_time_us": 2_100_000, "group_scale": 1.2, "shift_x_px": 54, "shift_y_px": 96},
            ]}
    return draft, spec, context


@pytest.mark.parametrize("canvas", [(1080, 1920), (1920, 1080)])
def test_group_preserves_native_entrances_timing_materials_and_relative_layout(canvas):
    draft, spec, context = fixture()
    context.plan["target"] = draft["canvas_config"] = dict(width=canvas[0], height=canvas[1])
    spec["anchor_px"] = dict(x=canvas[0]/2, y=canvas[1]/2)
    spec["keyframes"][-1].update(shift_x_px=canvas[0]/20, shift_y_px=canvas[1]/20)
    before = copy.deepcopy(draft)
    report = animate_preset_group_transform(draft, spec, context)
    assert report["native_visual_qa"] == "pending"
    assert draft["materials"] == before["materials"]
    for index, track in enumerate(draft["tracks"]):
        segment = track["segments"][0]
        old = before["tracks"][index]["segments"][0]
        assert {k: v for k, v in segment.items() if k not in {"common_keyframes", "uniform_scale"}} == {
            k: v for k, v in old.items() if k not in {"common_keyframes", "uniform_scale"}}
        groups = {g["property_type"]: g["keyframe_list"] for g in segment["common_keyframes"]}
        assert groups["KFTypeScaleX"][-1]["values"] == [1.2]
        assert groups["KFTypePositionX"][-1]["values"][0] == pytest.approx(index*.24+.1)
        assert groups["KFTypePositionY"][-1]["values"][0] == pytest.approx(-.1)
        assert groups["KFTypeScaleX"][0]["time_offset"] == 0
        assert groups["KFTypeScaleX"][1]["time_offset"] == 800_000-index*500_000
        assert groups["KFTypeScaleX"][0]["values"] == groups["KFTypeScaleX"][1]["values"] == [1.0]
        assert segment["uniform_scale"] == {"on": False, "value": 1.0}


@pytest.mark.parametrize("change", ["early", "late", "duplicate", "nonneutral", "nan", "conflict", "loop", "reference", "uniform"])
def test_invalid_or_late_track_conflict_is_atomic(change):
    draft, spec, context = fixture()
    segment = draft["tracks"][1]["segments"][0]
    if change == "early": spec["keyframes"][0]["timeline_time_us"] = 1_700_000
    elif change == "late": spec["keyframes"][-1]["timeline_time_us"] = 3_800_000
    elif change == "duplicate": spec["keyframes"][-1]["timeline_time_us"] = 1_800_000
    elif change == "nonneutral": spec["keyframes"][0]["group_scale"] = 1.1
    elif change == "nan": spec["keyframes"][-1]["group_scale"] = float("nan")
    elif change == "conflict": segment["common_keyframes"] = [{"property_type": "KFTypePositionX"}]
    elif change == "loop": draft["materials"]["material_animations"][1]["animations"][0]["type"] = "loop"
    elif change == "reference": segment["keyframe_refs"] = ["unknown"]
    elif change == "uniform": segment["uniform_scale"]["value"] = 2
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError): animate_preset_group_transform(draft, spec, context)
    assert draft == before


def test_registered_and_bound_to_keyframes():
    assert shared_operation_handlers()["animate_preset_group_transform"] is animate_preset_group_transform
    assert "animate_preset_group_transform" in TECHNIQUE_OPERATIONS["keyframes"]


def test_auxiliary_track_moves_by_same_group_factor():
    draft, spec, context = fixture()
    aux = copy.deepcopy(draft["tracks"][0])
    aux.update(name="JY_PRESET_T__NODE__n_AUX_VIDEO_01", type="video")
    aux["segments"][0]["clip"]["scale"] = {"x": .4, "y": .6}
    draft["tracks"].append(aux)
    animate_preset_group_transform(draft, spec, context)
    groups = {g["property_type"]: g["keyframe_list"] for g in aux["segments"][0]["common_keyframes"]}
    assert groups["KFTypeScaleX"][-1]["values"][0] == pytest.approx(.48)
    assert groups["KFTypeScaleY"][-1]["values"][0] == pytest.approx(.72)
