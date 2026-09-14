from __future__ import annotations

import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from jianying_adapter.shared_operations import (
    _extend_segment_final_state,
    _resize_loop_segment,
    _extend_declared_text_tracks,
    place_preset_group,
    add_broll,
    set_track_render_order,
    reflow_ordinary_captions,
    fit_template_text_bounds,
    measure_preset_internal_collisions,
    _measure_segment,
)


def _draft(*, duration: int = 1_000_000, extra_refs: list[str] | None = None) -> tuple[dict, dict]:
    segment = {
        "id": "segment-1",
        "material_id": "text-1",
        "target_timerange": {"start": 0, "duration": duration},
        "clip": {"transform": {"x": 0.0, "y": 0.0}, "scale": {"x": 1.0, "y": 1.0}},
        "extra_material_refs": extra_refs or [],
        "common_keyframes": [],
    }
    draft = {
        "tracks": [{"id": "track-1", "name": "text", "type": "text", "segments": [segment]}],
        "materials": {
            "texts": [{"id": "text-1", "content": json.dumps({"text": "稳定", "styles": [{"size": 20}]})}],
            "material_animations": [],
        },
    }
    return draft, segment


def test_hold_verified_hollow_text_resource_rejects_changed_package(tmp_path):
    package = Path("E:/剪映/保存位置/JianyingPro Presets/Combination/Resources/ImportedLibrary/5f010046a3f3fe1a071d89f5/package")
    if not package.is_dir():
        pytest.skip("native hollow-text fixture is not installed")
    copied = tmp_path / "hollow-text"
    shutil.copytree(package, copied)
    draft, segment = _draft(extra_refs=["hollow"])
    draft["materials"]["effects"] = [{
        "id": "hollow", "type": "text_effect", "resource_id": "6896144074487696654",
        "path": str(copied), "time_range": None, "adjust_params": [],
    }]
    original = deepcopy(draft["materials"]["effects"])
    _extend_segment_final_state(draft, segment, 2_000_000)
    assert draft["materials"]["effects"] == original
    assert segment["extra_material_refs"] == ["hollow"]
    assert segment["target_timerange"]["duration"] == 2_000_000
    (copied / "unexpected.lua").write_text("return {}", encoding="utf-8")
    before = deepcopy(segment)
    with pytest.raises(ValueError, match="缺少动画素材"):
        _extend_segment_final_state(draft, segment, 3_000_000)
    assert segment == before


def test_hold_keeps_intro_and_extends_terminal_state_with_keyframe_hold() -> None:
    draft, segment = _draft(extra_refs=["anim-in"])
    draft["materials"]["material_animations"] = [{
        "id": "anim-in",
        "animations": [{"type": "in", "start": 0, "duration": 300_000}],
    }]
    segment["common_keyframes"] = [{
        "property_type": "KFTypePositionX",
        "keyframe_list": [
            {"id": "kf-0", "time_offset": 0, "values": [-0.2]},
            {"id": "kf-1", "time_offset": 500_000, "values": [0.0]},
        ],
    }]

    report = _extend_segment_final_state(draft, segment, 2_000_000)

    assert segment["target_timerange"] == {"start": 0, "duration": 2_000_000}
    assert draft["materials"]["material_animations"][0]["animations"][0]["start"] == 0
    assert draft["materials"]["material_animations"][0]["animations"][0]["duration"] == 300_000
    assert [item["time_offset"] for item in segment["common_keyframes"][0]["keyframe_list"]] == [0, 500_000, 2_000_000]
    assert segment["common_keyframes"][0]["keyframe_list"][-1]["values"] == [0.0]
    assert report["extended_us"] == 1_000_000
    assert report["keyframe_hold_count"] == 1


def test_hold_moves_out_animation_to_new_tail_without_stretching_it() -> None:
    draft, segment = _draft(duration=1_500_000, extra_refs=["anim-out"])
    draft["materials"]["material_animations"] = [{
        "id": "anim-out",
        "animations": [{"type": "out", "start": 1_000_000, "duration": 500_000}],
    }]

    _extend_segment_final_state(draft, segment, 3_000_000)

    animation = draft["materials"]["material_animations"][0]["animations"][0]
    assert animation["start"] == 2_500_000
    assert animation["duration"] == 500_000
    assert animation["start"] + animation["duration"] == segment["target_timerange"]["duration"]


def test_hold_rejects_unknown_terminal_state_and_illegal_shortening() -> None:
    draft, segment = _draft(duration=1_000_000, extra_refs=["anim-loop"])
    draft["materials"]["material_animations"] = [{
        "id": "anim-loop",
        "animations": [{"type": "loop", "start": 0, "duration": 500_000}],
    }]
    with pytest.raises(ValueError, match="稳定终态"):
        _extend_segment_final_state(draft, segment, 2_000_000)

    plain_draft, plain_segment = _draft(duration=1_000_000)
    with pytest.raises(ValueError, match="非法缩短"):
        _extend_segment_final_state(plain_draft, plain_segment, 900_000)


@pytest.mark.parametrize("duration", [1_500_000, 3_000_000, 5_000_000])
def test_loop_duration_keeps_original_cycle_and_speed(duration):
    draft, segment = _draft(duration=3_000_000, extra_refs=["loop"])
    segment["speed"] = 1.0
    draft["materials"]["material_animations"] = [{"id": "loop", "animations": [
        {"type": "loop", "start": 0, "duration": 500_000, "resource_id": "ring"}]}]
    before = deepcopy(draft["materials"])
    report = _resize_loop_segment(draft, segment, duration)
    assert segment["target_timerange"]["duration"] == duration
    assert segment["speed"] == 1.0
    assert draft["materials"] == before
    assert report["cycle_duration_us"] == 500_000


