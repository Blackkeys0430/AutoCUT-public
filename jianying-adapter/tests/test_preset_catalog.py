import copy
import json
from pathlib import Path

from jianying_adapter.preset_catalog import scan_preset_root


def _segment(material_id: str = "seg-id", text: str = "原文", path: str = "C:/素材/a.mp4"):
    return {
        "id": material_id,
        "material_id": material_id,
        "target_timerange": {"start": 0, "duration": 4_000_000},
        "source_timerange": {"start": 0, "duration": 4_000_000},
        "caption_info": {"text": text},
        "clip": {"transform": {"x": 0, "y": 0}},
        "source": path,
    }


def _inner_draft(*, extra_audio=False, text="原文", path="C:/素材/a.mp4", app_version="8.8.0"):
    video_id = "11111111-1111-1111-1111-111111111111"
    text_id = "22222222-2222-2222-2222-222222222222"
    segments = [_segment(video_id, text=text, path=path)]
    materials = {
        "videos": [{"id": video_id, "material_id": video_id, "media_path": path, "duration": 4_000_000, "type": "video"}],
        "texts": [{"id": text_id, "text": text, "font": "宋体", "font_size": 32, "style": {"fill": "#FFFFFF", "stroke": 2, "shadow": {"opacity": 0.9}, "opacity": 1.0}}],
        "material_animations": [{"id": "anim-id", "type": "in", "animation_id": "aaaaaaaa-1111-1111-1111-111111111111", "keyframes": [{"time": 0, "value": 1.0}]}],
        "video_effects": [{"id": "effect-instance", "effect_id": "bbbbbbbb-2222-2222-2222-222222222222", "intensity": 0.8}],
        "audios": [],
    }
    tracks = [
        {"id": "track-video", "type": "video", "segments": segments},
        {"id": "track-text", "type": "text", "segments": [{"id": text_id, "material_id": text_id, "caption_info": {"text": text}, "target_timerange": {"start": 0, "duration": 4_000_000}}]},
    ]
    if extra_audio:
        audio_id = "33333333-3333-3333-3333-333333333333"
        materials["audios"] = [{"id": audio_id, "material_id": audio_id, "path": "D:/sounds/pop.mp3", "duration": 2_000_000, "type": "audio"}]
        tracks.append({"id": "track-audio", "type": "audio", "segments": [{"id": audio_id, "material_id": audio_id, "target_timerange": {"start": 0, "duration": 2_000_000}}]})
    return {"id": "draft-id", "name": "实例名称", "create_time": 123, "update_time": 456, "duration": 4_000_000, "platform": {"app_version": app_version}, "new_version": "8.8.0", "tracks": tracks, "materials": materials}


def _outer(inner):
    return {
        "id": "outer-id",
        "name": "包装名字",
        "create_time": 1,
        "update_time": 2,
        "duration": 0,
        "tracks": [{"id": "outer-track", "type": "video", "segments": [{"id": "outer-seg"}]}],
        "materials": {"drafts": [{"id": "wrapper-id", "name": "嵌套模板", "draft": inner}]},
    }


def _write(root: Path, name: str, payload: object):
    target = root / name / "preset_draft" / "draft_content.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def _record(catalog, name):
    return next(item for item in catalog["records"] if item["relative_path"].startswith(name + "/"))


