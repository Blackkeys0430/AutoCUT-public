from __future__ import annotations

import importlib.util
import itertools
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from jianying_adapter.preset_registry import apply_template_slots
from jianying_adapter.preset_trial import validate_trial_draft


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "build_five_template_trial.py"


def test_explicit_slot_copy_never_inserts_demo_text(trial_module):
    candidate = {"template_id": "T", "slots": {"text": [
        {"slot_id": "body", "required": True},
        {"slot_id": "quote", "required": True, "default_text": "“"},
    ]}}
    with pytest.raises(trial_module.FiveTemplateTrialError, match="禁止补入示例文案"):
        trial_module._generate_slot_values(candidate, {"T": ("当前原话",)}, strict=True)
    candidate["slots"]["text"][1]["decorative_locked"] = True
    values, skipped = trial_module._generate_slot_values(candidate, {"T": ("当前原话",)}, strict=True)
    assert values["text_slots"] == {"body": "当前原话"}
    assert skipped[0]["default_text"] == "“"


@pytest.fixture(scope="module")
def trial_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_five_template_trial", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _content(text: str, *, size: int = 38, fill: dict[str, Any] | None = None) -> str:
    style: dict[str, Any] = {
        "range": [0, len(text)],
        "size": size,
        "fill": fill or {"color": [1.0, 1.0, 1.0], "alpha": 1.0},
    }
    return json.dumps({"text": text, "styles": [style]}, ensure_ascii=False)


def test_shared_remote_material_id_is_not_internal_duplicate(trial_module: ModuleType) -> None:
    inner = {
        "materials": {
            "videos": [
                {"id": "clip-a", "material_id": "remote-1", "path": "a.mp4"},
                {"id": "clip-b", "material_id": "remote-1", "path": "a.mp4", "crop": {"x": 1}},
            ],
        },
    }
    removed: list[dict[str, Any]] = []
    trial_module._deduplicate_material_ids(inner, removed)
    assert len(inner["materials"]["videos"]) == 2
    assert removed == []


def test_nonidentical_duplicate_internal_id_still_fails_closed(trial_module: ModuleType) -> None:
    inner = {
        "materials": {
            "videos": [
                {"id": "clip-a", "path": "a.mp4"},
                {"id": "clip-a", "path": "b.mp4"},
            ],
        },
    }
    with pytest.raises(trial_module.FiveTemplateTrialError):
        trial_module._deduplicate_material_ids(inner, [])


def test_native_zero_offsets_expanded_before_sampling(trial_module, tmp_path):
    inner = {"platform": {"app_version": "8.8.0"}, "last_modified_platform": {"app_version": "8.8.0"},
             "duration": 2000000, "materials": {},
             "tracks": [{"type": "text", "segments": [{"target_timerange": {"duration": 2000000},
                 "common_keyframes": [{"keyframe_list": [{"values": [1.0]}]}]}]}]}
    source = {"materials": {"drafts": [{"draft": inner}]}}
    sanitized, report = trial_module._sanitize_source_payload(source, path_map={}, path_base=tmp_path)
    copied = sanitized["materials"]["drafts"][0]["draft"]
    sampled = trial_module._clip_inner_to_sampling_window(copied, max_duration_us=1000000)
    assert sampled["trimmed_segment_count"] == 1
    assert copied["tracks"][0]["segments"][0]["target_timerange"] == {"start": 0, "duration": 1000000}
    assert len(report["native_zero_offsets"]) == 2
    assert "start" not in inner["tracks"][0]["segments"][0]["target_timerange"]


