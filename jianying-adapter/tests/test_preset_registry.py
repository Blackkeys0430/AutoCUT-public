import copy
import json
from pathlib import Path

import pytest

from jianying_adapter.preset_catalog import scan_preset_root
from jianying_adapter.preset_registry import (
    _extract_slots,
    _quota_repair,
    apply_template_slots,
    build_purpose_inventory,
    build_registry,
    select_template,
)


def _inner(*, app_version="8.8.0", transform_x=0, text="默认文案", item_count=1, audio=False, multi_style=False):
    video_id = "11111111-1111-1111-1111-111111111111"
    text_id = "22222222-2222-2222-2222-222222222222"
    styles = [{"range": [0, len(text)], "size": 32, "fill": {"color": "#FFFFFF"}, "shadow": {"opacity": 0.9}}]
    if multi_style:
        styles.append({"range": [0, len(text)], "size": 36, "fill": {"color": "#FFD400"}})
    materials = {
        "videos": [{"id": video_id, "material_id": video_id, "media_path": "C:/source/base.mp4", "duration": 4_000_000, "type": "video"}],
        "texts": [{"id": text_id, "text": text, "font_name": "宋体", "font_size": 32, "font_path": "/Users/YOUR_USER/Library/Fonts/demo.ttf", "content": json.dumps({"styles": styles, "text": text}, ensure_ascii=False, sort_keys=True)}],
        "material_animations": [{"id": "33333333-3333-3333-3333-333333333333", "animation_id": "fade-in", "transform_x": transform_x}],
        "audios": [],
    }
    tracks = [
        {"id": "track-video", "type": "video", "segments": [{"id": video_id, "material_id": video_id, "clip": {"transform": {"x": transform_x, "y": 0}}, "target_timerange": {"start": 0, "duration": 4_000_000}}]},
        {"id": "track-text", "type": "text", "segments": [{"id": text_id, "material_id": text_id, "target_timerange": {"start": 0, "duration": 4_000_000}}]},
    ]
    if item_count > 1:
        for index in range(1, item_count):
            extra_id = f"{index + 10:08d}-0000-0000-0000-000000000000"
            materials["texts"].append({"id": extra_id, "text": f"项目{index + 1}", "font_name": "宋体", "font_size": 32, "content": json.dumps({"styles": styles, "text": f"项目{index + 1}"}, ensure_ascii=False, sort_keys=True)})
            tracks[1]["segments"].append({"id": extra_id, "material_id": extra_id, "target_timerange": {"start": index * 500_000, "duration": 4_000_000}})
    if audio:
        audio_id = "44444444-4444-4444-4444-444444444444"
        materials["audios"] = [{"id": audio_id, "material_id": audio_id, "path": "C:/source/pop.mp3", "type": "audio", "duration": 500_000}]
        tracks.append({"id": "track-audio", "type": "audio", "segments": [{"id": audio_id, "material_id": audio_id, "target_timerange": {"start": 0, "duration": 500_000}}]})
    inner = {"id": "draft-id", "name": "实例名", "duration": 4_000_000, "platform": {"app_version": app_version}, "materials": materials, "tracks": tracks}
    return {"id": "outer-id", "name": "包装名", "materials": {"drafts": [{"id": "wrapper-id", "draft": inner}]}, "tracks": [{"type": "video", "segments": []}]}


def _write(root: Path, name: str, payload: dict):
    path = root / name / "preset_draft" / "draft_content.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def _registry_from_files(root: Path, count: int = 30):
    for index in range(count):
        _write(root, f"单句模板{index:02d}", _inner(transform_x=index / 100))
    _write(root, "同结构旧版本", _inner(app_version="9.0.1", transform_x=0.77))
    preview_source = _write(root, "同结构88版本", _inner(app_version="8.8.0", transform_x=0.77))
    preview_source.parent.parent.joinpath("同结构88版本.jpeg").write_bytes(b"jpeg-placeholder")
    return build_registry(scan_preset_root(root), root)


def test_build_registry_selects_25_unique_structures_and_prefers_88(tmp_path):
    registry = _registry_from_files(tmp_path)
    assert registry["summary"]["selected"] == 25
    assert registry["summary"]["unique_structure_hashes"] == 25
    assert all(candidate["auto_fill_ready"] for candidate in registry["candidates"])
    same_structure = [candidate for candidate in registry["candidates"] if candidate["display_name"] == "同结构88版本"]
    assert same_structure
    assert same_structure[0]["jianying_8_8_compatibility"] == "le_8_8"
    assert same_structure[0]["preview_path"].endswith("同结构88版本.jpeg")
    assert registry["coverage_gap"]