@pytest.mark.parametrize("mutation", ["too_short", "mixed_exit", "keyframes", "speed"])
def test_loop_duration_rejects_unproven_timing_without_mutation(mutation):
    draft, segment = _draft(duration=3_000_000, extra_refs=["loop"])
    draft["materials"]["material_animations"] = [{"id": "loop", "animations": [
        {"type": "loop", "start": 0, "duration": 500_000}]}]
    if mutation == "mixed_exit":
        draft["materials"]["material_animations"][0]["animations"].append({"type": "out", "start": 2_000_000, "duration": 500_000})
    if mutation == "keyframes":
        segment["keyframe_refs"] = ["external-kf"]
    if mutation == "speed":
        segment["speed"] = 0.6
    before = deepcopy(draft)
    with pytest.raises(ValueError, match="preserve_loop_period"):
        _resize_loop_segment(draft, segment, 100_000 if mutation == "too_short" else 5_000_000)
    assert draft == before


def test_hold_static_quote_symbols_and_animated_main_text_preserves_native_materials() -> None:
    # Native quote presets use empty animation containers for fixed symbols,
    # while the editable main sentence retains a genuine entrance animation.
    draft, main = _draft(duration=1_000_000, extra_refs=["main-in"])
    draft["materials"]["material_animations"] = [
        {"id": "main-in", "animations": [{"type": "in", "name": "向上滑动", "start": 0, "duration": 300_000}]},
        {"id": "static-symbols", "animations": []},
    ]
    draft["tracks"][0]["name"] = "JY_PRESET_QUOTE__NODE__q_0"
    for index, text in enumerate(["“", "”"], 1):
        symbol = deepcopy(main)
        symbol.update(id=f"symbol-{index}", material_id=f"symbol-text-{index}", extra_material_refs=["static-symbols"])
        draft["tracks"].append({"name": f"JY_PRESET_QUOTE__NODE__q_{index}", "type": "text", "segments": [symbol]})
        draft["materials"]["texts"].append({"id": f"symbol-text-{index}", "content": json.dumps({"text": text})})
    before = deepcopy(draft)
    selection = {"actual_text_tracks": [{"track_name": t["name"], "start_us": 0, "end_us": 3_000_000, "segment_count": 1} for t in draft["tracks"]]}

    reports = _extend_declared_text_tracks(draft, selection, start_us=0, end_us=3_000_000)

    assert len(reports) == 3
    assert draft["materials"] == before["materials"]
    for track, original in zip(draft["tracks"], before["tracks"]):
        segment = track["segments"][0]
        assert segment["target_timerange"]["duration"] == 3_000_000
        restored = deepcopy(segment)
        restored["target_timerange"]["duration"] = 1_000_000
        assert restored == original["segments"][0]


@pytest.mark.parametrize("container", [{"id": "invalid"}, {"id": "invalid", "animations": None}, {"id": "invalid", "animations": {}}, {"id": "invalid", "animations": "[]"}])
def test_hold_still_rejects_missing_or_invalid_animation_list(container):
    draft, segment = _draft(extra_refs=["invalid"])
    draft["materials"]["material_animations"] = [container]
    before = deepcopy(draft)
    with pytest.raises(ValueError, match="缺失或非法 animations"):
        _extend_segment_final_state(draft, segment, 2_000_000)
    assert draft == before


def test_hold_native_pop_text_with_static_bloom_preserves_both_material_refs():
    # Real imported 562 shape: 1.216666 s text, 300 ms entrance and a separate
    # materials.effects/text_glow reference. The latter is not an animation.
    draft, segment = _draft(duration=1_216_666, extra_refs=["pop-in", "outline-glow"])
    draft["materials"]["material_animations"] = [{"id": "pop-in", "type": "sticker_animation", "animations": [
        {"type": "in", "name": "向上弹入", "start": 0, "duration": 300_000}]}]
    draft["materials"]["effects"] = [{
        "id": "outline-glow", "type": "bloom", "panel_id": "text_glow", "name": "轮廓光",
        "effect_id": "9762325", "resource_id": "7202575978646737469", "time_range": None,
        "bloom_params": {"color": "", "dir_x": 0.5, "dir_y": 0.5, "range": 0.4, "strength": 0.316818181818182},
        "adjust_params": [], "sub_type": "none", "value": 0.0,
    }]
    before = deepcopy(draft)
    report = _extend_segment_final_state(draft, segment, 1_640_000)
    assert report["extended_us"] == 423_334
    assert draft["materials"] == before["materials"]
    restored = deepcopy(segment)
    restored["target_timerange"]["duration"] = 1_216_666
    assert restored == before["tracks"][0]["segments"][0]


@pytest.mark.parametrize('change', [None, {'resource_id': 'active'}, {'path': 'bubble/package'},
    {'adjust_params': [{'value': 1}]}, {'time_range': {'start': 0, 'duration': 200_000}}])
def test_hold_preserves_native_inactive_bubble_but_rejects_active_or_timed_shape(change):
    # The installed 温29-2句并列 saves this empty effects reference beside its intro.
    draft, segment = _draft(extra_refs=['intro', 'bubble'])
    draft['materials']['material_animations'] = [{'id': 'intro', 'animations': [
        {'type': 'in', 'start': 0, 'duration': 300_000}]}]
    draft['materials']['effects'] = [{'id': 'bubble', 'type': 'text_shape', 'sub_type': 'none',
        'category_id': 'bubble', 'path': '', 'effect_id': '', 'resource_id': '',
        'third_resource_id': '', 'adjust_params': [], 'time_range': None, **(change or {})}]
    before = deepcopy(draft)
    if change is not None:
        with pytest.raises(ValueError, match='缺少动画素材'):
            _extend_segment_final_state(draft, segment, 2_520_000)
        assert draft == before
    else:
        _extend_segment_final_state(draft, segment, 2_520_000)
        assert segment['target_timerange']['duration'] == 2_520_000
        assert draft['materials'] == before['materials']
        assert segment['extra_material_refs'] == ['intro', 'bubble']


