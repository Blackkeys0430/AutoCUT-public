import copy
import json
from pathlib import Path

import pytest

from jianying_adapter.aroll_motion import animate_aroll_transform
from jianying_adapter.video_split import split_video_track
from jianying_adapter.candidate_plan import CandidateAssembler, get_shared_operation_registry
from jianying_adapter import candidate_plan
from jianying_adapter.shared_operations import shared_final_validator
from test_aroll_motion import draft_and_spec as motion_fixture
from test_video_mask import draft_and_spec as mask_fixture
from test_candidate_stages import _plan, _write


def fixture():
    draft, motion = motion_fixture()
    draft["tracks"][0]["segments"][0]["common_keyframes"] = []
    draft["materials"]["videos"][0].update(width=1080, height=1920)
    spec = {"id": "split", "kind": "split_video_track", "track_name": "JY_ROUGH_CUT_VIDEO",
            "segment_id": "aroll", "cut_points_us": [400000, 700000], "segment_ids": ["begin", "middle", "end"]}
    motion.update(segment_id="middle", keyframes=[
        {"time_offset_us": t, "scale_x": scale, "scale_y": scale, "position_x": 0, "position_y": -.3}
        for t, scale in [(0, 1), (100000, .65), (300000, .65)]])
    return draft, spec, motion


def add_fade(draft):
    draft["materials"]["audio_fades"] = [{"id": "fade", "type": "audio_fade", "fade_type": 0,
                                          "fade_in_duration": 30000, "fade_out_duration": 30000}]
    draft["tracks"][0]["segments"][0]["extra_material_refs"].append("fade")


def test_split_preserves_ordered_coverage_audio_and_unrelated_data():
    draft, spec, _ = fixture()
    before = copy.deepcopy(draft)
    report = get_shared_operation_registry().handlers["split_video_track"](draft, spec, None)
    segments = draft["tracks"][0]["segments"]
    assert [s["id"] for s in segments] == spec["segment_ids"]
    assert [s["source_timerange"] for s in segments] == [
        {"start": 0, "duration": 300000}, {"start": 300000, "duration": 300000}, {"start": 600000, "duration": 400000}]
    assert [s["target_timerange"] for s in segments] == [
        {"start": 100000, "duration": 300000}, {"start": 400000, "duration": 300000}, {"start": 700000, "duration": 400000}]
    for segment in segments:
        restored = copy.deepcopy(segment)
        for key in ("id", "source_timerange", "target_timerange"):
            restored[key] = before["tracks"][0]["segments"][0][key]
        assert restored == before["tracks"][0]["segments"][0]
    assert draft["tracks"][1:] == before["tracks"][1:]
    assert draft["materials"] == before["materials"]
    assert draft["duration"] == before["duration"]
    assert report["audio_continuity_preserved"] is True
    assert report["native_visual_qa"] == "pending"


def test_only_original_audio_endpoints_keep_fades():
    draft, spec, _ = fixture(); add_fade(draft)
    before = copy.deepcopy(draft)
    split_video_track(draft, spec, None)
    fades = {m["id"]: m for m in draft["materials"]["audio_fades"]}
    segments = draft["tracks"][0]["segments"]
    assert fades["fade"] == before["materials"]["audio_fades"][0]
    assert segments[1]["extra_material_refs"] == ["speed"]
    first = fades[segments[0]["extra_material_refs"][-1]]
    last = fades[segments[2]["extra_material_refs"][-1]]
    assert (first["fade_in_duration"], first["fade_out_duration"]) == (30000, 0)
    assert (last["fade_in_duration"], last["fade_out_duration"]) == (0, 30000)
    assert first["id"] != last["id"]
    assert all(s["volume"] == .7 for s in segments)
    assert draft["tracks"][1:] == before["tracks"][1:]
    # Same frozen operation creates identical structural output on a clean base.
    repeat = copy.deepcopy(before); split_video_track(repeat, spec, None)
    assert repeat == draft