def test_registry_slots_have_stable_locators_and_required_fields(tmp_path):
    first = _registry_from_files(tmp_path)
    second = _registry_from_files(tmp_path / "again")
    first_candidate = first["candidates"][0]
    second_candidate = second["candidates"][0]
    assert first_candidate["slots"]["text"][0]["slot_id"] == "text_01"
    assert first_candidate["slots"]["text"][0]["locators"][0]["materials_group"] == "texts"
    assert "segment_refs" in first_candidate["slots"]["text"][0]["locators"][0]
    assert "required" in first_candidate["slots"]["video"][0]
    assert first_candidate["slots"] == second_candidate["slots"]
    assert first_candidate["slots"]["text"][0]["required"] is True
    assert first_candidate["auto_fill_ready"] is True
    assert first_candidate["manual_slot_count"] == 0


def test_single_typing_layer_is_auto_fillable_but_multi_layer_split_is_manual():
    single = _extract_slots(_inner(), "打字机")["text"]
    assert len(single) == 1
    assert single[0]["required"] is True
    assert single[0]["requires_manual_slot_mapping"] is False

    payload = _inner(text="第一层")
    inner = payload["materials"]["drafts"][0]["draft"]
    first = inner["materials"]["texts"][0]
    second = copy.deepcopy(first)
    second["id"] = "77777777-7777-7777-7777-777777777777"
    second["text"] = "第二层"
    second["content"] = json.dumps({"styles": [{"range": [0, 3], "size": 32}], "text": "第二层"}, ensure_ascii=False)
    inner["materials"]["texts"].append(second)
    inner["tracks"][1]["segments"][0]["target_timerange"] = {"start": 0, "duration": 1_000_000}
    inner["tracks"][1]["segments"].append({"id": second["id"], "material_id": second["id"], "target_timerange": {"start": 2_000_000, "duration": 1_000_000}})
    split = _extract_slots(payload, "逐字显影")["text"]
    assert len(split) == 2
    assert all(slot["requires_manual_slot_mapping"] for slot in split)


def test_decorative_punctuation_is_locked_and_not_required():
    payload = _inner(text="?")
    slot = _extract_slots(payload, "普通字幕")["text"][0]
    assert slot["decorative_locked"] is True
    assert slot["required"] is False
    assert slot["requires_manual_slot_mapping"] is False