@pytest.mark.parametrize("effect", [
    None,
    {"type": "unknown", "panel_id": "text_glow", "bloom_params": {"strength": 0.3}},
    {"type": "bloom", "panel_id": "video_effect", "bloom_params": {"strength": 0.3}},
    {"type": "bloom", "panel_id": "text_glow"},
    {"type": "bloom", "panel_id": "text_glow", "bloom_params": {"strength": 0.3}, "time_range": {"start": 0, "duration": 500_000}},
    {"type": "bloom", "panel_id": "text_glow", "bloom_params": {"strength": 0.3}, "animations": [{"type": "loop"}]},
])
def test_hold_does_not_treat_unknown_or_timed_effect_as_static(effect):
    draft, segment = _draft(extra_refs=["effect"])
    draft["materials"]["effects"] = [{"id": "effect", **effect}] if effect is not None else []
    before = deepcopy(draft)
    with pytest.raises(ValueError, match="缺少动画素材"):
        _extend_segment_final_state(draft, segment, 2_000_000)
    assert draft == before


def test_declared_tracks_extend_to_node_end_without_blank_tail() -> None:
    draft, segment = _draft(duration=400_000)
    draft["tracks"][0]["name"] = "JY_PRESET_T__NODE__n_01"
    selection = {
        "hold_policy": "extend_final_state_only",
        "actual_text_tracks": [{
            "track_name": "JY_PRESET_T__NODE__n_01",
            "start_us": 100_000,
            "end_us": 1_000_000,
            "segment_count": 1,
        }],
    }
    segment["target_timerange"] = {"start": 100_000, "duration": 400_000}

    report = _extend_declared_text_tracks(draft, selection, start_us=100_000, end_us=1_000_000)

    assert segment["target_timerange"] == {"start": 100_000, "duration": 900_000}
    assert report[0]["new_end_us"] == 1_000_000


@pytest.mark.parametrize("canvas", [(1080, 1920), (1920, 1080)])
def test_place_group_applies_explicit_shift_to_text_and_auxiliary_segments(canvas) -> None:
    draft = {
        "tracks": [
            {"name": "JY_PRESET_T__NODE__n_01", "type": "text", "segments": [{
                "material_id": "text-1",
                "clip": {"transform": {"x": 0.0, "y": 0.0}},
                "common_keyframes": [{"property_type": "KFTypePositionX", "keyframe_list": [{"time_offset": 0, "values": [0.0]}]}],
            }]},
            {"name": "JY_PRESET_T__NODE__n_AUX_VIDEO_01", "type": "video", "segments": [{
                "material_id": "video-1", "clip": {"transform": {"x": 0.1, "y": 0.0}, "scale": {"x": 0.25, "y": 0.25}},
                "common_keyframes": [{"property_type": "KFTypeScaleX", "keyframe_list": [{"time_offset": 0, "values": [0.25]}]}],
            }]},
        ],
        "materials": {"texts": [{"id": "text-1", "content": json.dumps({"text": "组", "styles": [{"size": 20}]})}]},
    }
    draft["canvas_config"] = {"width": canvas[0], "height": canvas[1]}
    context = type("Context", (), {"plan": {"target": draft["canvas_config"], "presets": [{
        "template_id": "T", "node_id": "n", "final_position": {"x": canvas[0]/2, "y": 320}, "final_group_scale": 1.0,
    }]}})()

    _result, report = place_preset_group(draft, {"template_id": "T", "node_id": "n", "final_group_scale": 2.0}, context)

    assert draft["tracks"][0]["segments"][0]["clip"]["transform"]["y"] == pytest.approx(2 * (0.5 - 320.0 / canvas[1]))
    assert draft["tracks"][1]["segments"][0]["clip"]["transform"]["x"] == pytest.approx(0.2)
    assert draft["tracks"][1]["segments"][0]["clip"]["scale"] == {"x": 0.5, "y": 0.5}
    assert draft["tracks"][1]["segments"][0]["common_keyframes"][0]["keyframe_list"][0]["values"] == [0.5]
    assert draft["tracks"][0]["segments"][0]["common_keyframes"][0]["keyframe_list"][0]["values"][0] == pytest.approx(0.0)
    assert report["target_position_px"] == [canvas[0]/2, 320.0]