def _inner(
    prefix: str,
    texts: list[str],
    *,
    duration: int,
    media_count: int = 0,
    nested_duplicate: bool = False,
    unsupported_track: bool = False,
) -> dict[str, Any]:
    text_materials: list[dict[str, Any]] = []
    tracks: list[dict[str, Any]] = []
    for index, text in enumerate(texts):
        material_id = f"{prefix}-TEXT-MAT-{index}"
        item: dict[str, Any] = {
            "id": material_id,
            "type": "text",
            "text": text,
            "content": _content(text),
        }
        if nested_duplicate:
            item["animation"] = {
                "id": f"{prefix}-DUPLICATE-NESTED",
                "resource_id": f"{prefix}-DUPLICATE-NESTED",
                "third_resource_id": f"{prefix}-DUPLICATE-NESTED",
                "path": "missing-animation.bin",
            }
        text_materials.append(item)
        tracks.append(
            {
                "id": f"{prefix}-TEXT-TRACK-{index}",
                "type": "text",
                "segments": [
                    {
                        "id": f"{prefix}-TEXT-SEG-{index}",
                        "material_id": material_id,
                        "target_timerange": {
                            "start": min(index * 100_000, max(duration - 200_000, 0)),
                            "duration": min(800_000, duration),
                        },
                    }
                ],
            }
        )

    videos: list[dict[str, Any]] = []
    for index in range(media_count):
        material_id = f"{prefix}-VIDEO-MAT-{index}"
        videos.append(
            {
                "id": material_id,
                "type": "video",
                "path": f"visual-{prefix}-{index}.jpg",
                "duration": duration,
            }
        )
        tracks.append(
            {
                "id": f"{prefix}-VIDEO-TRACK-{index}",
                "type": "video",
                "segments": [
                    {
                        "id": f"{prefix}-VIDEO-SEG-{index}",
                        "material_id": material_id,
                        "target_timerange": {"start": 0, "duration": min(duration, 900_000)},
                    }
                ],
            }
        )
    if unsupported_track:
        tracks.append({"id": f"{prefix}-EFFECT-TRACK", "type": "effect", "segments": []})

    return {
        "id": f"{prefix}-INNER",
        "duration": duration,
        "materials": {"texts": text_materials, "videos": videos, "audios": []},
        "tracks": tracks,
    }


def _text_slot(
    slot_id: str,
    inner_index: int,
    material_index: int,
    material_id: str,
    *,
    required: bool = True,
    decorative_locked: bool = False,
) -> dict[str, Any]:
    return {
        "slot_id": slot_id,
        "kind": "text",
        "required": required,
        "decorative_locked": decorative_locked,
        "requires_manual_slot_mapping": False,
        "requires_manual_style_mapping": False,
        "default_text": "？" if decorative_locked else None,
        "locators": [
            {
                "inner_draft_index": inner_index,
                "material_id": material_id,
                "materials_group": "texts",
                "materials_index": material_index,
                "segment_refs": [{"start": 0, "end": 800_000}],
            }
        ],
    }


def _video_slot(slot_id: str, inner_index: int, material_index: int, material_id: str) -> dict[str, Any]:
    return {
        "slot_id": slot_id,
        "kind": "video",
        "required": True,
        "locators": [
            {
                "inner_draft_index": inner_index,
                "material_id": material_id,
                "materials_group": "videos",
                "materials_index": material_index,
                "segment_refs": [{"start": 0, "end": 900_000}],
            }
        ],
    }