def test_nested_draft_is_primary_and_instance_values_do_not_change_fingerprint(tmp_path):
    first = _outer(_inner_draft())
    second = copy.deepcopy(first)
    inner = second["materials"]["drafts"][0]["draft"]
    inner["id"] = "99999999-9999-9999-9999-999999999999"
    inner["name"] = "另一个名字"
    inner["create_time"] = 999999
    inner["tracks"][0]["segments"][0]["id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    inner["tracks"][0]["segments"][0]["material_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    inner["materials"]["videos"][0]["id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    inner["materials"]["videos"][0]["material_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    inner["tracks"][0]["segments"][0]["caption_info"]["text"] = "完全不同的文案"
    inner["materials"]["videos"][0]["media_path"] = "E:/另一个位置/b.mp4"

    _write(tmp_path, "a-first", first)
    _write(tmp_path, "b-second", second)
    catalog = scan_preset_root(tmp_path)
    a = _record(catalog, "a-first")
    b = _record(catalog, "b-second")
    assert a["uses_nested_draft"] is True
    assert a["structural_profile"]["track_shapes"]
    assert a["features"]["app_version"] == ["8.8.0"]
    assert a["features"]["jianying_8_8_compatibility"] == "le_8_8"
    assert a["normalized_structure_sha256"] == b["normalized_structure_sha256"]
    assert a["structural_profile"] == b["structural_profile"]


def test_structural_change_changes_fingerprint_and_exact_duplicate_is_grouped(tmp_path):
    payload = _outer(_inner_draft())
    _write(tmp_path, "one", payload)
    _write(tmp_path, "two", payload)
    changed = _outer(_inner_draft(extra_audio=True))
    _write(tmp_path, "three", changed)

    catalog = scan_preset_root(tmp_path)
    one = _record(catalog, "one")
    three = _record(catalog, "three")
    assert one["normalized_structure_sha256"] != three["normalized_structure_sha256"]
    assert any(group["count"] == 2 and {"one/preset_draft/draft_content.json", "two/preset_draft/draft_content.json"} == set(group["paths"]) for group in catalog["exact_duplicate_groups"])
    assert len(catalog["structural_clusters"]) == 2


def test_same_counts_but_behavior_fields_differ_have_different_structure_hash(tmp_path):
    first = _outer(_inner_draft())
    second = copy.deepcopy(first)
    inner = second["materials"]["drafts"][0]["draft"]
    inner["tracks"][0]["segments"][0]["clip"]["transform"]["x"] = 0.25
    inner["materials"]["texts"][0]["font_size"] = 46
    inner["materials"]["texts"][0]["style"]["fill"] = "#FFD400"
    inner["materials"]["material_animations"][0]["keyframes"][0]["value"] = 1.4
    inner["materials"]["video_effects"][0]["intensity"] = 0.3
    _write(tmp_path, "base", first)
    _write(tmp_path, "variant", second)
    catalog = scan_preset_root(tmp_path)
    base = _record(catalog, "base")
    variant = _record(catalog, "variant")
    assert base["structural_profile"] == variant["structural_profile"]
    assert base["normalized_structure_sha256"] != variant["normalized_structure_sha256"]


def test_uuid_and_references_are_remapped_as_a_stable_structure(tmp_path):
    first = _outer(_inner_draft())
    second = copy.deepcopy(first)
    inner = second["materials"]["drafts"][0]["draft"]
    replacements = {
        "11111111-1111-1111-1111-111111111111": "10101010-aaaa-bbbb-cccc-101010101010",
        "22222222-2222-2222-2222-222222222222": "20202020-aaaa-bbbb-cccc-202020202020",
        "aaaaaaaa-1111-1111-1111-111111111111": "30303030-aaaa-bbbb-cccc-303030303030",
        "bbbbbbbb-2222-2222-2222-222222222222": "40404040-aaaa-bbbb-cccc-404040404040",
    }

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(child) for key, child in value.items()}
        if isinstance(value, list):
            return [replace(child) for child in value]
        return replacements.get(value, value)

    second["materials"]["drafts"][0]["draft"] = replace(inner)
    _write(tmp_path, "uuid-a", first)
    _write(tmp_path, "uuid-b", second)
    catalog = scan_preset_root(tmp_path)
    assert _record(catalog, "uuid-a")["normalized_structure_sha256"] == _record(catalog, "uuid-b")["normalized_structure_sha256"]


def test_json_content_keeps_styles_but_ignores_nested_text(tmp_path):
    first = _outer(_inner_draft())
    second = copy.deepcopy(first)
    content = {"text": "第一段文案", "styles": [{"color": "#FFFFFF", "size": 32, "font": "宋体", "shadow": {"opacity": 0.9}, "range": [0, 5]}]}
    first["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["content"] = json.dumps(content, ensure_ascii=False, sort_keys=True)
    changed_text = copy.deepcopy(content)
    changed_text["text"] = "完全不同的文案"
    changed_text["styles"][0]["range"] = [0, len(changed_text["text"])]
    second["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["content"] = json.dumps(changed_text, ensure_ascii=False, sort_keys=True)
    _write(tmp_path, "text-a", first)
    _write(tmp_path, "text-b", second)
    catalog = scan_preset_root(tmp_path)
    assert _record(catalog, "text-a")["normalized_structure_sha256"] == _record(catalog, "text-b")["normalized_structure_sha256"]

    styled = copy.deepcopy(first)
    styled_content = copy.deepcopy(content)
    styled_content["styles"][0]["color"] = "#FFD400"
    styled_content["styles"][0]["size"] = 46
    styled_content["styles"][0]["shadow"]["opacity"] = 0.4
    styled["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["content"] = json.dumps(styled_content, ensure_ascii=False, sort_keys=True)
    _write(tmp_path, "text-styled", styled)
    styled_catalog = scan_preset_root(tmp_path)
    assert _record(styled_catalog, "text-a")["normalized_structure_sha256"] != _record(styled_catalog, "text-styled")["normalized_structure_sha256"]


def test_content_ranges_are_relative_to_text_length(tmp_path):
    full_a = _outer(_inner_draft())
    full_b = copy.deepcopy(full_a)
    content_a = {"text": "短句", "styles": [{"range": [0, 2], "size": 32, "fill": "#FFF"}]}
    content_b = {"text": "长度完全不同的整句文案", "styles": [{"range": [0, len("长度完全不同的整句文案")], "size": 32, "fill": "#FFF"}]}
    full_a["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["content"] = json.dumps(content_a, ensure_ascii=False, sort_keys=True)
    full_b["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["content"] = json.dumps(content_b, ensure_ascii=False, sort_keys=True)
    _write(tmp_path, "full-a", full_a)
    _write(tmp_path, "full-b", full_b)
    catalog = scan_preset_root(tmp_path)
    assert _record(catalog, "full-a")["normalized_structure_sha256"] == _record(catalog, "full-b")["normalized_structure_sha256"]

    emphasis = copy.deepcopy(full_a)
    emphasis_content = copy.deepcopy(content_a)
    emphasis_content["styles"][0]["range"] = [0, 1]
    emphasis["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["content"] = json.dumps(emphasis_content, ensure_ascii=False, sort_keys=True)
    _write(tmp_path, "partial-emphasis", emphasis)
    changed_catalog = scan_preset_root(tmp_path)
    assert _record(changed_catalog, "full-a")["normalized_structure_sha256"] != _record(changed_catalog, "partial-emphasis")["normalized_structure_sha256"]


def test_classification_is_multilabel_and_reasons_are_explainable(tmp_path):
    _write(tmp_path, "开场标题音效", _outer(_inner_draft(extra_audio=True)))
    catalog = scan_preset_root(tmp_path)
    record = catalog["records"][0]
    assert "subtitle/text" in record["categories"]
    assert "motion_animation" in record["categories"]
    assert "sfx_audio" in record["categories"]
    assert "intro_outro/title" in record["categories"]
    assert "mixed" in record["categories"]
    assert all(record["reasons"])


def test_version_facts_mark_newer_than_jianying_8_8(tmp_path):
    _write(tmp_path, "newer", _outer(_inner_draft(app_version="9.0.1")))
    record = scan_preset_root(tmp_path)["records"][0]
    assert record["features"]["app_version"] == ["9.0.1"]
    assert record["features"]["new_version"] == ["8.8.0"]
    assert record["features"]["app_version_buckets"] == ["gt_8_8"]
    assert record["features"]["jianying_8_8_compatibility"] == "gt_8_8"


def test_bad_json_and_missing_fields_do_not_abort_scan(tmp_path):
    bad = tmp_path / "bad" / "preset_draft" / "draft_content.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not-json", encoding="utf-8")
    _write(tmp_path, "missing", {"name": "不完整", "tracks": []})
    _write(tmp_path, "good", _outer(_inner_draft()))

    catalog = scan_preset_root(tmp_path)
    assert catalog["summary"]["files_discovered"] == 3
    assert catalog["summary"]["records"] == 2
    assert any(error["relative_path"] == "bad/preset_draft/draft_content.json" for error in catalog["errors"])
    assert any(error["relative_path"] == "missing/preset_draft/draft_content.json" and error["kind"] == "missing_field" for error in catalog["errors"])


def test_scan_output_is_stable(tmp_path):
    _write(tmp_path, "z", _outer(_inner_draft()))
    _write(tmp_path, "a", _outer(_inner_draft(extra_audio=True)))
    first = scan_preset_root(tmp_path)
    second = scan_preset_root(tmp_path)
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(second, ensure_ascii=False, sort_keys=True)
    assert [record["relative_path"] for record in first["records"]] == sorted(record["relative_path"] for record in first["records"])