def _segmented_placement():
    names = ["JY_PRESET_T__NODE__n_01", "JY_PRESET_T__NODE__n_02"]
    def segment(material, x, start, duration):
        return {"material_id": material, "target_timerange": {"start": start, "duration": duration},
                "clip": {"transform": {"x": x, "y": 0.0}},
                "common_keyframes": [{"property_type": "KFTypePositionX", "keyframe_list": [
                    {"time_offset": 0, "values": [x - .1]}, {"time_offset": duration, "values": [x]}]}]}
    draft = {"canvas_config": {"width": 1080, "height": 1920}, "tracks": [
        {"name": names[0], "type": "text", "segments": [segment("full", 0, 0, 1_200_000), segment("prefix", -.2, 1_200_000, 1_800_000)]},
        {"name": names[1], "type": "text", "segments": [segment("emphasis", .2, 1_200_000, 1_800_000)]},
        {"name": "JY_PRESET_T__NODE__n_AUX_AUDIO_01", "type": "audio", "segments": [{"volume": .5}]},
        {"name": "JY_PRESET_T__NODE__other_01", "type": "text", "segments": [segment("other", 0, 0, 3_000_000)]},
    ], "materials": {"texts": [{"id": name, "content": json.dumps({"text": name, "styles": [{"size": 20}]})}
                                for name in ["full", "prefix", "emphasis", "other"]]}}
    context = SimpleNamespace(plan={"target": draft["canvas_config"], "presets": [{"template_id": "T", "node_id": "n"}]})
    groups = [
        {"segments": [{"track_name": names[0], "segment_index": 0}],
         "source_anchor_px": {"x": 540, "y": 960}, "target_anchor_px": {"x": 440, "y": 400}, "scale_factor": 1},
        {"segments": [{"track_name": names[0], "segment_index": 1}, {"track_name": names[1], "segment_index": 0}],
         "source_anchor_px": {"x": 540, "y": 960}, "target_anchor_px": {"x": 640, "y": 500}, "scale_factor": 2},
    ]
    return draft, {"template_id": "T", "node_id": "n", "segment_groups": groups}, context


def test_place_segment_groups_moves_two_phases_and_keyframes_independently():
    draft, spec, context = _segmented_placement()
    before = deepcopy(draft)
    _, report = place_preset_group(draft, spec, context)
    assert report["transformed_segment_count"] == 3
    for group in spec["segment_groups"]:
        factor = group["scale_factor"]
        dx = 2 * (group["target_anchor_px"]["x"] - 540) / 1080
        dy = 2 * (.5 - group["target_anchor_px"]["y"] / 1920)
        for selector in group["segments"]:
            track_index = 0 if selector["track_name"].endswith("_01") else 1
            index = selector["segment_index"]
            new = draft["tracks"][track_index]["segments"][index]
            old = before["tracks"][track_index]["segments"][index]
            assert new["target_timerange"] == old["target_timerange"]
            assert new["clip"]["transform"] == pytest.approx({"x": old["clip"]["transform"]["x"] * factor + dx, "y": dy})
            for a, b in zip(new["common_keyframes"][0]["keyframe_list"], old["common_keyframes"][0]["keyframe_list"]):
                assert a["time_offset"] == b["time_offset"]
                assert a["values"] == pytest.approx([b["values"][0] * factor + dx])
    assert [json.loads(m["content"])["styles"][0]["size"] for m in draft["materials"]["texts"]] == [20, 40, 40, 20]
    assert draft["tracks"][2:] == before["tracks"][2:]


@pytest.mark.parametrize("case", ["missing", "duplicate", "cross_node", "bad_index", "bool_index", "nan_anchor", "bad_scale", "missing_anchor", "mixed", "shared_material", "duplicate_track"])
def test_segment_groups_invalid_partition_preserves_draft(case):
    draft, spec, context = _segmented_placement()
    groups = spec["segment_groups"]
    if case == "missing": groups[1]["segments"].pop()
    elif case == "duplicate": groups[1]["segments"][0] = deepcopy(groups[0]["segments"][0])
    elif case == "cross_node": groups[1]["segments"][0]["track_name"] = "JY_PRESET_T__NODE__other_01"
    elif case == "bad_index": groups[1]["segments"][0]["segment_index"] = 8
    elif case == "bool_index": groups[1]["segments"][0]["segment_index"] = True
    elif case == "nan_anchor": groups[1]["source_anchor_px"]["x"] = float("nan")
    elif case == "bad_scale": groups[1]["scale_factor"] = 0
    elif case == "missing_anchor": del groups[1]["target_anchor_px"]
    elif case == "mixed": spec["final_group_scale"] = 2
    elif case == "shared_material": draft["tracks"][1]["segments"][0]["material_id"] = "prefix"
    elif case == "duplicate_track": draft["tracks"].append(deepcopy(draft["tracks"][0]))
    before = deepcopy(draft)
    with pytest.raises(ValueError):
        place_preset_group(draft, spec, context)
    assert draft == before