def _candidate(
    template_id: str,
    source_name: str,
    inners: list[dict[str, Any]],
    slots: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = {
        "id": f"{template_id}-OUTER",
        "materials": {
            "drafts": [
                {"id": f"{template_id}-WRAPPER-{index}", "draft": inner}
                for index, inner in enumerate(inners)
            ]
        },
    }
    candidate = {
        "template_id": template_id,
        "display_name": f"T14 {template_id} 试剪预设",
        "source_path": source_name,
        "slots": slots,
    }
    return candidate, source


def _synthetic_registry(tmp_path: Path) -> dict[str, Any]:
    ids = (
        "JIANYING-25-08",
        "JIANYING-25-01",
        "JIANYING-25-16",
        "JIANYING-25-23",
        "JIANYING-25-06",
    )
    candidates: list[dict[str, Any]] = []

    inner_08 = _inner(
        "T08",
        ["我叫 Li Shuang，我從北京來", "20（20s)年以前來到日本", "？"],
        duration=1_000_000,
        nested_duplicate=True,
    )
    inner_08["materials"]["audios"] = [
        {
            "id": "T08-AUDIO-MAT-0",
            "type": "sound",
            "source_platform": 1,
            "effect_id": "6996393275758316808",
            "name": "打字（字幕专用）",
            "path": (
                "C:/Users/YOUR_USER/AppData/Local/JianyingPro/User Data/Cache/music/"
                "d6f70643aad342b8b90c7cba16c258aa.mp3"
            ),
            "duration": 300_000,
        }
    ]
    inner_08["tracks"].append(
        {
            "id": "T08-AUDIO-TRACK-0",
            "type": "audio",
            "segments": [
                {
                    "id": "T08-AUDIO-SEG-0",
                    "material_id": "T08-AUDIO-MAT-0",
                    "target_timerange": {"start": 0, "duration": 300_000},
                }
            ],
        }
    )
    candidates.append(
        _candidate(
            ids[0],
            "preset_08.json",
            [inner_08],
            {
                "text": [
                    _text_slot("text_01", 0, 0, "T08-TEXT-MAT-0"),
                    _text_slot("text_02", 0, 1, "T08-TEXT-MAT-1"),
                    _text_slot("text_03", 0, 2, "T08-TEXT-MAT-2", required=False, decorative_locked=True),
                ],
                "video": [],
                "image": [],
            },
        )[0]
    )

    inner_01 = _inner("T01", ["我從北京來", "來到日本", "兩個兒子", "國際交流"], duration=1_200_000)
    candidates.append(
        _candidate(
            ids[1],
            "preset_01.json",
            [inner_01],
            {
                "text": [
                    _text_slot(f"text_{index:02d}", 0, index - 1, f"T01-TEXT-MAT-{index - 1}")
                    for index in range(1, 5)
                ],
                "video": [],
                "image": [],
            },
        )[0]
    )

    inner_16 = _inner("T16", ["國際交流"], duration=900_000)
    inner_16["materials"]["texts"][0]["content"] = _content(
        "國際交流", size=77, fill={"color": [0.1, 0.3, 0.9], "alpha": 1.0}
    )
    candidates.append(
        _candidate(
            ids[2],
            "preset_16.json",
            [inner_16],
            {
                "text": [_text_slot("text_01", 0, 0, "T16-TEXT-MAT-0")],
                "video": [],
                "image": [],
            },
        )[0]
    )

    inner_23 = _inner("T23", ["他們在上海讀小學", "現在呢，也在日本讀中學"], duration=1_100_000)
    candidates.append(
        _candidate(
            ids[3],
            "preset_23.json",
            [inner_23],
            {
                "text": [
                    _text_slot("text_01", 0, 0, "T23-TEXT-MAT-0"),
                    _text_slot("text_02", 0, 1, "T23-TEXT-MAT-1"),
                ],
                "video": [],
                "image": [],
            },
        )[0]
    )

    inners_06 = [
        _inner("T06A", ["北京", "日本", "先生"], duration=1_000_000, media_count=1),
        _inner("T06B", ["兩個兒子", "小學", "英文"], duration=1_300_000),
        _inner("T06C", ["中學", "日文", "國際交流"], duration=1_500_000, media_count=1, unsupported_track=True),
    ]
    text_slots_06: list[dict[str, Any]] = []
    value_index = 0
    for inner_index, inner in enumerate(inners_06):
        for material_index, item in enumerate(inner["materials"]["texts"]):
            value_index += 1
            text_slots_06.append(
                _text_slot(
                    f"text_{value_index:02d}",
                    inner_index,
                    material_index,
                    item["id"],
                )
            )
    video_slots_06 = [
        _video_slot("video_01", 0, 0, "T06A-VIDEO-MAT-0"),
        _video_slot("video_02", 2, 0, "T06C-VIDEO-MAT-0"),
    ]
    candidates.append(
        _candidate(
            ids[4],
            "preset_06.json",
            inners_06,
            {"text": text_slots_06, "video": video_slots_06, "image": []},
        )[0]
    )

    # Reconstruct the payloads from the same slot material IDs used above.
    payloads = {
        "preset_08.json": {"id": "JIANYING-25-08-OUTER", "materials": {"drafts": [{"id": "W08", "draft": inner_08}]}},
        "preset_01.json": {"id": "JIANYING-25-01-OUTER", "materials": {"drafts": [{"id": "W01", "draft": inner_01}]}},
        "preset_16.json": {"id": "JIANYING-25-16-OUTER", "materials": {"drafts": [{"id": "W16", "draft": inner_16}]}},
        "preset_23.json": {"id": "JIANYING-25-23-OUTER", "materials": {"drafts": [{"id": "W23", "draft": inner_23}]}},
        "preset_06.json": {
            "id": "JIANYING-25-06-OUTER",
            "materials": {
                "drafts": [
                    {"id": "W06A", "draft": inners_06[0]},
                    {"id": "W06B", "draft": inners_06[1]},
                    {"id": "W06C", "draft": inners_06[2]},
                ]
            },
        },
    }
    for filename, payload in payloads.items():
        (tmp_path / filename).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert [candidate["template_id"] for candidate in candidates] == list(ids)
    return {"candidates": candidates}


def _base(duration: int = 20_000_000) -> dict[str, Any]:
    return {
        "id": "T14-BASE-DRAFT",
        "duration": duration,
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"videos": [], "texts": [], "audios": []},
        "tracks": [],
    }