def multi_segment_fixture(with_fade=False):
    draft, spec, _ = fixture()
    if with_fade:
        add_fade(draft)
    selected = draft["tracks"][0]["segments"][0]
    selected["source_timerange"]["start"] = 9_000_000
    selected["target_timerange"]["start"] = 2_000_000
    before, after = copy.deepcopy(selected), copy.deepcopy(selected)
    before.update(id="previous", source_timerange={"start": 4_000_000, "duration": 2_000_000},
                  target_timerange={"start": 0, "duration": 2_000_000})
    after.update(id="next", source_timerange={"start": 20_000_000, "duration": 1_000_000},
                 target_timerange={"start": 3_000_000, "duration": 1_000_000})
    # Neighbor-local motion is unrelated to splitting the selected clean segment.
    before["common_keyframes"] = [{"property_type": "KFTypeScaleX"}]
    draft["tracks"][0]["segments"] = [before, selected, after]
    draft["duration"] = 4_000_000
    spec["cut_points_us"] = [2_030_000, 2_970_000]
    return draft, spec


@pytest.mark.parametrize("with_fade", [False, True])
def test_split_middle_nonzero_source_preserves_neighbors_and_endpoint_fades(with_fade):
    draft, spec = multi_segment_fixture(with_fade)
    original = copy.deepcopy(draft)
    report = split_video_track(draft, spec, None)
    segments = draft["tracks"][0]["segments"]
    assert [s["id"] for s in segments] == ["previous", "begin", "middle", "end", "next"]
    assert segments[0] == original["tracks"][0]["segments"][0]
    assert segments[-1] == original["tracks"][0]["segments"][-1]
    assert [s["source_timerange"] for s in segments[1:-1]] == [
        {"start": 9_000_000, "duration": 30_000},
        {"start": 9_030_000, "duration": 940_000},
        {"start": 9_970_000, "duration": 30_000}]
    assert [s["target_timerange"] for s in segments[1:-1]] == [
        {"start": 2_000_000, "duration": 30_000},
        {"start": 2_030_000, "duration": 940_000},
        {"start": 2_970_000, "duration": 30_000}]
    assert all(s["volume"] == .7 for s in segments)
    assert draft["tracks"][1:] == original["tracks"][1:]
    assert draft["duration"] == original["duration"]
    assert report["source_range_us"] == [9_000_000, 10_000_000]
    assert report["target_range_us"] == [2_000_000, 3_000_000]
    if with_fade:
        fades = {m["id"]: m for m in draft["materials"]["audio_fades"]}
        assert fades["fade"] == original["materials"]["audio_fades"][0]
        assert segments[2]["extra_material_refs"] == ["speed"]
        first, last = (fades[s["extra_material_refs"][-1]] for s in (segments[1], segments[3]))
        assert (first["fade_in_duration"], first["fade_out_duration"]) == (30_000, 0)
        assert (last["fade_in_duration"], last["fade_out_duration"]) == (0, 30_000)
        assert len(fades) == 3
    else:
        assert draft["materials"] == original["materials"]


@pytest.mark.parametrize("cuts", [[2_000_000], [3_000_000], [2_029_999], [2_970_001]])
def test_multisegment_split_rejects_selected_range_or_fade_intersection(cuts):
    draft, spec = multi_segment_fixture(with_fade=True)
    spec.update(cut_points_us=cuts, segment_ids=["begin", "end"])
    original = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        split_video_track(draft, spec, None)
    assert draft == original


def test_local_motion_then_mask_does_not_affect_other_segments(tmp_path):
    draft, split, motion = fixture()
    _, mask = mask_fixture(tmp_path)
    mask.update(track_name="JY_ROUGH_CUT_VIDEO", segment_id="middle")
    split_video_track(draft, split, None)
    before = copy.deepcopy(draft)
    registry = get_shared_operation_registry()
    registry.handlers["animate_aroll_transform"](draft, motion, None)
    registry.handlers["apply_video_mask"](draft, mask, None)
    segments = draft["tracks"][0]["segments"]
    assert segments[0] == before["tracks"][0]["segments"][0]
    assert segments[2] == before["tracks"][0]["segments"][2]
    middle = segments[1]
    assert len(middle["common_keyframes"]) == 4
    assert all([k["time_offset"] for k in group["keyframe_list"]] == [0,100000,300000] for group in middle["common_keyframes"])
    assert middle["source_timerange"] == before["tracks"][0]["segments"][1]["source_timerange"]
    assert middle["target_timerange"] == before["tracks"][0]["segments"][1]["target_timerange"]
    mask_id = draft["materials"]["common_mask"][0]["id"]
    assert [mask_id in s["extra_material_refs"] for s in segments] == [False, True, False]
    assert middle["volume"] == .7


