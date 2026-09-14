from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from jianying_adapter.preset_trial import (
    _collect_material_entries,
    _material_index,
    TrialDependencyError,
    TrialValidationError,
    UnsupportedTrackError,
    build_trial_draft,
    collect_material_dependencies,
    extract_inner_draft,
    normalize_native_zero_offsets,
    validate_trial_draft,
)
from jianying_adapter.preset_registry import _extract_slots


def _outer(
    *,
    prefix: str = "A",
    with_audio: bool = False,
    with_extra_dependency: bool = False,
    unsupported: bool = False,
    mac_path: bool = False,
) -> dict:
    video_id = f"{prefix}-VIDEO-MAT"
    text_id = f"{prefix}-TEXT-MAT"
    audio_id = f"{prefix}-AUDIO-MAT"
    effect_id = f"{prefix}-EFFECT"
    video_segment_id = f"{prefix}-VIDEO-SEG"
    text_segment_id = f"{prefix}-TEXT-SEG"
    audio_segment_id = f"{prefix}-AUDIO-SEG"
    font_path = "/Users/YOUR_USER/Library/Fonts/demo.ttf" if mac_path else ""
    text = "默认字幕"
    text_material = {
        "id": text_id,
        "text": text,
        "font_name": "宋体",
        "font_path": font_path,
        "content": json.dumps(
            {"text": text, "styles": [{"range": [0, len(text)], "size": 32}]},
            ensure_ascii=False,
        ),
    }
    materials = {
        "videos": [{"id": video_id, "path": "video.mp4", "type": "video"}],
        "texts": [text_material],
        "audios": [],
        "effects": [],
    }
    tracks = [
        {
            "id": f"{prefix}-VIDEO-TRACK",
            "type": "video",
            "segments": [
                {
                    "id": video_segment_id,
                    "material_id": video_id,
                    "target_timerange": {"start": 0, "duration": 2_000_000},
                }
            ],
        },
        {
            "id": f"{prefix}-TEXT-TRACK",
            "type": "text",
            "segments": [
                {
                    "id": text_segment_id,
                    "material_id": text_id,
                    "extra_material_refs": [effect_id] if with_extra_dependency else [],
                    "target_timerange": {"start": 500_000, "duration": 1_000_000},
                }
            ],
        },
    ]
    if with_extra_dependency:
        materials["effects"].append({"id": effect_id, "type": "effect", "name": "demo"})
    if with_audio:
        materials["audios"] = [{"id": audio_id, "path": "sound.mp3", "type": "audio", "duration": 300_000}]
        tracks.append(
            {
                "id": f"{prefix}-AUDIO-TRACK",
                "type": "audio",
                "segments": [
                    {
                        "id": audio_segment_id,
                        "material_id": audio_id,
                        "target_timerange": {"start": 1_000_000, "duration": 300_000},
                    }
                ],
            }
        )
    if unsupported:
        tracks.append(
            {
                "id": f"{prefix}-STICKER-TRACK",
                "type": "sticker",
                "segments": [],
            }
        )
    inner = {
        "id": f"{prefix}-INNER",
        "duration": 2_000_000,
        "materials": materials,
        "tracks": tracks,
    }
    return {
        "id": f"{prefix}-OUTER",
        "materials": {"drafts": [{"id": f"{prefix}-WRAPPER", "draft": inner}]},
    }


def _candidate(outer: dict) -> dict:
    inner = outer["materials"]["drafts"][0]["draft"]
    return {
        "template_id": "TEST-01",
        "source_path": "unused.json",
        "slots": _extract_slots(outer, "普通字幕"),
    }


def _base() -> dict:
    return {
        "id": "BASE-INNER",
        "duration": 5_000_000,
        "materials": {"videos": [], "texts": [], "audios": [], "effects": []},
        "tracks": [],
    }


def _filled_values(candidate: dict, value: str = "替换后的字幕") -> dict:
    return {
        "text_slots": {
            slot["slot_id"]: value
            for slot in candidate["slots"]["text"]
            if slot.get("required")
        }
    }


def test_extracts_inner_draft_without_mutating_outer():
    outer = _outer()
    inner = extract_inner_draft(outer)
    inner["duration"] = 99
    assert outer["materials"]["drafts"][0]["draft"]["duration"] == 2_000_000