def _id_factory():
    counter = itertools.count(1)
    return lambda: f"TRIAL-GENERATED-{next(counter):04d}"


@pytest.mark.parametrize("defect", [None, "animation_overrun", "keyframe_overrun", "late_segment", "node_overrun"])
def test_shared_preset_clips_terminal_hold_before_base_length_check(tmp_path, trial_module, defect):
    from copy import deepcopy
    from types import SimpleNamespace
    from jianying_adapter.shared_operations import add_preset_group

    inner = _inner("HOLD", ["价差"], duration=3_000_000)
    segment = inner["tracks"][0]["segments"][0]
    segment["target_timerange"] = {"start": 0, "duration": 3_000_000}
    segment["extra_material_refs"] = ["HOLD-ANIM"]
    inner["materials"]["material_animations"] = [{"id": "HOLD-ANIM", "type": "sticker_animation", "animations": [
        {"type": "in", "start": 0, "duration": 500_000}]}]
    if defect == "animation_overrun":
        inner["materials"]["material_animations"][0]["animations"][0]["duration"] = 2_300_000
    if defect == "keyframe_overrun":
        segment["common_keyframes"] = [{"id": "KF-GROUP", "property_type": "KFTypePositionX", "keyframe_list": [
            {"id": "KF-START", "time_offset": 0, "values": [0.]},
            {"id": "KF-END", "time_offset": 2_300_000, "values": [1.]}]}]
    if defect == "late_segment":
        inner["tracks"][0]["segments"].append({**deepcopy(segment), "id": "late",
            "target_timerange": {"start": 2_500_000, "duration": 500_000}})
    candidate, source = _candidate("T", "hold.json", [inner], {
        "text": [_text_slot("body", 0, 0, "HOLD-TEXT-MAT-0")], "video": [], "image": []})
    (tmp_path / "hold.json").write_text(json.dumps(source), "utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"candidates": [candidate]}), "utf-8")
    base = _base(9_120_000)
    before = deepcopy(base)
    spec = {"template_id": "T", "node_id": "price", "start_us": 6_920_000,
            "end_us": 9_120_001 if defect == "node_overrun" else 9_120_000, "slots": ["价差"]}
    context = SimpleNamespace(plan_path=tmp_path / "plan.json", plan={
        "preset_registry": str(registry), "preset_root": str(tmp_path),
        "presets": [{"template_id": "T", "node_id": "price"}]})
    if defect is not None:
        message = {"animation_overrun": "animation exceeds", "keyframe_overrun": "keyframe exceeds",
                   "late_segment": "drop source segments", "node_overrun": "base duration"}[defect]
        with pytest.raises(ValueError, match=message):
            add_preset_group(base, spec, context)
    else:
        # This is the actual old failing order: untrimmed 3s cannot fit at 6.92s.
        with pytest.raises(trial_module.FiveTemplateTrialError, match="所选模板放不进"):
            trial_module.build_five_template_trial(base, registry, tmp_path, (), template_ids=("T",),
                template_offsets_us={"T": 6_920_000}, slot_copy_table={"T": ("价差",)})
        result, _ = add_preset_group(base, spec, context)
        assert result["duration"] == 9_120_000
        assert result["tracks"][0]["segments"][0]["target_timerange"] == {"start": 6_920_000, "duration": 2_200_000}
        animation = result["materials"]["material_animations"][0]["animations"][0]
        assert animation["duration"] == 500_000 and animation["start"] == 0
    assert base == before


def test_five_templates_are_imported_in_order_and_stay_inside_base(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    media = tmp_path / "T14_actual_frame_08.jpg"
    media.write_bytes(b"approved T14 frame")

    result = trial_module.build_five_template_trial(
        _base(),
        registry,
        tmp_path,
        [media],
        id_factory=_id_factory(),
    )
    manifest = result["manifest"]
    blocks = manifest["templates"]

    assert manifest["template_ids"] == list(trial_module.FIVE_TEMPLATE_IDS)
    assert manifest["disable_audio"] is True
    assert manifest["base_duration_us"] == 20_000_000
    assert manifest["trial_start_us"] == 3_500_000
    assert manifest["final_template_end_us"] == blocks[-1]["end_us"]
    assert all(block["imported_track_count"] > 0 for block in blocks)
    assert all(block["end_us"] <= manifest["base_duration_us"] for block in blocks)
    assert all(
        current["start_us"] >= previous["end_us"] + manifest["gap_us"]
        for previous, current in zip(blocks, blocks[1:])
    )
    assert manifest["structure_validation"]["template_blocks_non_overlapping"] is True
    assert validate_trial_draft(result["draft"], path_base=tmp_path).ok

    resource_id = "T08-DUPLICATE-NESTED"
    repaired = blocks[0]["sanitization"]["repaired_ids"]
    assert not any(item["original_id"] == resource_id for item in repaired)
    external_nodes = [
        item["animation"]
        for item in result["draft"]["materials"]["texts"]
        if isinstance(item.get("animation"), dict)
        and item["animation"].get("resource_id") == resource_id
    ]
    assert len(external_nodes) == 3
    assert all(node["id"] == resource_id for node in external_nodes)
    assert all(node["resource_id"] == resource_id for node in external_nodes)
    assert all(node["third_resource_id"] == resource_id for node in external_nodes)


def test_five_template_entry_rebinds_official_sfx_to_current_cache(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    cache_root = tmp_path / "music"
    cache_root.mkdir()
    cache_file = cache_root / "d6f70643aad342b8b90c7cba16c258aa.mp3"
    cache_file.write_bytes(b"audio")

    result = trial_module.build_five_template_trial(
        _base(),
        registry,
        tmp_path,
        [],
        disable_audio=False,
        audio_cache_root=cache_root,
        template_ids=("JIANYING-25-08",),
        id_factory=_id_factory(),
    )

    audios = result["draft"]["materials"]["audios"]
    assert len(audios) == 1
    assert audios[0]["path"] == str(cache_file)
    assert audios[0]["effect_id"] == "6996393275758316808"
    resolved = result["manifest"]["audio_rehydrate"]["resolved"]
    assert len(resolved) == 1
    assert resolved[0]["pending_rehydrate"] is False


def test_five_template_entry_accepts_caller_slot_copy(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    result = trial_module.build_five_template_trial(
        _base(),
        registry,
        tmp_path,
        [],
        template_ids=("JIANYING-25-16",),
        slot_copy_table={"JIANYING-25-16": ("当前视频金句",)},
        id_factory=_id_factory(),
    )

    texts = [
        json.loads(item["content"])["text"]
        for item in result["draft"]["materials"]["texts"]
        if isinstance(item.get("content"), str)
    ]
    assert "当前视频金句" in texts
    assert result["manifest"]["slot_copy_source"] == "caller"


def test_five_template_entry_preserves_existing_animation_package_without_path_map(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    package = tmp_path / "frozen-animation"
    package.mkdir()
    (package / "config.json").write_text("{}", encoding="utf-8")
    source_path = tmp_path / "preset_08.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    inner = source["materials"]["drafts"][0]["draft"]
    inner["materials"]["material_animations"] = [{
        "id": "local-animation", "animations": [{
            "path": str(package), "resource_id": "native-resource",
            "type": "in", "start": 0, "duration": 300_000,
        }],
    }]
    inner["tracks"][0]["segments"][0]["extra_material_refs"] = ["local-animation"]
    source_path.write_text(json.dumps(source), encoding="utf-8")
    result = trial_module.build_five_template_trial(
        _base(), registry, tmp_path, [], template_ids=("JIANYING-25-08",),
        disable_audio=True, id_factory=_id_factory(),
    )
    animations = result["draft"]["materials"]["material_animations"]
    assert len(animations) == 1
    assert animations[0]["animations"][0]["path"] == str(package)
    assert animations[0]["animations"][0]["duration"] == 300_000


def test_five_template_entry_rebinds_visual_package_to_current_machine(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    package = tmp_path / "current-cache" / "effect-package"
    package.mkdir(parents=True)
    (package / "config.json").write_text("{}", encoding="utf-8")

    result = trial_module.build_five_template_trial(
        _base(),
        registry,
        tmp_path,
        [],
        template_ids=("JIANYING-25-08",),
        preserve_visual_resource_refs=True,
        visual_path_maps={
            "JIANYING-25-08": {"missing-animation.bin": str(package)}
        },
        id_factory=_id_factory(),
    )

    assert result["manifest"]["visual_resource_rehydrate"]["pending_paths"] == []
    mapped_rows = [
        row
        for block in result["manifest"]["templates"]
        for inner in block["inner_drafts"]
        for row in inner["build_report"]["visual_rehydrations"]
    ]
    assert mapped_rows
    assert all(row["resolution"] == "explicit_visual_path_map" for row in mapped_rows)
    assert all(row["resolved_path"] == str(package) for row in mapped_rows)
    assert all(row["pending_rehydrate"] is False for row in mapped_rows)


def test_five_template_entry_rejects_unrecognised_missing_audio(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    source_path = tmp_path / "preset_08.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    material = source["materials"]["drafts"][0]["draft"]["materials"]["audios"][0]
    material["source_platform"] = 0
    material["effect_id"] = ""
    material["path"] = str(tmp_path / "missing-local-sfx.wav")
    source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(trial_module.FiveTemplateTrialError) as exc_info:
        trial_module.build_five_template_trial(
            _base(),
            registry,
            tmp_path,
            [],
            disable_audio=False,
            audio_cache_root=tmp_path / "music",
            allow_audio_rehydrate=True,
            template_ids=("JIANYING-25-08",),
            id_factory=_id_factory(),
        )
    assert "missing-local-sfx.wav" in str(exc_info.value)


def test_two_templates_can_be_imported_successfully(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    media = tmp_path / "T14_actual_frame_08.jpg"
    media.write_bytes(b"approved T14 frame")
    template_ids = trial_module.FIVE_TEMPLATE_IDS[:2]

    result = trial_module.build_five_template_trial(
        _base(),
        registry,
        tmp_path,
        [media],
        template_ids=template_ids,
        id_factory=_id_factory(),
    )
    manifest = result["manifest"]

    assert manifest["template_ids"] == list(template_ids)
    assert len(manifest["templates"]) == 2
    assert manifest["structure_validation"]["template_blocks_non_overlapping"] is True
    assert validate_trial_draft(result["draft"], path_base=tmp_path).ok


def test_sampling_clips_overlong_segment_and_source_timerange_together(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    source_path = tmp_path / "preset_01.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    inner = source["materials"]["drafts"][0]["draft"]
    inner["duration"] = 10_000_000
    for track in inner["tracks"]:
        for segment in track["segments"]:
            segment["target_timerange"] = {"start": 0, "duration": 10_000_000}
            segment["source_timerange"] = {"start": 0, "duration": 10_000_000}
    source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

    result = trial_module.build_five_template_trial(
        _base(),
        registry,
        tmp_path,
        [],
        template_ids=("JIANYING-25-01",),
        id_factory=_id_factory(),
    )
    block = result["manifest"]["templates"][0]
    assert result["manifest"]["max_template_duration_us"] == 3_000_000
    assert block["duration_us"] == 3_000_000
    assert block["sampling"]["0"]["trimmed_segment_count"] == 4
    imported_segments = [
        segment
        for track in result["draft"]["tracks"]
        if track.get("type") == "text"
        for segment in track["segments"]
    ]
    assert imported_segments
    for segment in imported_segments:
        assert segment["target_timerange"]["duration"] == 3_000_000
        assert segment["source_timerange"]["duration"] == 3_000_000


def test_timeline_capacity_is_checked_before_any_template_build(
    tmp_path: Path, trial_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _synthetic_registry(tmp_path)
    media = tmp_path / "T14_actual_frame_18.jpg"
    media.write_bytes(b"approved T14 frame")
    called = False

    def fail_if_called(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True
        raise AssertionError("容量预检失败前不应调用 build_trial_draft")

    monkeypatch.setattr(trial_module, "build_trial_draft", fail_if_called)
    with pytest.raises(trial_module.FiveTemplateTrialError, match="base_duration_us"):
        trial_module.build_five_template_trial(
            _base(duration=6_000_000),
            registry,
            tmp_path,
            [media],
            id_factory=_id_factory(),
        )
    assert called is False


def test_required_media_is_explicit_and_old_t14_frames_are_rejected(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    with pytest.raises(trial_module.FiveTemplateTrialError, match="--media"):
        trial_module.build_five_template_trial(
            _base(), registry, tmp_path, [], id_factory=_id_factory()
        )

    old_frame = tmp_path / "T14_frame_08.jpg"
    old_frame.write_bytes(b"forbidden old frame")
    with pytest.raises(trial_module.FiveTemplateTrialError, match="旧 T14"):
        trial_module.build_five_template_trial(
            _base(), registry, tmp_path, [old_frame], id_factory=_id_factory()
        )


def test_keyword_slot_values_do_not_hardcode_visual_style(
    tmp_path: Path, trial_module: ModuleType
) -> None:
    registry = _synthetic_registry(tmp_path)
    candidate = next(item for item in registry["candidates"] if item["template_id"] == "JIANYING-25-16")
    values = trial_module.generate_slot_values(candidate)
    assert values == {"text_slots": {"text_01": "國際交流"}, "media_slots": {}}
    assert not any(key in values for key in ("color", "fill", "size", "font_size", "style"))

    source = json.loads((tmp_path / "preset_16.json").read_text(encoding="utf-8-sig"))
    before = json.loads(source["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["content"])
    filled = apply_template_slots(source, values, candidate=candidate)
    after = json.loads(filled["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["content"])
    assert after["styles"][0]["size"] == before["styles"][0]["size"] == 77
    assert after["styles"][0]["fill"] == before["styles"][0]["fill"]
    assert after["text"] == "國際交流"


def test_help_and_sample_copy_reference_current_t14_only(trial_module: ModuleType) -> None:
    help_text = trial_module.build_parser().format_help()
    assert "T14_Shuyang_Mandarin_blind_vertical.mp4" in help_text
    assert "T14_actual_frame_08.jpg" in help_text
    assert all(old_name not in help_text for old_name in trial_module.FORBIDDEN_OLD_T14_MEDIA)
    assert "他們在上海讀小學" in trial_module.T14_SLOT_COPY["JIANYING-25-23"]
    assert "我叫 Li Shuang，我從北京來" in trial_module._sample_metadata()["copy_excerpt"]["opening"]


def test_output_path_is_workspace_scoped(tmp_path: Path, trial_module: ModuleType) -> None:
    envelope = {"draft": _base(), "manifest": {"sample": "T14"}}
    output = trial_module.PROJECT_ROOT / "jianying-adapter" / "trial_outputs" / "five_template_blind_test" / "unit_test_write_output.json"
    try:
        written = trial_module.write_trial_output(envelope, output)
        assert written == output.resolve()
        assert json.loads(written.read_text(encoding="utf-8")) == envelope
    finally:
        output.unlink(missing_ok=True)