@pytest.mark.parametrize("case", ["kf", "volume_kf", "animation", "speed", "speed_material", "curve", "reverse", "range", "offset", "duplicate_track", "multiple", "unknown_id", "bad_ids", "id_collision", "cut_before", "cut_end", "cut_duplicate", "cut_float", "cut_bool", "missing_ref", "transition", "mask", "fade_cut", "fade_unknown", "fade_type"])
def test_unsafe_split_rejected_without_mutation(case):
    draft, spec, _ = fixture(); s = draft["tracks"][0]["segments"][0]
    if case in ("kf", "volume_kf"): s["common_keyframes"] = [{"property_type": "KFTypeScaleX" if case == "kf" else "KFTypeVolume"}]
    elif case == "animation": s["animations"] = ["in"]
    elif case == "speed": s["speed"] = 1.1
    elif case == "speed_material": draft["materials"]["speeds"][0]["speed"] = 2
    elif case == "curve": draft["materials"]["speeds"][0]["curve_speed"] = {"points": [1]}
    elif case == "reverse": s["reverse"] = True
    elif case == "range": s["source_timerange"]["duration"] -= 1
    elif case == "offset": s["source_timerange"]["start"] = -1
    elif case == "duplicate_track": draft["tracks"].append(copy.deepcopy(draft["tracks"][0]))
    elif case == "multiple": draft["tracks"][0]["segments"].append(copy.deepcopy(s))
    elif case == "unknown_id": spec["segment_id"] = "other"
    elif case == "bad_ids": spec["segment_ids"] = ["same"] * 3
    elif case == "id_collision": draft["tracks"][1]["segments"][0]["id"] = "middle"
    elif case == "cut_before": spec["cut_points_us"][0] = 100000
    elif case == "cut_end": spec["cut_points_us"][-1] = 1100000
    elif case == "cut_duplicate": spec["cut_points_us"][-1] = 400000
    elif case == "cut_float": spec["cut_points_us"][0] = 400000.5
    elif case == "cut_bool": spec["cut_points_us"][0] = True
    elif case == "missing_ref": s["extra_material_refs"].append("missing")
    elif case in ("transition", "mask"):
        draft["materials"]["transitions" if case == "transition" else "common_mask"] = [{"id": "complex", "type": case}]
        s["extra_material_refs"].append("complex")
    elif case.startswith("fade"):
        add_fade(draft)
        if case == "fade_cut": spec["cut_points_us"][0] = 110000
        elif case == "fade_unknown": draft["materials"]["audio_fades"][0]["curve"] = [0, 1]
        elif case == "fade_type": draft["materials"]["audio_fades"][0]["fade_type"] = 1
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError): split_video_track(draft, spec, None)
    assert draft == before


@pytest.mark.parametrize("case", ["ambiguous", "missing", "beyond_local", "mismatched_duration"])
def test_segment_motion_invalid_input_preserves_draft(case):
    draft, split, motion = fixture(); split_video_track(draft, split, None)
    if case == "ambiguous": draft["tracks"][0]["segments"].append(copy.deepcopy(draft["tracks"][0]["segments"][1]))
    elif case == "missing": motion["segment_id"] = "missing"
    elif case == "beyond_local": motion["keyframes"][-1]["time_offset_us"] = 400000
    elif case == "mismatched_duration": draft["tracks"][0]["segments"][1]["source_timerange"]["duration"] -= 1
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError): animate_aroll_transform(draft, motion, None)
    assert draft == before


def test_candidate_assembler_split_motion_mask_chain(tmp_path: Path, monkeypatch):
    plan_path = _plan(tmp_path); plan = json.loads(plan_path.read_text(encoding="utf-8"))
    base_path = Path(plan["base_draft"]); base = json.loads(base_path.read_text(encoding="utf-8"))
    draft, split, motion = fixture(); add_fade(draft)
    _, mask = mask_fixture(tmp_path); mask.update(track_name="JY_ROUGH_CUT_VIDEO", segment_id="middle")
    base["tracks"].insert(0, draft["tracks"][0]); base["materials"].update(draft["materials"])
    plan["operations"][:0] = [split, motion, mask]
    _write(base_path, base); _write(plan_path, plan)
    monkeypatch.setattr(candidate_plan, "exact_process_identifier", lambda _path: {"ok": True})
    result = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=shared_final_validator).preview()
    built = json.loads(Path(result["candidate"]).read_text(encoding="utf-8"))
    assert len(built["tracks"][0]["segments"]) == 3
    assert len(built["tracks"][0]["segments"][1]["common_keyframes"]) == 4
    assert len(built["materials"]["common_mask"]) == 1
    assert json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["writer_allowed"] is False