def test_apply_text_slot_updates_plain_text_and_json_content_without_mutating_source():
    payload = _inner(text="旧文案")
    result = apply_template_slots(payload, {"text_slots": {"text_01": "一段更长的新文案"}})
    item = result["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]
    assert item["text"] == "一段更长的新文案"
    content = json.loads(item["content"])
    assert content["text"] == "一段更长的新文案"
    assert content["styles"][0]["range"] == [0, len("一段更长的新文案")]
    assert payload["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["text"] == "旧文案"


def test_apply_rejects_multi_style_content_safely():
    payload = _inner(text="旧文案", multi_style=True)
    original = copy.deepcopy(payload)
    with pytest.raises(ValueError, match="多 style"):
        apply_template_slots(payload, {"text_slots": {"text_01": "新文案"}})
    assert payload == original


def test_multistyle_required_slot_is_not_auto_fill_ready():
    candidate_payload = _inner(text="旧文案", multi_style=True)
    slots = _extract_slots(candidate_payload, "普通字幕")
    assert slots["text"][0]["required"] is True
    assert slots["text"][0]["requires_manual_style_mapping"] is True


def test_apply_uses_explicit_registry_slots_when_inner_name_differs():
    payload = _inner(text="甲")
    inner = payload["materials"]["drafts"][0]["draft"]
    first = inner["materials"]["texts"][0]
    second = copy.deepcopy(first)
    second["id"] = "88888888-8888-8888-8888-888888888888"
    second["text"] = "乙"
    second["content"] = json.dumps({"styles": [{"range": [0, 1], "size": 32}], "text": "乙"}, ensure_ascii=False)
    inner["materials"]["texts"].append(second)
    inner["tracks"][1]["segments"][0]["target_timerange"] = {"start": 0, "duration": 1_000_000}
    inner["tracks"][1]["segments"].append({"id": second["id"], "material_id": second["id"], "target_timerange": {"start": 2_000_000, "duration": 1_000_000}})
    schema = _extract_slots(payload, "普通字幕")
    inner["name"] = "逐字显影"
    result = apply_template_slots(payload, {"text_slots": {"text_01": "新甲", "text_02": "新乙"}}, slot_schema=schema)
    texts = result["materials"]["drafts"][0]["draft"]["materials"]["texts"]
    assert [texts[0]["text"], texts[1]["text"]] == ["新甲", "新乙"]


def test_apply_requires_required_values_unless_partial_is_explicit():
    payload = _inner(text="示例文案")
    schema = _extract_slots(payload, "普通字幕")
    with pytest.raises(ValueError, match="required"):
        apply_template_slots(payload, {}, slot_schema=schema)
    partial = apply_template_slots(payload, {}, slot_schema=schema, allow_partial=True)
    assert partial["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["text"] == "示例文案"


def test_auto_fill_candidates_apply_all_required_text_slots(tmp_path):
    registry = _registry_from_files(tmp_path, count=5)
    for candidate in registry["candidates"]:
        if not candidate["auto_fill_ready"]:
            continue
        source = tmp_path / candidate["source_path"]
        payload = json.loads(source.read_text(encoding="utf-8"))
        values = {slot["slot_id"]: "测试文案" for slot in candidate["slots"]["text"] if slot.get("required")}
        result = apply_template_slots(payload, {"text_slots": values}, candidate=candidate)
        assert result != payload


def test_leaf_name_does_not_inherit_cta_from_ancestor_bundle(tmp_path):
    _write(tmp_path / "146个开场结尾名字场动画＋音效", "预览打字", _inner())
    registry = build_registry(scan_preset_root(tmp_path), tmp_path)
    candidate = registry["candidates"][0]
    assert "closing_cta" not in candidate["semantic_tags"]
    assert "title_hook" not in candidate["semantic_tags"]


def test_inferred_counts_separate_items_lines_and_characters(tmp_path):
    _write(tmp_path, "温9-2行疑问", _inner(transform_x=0.1))
    _write(tmp_path, "温38-3句并列", _inner(transform_x=0.2))
    _write(tmp_path, "4个大字", _inner(transform_x=0.3))
    registry = build_registry(scan_preset_root(tmp_path), tmp_path)
    by_name = {candidate["display_name"]: candidate for candidate in registry["candidates"]}
    rows = by_name["温9-2行疑问"]
    assert rows["min_item_count"] is None and rows["max_item_count"] is None
    assert rows["inferred_counts"]["line_count"] == 2
    assert rows["selection_constraints"]["item_count"] == {"min": None, "max": None}
    parallel = by_name["温38-3句并列"]
    assert parallel["min_item_count"] == parallel["max_item_count"] == 3
    chars = by_name["4个大字"]
    assert chars["min_item_count"] is None
    assert chars["inferred_counts"]["character_count"] == 4


def test_duplicate_text_at_different_visible_times_is_not_merged():
    payload = _inner(text="一个")
    inner = payload["materials"]["drafts"][0]["draft"]
    first_id = inner["materials"]["texts"][0]["id"]
    second_id = "55555555-5555-5555-5555-555555555555"
    inner["materials"]["texts"].append(copy.deepcopy(inner["materials"]["texts"][0]))
    inner["materials"]["texts"][1]["id"] = second_id
    inner["materials"]["texts"][1]["text"] = "一个"
    inner["materials"]["texts"][1]["content"] = inner["materials"]["texts"][0]["content"]
    text_track = inner["tracks"][1]
    text_track["segments"][0]["target_timerange"] = {"start": 866666, "duration": 1000000}
    text_track["segments"].append({"id": second_id, "material_id": second_id, "target_timerange": {"start": 3333333, "duration": 1000000}})
    slots = _extract_slots(payload, "普通字幕")["text"]
    assert len(slots) == 2
    assert all(slot["ambiguous_duplicate_text"] for slot in slots)
    assert all(slot["requires_manual_slot_mapping"] for slot in slots)


def test_duplicate_text_normal_and_later_low_opacity_history_is_merged():
    payload = _inner(text="历史态")
    inner = payload["materials"]["drafts"][0]["draft"]
    first = inner["materials"]["texts"][0]
    first_id = first["id"]
    second = copy.deepcopy(first)
    second_id = "66666666-6666-6666-6666-666666666666"
    second["id"] = second_id
    second["text_alpha"] = 0.4
    inner["materials"]["texts"].append(second)
    text_track = inner["tracks"][1]
    text_track["segments"][0]["target_timerange"] = {"start": 0, "duration": 1000000}
    text_track["segments"].append({"id": second_id, "material_id": second_id, "target_timerange": {"start": 2000000, "duration": 1000000}})
    slots = _extract_slots(payload, "普通字幕")["text"]
    assert len(slots) == 1
    assert len(slots[0]["locators"]) == 2
    assert not slots[0].get("ambiguous_duplicate_text", False)


def test_j140_is_selected_when_88_compatible_or_reported_as_gap(tmp_path):
    _write(tmp_path, "J140-点赞收藏", _inner(app_version="8.8.0"))
    registry = build_registry(scan_preset_root(tmp_path), tmp_path, candidate_limit=1)
    assert "closing_cta" in registry["candidates"][0]["semantic_tags"]

    newer_root = tmp_path / "newer"
    _write(newer_root, "J140-点赞收藏", _inner(app_version="9.0.1"))
    newer = build_registry(scan_preset_root(newer_root), newer_root, candidate_limit=1)
    cta_gap = next(gap for gap in newer["coverage_gap"] if gap["tag"] == "closing_cta")
    assert cta_gap["reason"] == "有1个CTA但无8.8兼容候选"


def test_material_showcase_is_visual_candidate_and_true_transition_is_honest(tmp_path):
    _write(tmp_path, "素材展示", _inner())
    registry = build_registry(scan_preset_root(tmp_path), tmp_path, candidate_limit=1)
    candidate = registry["candidates"][0]
    assert "broll_visual" in candidate["semantic_tags"]
    assert any(slot["required"] for slot in candidate["slots"]["video"])
    assert registry["coverage"]["available_representatives"]["broll_visual"] == 1
    assert any(gap["tag"] == "true_transition" for gap in registry["coverage_gap"])


def test_media_showcase_fixed_multistyle_labels_do_not_block_visual_autofill(tmp_path):
    _write(tmp_path, "素材展示", _inner(multi_style=True))
    candidate = build_registry(scan_preset_root(tmp_path), tmp_path, candidate_limit=1)["candidates"][0]
    assert candidate["auto_fill_ready"] is True
    assert candidate["manual_style_slot_count"] == 0
    assert all(not slot["required"] for slot in candidate["slots"]["text"])
    assert any(slot["required"] for slot in candidate["slots"]["video"])


def test_pure_sticker_decoration_is_not_broll(tmp_path):
    payload = _inner()
    inner = payload["materials"]["drafts"][0]["draft"]
    inner["materials"]["videos"] = []
    inner["tracks"] = [track for track in inner["tracks"] if track.get("type") != "video"]
    sticker_id = "99999999-9999-9999-9999-999999999999"
    inner["materials"]["stickers"] = [{"id": sticker_id, "material_id": sticker_id, "path": "C:/source/decorative.png", "type": "sticker"}]
    inner["tracks"].append({"id": "track-sticker", "type": "sticker", "segments": [{"id": sticker_id, "material_id": sticker_id, "target_timerange": {"start": 0, "duration": 500_000}}]})
    _write(tmp_path, "普通贴纸装饰", payload)
    candidate = build_registry(scan_preset_root(tmp_path), tmp_path, candidate_limit=1)["candidates"][0]
    assert "broll_visual" not in candidate["semantic_tags"]
    assert "visual_decorated_text" in candidate["semantic_tags"]


def test_full_selection_reserves_two_distinct_media_showcase_templates(tmp_path):
    _write(tmp_path, "素材展示", _inner(transform_x=0.11))
    _write(tmp_path, "素材展示2", _inner(transform_x=0.22))
    for index in range(5):
        _write(tmp_path, f"普通字幕{index}", _inner(transform_x=0.4 + index / 100))
    registry = build_registry(scan_preset_root(tmp_path), tmp_path, candidate_limit=5)
    showcase = [candidate for candidate in registry["candidates"] if "media_showcase" in candidate["semantic_tags"]]
    assert len(showcase) >= 2
    assert len({candidate["structure_hash"] for candidate in showcase}) >= 2
    assert all(candidate["jianying_8_8_compatibility"] == "le_8_8" for candidate in showcase[:2])
    assert all(candidate["auto_fill_ready"] for candidate in showcase[:2])


def test_selector_excludes_manual_candidates_by_default():
    registry = {"candidates": [
        {"template_id": "manual", "semantic_tags": ["question_hook"], "manual_slot_count": 2, "auto_fill_ready": False, "selection_score": {"total": 100}, "jianying_8_8_compatibility": "le_8_8"},
        {"template_id": "auto", "semantic_tags": ["question_hook"], "manual_slot_count": 0, "auto_fill_ready": True, "selection_score": {"total": 1}, "jianying_8_8_compatibility": "le_8_8"},
    ]}
    default = select_template(registry, {"intent": "question", "top_k": 1})
    assert default["top_k"][0]["template_id"] == "auto"
    allowed = select_template(registry, {"intent": "question", "allow_manual": True, "top_k": 1})
    assert allowed["top_k"][0]["template_id"] == "manual"


def test_list_selection_prefers_exact_four_and_numbered_and_rejects_plain_two_lines(tmp_path):
    _write(tmp_path, "4点建议分别出现", _inner(transform_x=0.1))
    _write(tmp_path, "3序号并列", _inner(transform_x=0.2))
    _write(tmp_path, "普通2行字幕", _inner(transform_x=0.3))
    registry = build_registry(scan_preset_root(tmp_path), tmp_path, candidate_limit=3)
    selected_names = {candidate["display_name"] for candidate in registry["candidates"]}
    assert "4点建议分别出现" in selected_names
    assert "3序号并列" in selected_names

    candidates = [
        {"template_id": "three", "semantic_tags": ["list_3_5"], "min_item_count": 3, "max_item_count": 3, "selection_constraints": {"item_count": {"min": 3, "max": 3}}, "selection_score": {"total": 1}, "jianying_8_8_compatibility": "le_8_8"},
        {"template_id": "four", "semantic_tags": ["list_3_5"], "min_item_count": 4, "max_item_count": 4, "selection_constraints": {"item_count": {"min": 4, "max": 4}}, "selection_score": {"total": 1}, "jianying_8_8_compatibility": "le_8_8"},
        {"template_id": "two-lines", "semantic_tags": ["explanation_two_line"], "min_item_count": None, "max_item_count": None, "selection_constraints": {"item_count": {"min": None, "max": None}}, "selection_score": {"total": 50}, "jianying_8_8_compatibility": "le_8_8"},
    ]
    semantic = {"candidates": candidates}
    assert select_template(semantic, {"intent": "list", "item_count": 4, "top_k": 1})["top_k"][0]["template_id"] == "four"
    assert select_template(semantic, {"intent": "list", "item_count": 3, "top_k": 1})["top_k"][0]["template_id"] == "three"


def test_quota_repair_fills_underrepresented_families_without_breaking_protected_shapes():
    def item(name, tags, minimum=None, maximum=None):
        return {"display_name": name, "semantic_tags": tags, "min_item_count": minimum, "max_item_count": maximum, "auto_fill_ready": True, "jianying_8_8_compatibility": "le_8_8", "selection_score": {"total": 1}, "source_path": name}

    selected = [
        item("exact4", ["list_3_5"], 4, 4), item("numbered", ["numbered_list"]),
        item("showcase1", ["media_showcase"]), item("showcase2", ["media_showcase"]),
        item("q1", ["question_hook"]), item("q2", ["question_hook"]),
        item("t1", ["title_hook"]), item("t2", ["title_hook"]),
        item("k1", ["keyword_emphasis"]), item("k2", ["keyword_emphasis"]), item("k3", ["keyword_emphasis"]),
        item("quote1", ["quote_opinion"]), item("quote2", ["quote_opinion"]), item("quote3", ["quote_opinion"]),
        item("list2", ["list_3_5"], 3, 3), item("list3", ["list_3_5"], 3, 3), item("list4", ["list_3_5"], 3, 3),
        item("single1", ["single_statement"]), item("single2", ["single_statement"]), item("single3", ["single_statement"]),
        item("explain1", ["explanation_two_line"]), item("explain2", ["explanation_two_line"]),
        item("word1", ["word_by_word"]), item("word2", ["word_by_word"]),
        item("filler", []),
    ]
    selected.extend([item("filler2", []), item("filler3", [])])
    remaining = [
        item("single4", ["single_statement"]), item("explain3", ["explanation_two_line"]), item("word3", ["word_by_word"]),
    ]
    repaired = _quota_repair(selected, remaining, 25)
    counts = {tag: sum(tag in c["semantic_tags"] for c in repaired) for tag in ("single_statement", "explanation_two_line", "word_by_word")}
    assert counts == {"single_statement": 4, "explanation_two_line": 3, "word_by_word": 3}
    assert sum("media_showcase" in c["semantic_tags"] for c in repaired) == 2
    assert any(c["min_item_count"] == c["max_item_count"] == 4 for c in repaired)
    assert sum("numbered_list" in c["semantic_tags"] for c in repaired) == 1


def test_mac_paths_are_recorded_as_missing_dependencies(tmp_path):
    _write(tmp_path, "字体候选", _inner())
    candidate = build_registry(scan_preset_root(tmp_path), tmp_path)["candidates"][0]
    deps = candidate["dependencies"]
    assert deps["missing_path_count"] > 0
    assert any(item["original_path"].startswith("/Users/") and not item["exists_on_current_machine"] for item in deps["path_dependencies"])
    assert deps["remote_resource_ids"]


def _manual_registry():
    def candidate(template_id, tags, minimum=1, maximum=1):
        return {"template_id": template_id, "semantic_tags": tags, "min_item_count": minimum, "max_item_count": maximum, "selection_constraints": {"item_count": {"min": minimum, "max": maximum}}, "selection_score": {"total": 10}, "jianying_8_8_compatibility": "le_8_8"}

    return {"candidates": [
        candidate("question", ["question_hook", "title_hook"]),
        candidate("quote", ["quote_opinion", "single_statement"]),
        candidate("list", ["list_3_5"], 3, 5),
        candidate("word", ["word_by_word", "keyword_emphasis"]),
        candidate("cta", ["closing_cta"]),
    ]}


@pytest.mark.parametrize(
    ("semantic_request", "expected"),
    [
        ({"intent": "question", "top_k": 1}, "question"),
        ({"intent": "list", "item_count": 3, "top_k": 1}, "list"),
        ({"intent": "金句", "top_k": 1}, "quote"),
        ({"intent": "逐字", "emphasis": "关键词", "top_k": 1}, "word"),
        ({"closing": True, "top_k": 1}, "cta"),
    ],
)
def test_select_template_matches_high_frequency_intents(semantic_request, expected):
    result = select_template(_manual_registry(), semantic_request)
    assert result["top_k"][0]["template_id"] == expected
    assert result["top_k"][0]["reasons"]
    assert result["fallback"]


def test_select_template_is_deterministic_and_has_fallback():
    registry = _manual_registry()
    request = {"intent": "不存在的意图", "needs_sfx": True, "top_k": 3}
    assert select_template(registry, request) == select_template(registry, request)
    assert len(select_template(registry, request)["top_k"]) == 3


def test_sequence_selection_can_change_or_continue_without_changing_semantics():
    from jianying_adapter.preset_registry import describe_visual_form
    first = describe_visual_form(_inner())
    second = describe_visual_form(_inner(item_count=2))
    registry = {'candidates': [
        {'template_id': 'same', 'semantic_tags': ['question_hook'], 'visual_features': first},
        {'template_id': 'other', 'semantic_tags': ['question_hook'], 'visual_features': second},
        {'template_id': 'wrong-purpose', 'semantic_tags': ['closing_cta'],
         'selection_score': {'total': 1000}, 'visual_features': second},
    ]}
    request = {'intent': 'question', 'sequence_context': {
        'neighbors': [{'template_ids': ['same']}], 'used_template_ids': ['same'],
        'transition': {'intent': 'change', 'reason': '从提问转入另一层问题'}}}
    before = copy.deepcopy(registry)
    result = select_template(registry, request)
    assert result['top_k'][0]['template_id'] == 'other'
    assert {r['template_id'] for r in result['top_k']} == {'same', 'other'}
    request['sequence_context']['transition']['intent'] = 'hold'
    assert select_template(registry, request)['top_k'][0]['template_id'] == 'same'
    assert registry == before and result['current_video_ready'] is False


@pytest.mark.parametrize('different', [
    {'layout': 'vertical_groups'},
    {'information_order': 'cumulative_reveal'},
])
def test_shortlist_offers_composition_alternative_before_another_animation_id(different):
    def candidate(tid, score, **features):
        return {'template_id': tid, 'semantic_tags': ['question_hook'],
                'selection_score': {'total': score},
                'visual_features': {'evidence': 'native_structure', 'layout': 'horizontal_groups',
                                    'information_order': 'simultaneous', **features}}
    registry = {'candidates': [
        candidate('first', 30, entrance_effects=['animation-a']),
        candidate('same-layout', 29, entrance_effects=['animation-b']),
        candidate('different', 20, **different),
    ]}
    request = {'intent': 'question', 'top_k': 2, 'sequence_context': {}}
    result = select_template(registry, request)
    assert [row['template_id'] for row in result['top_k']] == ['first', 'different']
    # A concrete layout/reveal preference still outweighs mere variety.
    request['preferred_visual_features'] = {'layout': 'horizontal_groups', 'information_order': 'simultaneous'}
    assert [row['template_id'] for row in select_template(registry, request)['top_k']] == ['first', 'same-layout']


def test_shortlist_does_not_group_unknown_features_as_a_known_composition():
    registry = {'candidates': [
        {'template_id': tid, 'semantic_tags': ['question_hook'],
         'selection_score': {'total': score}, 'visual_features': features}
        for tid, score, features in [
            ('first', 30, {'evidence': 'unknown'}),
            ('second', 29, {'evidence': 'unknown'}),
            ('known', 20, {'evidence': 'native_structure', 'layout': 'vertical_groups',
                           'information_order': 'cumulative_reveal'}),
        ]
    ]}
    result = select_template(registry, {'intent': 'question', 'top_k': 2, 'sequence_context': {}})
    assert [row['template_id'] for row in result['top_k']] == ['first', 'second']


def test_native_visual_form_ignores_empty_or_unreferenced_animation_containers():
    from jianying_adapter.preset_registry import describe_visual_form
    payload = _inner()
    inner = payload['materials']['drafts'][0]['draft']
    animation = inner['materials']['material_animations'][0]
    animation['animations'] = [{'type': 'in', 'start': 0, 'duration': 200000, 'resource_id': 'real-intro'}]
    assert not describe_visual_form(payload)['entrance_effects']
    inner['tracks'][1]['segments'][0]['extra_material_refs'] = [animation['id']]
    assert describe_visual_form(payload)['entrance_effects'] == ['real-intro']
    animation['animations'][0]['duration'] = 0
    assert not describe_visual_form(payload)['entrance_effects']


def test_native_loop_form_retains_cycle_and_differs_from_static_text():
    from jianying_adapter.preset_registry import describe_visual_form, visual_form_key
    payload = _inner()
    before = describe_visual_form(payload)
    inner = payload['materials']['drafts'][0]['draft']
    animation = inner['materials']['material_animations'][0]
    animation['animations'] = [{'type': 'loop', 'start': 0, 'duration': 500000, 'resource_id': 'ring'}]
    inner['tracks'][1]['segments'][0]['extra_material_refs'] = [animation['id']]
    after = describe_visual_form(payload)
    assert after['loop_effects'] == ['ring']
    assert after['loop_periods_us'] == [500000]
    assert visual_form_key(before) != visual_form_key(after)


def test_canonical_usage_selection_excludes_wrong_role_and_known_bad_version():
    registry = {'records': [
        {'template_id': 'good', 'primary_category': 'quote_conclusion',
         'source': {'resolved_source_path': 'native.json'}, 'technical_gate': {'compatibility_tier': 'exact_8_8'}},
        {'template_id': 'new-version', 'primary_category': 'quote_conclusion',
         'technical_gate': {'compatibility_tier': 'gt_8_8_hold'}},
        {'template_id': 'wrong', 'primary_category': 'question_hook', 'source': {'semantic_tags': ['quote_opinion']}},
    ]}
    result = select_template(registry, {'intent': '金句'})
    assert [r['template_id'] for r in result['top_k']] == ['good']
    assert result['top_k'][0]['visual_features']['evidence'] == 'unknown'
    result = select_template(registry, {'intent': 'parallel_list'})
    assert not result['top_k'] and result['fallback'] is None and result['gap']


def test_event_duration_prefers_a_fitting_native_sequence_without_faking_capacity():
    registry = {'records': [
        {'template_id': 'too-long', 'primary_category': 'question_hook',
         'timing_contract': {'native_duration_seconds': 5}},
        {'template_id': 'fits', 'primary_category': 'question_hook',
         'timing_contract': {'native_duration_seconds': 1},
         'content_contract': {'conservative_max_chars_per_slot': [4, 6]}},
    ]}
    result = select_template(registry, {'intent': 'question', 'duration_us': 2_000_000})
    assert result['top_k'][0]['template_id'] == 'fits'
    assert result['top_k'][0]['content_contract']['conservative_max_chars_per_slot'] == [4, 6]
    assert any('长于当前事件' in r for r in result['top_k'][1]['reasons'])
    assert not result['current_video_ready']


def test_cli_preset_selection_reads_neighbors_from_the_actual_plan(tmp_path, capsys):
    from jianying_adapter.cli import main
    registry = tmp_path / 'registry.json'
    registry.write_text(json.dumps(_manual_registry()), encoding='utf-8')
    plan = {'visual_events': [
        {'id': 'previous', 'start_us': 0, 'end_us': 1_000_000,
         'techniques': [{'kind': 'text_preset', 'operation_ids': ['q']}], 'audience_need': '前一段'},
        {'id': 'current', 'start_us': 1_000_000, 'end_us': 2_000_000, 'expression_role': 'question',
         'preset_request': {'intent': 'question'}, 'transition': {'intent': 'change', 'reason': '切换问题'}}],
        'operations': [{'id': 'q', 'kind': 'add_preset_group', 'template_id': 'question', 'node_id': 'previous'}]}
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(plan), encoding='utf-8')
    assert main(['preset-select', str(path), '--event', 'current', '--registry', str(registry)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['request']['sequence_context']['neighbors'][0]['template_ids'] == ['question']
    assert result['request']['sequence_context']['used_template_ids'] == ['question']
    assert result['request']['duration_us'] == 1_000_000
    assert not result['current_video_ready']


def test_purpose_inventory_assigns_one_plain_primary_purpose_per_preset(tmp_path):
    _write(tmp_path, "提问字幕", _inner(audio=True, transform_x=0.11))
    _write(tmp_path, "四宫格顺序出现", _inner(transform_x=0.22))
    _write(tmp_path, "人物侧面", _inner(transform_x=0.33))
    _write(tmp_path, "举例3点", _inner(item_count=3, transform_x=0.44))
    catalog = scan_preset_root(tmp_path)

    inventory = build_purpose_inventory(catalog, tmp_path)
    assert inventory["summary"]["records"] == 4
    assert inventory["summary"]["errors"] == 0
    assert all(record["primary_category"] for record in inventory["records"])
    assert all(record["plain_purpose"] for record in inventory["records"])
    assert all(record["visual_review_status"] == "structural_only_unreviewed" for record in inventory["records"])

    by_name = {record["display_name"]: record for record in inventory["records"]}
    assert by_name["提问字幕"]["primary_category"] == "question_hook"
    assert by_name["四宫格顺序出现"]["primary_category"] == "grid_split_screen"
    assert "不是逐个依次出现" in by_name["四宫格顺序出现"]["plain_purpose"]
    assert by_name["人物侧面"]["reuse_class"] == "jianying_recompute"
    assert by_name["举例3点"]["primary_category"] == "parallel_list"


def test_audio_does_not_decide_primary_purpose_and_transition_requires_structure(tmp_path):
    _write(tmp_path, "开头疑问", _inner(audio=True, transform_x=0.11))
    _write(tmp_path, "名字叫转场但没有转场材料", _inner(audio=True, transform_x=0.22))
    transition_payload = _inner(transform_x=0.33)
    inner = transition_payload["materials"]["drafts"][0]["draft"]
    inner["materials"]["transitions"] = [{"id": "transition-1", "type": "transition"}]
    _write(tmp_path, "无语义名称", transition_payload)

    inventory = build_purpose_inventory(scan_preset_root(tmp_path), tmp_path)
    by_name = {record["display_name"]: record for record in inventory["records"]}
    assert by_name["开头疑问"]["primary_category"] == "question_hook"
    assert by_name["名字叫转场但没有转场材料"]["primary_category"] != "true_transition"
    assert by_name["无语义名称"]["primary_category"] == "true_transition"