def test_native_88_saved_zero_offsets_reimport_without_losing_motion(tmp_path):
    outer = _outer()
    inner = outer["materials"]["drafts"][0]["draft"]
    inner["platform"] = inner["last_modified_platform"] = {"app_version": "8.8.0"}
    segment = inner["tracks"][1]["segments"][0]
    segment["target_timerange"].pop("start")
    segment["extra_material_refs"] = ["native-animations"]
    segment["common_keyframes"] = [{"id": "keyframe-group", "property_type": "KFTypeScaleX",
        "keyframe_list": [{"id": "zero-point", "values": [0.8]},
                          {"id": "end-point", "time_offset": 600000, "values": [1.0]}]}]
    inner["materials"]["material_animations"] = [{"id": "native-animations",
        "type": "sticker_animation", "animations": [
            {"id": "native-intro", "type": "in", "duration": 200000},
            {"id": "native-loop", "type": "loop", "start": 200000, "duration": 600000},
            {"id": "native-outro", "type": "out", "start": 800000, "duration": 200000}]}]
    inner["tracks"][0]["segments"][0]["target_timerange"].pop("start")
    inner["tracks"][0]["segments"][0]["source_timerange"] = {"duration": 2000000}
    before = copy.deepcopy(outer)
    candidate = _candidate(outer)
    source_video = tmp_path / "video.mp4"
    source_video.write_bytes(b"synthetic video fixture")
    result = build_trial_draft(_base(), {"candidates": [candidate]}, "TEST-01",
                              _filled_values(candidate), outer_preset=outer, offset=1000000,
                              path_map={"video.mp4": source_video})
    assert result.validation.ok
    assert outer == before
    imported = result.draft["tracks"][1]["segments"][0]
    assert imported["target_timerange"] == {"start": 1000000, "duration": 1000000}
    points = imported["common_keyframes"][0]["keyframe_list"]
    assert [(p["time_offset"], p["values"]) for p in points] == [(0, [0.8]), (600000, [1.0])]
    animations = result.draft["materials"]["material_animations"][0]["animations"]
    assert [(a["type"], a["start"], a["duration"]) for a in animations] == [
        ("in", 0, 200000), ("loop", 200000, 600000), ("out", 800000, 200000)]
    assert result.draft["tracks"][0]["segments"][0]["source_timerange"] == {"start": 0, "duration": 2000000}


@pytest.mark.parametrize("version", [None, "8.7.0", "11.4.0"])
def test_zero_offset_expansion_requires_observed_native_version(version):
    inner = {"platform": {"app_version": version}, "last_modified_platform": {"app_version": version},
             "tracks": [{"segments": [{"target_timerange": {"duration": 1000000}}]}]}
    before = copy.deepcopy(inner)
    assert normalize_native_zero_offsets(inner) == []
    assert inner == before


def test_zero_offset_expansion_does_not_repair_invalid_explicit_values():
    inner = {"platform": {"app_version": "8.8.0"}, "last_modified_platform": {"app_version": "8.8.0"},
             "tracks": [{"segments": [{"target_timerange": {"start": None, "duration": 1000000}},
                                      {"target_timerange": {}}]}]}
    before = copy.deepcopy(inner)
    assert normalize_native_zero_offsets(inner) == []
    assert inner == before