@pytest.fixture
def native_broll_source(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the native material probe test")
    source = tmp_path / "broll.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2", "-c:a", "aac", str(source)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return source


def test_add_broll_crop_preserves_aspect_and_isolates_materials(tmp_path, native_broll_source):
    from jianying_adapter.final_layout import _broll_geometry
    from jianying_adapter.media_crop import material_crop
    context = SimpleNamespace(plan_path=str(tmp_path / 'plan.json'))
    spec = dict(node_id='crop', source_path=str(native_broll_source), start_us=0, end_us=1_000_000,
                source_timerange=dict(start=0, duration=1_000_000),
                clip=dict(scale=dict(x=.4, y=.4), transform=dict(x=0, y=0)))
    draft = dict(tracks=[], materials={})
    add_broll(draft, spec, context)
    add_broll(draft, dict(spec, node_id='square', source_crop=[.21875, 0, .78125, 1]), context)
    assert len(draft['materials']['videos']) == 2
    material = draft['materials']['videos'][1]
    assert material_crop(material) == [.21875, 0, .78125, 1]
    assert material['width'] == 320 and material['height'] == 180
    segment = draft['tracks'][1]['segments'][0]
    box = _broll_geometry(segment, material, {})[0]['bbox']
    assert box[2]-box[0] == pytest.approx(432)
    assert box[3]-box[1] == pytest.approx(432)
    from jianying_adapter.candidate_plan import validate_timeline_equivalence
    declaration = dict(spec, node_id='square', track_name='JY_BROLL_square', segment_count=1,
                       source_crop=[.21875, 0, .78125, 1])
    plan = {'brolls': [dict(spec, track_name='JY_BROLL_crop', segment_count=1), declaration]}
    report = validate_timeline_equivalence(plan, draft)
    assert report['ok'], report
    declaration['source_crop'] = [0, 0, 1, 1]
    assert not validate_timeline_equivalence(plan, draft)['ok']
    original = deepcopy(draft)
    for invalid in ([-.1, 0, 1, 1], [0, 0, 1.1, 1], [.5, 0, .5, 1], [0, 0, float('nan'), 1]):
        with pytest.raises(ValueError, match='source_crop'):
            add_broll(draft, dict(spec, source_crop=invalid), context)
        assert draft == original


def test_add_broll_exports_complete_native_video_material(tmp_path, native_broll_source) -> None:
    source = native_broll_source
    clip = {"alpha": 1.0, "scale": {"x": 0.45, "y": 0.6}, "transform": {"x": 0.35, "y": -0.2}}
    draft, report = add_broll(
        {"tracks": [], "materials": {"videos": []}},
        {"node_id": "b", "track_name": "JY_BROLL_b", "source_path": str(source), "material_id": "fixed-id", "start_us": 10_000, "end_us": 1_010_000, "clip": clip, "segment": {"source_timerange": {"start": 0, "duration": 1_000_000}, "speed": 1.0, "volume": 0.0}},
        type("Context", (), {"plan_path": str(tmp_path / "plan.json")})(),
    )
    material = draft["materials"]["videos"][0]
    assert material["id"] == "fixed-id"
    assert material["type"] == "video"
    assert material["width"] == 320 and material["height"] == 180
    assert material["duration"] >= 1_000_000
    assert "crop" in material and material["path"] == str(source.resolve())
    segment = draft["tracks"][0]["segments"][0]
    assert segment["source_timerange"] == {"start": 0, "duration": 1_000_000}
    assert segment["clip"] == clip
    assert segment["id"]
    assert segment["enable_video_mask"] is True
    assert segment["hdr_settings"] == {"intensity": 1.0, "mode": 1, "nits": 1000}
    assert segment["visible"] is True
    assert draft["tracks"][0]["attribute"] == 1
    assert draft["tracks"][0]["flag"] == 0
    speed = draft["materials"]["speeds"][0]
    assert speed["id"] in segment["extra_material_refs"]
    assert speed["speed"] == segment["speed"] == 1.0
    assert report["segment_count"] == 1
    with pytest.raises(ValueError, match="exceeds probed"):
        add_broll(
            {"tracks": [], "materials": {"videos": []}},
            {"node_id": "overflow", "source_path": str(source),
             "start_us": 0, "end_us": 1_000_000, "clip": clip,
             "segment": {"source_timerange": {"start": material["duration"], "duration": 1_000_000}}},
            type("Context", (), {"plan_path": str(tmp_path / "plan.json")})(),
        )


def test_add_broll_same_source_nodes_keep_native_unique_segments_and_default_silence(tmp_path, native_broll_source) -> None:
    from jianying_adapter.candidate_plan import validate_timeline_equivalence

    draft = {"tracks": [], "materials": {}}
    context = type("Context", (), {"plan_path": str(tmp_path / "plan.json")})()
    clip = {"scale": {"x": 0.5, "y": 0.5}, "transform": {"x": 0.1, "y": 0.2}}
    plan = {"brolls": []}
    for index in range(3):
        spec = {"node_id": f"b{index}", "track_name": f"JY_BROLL_b{index}",
                "source_path": str(native_broll_source), "start_us": index * 1_000_000,
                "end_us": (index + 1) * 1_000_000, "clip": clip,
                "source_timerange": {"start": 500_000, "duration": 1_000_000},
                "segment": {"id": "copied-stale-id", "render_index": index,
                            "uniform_scale": {"on": False, "value": 1.0}}}
        draft, _ = add_broll(draft, spec, context)
        plan["brolls"].append({**spec, "segment_count": 1})
    tracks = draft["tracks"]
    segments = [t["segments"][0] for t in tracks]
    assert len({t["id"] for t in tracks}) == 3
    assert len({s["id"] for s in segments}) == 3
    assert all(s["id"] != "copied-stale-id" for s in segments)
    assert len(draft["materials"]["videos"]) == 1
    speed_ids = {s["id"] for s in draft["materials"]["speeds"]}
    assert len(speed_ids) == 3
    for index, segment in enumerate(segments):
        assert segment["source_timerange"] == {"start": 500_000, "duration": 1_000_000}
        assert segment["target_timerange"] == {"start": index * 1_000_000, "duration": 1_000_000}
        assert segment["volume"] == 0.0 and tracks[index]["attribute"] == 1
        assert segment["speed"] == 1.0
        assert set(segment["extra_material_refs"]) <= speed_ids
        assert segment["clip"] == clip
        assert segment["render_index"] == index
        assert segment["uniform_scale"]["on"] is False
    assert validate_timeline_equivalence(plan, draft)["ok"]
    # Native 8.8 may omit its default speed; identity validation must allow that.
    for segment in segments:
        segment.pop("speed")
    assert validate_timeline_equivalence(plan, draft)["ok"]
    for bad_id in ("", segments[1]["id"]):
        damaged = deepcopy(draft)
        damaged["tracks"][0]["segments"][0]["id"] = bad_id
        report = validate_timeline_equivalence(plan, damaged)
        assert not report["ok"]
        assert any("unique track/segment id" in error for error in report["errors"])
    damaged = deepcopy(draft)
    damaged["tracks"][0]["id"] = damaged["tracks"][1]["id"]
    assert not validate_timeline_equivalence(plan, damaged)["ok"]


def test_add_broll_fixed_segment_id_supports_following_mask_and_writer_readback(tmp_path, native_broll_source) -> None:
    from jianying_adapter.candidate_plan import validate_timeline_equivalence
    from jianying_adapter.video_mask import apply_video_mask
    from test_video_mask import draft_and_spec

    draft = {"tracks": [], "materials": {}, "canvas_config": {"width": 1080, "height": 1920}}
    spec = {"id": "clear_face", "kind": "add_broll", "node_id": "clear_face", "track_name": "JY_BROLL_clear_face",
            "source_path": str(native_broll_source), "start_us": 0, "end_us": 1_000_000,
            "source_timerange": {"start": 0, "duration": 1_000_000},
            "segment_id": "clear_face_segment", "segment": {"id": "ignored-old-id"},
            "clip": {"alpha": 1.0, "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0, "y": 0}}}
    context = type("Context", (), {"plan_path": str(tmp_path / "plan.json")})()
    draft, _ = add_broll(draft, spec, context)
    assert draft["tracks"][0]["segments"][0]["id"] == "clear_face_segment"
    _, mask_spec = draft_and_spec(tmp_path)
    mask_spec.update(track_name="JY_BROLL_clear_face", segment_id="clear_face_segment", center_y_px=0)
    apply_video_mask(draft, mask_spec, None)
    assert draft["materials"]["common_mask"][0]["id"] in draft["tracks"][0]["segments"][0]["extra_material_refs"]
    plan = {"operations": [spec], "brolls": [{k: v for k, v in {**spec, "segment_count": 1}.items() if k != "segment_id"}]}
    assert validate_timeline_equivalence(plan, draft)["ok"]
    draft["tracks"][0]["segments"][0]["id"] = "changed-id"
    assert not validate_timeline_equivalence(plan, draft)["ok"]


@pytest.mark.parametrize("segment_id", [None, "", "  ", 123, True, "existing"])
def test_add_broll_invalid_fixed_identity_rejected_atomically(tmp_path, native_broll_source, segment_id) -> None:
    draft = {"tracks": [{"type": "video", "segments": [{"id": "existing"}]}], "materials": {}}
    before = json.loads(json.dumps(draft))
    spec = {"node_id": "b", "source_path": str(native_broll_source), "start_us": 0, "end_us": 1_000_000,
            "source_timerange": {"start": 0, "duration": 1_000_000}, "clip": {"alpha": 1.0}, "segment_id": segment_id}
    context = type("Context", (), {"plan_path": str(tmp_path / "plan.json")})()
    with pytest.raises(ValueError, match="segment_id"):
        add_broll(draft, spec, context)
    assert draft == before


def test_add_broll_rejects_implicit_retiming_without_mutating_draft(tmp_path, native_broll_source) -> None:
    draft = {"tracks": [], "materials": {}}
    spec = {"node_id": "b", "source_path": str(native_broll_source), "start_us": 0, "end_us": 500_000,
            "source_timerange": {"start": 0, "duration": 1_000_000}, "clip": {"alpha": 1.0}}
    context = type("Context", (), {"plan_path": str(tmp_path / "plan.json")})()
    with pytest.raises(ValueError, match="implicit retiming"):
        add_broll(draft, spec, context)
    assert draft == {"tracks": [], "materials": {}}
    # An explicitly requested speed must preserve both declared timeranges.
    draft, _ = add_broll(draft, {**spec, "segment": {"speed": 2.0, "volume": 0.25}}, context)
    segment = draft["tracks"][0]["segments"][0]
    assert segment["target_timerange"]["duration"] == 500_000
    assert segment["source_timerange"]["duration"] == 1_000_000
    assert segment["speed"] == draft["materials"]["speeds"][0]["speed"] == 2.0
    assert segment["volume"] == 0.25 and draft["tracks"][0]["attribute"] == 0


def test_set_track_render_order_assigns_bottom_to_top_without_touching_other_fields() -> None:
    draft = {"tracks": [
        {"name": "base", "type": "video", "segments": [{"id": "b", "render_index": 99, "track_render_index": 4, "target_timerange": {"start": 0, "duration": 2}}]},
        {"name": "captions", "type": "text", "segments": [{"id": "c", "target_timerange": {"start": 0, "duration": 2}}]},
        {"name": "preset", "type": "text", "segments": [{"id": "p", "target_timerange": {"start": 0, "duration": 1}}]},
        {"name": "broll", "type": "video", "segments": [{"id": "r", "target_timerange": {"start": 1, "duration": 1}}]},
        {"name": "voice", "type": "audio", "segments": [{"id": "a"}]},
        {"name": "empty", "type": "video", "segments": []},
    ]}
    before = json.loads(json.dumps(draft))
    report = set_track_render_order(draft, {"ordered_track_names": ["base", "broll", "captions", "preset"]}, None)
    assert report["track_count"] == 4
    assert [track["name"] for track in draft["tracks"]] == ["base", "broll", "captions", "preset", "voice", "empty"]
    assert [draft["tracks"][i]["segments"][0]["render_index"] for i in (0, 1, 2, 3)] == [0, 1, 2, 3]
    assert all(draft["tracks"][i]["segments"][0]["track_render_index"] == 0 for i in (0, 1, 2, 3))
    assert draft["tracks"][4] == before["tracks"][4]
    assert draft["tracks"][5] == {**before["tracks"][5], "flag": 2}
    for i in (0, 1, 2, 3):
        expected = before["tracks"][[0, 3, 1, 2][i]]["segments"][0]
        actual = draft["tracks"][i]["segments"][0]
        for key in expected:
            if key not in {"render_index", "track_render_index"}:
                assert actual[key] == expected[key]


def test_render_order_preserves_base_main_track_when_delayed_background_is_below_person(tmp_path):
    def video(name, track_id, start, duration, index, volume):
        return {"name": name, "id": track_id, "type": "video", "flag": 0,
                "segments": [{"id": track_id+"-segment", "material_id": track_id+"-material",
                              "target_timerange": {"start": start, "duration": duration},
                              "source_timerange": {"start": 0, "duration": duration},
                              "volume": volume, "render_index": index, "track_render_index": 0}]}
    aroll = video("Aroll", "original", 0, 44_700_000, 2, .84)
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps({"tracks": [aroll]}), encoding="utf-8")
    context = type("Context", (), {"plan": {"base_draft": str(base_path)}, "plan_path": tmp_path / "plan.json"})()
    draft = {"config": {"maintrack_adsorb": True}, "render_index_track_mode_on": True,
             "tracks": [video("city", "city", 6_390_000, 4_460_000, 0, 0),
                        video("office", "office", 10_850_000, 2_400_000, 1, 0), aroll]}
    expected = deepcopy(draft)
    expected["tracks"][0]["flag"] = 2
    expected["tracks"][1]["flag"] = 2
    spec = {"ordered_track_names": ["city", "office", "Aroll"]}
    report = set_track_render_order(draft, spec, context)
    # Reproduce v9's already-sorted input: the original base, not the first
    # background, identifies the main track. Only native flag bits change.
    assert draft == expected
    assert report["main_video_track_name"] == "Aroll"
    assert report["native_main_track_roundtrip_verified"] is False
    set_track_render_order(draft, spec, context)
    assert draft == expected


@pytest.mark.parametrize("ordered", [
    ["base"], ["base", "unknown", "captions"], ["base", "base", "captions"],
])
def test_set_track_render_order_rejects_invalid_list_without_partial_mutation(ordered) -> None:
    draft = {"tracks": [
        {"name": "base", "type": "video", "segments": [{"render_index": 7}]},
        {"name": "captions", "type": "text", "segments": [{"render_index": 8}]},
    ]}
    before = json.loads(json.dumps(draft))
    with pytest.raises(ValueError):
        set_track_render_order(draft, {"ordered_track_names": ordered}, None)
    assert draft == before


def test_set_track_render_order_late_invalid_segment_is_atomic() -> None:
    draft = {"tracks": [
        {"name": "base", "type": "video", "segments": [{"render_index": 7}]},
        {"name": "broll", "type": "video", "segments": ["invalid"]},
    ]}
    with pytest.raises(ValueError, match="non-object"):
        set_track_render_order(draft, {"ordered_track_names": ["base", "broll"]}, None)
    assert draft["tracks"][0]["segments"][0]["render_index"] == 7


def test_set_track_render_order_rejects_non_list_segments() -> None:
    draft = {"tracks": [
        {"name": "base", "type": "video", "segments": [{"id": "b"}]},
        {"name": "captions", "type": "text", "segments": {"id": "bad"}},
    ]}
    with pytest.raises(ValueError, match="segments must be a list"):
        set_track_render_order(draft, {"ordered_track_names": ["base", "captions"]}, None)
    assert "render_index" not in draft["tracks"][0]["segments"][0]


def test_reflow_ordinary_captions_applies_final_font_size_before_measurement() -> None:
    draft, _segment = _draft()
    material = draft["materials"]["texts"][0]
    payload = json.loads(material["content"])
    payload["text"] = "这是需要换行的字幕"
    payload["styles"][0]["size"] = 10
    material["content"] = json.dumps(payload, ensure_ascii=False)
    result = reflow_ordinary_captions(draft, {"safe_width_ratio": 0.82, "font_size": 28}, None)
    updated = json.loads(material["content"])
    assert updated["styles"][0]["size"] == 28
    assert material["font_size"] == 28
    assert "\n" in updated["text"]
    assert result["caption_count"] == 1


def test_reflow_ordinary_captions_without_font_size_preserves_existing_behavior() -> None:
    draft, _segment = _draft()
    before = json.loads(draft["materials"]["texts"][0]["content"])
    reflow_ordinary_captions(draft, {}, None)
    after = json.loads(draft["materials"]["texts"][0]["content"])
    assert after["styles"][0]["size"] == before["styles"][0]["size"]


def test_reflow_uses_bound_font_plan_units_and_maximum_segment_scale():
    from PIL import ImageFont
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if not font.is_file():
        pytest.skip("Windows layout fixture font unavailable")
    draft, segment = _draft()
    material = draft["materials"]["texts"][0]
    material["content"] = json.dumps({"text": "这个字幕需要实际测宽", "styles": [
        {"size": 12.5, "font": {"path": str(font)}}]})
    material["letter_spacing"] = .3
    segment["clip"]["scale"] = {"x": 1.5, "y": 1}
    segment["common_keyframes"] = [{"property_type": "KFTypeScaleX", "keyframe_list": [
        {"time_offset": 100000, "values": [2.0]}]}]
    context = SimpleNamespace(plan={"layout_checks": {"text_unit_to_px": 6.5}})
    report = reflow_ordinary_captions(draft, {"safe_width_ratio": .84}, context)["captions"][0]
    assert report["measurement_mode"] == "exact_font"
    assert Path(report["font_path"]) == font
    assert report["line_count"] == 2
    assert report["measurement_scale_x"] == 2
    assert report["text_unit_to_px"] == 6.5
    assert report["safe_width_px"] == 907.2
    measured_font = ImageFont.truetype(str(font), round(12.5 * 6.5))
    for line in report["lines"]:
        expected = (measured_font.getlength(line["text"]) + (len(line["text"]) - 1) * .3 * 6.5) * 2
        assert line["width_px"] == pytest.approx(expected, abs=.002)
        assert line["width_px"] <= report["safe_width_px"]
    assert segment["clip"]["scale"] == {"x": 1.5, "y": 1}


def test_reflow_passes_semantic_offsets_for_logical_caption_text():
    draft, _segment = _draft()
    material = draft["materials"]["texts"][0]
    material["content"] = json.dumps({"text": "真的能舒服自\n己和身边的人", "styles": [{"size": 12.5}]})
    context = SimpleNamespace(plan={"layout_checks": {"text_unit_to_px": 6.5}})
    report = reflow_ordinary_captions(draft, {"safe_width_ratio": .84,
        "preferred_breaks": {"真的能舒服自己和身边的人": [7]}}, context)["captions"][0]
    assert report["text"] == "真的能舒服自己\n和身边的人"
    assert report["measurement_mode"] == "conservative_font_fallback"
    assert json.loads(material["content"])["styles"][0]["range"] == [0, 13]


@pytest.mark.parametrize("factor", [True, 0, -1, float("inf"), "6.5"])
def test_reflow_rejects_invalid_plan_units_before_changes(factor):
    draft, _ = _draft()
    before = deepcopy(draft)
    with pytest.raises(ValueError, match="text_unit_to_px"):
        reflow_ordinary_captions(draft, {"font_size": 12.5},
            SimpleNamespace(plan={"layout_checks": {"text_unit_to_px": factor}}))
    assert draft == before


@pytest.mark.parametrize("font_size", [True, 0, -1, float("inf"), "28"])
def test_reflow_ordinary_captions_rejects_invalid_font_size(font_size) -> None:
    draft, _segment = _draft()
    before = json.loads(draft["materials"]["texts"][0]["content"])
    with pytest.raises(ValueError, match="font_size"):
        reflow_ordinary_captions(draft, {"font_size": font_size}, None)
    assert json.loads(draft["materials"]["texts"][0]["content"]) == before


def test_landscape_reflow_uses_width_and_preserves_lower_caption_position():
    portrait, _ = _draft()
    material = portrait["materials"]["texts"][0]
    payload = json.loads(material["content"])
    payload["text"] = "这是需要换行的字幕"
    material["content"] = json.dumps(payload, ensure_ascii=False)
    landscape = deepcopy(portrait)
    landscape["canvas_config"] = {"width": 1920, "height": 1080}
    segment = landscape["tracks"][0]["segments"][0]
    segment["clip"]["transform"]["y"] = -.8
    for draft in (portrait, landscape):
        reflow_ordinary_captions(draft, {"font_size": 28, "safe_width_ratio": .82}, None)
    assert "\n" in json.loads(portrait["materials"]["texts"][0]["content"])["text"]
    assert "\n" not in json.loads(landscape["materials"]["texts"][0]["content"])["text"]
    assert segment["clip"]["transform"]["y"] == -.8


def test_landscape_fit_and_internal_collision_measure_real_glyphs():
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if not font.is_file():
        pytest.skip("Windows layout fixture font unavailable")
    draft, first = _draft()
    draft["canvas_config"] = {"width": 1920, "height": 1080}
    first["clip"]["transform"] = {"x": 2*1650/1920-1, "y": 1-2*400/1080}
    draft["tracks"][0]["name"] = "JY_PRESET_T__NODE__n_01"
    material = draft["materials"]["texts"][0]
    payload = json.loads(material["content"])
    payload["styles"][0]["font"] = {"path": str(font)}
    material["content"] = json.dumps(payload, ensure_ascii=False)
    second_track = deepcopy(draft["tracks"][0])
    second_track.update(id="track-2", name="JY_PRESET_T__NODE__n_02")
    second = second_track["segments"][0]
    second.update(id="segment-2", material_id="text-2")
    second["clip"]["transform"]["x"] = 2*2050/1920-1
    second_material = deepcopy(material)
    second_material["id"] = "text-2"
    draft["tracks"].extend([second_track, {"id": "captions", "name": "JY_ZH_SUBTITLES", "type": "text", "segments": []}])
    draft["materials"]["texts"].append(second_material)
    context = SimpleNamespace(plan={"target": draft["canvas_config"], "presets": [{"template_id": "T", "node_id": "n"}]})
    assert _measure_segment(second, second_material, canvas=(1920, 1080))["bbox"][2] > 1920
    result = fit_template_text_bounds(draft, {"safe_area_px": [100, 80, 1820, 820]}, context)
    box = result["groups"][0]["final_union_bbox"]
    assert 100 <= box[0] < box[2] <= 1820.01
    assert 80 <= box[1] < box[3] <= 820
    assert measure_preset_internal_collisions(draft, {}, context)["ok"]
    second["clip"]["transform"] = deepcopy(first["clip"]["transform"])
    collision = measure_preset_internal_collisions(draft, {}, context)
    assert not collision["ok"] and collision["collision_count"] == 1


@pytest.mark.parametrize("size", [(1080, 608), (1280, 720)])
def test_other_canvas_sizes_remain_rejected_before_caption_changes(size):
    draft, _ = _draft()
    draft["canvas_config"] = dict(width=size[0], height=size[1])
    before = deepcopy(draft)
    with pytest.raises(ValueError, match="1080x1920.*1920x1080"):
        reflow_ordinary_captions(draft, {"font_size": 28}, None)
    assert draft == before


def test_mismatched_draft_and_target_canvas_rejected_before_group_mutation():
    draft, _ = _draft()
    draft["canvas_config"] = {"width": 1080, "height": 1920}
    before = deepcopy(draft)
    context = SimpleNamespace(plan={"target": {"width": 1920, "height": 1080}})
    with pytest.raises(ValueError, match="does not match target"):
        place_preset_group(draft, {}, context)
    assert draft == before