def test_single_template_fills_slots_collects_transitive_dependencies_and_offsets(tmp_path: Path):
    outer = _outer(with_extra_dependency=True)
    candidate = _candidate(outer)
    source_video = tmp_path / "video.mp4"
    source_video.write_bytes(b"video")
    values = _filled_values(candidate)
    result = build_trial_draft(
        _base(),
        {"candidates": [candidate]},
        "TEST-01",
        values,
        outer_preset=outer,
        offset=2_000_000,
        path_map={"video.mp4": source_video},
    )
    assert result.validation.ok
    assert result.report["imported_material_count"] == 3
    assert result.draft["duration"] == 5_000_000
    text = result.draft["materials"]["texts"][0]
    assert text["text"] == "替换后的字幕"
    segment = result.draft["tracks"][1]["segments"][0]
    assert segment["target_timerange"]["start"] == 2_500_000
    assert segment["material_id"] == text["id"]
    assert result.draft["tracks"][1]["segments"][0]["extra_material_refs"]
    assert outer["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["text"] == "默认字幕"


def test_two_templates_from_same_source_get_noncolliding_ids(tmp_path: Path):
    outer_a = _outer(prefix="A")
    outer_b = _outer(prefix="A")
    candidate_a = _candidate(outer_a)
    candidate_b = dict(candidate_a, template_id="TEST-02", slots=candidate_a["slots"])
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    base = _base()
    first = build_trial_draft(
        base,
        {"candidates": [candidate_a]},
        "TEST-01",
        _filled_values(candidate_a, "第一段"),
        outer_preset=outer_a,
        path_map={"video.mp4": video},
    )
    second = build_trial_draft(
        first.draft,
        {"candidates": [candidate_b]},
        "TEST-02",
        _filled_values(candidate_b, "第二段"),
        outer_preset=outer_b,
        path_map={"video.mp4": video},
    )
    ids = [
        item["id"]
        for item in second.draft["materials"]["videos"] + second.draft["materials"]["texts"]
    ]
    ids.extend(track["id"] for track in second.draft["tracks"])
    ids.extend(segment["id"] for track in second.draft["tracks"] for segment in track["segments"])
    assert len(ids) == len(set(ids))
    assert len(second.draft["tracks"]) == 4


def test_repeated_import_remaps_nested_keyframe_ids_and_preserves_animation(
    tmp_path: Path,
):
    """Nested animation IDs must be isolated just like track/segment IDs."""

    def with_animation(outer: dict) -> dict:
        segment = outer["materials"]["drafts"][0]["draft"]["tracks"][1]["segments"][0]
        segment["common_keyframes"] = [
            {
                # Deliberately collides with a track ID in the source.  This
                # is the failure shape seen when a later preset's nested
                # keyframe ID is left in the merged draft.
                "id": "A-VIDEO-TRACK",
                "property_type": "KFTypeGlobalAlpha",
                "keyframe_list": [
                    {
                        "id": "POINT-SHARED",
                        "keyframe_id": "POINT-SHARED",
                        "curveType": "Line",
                        "time_offset": 250_000,
                        "values": [0.25],
                    },
                    {
                        "id": "POINT-SHARED-2",
                        "curveType": "Line",
                        "time_offset": 500_000,
                        "values": [1.0],
                    },
                ],
            }
        ]
        return outer

    outer_a = with_animation(_outer(prefix="A"))
    outer_b = with_animation(_outer(prefix="A"))
    candidate_a = _candidate(outer_a)
    candidate_b = dict(candidate_a, template_id="TEST-02", slots=candidate_a["slots"])
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    first = build_trial_draft(
        _base(),
        {"candidates": [candidate_a]},
        "TEST-01",
        _filled_values(candidate_a, "第一段"),
        outer_preset=outer_a,
        path_map={"video.mp4": video},
    )
    second = build_trial_draft(
        first.draft,
        {"candidates": [candidate_b]},
        "TEST-02",
        _filled_values(candidate_b, "第二段"),
        outer_preset=outer_b,
        path_map={"video.mp4": video},
    )

    first_keyframes = first.draft["tracks"][1]["segments"][0]["common_keyframes"]
    second_keyframes = second.draft["tracks"][3]["segments"][0]["common_keyframes"]
    assert first_keyframes[0]["id"] != second_keyframes[0]["id"]
    assert first_keyframes[0]["id"] != first.draft["tracks"][0]["id"]
    assert second_keyframes[0]["id"] != second.draft["tracks"][0]["id"]
    assert first_keyframes[0]["keyframe_list"][0]["id"] != second_keyframes[0]["keyframe_list"][0]["id"]
    assert second_keyframes[0]["keyframe_list"][0]["keyframe_id"] == second_keyframes[0]["keyframe_list"][0]["id"]
    assert second_keyframes[0]["property_type"] == "KFTypeGlobalAlpha"
    assert [item["time_offset"] for item in second_keyframes[0]["keyframe_list"]] == [250_000, 500_000]
    assert [item["values"] for item in second_keyframes[0]["keyframe_list"]] == [[0.25], [1.0]]
    assert validate_trial_draft(second.draft).ok


@pytest.mark.parametrize("effect_id", ["7041836555903701540", "27144470"])
def test_external_animation_identity_is_preserved_while_internal_ids_are_remapped(tmp_path: Path, effect_id: str):
    outer = _outer()
    inner = outer["materials"]["drafts"][0]["draft"]
    animation_material_id = "A-ANIMATION-MAT"
    resource_id = "7041836555903701540"
    inner["materials"]["material_animations"] = [
        {
            "id": animation_material_id,
            "type": "sticker_animation",
            "animations": [
                {
                    "id": effect_id,
                    "material_type": "sticker",
                    "resource_id": resource_id,
                    "third_resource_id": resource_id,
                    "name": "缩小 II",
                    "type": "in",
                    "path": "",
                },
                {
                    "id": effect_id,
                    "material_type": "sticker",
                    "resource_id": resource_id,
                    "third_resource_id": resource_id,
                    "name": "缩小 II",
                    "type": "in",
                    "path": "",
                }
            ],
        }
    ]
    source_track = inner["tracks"][1]
    source_segment = source_track["segments"][0]
    source_segment["extra_material_refs"] = [animation_material_id]
    candidate = _candidate(outer)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    result = build_trial_draft(
        _base(),
        {"candidates": [candidate]},
        "TEST-01",
        _filled_values(candidate),
        outer_preset=outer,
        path_map={"video.mp4": video},
    )

    animation_material = result.draft["materials"]["material_animations"][0]
    animations = animation_material["animations"]
    assert len(animations) == 2
    assert all(animation["id"] == effect_id for animation in animations)
    assert all(animation["resource_id"] == resource_id for animation in animations)
    assert all(animation["third_resource_id"] == resource_id for animation in animations)
    assert all(animation["id"].isdigit() for animation in animations)
    assert animation_material["id"] != animation_material_id

    imported_track = result.draft["tracks"][1]
    imported_segment = imported_track["segments"][0]
    assert imported_track["id"] != source_track["id"]
    assert imported_segment["id"] != source_segment["id"]
    assert imported_segment["extra_material_refs"] == [animation_material["id"]]
    assert result.validation.ok


def test_validation_ignores_repeated_external_identity_but_reports_internal_duplicate():
    resource_id = "7041836555903701540"
    external = {
        "id": resource_id,
        "resource_id": resource_id,
        "third_resource_id": resource_id,
    }
    draft = {
        "id": "draft",
        "duration": 0,
        "materials": {
            "material_animations": [
                {
                    "id": "animation-material",
                    "animations": [copy.deepcopy(external), copy.deepcopy(external)],
                }
            ]
        },
        "tracks": [],
    }
    assert validate_trial_draft(draft).ok

    draft["tracks"] = [
        {"id": "duplicate-track", "type": "text", "segments": []},
        {"id": "duplicate-track", "type": "text", "segments": []},
    ]
    report = validate_trial_draft(draft)
    assert not report.ok
    assert any(issue.code == "duplicate_id" for issue in report.issues)


def test_material_index_drops_ambiguous_shared_remote_alias_but_keeps_root_ids():
    draft = {
        "materials": {
            "videos": [
                {"id": "clip-a", "material_id": "remote-1", "path": "a.mp4"},
                {"id": "clip-b", "material_id": "remote-1", "path": "a.mp4", "crop": {"x": 1}},
            ],
        },
    }
    index = _material_index(draft)
    assert "clip-a" in index
    assert "clip-b" in index
    assert "remote-1" not in index


def test_shared_remote_alias_is_not_treated_as_transitive_local_dependency():
    draft = {
        "materials": {
            "videos": [
                {"id": "clip-a", "material_id": "remote-1", "path": "a.mp4"},
                {"id": "clip-b", "material_id": "remote-1", "path": "a.mp4", "crop": {"x": 1}},
            ],
        },
    }
    tracks = [{
        "type": "video",
        "segments": [
            {"material_id": "clip-a"},
            {"material_id": "clip-b"},
        ],
    }]
    entries = _collect_material_entries(draft, tracks)
    assert {entry.item["id"] for entry in entries} == {"clip-a", "clip-b"}

    validated = {
        "id": "draft",
        "duration": 1_000_000,
        "materials": draft["materials"],
        "tracks": [{
            "id": "track",
            "type": "video",
            "segments": [
                {"id": "segment-a", "material_id": "clip-a", "target_timerange": {"start": 0, "duration": 1_000_000}},
                {"id": "segment-b", "material_id": "clip-b", "target_timerange": {"start": 0, "duration": 1_000_000}},
            ],
        }],
    }
    report = validate_trial_draft(validated, allowed_missing_paths=["a.mp4"])
    assert not any(issue.code == "duplicate_material_id" for issue in report.issues)


def test_missing_required_slot_is_rejected_before_merge():
    outer = _outer()
    candidate = _candidate(outer)
    with pytest.raises(ValueError, match="required"):
        build_trial_draft(
            _base(),
            {"candidates": [candidate]},
            "TEST-01",
            {},
            outer_preset=outer,
        )


def test_mac_path_is_rejected_but_explicit_map_allows_it(tmp_path: Path):
    outer = _outer(mac_path=True)
    candidate = _candidate(outer)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    replacement = tmp_path / "demo.ttf"
    replacement.write_bytes(b"font")
    with pytest.raises(TrialDependencyError, match="外部路径"):
        build_trial_draft(
            _base(),
            {"candidates": [candidate]},
            "TEST-01",
            _filled_values(candidate),
            outer_preset=outer,
            path_map={"video.mp4": video},
        )
    result = build_trial_draft(
        _base(),
        {"candidates": [candidate]},
        "TEST-01",
        _filled_values(candidate),
        outer_preset=outer,
        path_map={"video.mp4": video, "/Users/YOUR_USER/Library/Fonts/demo.ttf": replacement},
    )
    assert result.validation.ok
    assert any(item["mapped"] and item["kind"] == "font" for item in result.report["path_dependencies"])


def test_missing_audio_can_only_be_handled_by_explicit_disable_or_map(tmp_path: Path):
    outer = _outer(with_audio=True)
    candidate = _candidate(outer)
    # The video is mapped to an existing fixture below; the first failure must
    # therefore be the audio path, not an unrelated missing video path.
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    with pytest.raises(TrialDependencyError, match="外部路径"):
        build_trial_draft(
            _base(),
            {"candidates": [candidate]},
            "TEST-01",
            _filled_values(candidate),
            outer_preset=outer,
            path_map={"video.mp4": video},
        )
    result = build_trial_draft(
        _base(),
        {"candidates": [candidate]},
        "TEST-01",
        _filled_values(candidate),
        outer_preset=outer,
        disable_audio=True,
        path_map={"video.mp4": video},
        unsupported_track_policy="skip",
    )
    assert result.report["skipped_tracks"][-1]["reason"] == "audio_disabled"
    assert all(track["type"] != "audio" for track in result.draft["tracks"])


def test_explicit_audio_map_replaces_missing_audio_path(tmp_path: Path):
    outer = _outer(with_audio=True)
    candidate = _candidate(outer)
    video = tmp_path / "video.mp4"
    audio = tmp_path / "sound.mp3"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    result = build_trial_draft(
        _base(),
        {"candidates": [candidate]},
        "TEST-01",
        _filled_values(candidate),
        outer_preset=outer,
        path_map={"video.mp4": video, "sound.mp3": audio},
    )
    assert result.validation.ok
    assert result.draft["materials"]["audios"][0]["path"] == str(audio)


def test_official_sound_rebinds_to_current_jianying_cache_and_keeps_effect_id(
    tmp_path: Path,
):
    outer = _outer(with_audio=True)
    inner = outer["materials"]["drafts"][0]["draft"]
    source_audio = inner["materials"]["audios"][0]
    cache_name = "d6f70643aad342b8b90c7cba16c258aa.mp3"
    source_audio.update(
        {
            "path": f"C:/Users/YOUR_USER/AppData/Local/JianyingPro/User Data/Cache/music/{cache_name}",
            "type": "sound",
            "source_platform": 1,
            "effect_id": "6996393275758316808",
            "name": "打字（字幕专用）",
        }
    )
    candidate = _candidate(outer)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    cache_root = tmp_path / "music"
    cache_root.mkdir()
    cached_audio = cache_root / cache_name
    cached_audio.write_bytes(b"audio")

    result = build_trial_draft(
        _base(),
        {"candidates": [candidate]},
        "TEST-01",
        _filled_values(candidate),
        outer_preset=outer,
        path_map={"video.mp4": video},
        audio_cache_root=cache_root,
    )

    imported = result.draft["materials"]["audios"][0]
    assert imported["path"] == str(cached_audio)
    assert imported["effect_id"] == "6996393275758316808"
    assert imported["id"] != source_audio["id"]
    assert len(result.report["audio_rehydrations"]) == 1
    rehydration = result.report["audio_rehydrations"][0]
    assert rehydration["resolution"] == "jianying_official_audio_cache"
    assert rehydration["pending_rehydrate"] is False
    assert rehydration["effect_id"] == "6996393275758316808"


def test_official_sound_missing_cache_requires_explicit_rehydrate_permission(
    tmp_path: Path,
):
    outer = _outer(with_audio=True)
    source_audio = outer["materials"]["drafts"][0]["draft"]["materials"]["audios"][0]
    cache_name = "917cf0392d65708f2f7f3b2fa7d6c6b0.mp3"
    source_audio.update(
        {
            "path": f"/Users/YOUR_USER/Library/Containers/com.lemon.lvpro/Data/Movies/JianyingPro/User Data/Cache/music/{cache_name}",
            "type": "sound",
            "source_platform": 1,
            "effect_id": "7002502489429789983",
            "name": "页面切换",
        }
    )
    candidate = _candidate(outer)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    cache_root = tmp_path / "music"
    cache_root.mkdir()
    kwargs = {
        "outer_preset": outer,
        "path_map": {"video.mp4": video},
        "audio_cache_root": cache_root,
    }

    with pytest.raises(TrialDependencyError, match="官方音效尚未缓存"):
        build_trial_draft(
            _base(),
            {"candidates": [candidate]},
            "TEST-01",
            _filled_values(candidate),
            **kwargs,
        )

    result = build_trial_draft(
        _base(),
        {"candidates": [candidate]},
        "TEST-01",
        _filled_values(candidate),
        allow_audio_rehydrate=True,
        **kwargs,
    )
    imported = result.draft["materials"]["audios"][0]
    assert imported["path"] == str(cache_root / cache_name)
    assert imported["effect_id"] == "7002502489429789983"
    assert result.validation.ok
    assert result.report["audio_rehydrations"][0]["pending_rehydrate"] is True


def test_unsupported_track_rejects_or_skips_only_when_explicit(tmp_path: Path):
    outer = _outer(unsupported=True)
    candidate = _candidate(outer)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    kwargs = {
        "outer_preset": outer,
        "path_map": {"video.mp4": video},
    }
    with pytest.raises(UnsupportedTrackError, match="不支持"):
        build_trial_draft(
            _base(), {"candidates": [candidate]}, "TEST-01", _filled_values(candidate), **kwargs
        )
    result = build_trial_draft(
        _base(),
        {"candidates": [candidate]},
        "TEST-01",
        _filled_values(candidate),
        unsupported_track_policy="skip",
        **kwargs,
    )
    assert result.report["skipped_tracks"][-1]["type"] == "sticker"
    assert result.validation.ok


def test_native_effect_track_is_supported_and_resolves_video_effect_material():
    draft = _base()
    draft["materials"]["video_effects"] = [{"id": "effect-material", "type": "video_effect"}]
    draft["tracks"].append(
        {
            "id": "effect-track",
            "type": "effect",
            "segments": [
                {
                    "id": "effect-segment",
                    "material_id": "effect-material",
                    "target_timerange": {"start": 1_000_000, "duration": 500_000},
                }
            ],
        }
    )
    assert validate_trial_draft(draft).ok


def test_validation_catches_dangling_refs_duplicate_ids_bad_timerange_and_path(tmp_path: Path):
    draft = {
        "id": "draft",
        "duration": 1_000_000,
        "materials": {"texts": [{"id": "same", "font_path": "missing.ttf"}]},
        "tracks": [
            {
                "id": "track",
                "type": "text",
                "segments": [
                    {
                        "id": "same",
                        "material_id": "missing",
                        "extra_material_refs": ["also-missing"],
                        "target_timerange": {"start": -1, "duration": 2_000_000},
                    }
                ],
            }
        ],
    }
    report = validate_trial_draft(draft, path_base=tmp_path)
    assert not report.ok
    codes = {issue.code for issue in report.issues}
    assert {"duplicate_id", "material_reference_dangling", "timerange_invalid", "forbidden_path"} <= codes


@pytest.mark.parametrize(
    ("material", "expected_kind"),
    [
        (
            {"id": "compound", "type": "video", "material_name": "复合片段1", "path": "", "media_path": ""},
            "empty_path_compound_clip",
        ),
        (
            {
                "id": "online",
                "type": "video",
                "material_name": "萌宠动物搞笑瞬间",
                "source_platform": 1,
                "path": "",
                "media_path": "",
                "material_id": "online-remote-id",
            },
            "empty_path_online_media",
        ),
        (
            {"id": "transparent", "type": "photo", "material_name": "透明", "path": "", "media_path": ""},
            "empty_path_transparent_photo",
        ),
    ],
)
def test_final_visual_gate_reports_empty_path_material_with_segment_time(
    material: dict, expected_kind: str
) -> None:
    draft = {
        "id": "draft",
        "duration": 5_000_000,
        "materials": {"videos": [material]},
        "tracks": [
            {
                "id": "visual-track",
                "type": "video",
                "segments": [
                    {
                        "id": "visual-segment",
                        "material_id": material["id"],
                        "target_timerange": {"start": 1_250_000, "duration": 900_000},
                    }
                ],
            }
        ],
    }
    report = validate_trial_draft(draft)
    assert not report.ok
    assert any(issue.code == "visual_material_pending" for issue in report.issues)
    pending = report.facts["pending_visual_materials"]
    assert len(pending) == 1
    assert pending[0]["missing_kind"] == expected_kind
    assert pending[0]["material_name"] == material.get("material_name")
    assert pending[0]["target_timerange"] == {"start": 1_250_000, "duration": 900_000}


def test_final_visual_gate_keeps_existing_local_visual_material_valid(tmp_path: Path) -> None:
    media = tmp_path / "normal.mp4"
    media.write_bytes(b"video")
    draft = {
        "id": "draft",
        "duration": 5_000_000,
        "materials": {"videos": [{"id": "normal", "type": "video", "path": str(media)}]},
        "tracks": [
            {
                "id": "visual-track",
                "type": "video",
                "segments": [
                    {
                        "id": "visual-segment",
                        "material_id": "normal",
                        "target_timerange": {"start": 0, "duration": 900_000},
                    }
                ],
            }
        ],
    }
    report = validate_trial_draft(draft)
    assert report.ok
    assert report.facts["pending_visual_materials"] == []


def test_same_material_id_and_id_alias_on_one_material_is_not_a_collision(tmp_path: Path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    draft = {
        "id": "draft",
        "duration": 1_000_000,
        "materials": {"videos": [{"id": "material", "material_id": "material", "path": str(media)}]},
        "tracks": [
            {
                "id": "track",
                "type": "video",
                "segments": [
                    {
                        "id": "segment",
                        "material_id": "material",
                        "target_timerange": {"start": 0, "duration": 1_000_000},
                    }
                ],
            }
        ],
    }
    assert validate_trial_draft(draft).ok


def test_dependency_helper_reports_material_and_transitive_effect():
    outer = _outer(with_extra_dependency=True)
    inner = extract_inner_draft(outer)
    facts = collect_material_dependencies(inner, inner["tracks"])
    assert {item["group"] for item in facts} == {"videos", "texts", "effects"}


def test_source_and_base_are_not_mutated_by_failed_or_successful_build(tmp_path: Path):
    outer = _outer()
    candidate = _candidate(outer)
    base = _base()
    outer_before = copy.deepcopy(outer)
    base_before = copy.deepcopy(base)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    build_trial_draft(
        base,
        {"candidates": [candidate]},
        "TEST-01",
        _filled_values(candidate),
        outer_preset=outer,
        path_map={"video.mp4": video},
    )
    assert outer == outer_before
    assert base == base_before
