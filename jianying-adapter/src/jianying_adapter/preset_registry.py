"""Minimal production registry for reusable Jianying preset drafts.

This module is deliberately file-oriented at the edges and pure in memory for
slot application.  It never writes a source preset or a Jianying draft.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA = "jianying-adapter.preset-registry.v1"
CATALOG_SCHEMA = "jianying-adapter.preset-catalog.v1"
PURPOSE_SCHEMA = "jianying-adapter.preset-purpose-inventory.v1"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_PATH_KEYS = {"path", "media_path", "font_path", "font_url", "url", "source"}
_COUNT_WORDS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_SPLIT_WORD_HINTS = ("逐字", "打字", "打字机", "显影", "逐个", "逐句", "拆词", "键入")

SEMANTIC_TARGETS = {
    "question_hook": 2,
    "title_hook": 2,
    "single_statement": 4,
    "quote_opinion": 3,
    "explanation_two_line": 3,
    "list_3_5": 4,
    "word_by_word": 3,
    "keyword_emphasis": 3,
    "special_overlay": 3,
    "broll_visual": 2,
    "media_showcase": 2,
    "numbered_list": 1,
    "true_transition": 1,
    "closing_cta": 2,
}
_SEMANTIC_PRIORITY = {
    "closing_cta": 6,
    "question_hook": 5,
    "title_hook": 5,
    "list_3_5": 4,
    "quote_opinion": 4,
    "explanation_two_line": 3,
    "word_by_word": 3,
    "keyword_emphasis": 3,
    "special_overlay": 2,
    "broll_visual": 5,
    "media_showcase": 6,
    "numbered_list": 5,
    "true_transition": 5,
    "single_statement": 2,
}

_CORE_GROUPS = {
    "audios",
    "beats",
    "material_animations",
    "placeholder_infos",
    "sound_channel_mappings",
    "speeds",
    "texts",
    "vocal_separations",
    "videos",
}

PURPOSE_CATEGORIES = {
    "true_transition": ("真正转场", "连接前后两个镜头，负责画面从上一镜过渡到下一镜。"),
    "person_processing_composition": ("人物处理/构图", "用于人物抠像、侧置、头像、字在人后或人物与文字的组合；换人物后通常要让剪映重新分析。"),
    "grid_split_screen": ("分屏/宫格构图", "把一条或多条画面排成分屏、宫格或画中画，用来同时展示多个画面。"),
    "media_showcase_broll": ("素材展示/B-roll", "把产品图、截图或补充画面放进口播中，用视觉素材代替纯文字解释。"),
    "closing_cta": ("结尾 CTA", "用于收尾时提醒关注、点赞、收藏、评论、分享或私信。"),
    "question_hook": ("提问/开场钩子", "把“为什么、怎么做、有没有”等问题醒目抛出来，适合开头或转折处抓注意。"),
    "cover_main_title": ("封面/主标题", "用于片头、封面或一个大段落的主标题，不承担逐句字幕。"),
    "section_lower_third": ("小标题/章节/人名条", "用于标章节、人物身份或当前话题，通常固定在画面一角或字幕上方。"),
    "numbered_steps": ("序号/步骤", "按 1、2、3 等顺序讲步骤、方法或排名。"),
    "parallel_list": ("并列清单", "把多个卖点、例子、建议或物品按口播节奏逐项列出来。"),
    "quote_conclusion": ("金句/观点/结论", "突出一句核心观点、总结或结论，适合短暂停顿后的重点表达。"),
    "keyword_emphasis": ("关键词/重点字", "只放大、变色或强调少量关键词，不能替代整段普通字幕。"),
    "data_progress_price": ("数字/费用/进度信息", "展示价格、数字、时间、百分比、费用或进度等结构化信息。"),
    "multi_line_explanation": ("双行/多行解释", "把一组解释性文字排成两行、多行或上下组合，适合补充说明。"),
    "typing_word_reveal": ("逐字/打字机", "让文字逐字、逐词或逐句出现，适合短句揭晓与节奏强调。"),
    "special_overlay": ("特殊位置/覆盖字幕", "把文字放在顶部、侧边、底部、全屏、弹幕或环绕位置，属于局部包装而非普通字幕。"),
    "standard_caption": ("常规动态字幕", "用于正常口播句子的字幕包装；主要解决字的样式和入场，不改变画面构图。"),
    "kinetic_short_text": ("动态短字/纯文字动效", "给一小段文字套用特定运动效果；只说明文字怎么动，不代表适合哪个语义节点。"),
    "unclear": ("待人工辨认", "名称和结构不足以可靠判断用途，保留缩略图与原路径供后续人工确认。"),
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _material_id(item: Mapping[str, Any]) -> str:
    for key in ("id", "material_id", "local_id", "origin_material_id"):
        value = _string(item.get(key))
        if value:
            return value
    return ""


def _segment_start(segment: Mapping[str, Any]) -> int:
    timerange = segment.get("target_timerange")
    if isinstance(timerange, Mapping):
        try:
            return int(timerange.get("start", 0))
        except (TypeError, ValueError):
            pass
    return 0


def _parse_content(value: Any) -> tuple[Mapping[str, Any] | None, str]:
    if isinstance(value, Mapping):
        return value, "object"
    if not isinstance(value, str):
        return None, "none"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None, "plain"
    return (parsed, "json") if isinstance(parsed, Mapping) else (None, "plain")


def _style_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    selected = (
        "font_name", "font_size", "text_size", "text_color", "text_alpha",
        "global_alpha", "shadow_alpha", "shadow_color", "shadow_distance",
        "shadow_angle", "border_alpha", "border_color", "border_width",
        "background_color", "background_alpha", "alignment", "line_spacing",
        "letter_spacing", "typesetting", "style_name", "has_shadow",
    )
    result = {key: item[key] for key in selected if key in item and isinstance(item[key], (str, int, float, bool, type(None)))}
    parsed, mode = _parse_content(item.get("content"))
    if parsed is not None:
        styles = parsed.get("styles")
        style_items = styles if isinstance(styles, list) else ([styles] if isinstance(styles, Mapping) else [])
        result["content_mode"] = mode
        result["content_style_count"] = len(style_items)
        result["content_style_keys"] = sorted({str(key) for style in style_items if isinstance(style, Mapping) for key in style})
        result["content_range_count"] = sum(1 for style in style_items if isinstance(style, Mapping) and isinstance(style.get("range"), list))
        result["content_style_values"] = [
            {key: style[key] for key in ("size", "useLetterColor", "range") if key in style and isinstance(style[key], (str, int, float, bool, list))}
            for style in style_items[:3]
            if isinstance(style, Mapping)
        ]
    elif "content" in item:
        result["content_mode"] = mode
    return result


def _resource_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    source = ""
    for key in ("media_path", "path", "source", "font_path"):
        if _string(item.get(key)):
            source = Path(_string(item[key])).name
            break
    result = {"type": _string(item.get("type")) or None, "name": _string(item.get("material_name")) or _string(item.get("font_name")) or None}
    if source:
        result["source_basename"] = source
    if isinstance(item.get("duration"), (int, float)):
        result["duration_us"] = item["duration"]
    return result


def _leaf_text(record_or_name: Mapping[str, Any] | str) -> str:
    if isinstance(record_or_name, Mapping):
        return _string(record_or_name.get("display_name")).lower()
    return _string(record_or_name).lower()


def _number_value(raw: str) -> int | None:
    return int(raw) if raw.isdigit() else _COUNT_WORDS.get(raw)


def _infer_counts(display_name: str) -> dict[str, Any]:
    """Infer semantic counts from leaf naming evidence, not material layers."""

    name = _leaf_text(display_name)
    number = r"(?:\d+|[零一二两三四五六七八九十]+)"
    list_evidence = ("并列", "序号", "列表", "清单", "建议", "要点", "步骤", "多项", "多条")
    item_count: int | None = None
    item_count_min: int | None = None
    item_count_max: int | None = None
    item_reason: str | None = None
    line_count: int | None = None
    token_count: int | None = None
    character_count: int | None = None
    reason: list[str] = []

    # 句数 is an item count only when the leaf explicitly says it is a list.
    list_match = re.search(rf"({number})\s*(?:句|项|条|个|点)\s*(?:{'|'.join(list_evidence)})", name)
    reverse_list_match = re.search(rf"({number})\s*(?:{'|'.join(list_evidence)})", name)
    if list_match:
        item_count = _number_value(list_match.group(1))
        item_count_min = item_count_max = item_count
        item_reason = "explicit_list_count"
        reason.append("叶子名以列表/并列/序号/建议等词明确给出项目数")
    elif reverse_list_match and any(word in name for word in ("序号", "并列", "列表", "清单")):
        item_count = _number_value(reverse_list_match.group(1))
        item_count_min = item_count_max = item_count
        item_reason = "explicit_list_count"
        reason.append("叶子名以列表/并列/序号明确给出项目数")
    elif any(word in name for word in ("序号", "并列", "列表", "清单")):
        item_count = 2
        item_count_min, item_count_max = 2, 5
        item_reason = "list_range"
        reason.append("叶子名只证明是列表形态，项目数保守取 2-5 范围")

    line_match = re.search(rf"({number})\s*(?:行|排)", name)
    if line_match:
        line_count = _number_value(line_match.group(1))
        reason.append("叶子名明确给出行/排数")

    token_match = re.search(rf"({number})\s*(?:个)?\s*(?:大字|字)", name)
    if token_match:
        character_count = _number_value(token_match.group(1))
        token_count = character_count
        reason.append("叶子名明确给出字/大字数量，不作为列表项目数")

    return {
        "item_count": item_count,
        "item_count_min": item_count_min,
        "item_count_max": item_count_max,
        "line_count": line_count,
        "token_count": token_count,
        "character_count": character_count,
        "reason": "; ".join(reason) if reason else None,
        "item_count_reason": item_reason,
    }


def _track_reference_index(draft: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    refs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for track_index, track in enumerate(_as_list(draft.get("tracks"))):
        if not isinstance(track, Mapping):
            continue
        for segment_index, segment in enumerate(_as_list(track.get("segments"))):
            if not isinstance(segment, Mapping):
                continue
            material_id = _string(segment.get("material_id"))
            if material_id:
                timerange = segment.get("target_timerange")
                duration = 0
                if isinstance(timerange, Mapping):
                    try:
                        duration = int(timerange.get("duration", 0))
                    except (TypeError, ValueError):
                        duration = 0
                start = _segment_start(segment)
                refs[material_id].append({
                    "track_index": track_index,
                    "segment_index": segment_index,
                    "track_type": _string(track.get("type")) or "unknown",
                    "start": start,
                    "duration": duration,
                    "end": start + max(duration, 0),
                })
    return refs


def _state_facts(item: Mapping[str, Any]) -> dict[str, Any]:
    alpha_values = []
    for key in ("text_alpha", "global_alpha", "opacity", "alpha"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            alpha_values.append(float(value))
    minimum_alpha = min(alpha_values) if alpha_values else 1.0
    return {"minimum_alpha": minimum_alpha, "low_opacity": minimum_alpha < 0.8}


def _slot_starts(slot: Mapping[str, Any], locator: Mapping[str, Any] | None = None) -> list[int]:
    locators = [locator] if locator is not None else list(slot.get("_locators", []))
    return [ref["start"] for loc in locators for ref in loc.get("segment_refs", [])]


def _can_merge_text_states(existing: Mapping[str, Any], incoming: Mapping[str, Any], incoming_locator: Mapping[str, Any]) -> bool:
    if _string(existing.get("default_text")) != _string(incoming.get("default_text")):
        return False
    if existing.get("_locators", [{}])[0].get("inner_draft_index") != incoming_locator.get("inner_draft_index"):
        return False
    for old_locator in existing.get("_locators", []):
        for old_ref in old_locator.get("segment_refs", []):
            old_start = old_ref.get("start", 0)
            old_end = old_ref.get("end", old_start)
            for new_ref in incoming_locator.get("segment_refs", []):
                new_start = new_ref.get("start", 0)
                new_end = new_ref.get("end", new_start)
                if old_start == new_start or (old_end > new_start and new_end > old_start):
                    return True
    old_states = existing.get("_state_variants", [existing.get("_state", {})])
    new_state = incoming.get("_state", {})
    if any(bool(state.get("low_opacity")) != bool(new_state.get("low_opacity")) for state in old_states):
        old_starts = _slot_starts(existing)
        new_starts = _slot_starts(incoming, incoming_locator)
        if old_starts and new_starts:
            low_start = max(start for start, state in [(min(old_starts), old_states[0]), (min(new_starts), new_state)] if state.get("low_opacity"))
            normal_start = min(start for start, state in [(min(old_starts), old_states[0]), (min(new_starts), new_state)] if not state.get("low_opacity"))
            return low_start > normal_start
    return False


def _extract_slots(payload: Mapping[str, Any], display_name: str = "") -> dict[str, list[dict[str, Any]]]:
    materials_by_kind = {"text": ("texts",), "audio": ("audios",), "image": ("images", "stickers", "canvases"), "video": ("videos",)}
    slots: dict[str, list[dict[str, Any]]] = {kind: [] for kind in materials_by_kind}
    drafts: list[Mapping[str, Any]] = []
    materials = payload.get("materials")
    if isinstance(materials, Mapping):
        for wrapper in _as_list(materials.get("drafts")):
            if isinstance(wrapper, Mapping) and isinstance(wrapper.get("draft"), Mapping):
                drafts.append(wrapper["draft"])
    if not drafts:
        drafts = [payload]

    split_word_template = any(word in _leaf_text(display_name) for word in _SPLIT_WORD_HINTS)
    for inner_index, draft in enumerate(drafts):
        materials = draft.get("materials")
        materials = materials if isinstance(materials, Mapping) else {}
        refs = _track_reference_index(draft)
        for kind, groups in materials_by_kind.items():
            for group in groups:
                for material_index, item in enumerate(_as_list(materials.get(group))):
                    if not isinstance(item, Mapping):
                        continue
                    identifiers = {_material_id(item)} - {""}
                    material_refs = sorted((ref for identifier in identifiers for ref in refs.get(identifier, [])), key=lambda ref: (ref["start"], ref["track_index"], ref["segment_index"]))
                    content, content_mode = _parse_content(item.get("content"))
                    default_text = _string(item.get("text"))
                    if not default_text and content is not None:
                        default_text = _string(content.get("text"))
                    if kind == "text":
                        default_value: Any = default_text
                        style = _style_summary(item)
                    else:
                        default_value = _resource_summary(item)
                        style = {}
                    slots[kind].append({
                        "_sort": (material_refs[0]["start"] if material_refs else math.inf, inner_index, material_refs[0]["track_index"] if material_refs else math.inf, material_refs[0]["segment_index"] if material_refs else math.inf, material_index),
                        "kind": kind,
                        "default_text": default_value if kind == "text" else None,
                        "default_resource": default_value if kind != "text" else None,
                        "default_style": style,
                        "required": bool(material_refs) and (kind == "video" or group == "images"),
                        "content_mode": content_mode if kind == "text" else None,
                        "_state": _state_facts(item) if kind == "text" else {},
                        "_locator": {
                            "inner_draft_index": inner_index,
                            "materials_group": group,
                            "materials_index": material_index,
                            "material_id": _material_id(item) or None,
                            "segment_refs": material_refs,
                        },
                    })
    for kind, kind_slots in slots.items():
        if kind == "text":
            # Several text materials can be states of one logical label.  A
            # repeated default is one slot only when timing/state proves it.
            grouped: list[dict[str, Any]] = []
            for slot in kind_slots:
                locator = slot.pop("_locator")
                match = next((existing for existing in grouped if _can_merge_text_states(existing, slot, locator)), None)
                if match is None:
                    slot["_locators"] = [locator]
                    slot["_state_variants"] = [slot.get("_state", {})]
                    grouped.append(slot)
                else:
                    match["_locators"].append(locator)
                    match["_state_variants"].append(slot.get("_state", {}))
                    match["_sort"] = min(match["_sort"], slot["_sort"])
            kind_slots = grouped
            duplicate_groups: Counter[tuple[int, str]] = Counter((slot["_locators"][0]["inner_draft_index"], _string(slot.get("default_text"))) for slot in kind_slots)
            for slot in kind_slots:
                key = (slot["_locators"][0]["inner_draft_index"], _string(slot.get("default_text")))
                if duplicate_groups[key] > 1:
                    slot["ambiguous_duplicate_text"] = True
                    slot["requires_manual_slot_mapping"] = True
            nondecorative_count = sum(not _is_decorative_text(slot.get("default_text")) for slot in kind_slots)
            manual_from_split = split_word_template and nondecorative_count > 1
            for slot in kind_slots:
                if manual_from_split and not _is_decorative_text(slot.get("default_text")):
                    slot["requires_manual_slot_mapping"] = True
        kind_slots.sort(key=lambda slot: slot["_sort"])
        for index, slot in enumerate(kind_slots, 1):
            slot["slot_id"] = f"{kind}_{index:02d}"
            if kind == "text":
                slot["locators"] = sorted(slot.pop("_locators"), key=lambda locator: (locator["inner_draft_index"], locator["segment_refs"][0]["start"] if locator["segment_refs"] else math.inf, locator["materials_index"]))
                # A single text layer with a typing/reveal animation is still
                # directly replaceable.  Split-word manual mapping is only a
                # concern once there are multiple logical text layers.
                slot["requires_manual_slot_mapping"] = bool(slot.get("requires_manual_slot_mapping", False) or manual_from_split)
                slot["decorative_locked"] = _is_decorative_text(slot.get("default_text"))
                if slot["decorative_locked"]:
                    slot["requires_manual_slot_mapping"] = False
                slot["required"] = bool(slot.get("locators")) and not slot["decorative_locked"]
                locator_has_multistyle = False
                if slot["required"]:
                    for locator in slot["locators"]:
                        try:
                            locator_draft = drafts[int(locator["inner_draft_index"])]
                            locator_items = locator_draft["materials"][locator["materials_group"]]
                            locator_item = locator_items[int(locator["materials_index"])]
                        except (KeyError, IndexError, TypeError, ValueError):
                            continue
                        parsed_content, _content_mode = _parse_content(locator_item.get("content"))
                        styles = parsed_content.get("styles") if parsed_content is not None else None
                        style_count = len(styles) if isinstance(styles, list) else 1 if isinstance(styles, Mapping) else 0
                        if style_count > 1:
                            locator_has_multistyle = True
                            break
                slot["requires_manual_style_mapping"] = bool(
                    locator_has_multistyle or (
                        slot["required"] and slot.get("default_style", {}).get("content_style_count", 0) > 1
                    )
                )
                slot.pop("_state", None)
                slot.pop("_state_variants", None)
            else:
                slot["locator"] = slot.pop("_locator")
            slot.pop("_sort", None)
        slots[kind] = kind_slots
    return slots


def _is_decorative_text(value: Any) -> bool:
    text = _string(value).strip()
    if not text:
        return True
    if text in {"?", "？", "!", "！", ".", "。", ",", "，", "…", "·", "-", "—"}:
        return True
    if len(text) <= 2 and all(char.isascii() and not char.isalnum() for char in text):
        return True
    if re.fullmatch(r"[0-9一二三四五六七八九十]+[.、)）]", text):
        return True
    return False


def _inner_drafts(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    materials = payload.get("materials")
    if isinstance(materials, Mapping):
        drafts = [item["draft"] for item in _as_list(materials.get("drafts")) if isinstance(item, Mapping) and isinstance(item.get("draft"), Mapping)]
        if drafts:
            return drafts
    return [payload]


@lru_cache(maxsize=512)
def _resource_digest(path: str, size: int, modified_ns: int) -> str:
    # Cache by file state, not merely by a path that can be replaced in place.
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _expression_resource(path: Any, name: Any = None, *, base: Path | None = None) -> dict[str, Any]:
    value = str(path or '')
    file_name = value.replace('\\', '/').rsplit('/', 1)[-1]
    result = {'name': str(name or file_name), 'path': value, 'file_name': file_name,
              'available': False, 'identity': None, 'identity_basis': 'unknown'}
    if value:
        local = Path(value)
        if not local.is_absolute() and base is not None:
            local = base / local
        if local.is_file():
            stat = local.stat()
            result.update(path=str(local.resolve()), available=True,
                          identity='sha256:' + _resource_digest(str(local.resolve()), stat.st_size, stat.st_mtime_ns),
                          identity_basis='file_content')
        else:
            result.update(identity='reference:' + value.replace('\\', '/'), identity_basis='declared_reference')
    return result


def _hex_color(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?', value):
        return value[:7].lower()
    if (isinstance(value, (list, tuple)) and len(value) >= 3
            and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in value[:3])):
        return '#' + ''.join(f'{round(v * 255):02x}' for v in value[:3])
    return None


def _fill_colors(fill: Any) -> set[str]:
    colors = set()
    if isinstance(fill, Mapping):
        color = _hex_color(fill.get('color'))
        if color:
            colors.add(color)
        for value in fill.values():
            if isinstance(value, (Mapping, list)):
                colors.update(_fill_colors(value))
    elif isinstance(fill, list):
        for value in fill:
            colors.update(_fill_colors(value))
    return colors


def describe_visual_form(payload: Mapping[str, Any], *, resource_base: Path | None = None) -> dict[str, Any]:
    """Describe referenced native structure; never infer motion from a preset name.

    Used by the existing catalogue builder and by final timeline diagnostics.
    Positions and animation identifiers describe mechanics, not visual quality.
    """
    windows, positions, entrances, exits, keyframes = [], set(), set(), set(), set()
    loops, loop_periods = set(), set()
    sound = False
    fonts, colors, audio_sources = {}, set(), {}
    for draft in _inner_drafts(payload):
        materials = {str(m['id']): m for group in draft.get('materials', {}).values()
                     if isinstance(group, list) for m in group if isinstance(m, Mapping) and m.get('id')}
        for track in draft.get('tracks', []):
            if track.get('visible') is False:
                continue
            for segment in track.get('segments', []):
                if segment.get('visible') is False or segment.get('track_attribute', 0) & 1:
                    continue
                span = segment.get('target_timerange') or {}
                start, duration = span.get('start'), span.get('duration')
                if type(start) is not int or type(duration) is not int or duration <= 0:
                    continue
                clip = segment.get('clip') or {}
                if clip.get('alpha', 1) <= 0:
                    continue
                material = materials.get(str(segment.get('material_id')), {})
                if track.get('type') == 'audio':
                    audible = bool(material.get('path') and segment.get('volume', 1) > 0
                                   and not track.get('attribute', 0) & 1)
                    sound |= audible
                    if audible:
                        resource = _expression_resource(material.get('path'), material.get('name'), base=resource_base)
                        key = resource['identity'] or resource['name']
                        audio_sources.setdefault(key, {**resource, 'segments': []})['segments'].append({
                            'start_us': start, 'duration_us': duration,
                            'source_timerange': copy.deepcopy(segment.get('source_timerange')),
                            'volume': segment.get('volume', 1)})
                    continue
                if track.get('type') != 'text' or not material:
                    continue
                windows.append((start, start + duration))
                try:
                    content = json.loads(material.get('content') or '{}')
                except (TypeError, ValueError):
                    content = {}
                styles = content.get('styles', []) if isinstance(content, Mapping) else []
                font_specs = [(material.get('font_path'), material.get('font_name'))]
                for style in styles:
                    if not isinstance(style, Mapping):
                        continue
                    font = style.get('font') or {}
                    if isinstance(font, Mapping):
                        font_specs.append((font.get('path'), font.get('name')))
                    colors.update(_fill_colors(style.get('fill', {})))
                if (color := _hex_color(material.get('text_color'))):
                    colors.add(color)
                for path, name in font_specs:
                    if path or name:
                        resource = _expression_resource(path, name, base=resource_base)
                        fonts[resource['identity'] or resource['name']] = resource
                transform = clip.get('transform') or {}
                positions.add((round(float(transform.get('x', 0)), 3), round(float(transform.get('y', 0)), 3)))
                for ref in segment.get('extra_material_refs', []):
                    for animation in materials.get(str(ref), {}).get('animations', []):
                        length, offset = animation.get('duration'), animation.get('start', 0)
                        identity = animation.get('resource_id') or animation.get('effect_id') or animation.get('path')
                        if (not identity or type(length) is not int or length <= 0
                                or type(offset) is not int or offset < 0 or offset + length > duration):
                            continue
                        kind = animation.get('type')
                        if kind in ('in', 'intro'):
                            entrances.add(str(identity))
                        elif kind in ('out', 'outro'):
                            exits.add(str(identity))
                        elif kind == 'loop':
                            loops.add(str(identity))
                            loop_periods.add(length)
                for group in segment.get('common_keyframes', []):
                    frames = group.get('keyframe_list', [])
                    valid = [f for f in frames if type(f.get('time_offset')) is int
                             and 0 <= f['time_offset'] <= duration]
                    if (group.get('property_type') not in ('KFTypeVolume', 'KFTypeTextColor')
                            and len({f['time_offset'] for f in valid}) > 1
                            and len({tuple(f.get('values', [])) for f in valid}) > 1):
                        keyframes.add(str(group.get('property_type')))
    if not windows:
        return {'evidence': 'unknown', 'information_order': 'unknown', 'layout': 'unknown',
                'entrance_effects': [], 'exit_effects': [], 'loop_effects': [], 'loop_periods_us': [],
                'keyframe_properties': [], 'has_audio': sound,
                'font_sources': list(fonts.values()), 'text_colors': sorted(colors),
                'audio_sources': list(audio_sources.values())}
    windows.sort()
    starts, ends = [w[0] for w in windows], [w[1] for w in windows]
    if len(windows) == 1:
        order = 'single_hit'
    elif max(starts) - min(starts) <= 120_000:
        order = 'simultaneous'
    elif max(ends) - min(ends) <= 120_000:
        order = 'cumulative_reveal'
    elif all(a[1] <= b[0] + 120_000 for a, b in zip(windows, windows[1:])):
        order = 'sequential_replace'
    else:
        order = 'staggered_overlap'
    xs, ys = [p[0] for p in positions], [p[1] for p in positions]
    dx, dy = max(xs) - min(xs), max(ys) - min(ys)
    layout = ('single_anchor' if max(dx, dy) <= .03 else
              'horizontal_groups' if dy <= .03 else 'vertical_groups' if dx <= .03 else 'distributed_groups')
    return {'evidence': 'native_structure', 'information_order': order, 'layout': layout,
            'text_positions': [list(p) for p in sorted(positions)],
            'entrance_effects': sorted(entrances), 'exit_effects': sorted(exits),
            'loop_effects': sorted(loops), 'loop_periods_us': sorted(loop_periods),
            'keyframe_properties': sorted(keyframes), 'has_audio': sound,
            'font_sources': sorted(fonts.values(), key=lambda x: (x['name'], x['path'])),
            'text_colors': sorted(colors),
            'audio_sources': sorted(audio_sources.values(), key=lambda x: (x['name'], x['path']))}


def compare_expression_features(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    """Compare observed ingredients, without equating them with taste or sound perception."""
    def identities(features, field):
        return {row['identity'] for row in features.get(field, []) if row.get('identity')}
    def names(features):
        return {row.get('name') for row in features.get('font_sources', []) if row.get('name')}
    fonts = identities(first, 'font_sources') & identities(second, 'font_sources')
    audio = identities(first, 'audio_sources') & identities(second, 'audio_sources')
    a, b = visual_form_key(first), visual_form_key(second)
    return {'same_structure': a is not None and a == b,
            'shared_font_names': sorted(names(first) & names(second)),
            'shared_font_resources': sorted(fonts),
            'shared_text_colors': sorted(set(first.get('text_colors', [])) & set(second.get('text_colors', []))),
            'same_audio_content': sorted(value for value in audio if value.startswith('sha256:')),
            'shared_audio_references': sorted(value for value in audio if not value.startswith('sha256:')),
            'native_visual_verified': False, 'listened': False}


def visual_form_key(features: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Unknown forms cannot be advertised as distinct alternatives."""
    if features.get('evidence') != 'native_structure':
        return None
    return (features.get('layout'), features.get('information_order'),
            tuple(features.get('entrance_effects', [])), tuple(features.get('exit_effects', [])),
            tuple(features.get('loop_effects', [])), tuple(features.get('loop_periods_us', [])),
            tuple(features.get('keyframe_properties', [])))


def _version_status(features: Mapping[str, Any]) -> str:
    value = features.get("jianying_8_8_compatibility")
    return value if value in {"le_8_8", "gt_8_8", "mixed", "unknown"} else "unknown"


def _path_text(record: Mapping[str, Any]) -> str:
    # The leaf preset name is evidence.  Ancestor package names such as
    # "开场结尾..." describe the bundle, not every child preset.
    return _leaf_text(record)


def _semantic_tags(record: Mapping[str, Any], slots: Mapping[str, list[Mapping[str, Any]]]) -> list[str]:
    path = _path_text(record)
    inferred = _infer_counts(_string(record.get("display_name")))
    min_item_count = inferred["item_count_min"]
    max_item_count = inferred["item_count_max"]
    tags: set[str] = set()
    if any(word in path for word in ("提问", "疑问", "问题", "为什么", "怎么", "如何", "问句", "question", "hook")):
        tags.add("question_hook")
    if any(word in path for word in ("开场", "开头", "片头", "标题", "引入", "钩子", "intro", "title")):
        tags.add("title_hook")
    if (min_item_count, max_item_count) == (1, 1) or (
        min_item_count is None and max_item_count is None
        and any(word in path for word in ("单句", "一句", "金句", "观点", "quote", "opinion"))
    ):
        tags.add("single_statement")
    if any(word in path for word in ("金句", "观点", "语录", "结论", "一句话", "态度", "quote", "opinion")):
        tags.add("quote_opinion")
    if any(word in path for word in ("两排", "两行", "双行", "上下", "解释", "说明", "多信息", "two")):
        tags.add("explanation_two_line")
    if min_item_count is not None and max_item_count is not None and 3 <= min_item_count <= max_item_count <= 5:
        tags.add("list_3_5")
    if "序号" in path:
        tags.add("numbered_list")
    if any(word in path for word in ("逐字", "打字", "键入", "显影", "逐个", "逐句", "typing", "word")):
        tags.add("word_by_word")
    if any(word in path for word in ("关键词", "重点", "高亮", "强调", "彩色", "变色", "标注", "突出", "keyword", "highlight")):
        tags.add("keyword_emphasis")
    if any(word in path for word in ("弹幕", "覆盖", "气泡", "环绕", "侧面", "顶部", "底层", "特殊", "overlay", "bubble")):
        tags.add("special_overlay")
    if any(word in path for word in ("竖排", "直排", "垂直", "竖", "vertical")):
        tags.add("vertical")
    if any(word in path for word in ("结尾", "片尾", "收尾", "关注", "点赞", "收藏", "评论", "分享", "私信", "cta", "outro")):
        tags.add("closing_cta")
    profile = record.get("structural_profile", {})
    features = profile.get("feature_counts", {}) if isinstance(profile, Mapping) else {}
    referenced_media = any(
        slot.get("locator", {}).get("segment_refs")
        and slot.get("locator", {}).get("materials_group") in {"images", "videos"}
        for kind in ("image", "video")
        for slot in slots.get(kind, [])
        if isinstance(slot, Mapping)
    )
    if "素材展示" in path and referenced_media:
        tags.add("media_showcase")
        tags.add("broll_visual")
    elif referenced_media:
        tags.add("broll_visual")
    elif features.get("sticker", 0):
        tags.add("visual_decorated_text")
    if features.get("audio", 0) or features.get("audio_effect", 0):
        tags.add("sfx_audio")
    if features.get("animation", 0):
        tags.add("animated_text")
    groups = profile.get("dependency_groups", {}) if isinstance(profile, Mapping) else {}
    if groups.get("transitions", 0):
        tags.add("true_transition")
    return sorted(tags)


def _preview_path(source: Path, root: Path) -> str | None:
    preset_dir = source.parent.parent if source.parent.name == "preset_draft" else source.parent
    images = sorted([*preset_dir.glob("*.jpeg"), *preset_dir.glob("*.jpg"), *preset_dir.glob("*.JPG")], key=lambda p: (p.stem != preset_dir.name, p.name.lower()))
    if not images:
        return None
    try:
        return images[0].relative_to(root).as_posix()
    except ValueError:
        return None


def _duration_summary(drafts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = [draft.get("duration") for draft in drafts if isinstance(draft.get("duration"), (int, float))]
    if not values:
        return {"microseconds": None, "seconds": None}
    value = max(values)
    return {"microseconds": value, "seconds": round(float(value) / 1_000_000, 3)}


def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value or bool(re.search(r"\.(?:ttf|otf|mp3|wav|mp4|mov|png|jpg|jpeg)$", value, re.IGNORECASE))


def _path_kind(key: str) -> str:
    lowered = key.lower()
    if "font" in lowered:
        return "font"
    if "audio" in lowered or lowered.endswith(("mp3", "wav")):
        return "audio"
    if "video" in lowered or "media" in lowered or lowered.endswith(("mp4", "mov")):
        return "video"
    if "image" in lowered or lowered.endswith(("png", "jpg", "jpeg")):
        return "image"
    if "effect" in lowered or "animation" in lowered:
        return "effect"
    return "resource"


def _path_and_resource_dependencies(value: Any, key: str = "", path_result: list[dict[str, Any]] | None = None, resource_result: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path_result = path_result if path_result is not None else []
    resource_result = resource_result if resource_result is not None else []
    if isinstance(value, Mapping):
        for child_key in sorted(value):
            child = value[child_key]
            lowered = str(child_key).lower()
            if isinstance(child, str) and (lowered in _PATH_KEYS or lowered.endswith("_path") or lowered.endswith("_url")) and child and _looks_like_path(child):
                path_result.append({"kind": _path_kind(str(child_key)), "original_path": child, "exists_on_current_machine": Path(child).exists()})
            if isinstance(child, str) and ("resource" in lowered or "effect" in lowered or "animation" in lowered or "font" in lowered) and lowered.endswith("id") and child:
                resource_result.append({"kind": _path_kind(str(child_key)), "key": str(child_key), "remote_resource_id": child})
            _path_and_resource_dependencies(child, str(child_key), path_result, resource_result)
    elif isinstance(value, list):
        for child in value:
            _path_and_resource_dependencies(child, key, path_result, resource_result)
    elif isinstance(value, str) and key.lower() == "content":
        parsed, mode = _parse_content(value)
        if parsed is not None:
            _path_and_resource_dependencies(parsed, "content_json", path_result, resource_result)
    return path_result, resource_result


def _dedupe_dependencies(values: Iterable[Mapping[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    result: dict[tuple[Any, ...], dict[str, Any]] = {}
    for value in values:
        key = tuple(value.get(field) for field in keys)
        result[key] = dict(value)
    return [result[key] for key in sorted(result, key=lambda item: tuple(str(part) for part in item))]


def _candidate_score(record: Mapping[str, Any], preview: str | None, path_dependencies: Iterable[Mapping[str, Any]] = ()) -> dict[str, float]:
    features = record.get("features", {})
    compatibility = _version_status(features if isinstance(features, Mapping) else {})
    version_score = {"le_8_8": 40.0, "unknown": 20.0, "mixed": 15.0, "gt_8_8": 0.0}.get(compatibility, 20.0)
    preview_score = 8.0 if preview else 0.0
    profile = record.get("structural_profile", {})
    groups = profile.get("dependency_groups", {}) if isinstance(profile, Mapping) else {}
    external_groups = [group for group in groups if group not in _CORE_GROUPS]
    dependency_score = max(0.0, 10.0 - 2.0 * len(external_groups))
    path_dependencies = list(path_dependencies)
    missing_path_count = sum(not bool(item.get("exists_on_current_machine")) for item in path_dependencies)
    path_dependency_penalty = -min(20.0, float(len(path_dependencies)))
    missing_path_penalty = -min(20.0, float(missing_path_count) * 2.0)
    frequency = min(float(record.get("cluster_frequency", 1)), 10.0)
    semantic_score = min(5.0, float(len(record.get("categories", []))))
    showcase_priority = 35.0 if "素材展示" in _string(record.get("display_name")) else 0.0
    parts = {
        "version_8_8": version_score,
        "preview": preview_score,
        "dependency_simplicity": dependency_score,
        "path_dependency_penalty": path_dependency_penalty,
        "missing_path_penalty": missing_path_penalty,
        "cluster_frequency": frequency,
        "semantic_specificity": semantic_score,
        "material_showcase_priority": showcase_priority,
    }
    parts["total"] = sum(parts.values())
    return parts


def _build_candidate(record: Mapping[str, Any], source: Path, root: Path, cluster_frequency: int) -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    drafts = _inner_drafts(payload)
    display_name = _string(record.get("display_name")) or source.parent.name
    slots = _extract_slots(payload, display_name)
    # The 素材展示 presets contain fixed, multi-style label layers alongside
    # the replaceable media stack.  They are not safe text parameters; lock
    # those labels as decorative so the visual template remains auto-fillable
    # through its required video/image slots instead of promising an unsafe
    # text rewrite.
    if "素材展示" in display_name:
        for slot in slots.get("text", []):
            if slot.get("requires_manual_style_mapping"):
                slot["decorative_locked"] = True
                slot["required"] = False
                slot["requires_manual_style_mapping"] = False
                slot["style_lock_reason"] = "素材展示固定多样式标签，不作为自动文字参数"
    profile = record.get("structural_profile", {})
    groups = profile.get("dependency_groups", {}) if isinstance(profile, Mapping) else {}
    external_groups = {group: count for group, count in groups.items() if group not in _CORE_GROUPS}
    tags = _semantic_tags(record, slots)
    inferred_counts = _infer_counts(display_name)
    min_item_count = inferred_counts["item_count_min"]
    max_item_count = inferred_counts["item_count_max"]
    preview = _preview_path(source, root)
    features = record.get("features", {})
    versions = features.get("app_version", []) if isinstance(features, Mapping) else []
    compatibility = _version_status(features if isinstance(features, Mapping) else {})
    duration = _duration_summary(drafts)
    path_dependencies, remote_resource_ids = _path_and_resource_dependencies(payload)
    path_dependencies = _dedupe_dependencies(path_dependencies, ("kind", "original_path"))
    remote_resource_ids = _dedupe_dependencies(remote_resource_ids, ("kind", "key", "remote_resource_id"))
    selection_score = _candidate_score({**record, "cluster_frequency": cluster_frequency}, preview, path_dependencies)
    text_slots = slots.get("text", [])
    manual_slot_count = sum(1 for slot in text_slots if slot.get("requires_manual_slot_mapping"))
    manual_style_slot_count = sum(1 for slot in text_slots if slot.get("requires_manual_style_mapping"))
    auto_text_slot_count = sum(
        1 for slot in text_slots
        if slot.get("required") and not slot.get("decorative_locked")
        and not slot.get("requires_manual_slot_mapping")
        and not slot.get("requires_manual_style_mapping")
    )
    auto_visual_slot_count = sum(
        1 for kind in ("image", "video")
        for slot in slots.get(kind, [])
        if slot.get("required") and slot.get("locator", {}).get("segment_refs")
        and slot.get("locator", {}).get("materials_group") in {"images", "videos"}
    )
    auto_fill_ready = (
        manual_slot_count == 0
        and manual_style_slot_count == 0
        and (auto_text_slot_count > 0 or auto_visual_slot_count > 0)
    )
    return {
        "template_id": "",
        "source_path": record["relative_path"],
        "source_hash": record["exact_file_sha256"],
        "structure_hash": record["normalized_structure_sha256"],
        "display_name": display_name,
        "app_version": versions[0] if len(versions) == 1 else versions,
        "app_versions": versions,
        "jianying_8_8_compatibility": compatibility,
        "duration": duration,
        "preview_path": preview,
        "semantic_tags": tags,
        "visual_features": describe_visual_form(payload),
        "min_item_count": min_item_count,
        "max_item_count": max_item_count,
        "inferred_counts": inferred_counts,
        "dependencies": {
            "material_groups": dict(sorted(groups.items())),
            "external_groups": dict(sorted(external_groups.items())),
            "external_group_count": len(external_groups),
            "external_material_count": len(path_dependencies),
            "path_dependencies": path_dependencies,
            "missing_path_count": sum(not item["exists_on_current_machine"] for item in path_dependencies),
            "remote_resource_ids": remote_resource_ids,
            "true_transition": bool(groups.get("transitions", 0)),
            "broll_material_present": "broll_visual" in tags,
        },
        "selection_constraints": {
            "jianying_8_8": compatibility,
            "item_count": {"min": min_item_count, "max": max_item_count},
            "needs_sfx": "sfx_audio" in tags,
            "supports_broll_visual": "broll_visual" in tags,
            "supports_true_transition": bool(groups.get("transitions", 0)),
        },
        "status": "candidate_unvalidated",
        "auto_fill_ready": auto_fill_ready,
        "manual_slot_count": manual_slot_count,
        "manual_style_slot_count": manual_style_slot_count,
        "selection_score": selection_score,
        "cluster_frequency": cluster_frequency,
        "slots": slots,
    }


def _meaningful_feature_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_meaningful_feature_value(child) for child in value.values())
    if isinstance(value, list):
        return any(_meaningful_feature_value(child) for child in value)
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().lower() not in {"none", "false", "0"}
    return bool(value)


def _payload_has_key_fragment(value: Any, fragments: Iterable[str]) -> bool:
    wanted = tuple(fragment.lower() for fragment in fragments)
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in wanted) and _meaningful_feature_value(child):
                return True
            if _payload_has_key_fragment(child, wanted):
                return True
    elif isinstance(value, list):
        return any(_payload_has_key_fragment(child, wanted) for child in value)
    return False


def _matched_name_tokens(name: str, tokens: Iterable[str]) -> list[str]:
    return [token for token in tokens if token.lower() in name]


def _purpose_for_candidate(candidate: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return one plain-language purpose, with evidence kept separate from QA."""

    name = _leaf_text(candidate)
    tags = set(candidate.get("semantic_tags", []))
    groups = candidate.get("dependencies", {}).get("material_groups", {})
    slots = candidate.get("slots", {})
    structural = {
        "text_slots": len(slots.get("text", [])),
        "audio_slots": len(slots.get("audio", [])),
        "image_slots": len(slots.get("image", [])),
        "video_slots": len(slots.get("video", [])),
        "animation_materials": int(groups.get("material_animations", 0)),
        "mask_materials": int(groups.get("common_mask", 0)) + int(groups.get("mask", 0)),
        "effect_materials": int(groups.get("effects", 0)) + int(groups.get("video_effects", 0)),
        "transition_materials": int(groups.get("transitions", 0)),
    }
    name_evidence: list[str] = []

    def choose(code: str, tokens: Iterable[str] = ()) -> str:
        name_evidence.extend(_matched_name_tokens(name, tokens))
        return code

    grid_tokens = ("四宫格", "九宫格", "宫格", "分屏", "画中画", "小窗", "左右画面", "上下画面")
    person_tokens = ("人物", "人像", "头像", "抠像", "扣像", "字在人后", "字左人右", "跟踪", "描边")
    cta_tokens = ("结尾", "片尾", "收尾", "关注", "点赞", "收藏", "评论", "分享", "私信", "cta", "outro")
    question_tokens = ("提问", "疑问", "问题", "为什么", "怎么", "如何", "问句", "question")
    section_tokens = ("小标题", "节点标题", "章节", "人名条", "人物条", "身份条", "角标", "lower third")
    title_tokens = ("封面", "主标题", "开场", "开头", "片头", "标题", "引入", "钩子", "开幕", "intro")
    numbered_tokens = ("序号", "步骤", "排名", "第1", "第2", "第3")
    list_tokens = ("并列", "列表", "清单", "建议", "要点", "多项", "多条", "举例3点", "4点")
    quote_tokens = ("金句", "观点", "语录", "结论", "一句话", "态度", "总结", "quote", "opinion")
    keyword_tokens = ("关键词", "重点", "高亮", "强调", "彩色", "变色", "标注", "突出", "填充", "keyword")
    data_tokens = ("数字", "费用", "价格", "金额", "进度", "时间", "百分比", "数据", "计数", "倒计时")
    multiline_tokens = ("两排", "三排", "多排", "两行", "三行", "多行", "双行", "上下", "解释", "说明", "多信息", "2句字幕", "两句话")
    typing_tokens = ("逐字", "打字", "键入", "显影", "逐个", "逐句", "typing")
    overlay_tokens = ("弹幕", "覆盖", "气泡", "环绕", "侧面", "顶部", "底层", "竖排", "竖版", "全屏", "撕裂", "田字格", "左侧", "右侧", "屏幕下方", "背景")
    caption_tokens = ("字幕", "文字", "字体", "出字", "大字")

    content_sensitive = bool(
        _matched_name_tokens(name, person_tokens)
        or _payload_has_key_fragment(payload, ("matting", "tracking", "segmentation", "portrait_mask", "human_mask"))
    )
    if structural["transition_materials"] > 0:
        primary = choose("true_transition")
    elif _matched_name_tokens(name, grid_tokens):
        primary = choose("grid_split_screen", grid_tokens)
    elif content_sensitive:
        primary = choose("person_processing_composition", person_tokens)
    elif (
        "media_showcase" in tags
        or _matched_name_tokens(name, ("素材展示", "产品展示", "配图", "图片展示", "b-roll", "broll"))
    ):
        primary = choose("media_showcase_broll", ("素材展示", "产品展示", "配图", "图片展示", "b-roll", "broll"))
    elif _matched_name_tokens(name, cta_tokens):
        primary = choose("closing_cta", cta_tokens)
    elif _matched_name_tokens(name, question_tokens):
        primary = choose("question_hook", question_tokens)
    elif _matched_name_tokens(name, section_tokens):
        primary = choose("section_lower_third", section_tokens)
    elif _matched_name_tokens(name, title_tokens):
        primary = choose("cover_main_title", title_tokens)
    elif _matched_name_tokens(name, data_tokens):
        primary = choose("data_progress_price", data_tokens)
    elif _matched_name_tokens(name, numbered_tokens) or "numbered_list" in tags:
        primary = choose("numbered_steps", numbered_tokens)
    elif _matched_name_tokens(name, list_tokens) or "list_3_5" in tags:
        primary = choose("parallel_list", list_tokens)
    elif _matched_name_tokens(name, quote_tokens):
        primary = choose("quote_conclusion", quote_tokens)
    elif _matched_name_tokens(name, keyword_tokens):
        primary = choose("keyword_emphasis", keyword_tokens)
    elif _matched_name_tokens(name, multiline_tokens):
        primary = choose("multi_line_explanation", multiline_tokens)
    elif _matched_name_tokens(name, typing_tokens) or "word_by_word" in tags:
        primary = choose("typing_word_reveal", typing_tokens)
    elif _matched_name_tokens(name, overlay_tokens) or "special_overlay" in tags:
        primary = choose("special_overlay", overlay_tokens)
    elif _matched_name_tokens(name, caption_tokens):
        primary = choose("standard_caption", caption_tokens)
    elif structural["text_slots"] or structural["animation_materials"]:
        primary = "kinetic_short_text"
    else:
        primary = "unclear"

    inferred = candidate.get("inferred_counts", {})
    item_count = inferred.get("item_count") if isinstance(inferred, Mapping) else None
    label, general_purpose = PURPOSE_CATEGORIES[primary]
    if primary == "parallel_list" and item_count:
        plain_purpose = f"把 {item_count} 个卖点、例子、建议或物品按预设节奏列出来。"
    elif primary == "grid_split_screen" and "四宫格" in name:
        plain_purpose = "把画面同时排成四宫格；这份原预设不是逐个依次出现。"
    elif primary == "kinetic_short_text":
        plain_purpose = f"给短文字套用“{candidate.get('display_name', '')}”这类运动效果；只解决文字怎么动，不决定语义位置。"
    else:
        plain_purpose = general_purpose

    if name_evidence:
        confidence = "high"
    elif primary in {"true_transition", "person_processing_composition", "media_showcase_broll"}:
        confidence = "medium"
    elif primary == "kinetic_short_text":
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "primary_category": primary,
        "category_label": label,
        "plain_purpose": plain_purpose,
        "content_sensitive_processing": content_sensitive,
        "classification_confidence": confidence,
        "name_evidence": sorted(set(name_evidence)),
        "structural_evidence": structural,
    }


def _reuse_for_candidate(candidate: Mapping[str, Any], purpose: Mapping[str, Any]) -> tuple[str, str]:
    compatibility = candidate.get("jianying_8_8_compatibility")
    if compatibility in {"gt_8_8", "mixed"}:
        return "unusable", "当前以剪映 8.8 为基线，这份预设含更高版本结构，不能直接进入生产。"
    if compatibility == "unknown":
        return "manual_step", "缺少可靠版本证据，需先在剪映 8.8 手工打开验证。"
    if purpose.get("content_sensitive_processing"):
        return "jianying_recompute", "换入客户素材后必须由剪映重新计算人物抠像、跟踪或内容相关蒙版。"
    if candidate.get("auto_fill_ready"):
        return "direct_reuse", "文字或素材槽位可自动替换；仍需保留原资源 ID，并由用户做一次主观画面验收。"
    if candidate.get("manual_slot_count") or candidate.get("manual_style_slot_count"):
        return "manual_step", "文字拆层或多样式映射存在歧义，需要人工确认一次槽位。"
    return "unusable", "没有可安全自动替换的文字或视觉槽位。"


def build_purpose_inventory(catalog: Mapping[str, Any], preset_root: str | Path) -> dict[str, Any]:
    """Classify every scanned preset; this does not modify source presets or drafts."""

    root = Path(preset_root).expanduser().resolve()
    source_records = [record for record in catalog.get("records", []) if isinstance(record, Mapping)]
    structure_counts = Counter(str(record.get("normalized_structure_sha256")) for record in source_records)
    exact_counts = Counter(str(record.get("exact_file_sha256")) for record in source_records)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source_record in sorted(source_records, key=lambda item: str(item.get("relative_path", ""))):
        relative = str(source_record.get("relative_path", ""))
        source = root / relative
        try:
            payload = json.loads(source.read_text(encoding="utf-8-sig"))
            candidate = _build_candidate(
                source_record,
                source,
                root,
                structure_counts[str(source_record.get("normalized_structure_sha256"))],
            )
            purpose = _purpose_for_candidate(candidate, payload)
            reuse_class, reuse_reason = _reuse_for_candidate(candidate, purpose)
            dependencies = candidate["dependencies"]
            path_dependencies = dependencies.get("path_dependencies", [])
            missing_kinds = Counter(
                str(item.get("kind", "resource"))
                for item in path_dependencies
                if not item.get("exists_on_current_machine")
            )
            records.append({
                "source_path": relative,
                "display_name": candidate["display_name"],
                "preview_path": candidate.get("preview_path"),
                "primary_category": purpose["primary_category"],
                "category_label": purpose["category_label"],
                "plain_purpose": purpose["plain_purpose"],
                "secondary_tags": candidate.get("semantic_tags", []),
                "evidence": {
                    "classification_confidence": purpose["classification_confidence"],
                    "name_tokens": purpose["name_evidence"],
                    "preview_present": bool(candidate.get("preview_path")),
                    "structure": purpose["structural_evidence"],
                },
                "jianying_8_8_compatibility": candidate["jianying_8_8_compatibility"],
                "app_versions": candidate.get("app_versions", []),
                "resource_facts": {
                    "auto_fill_ready": bool(candidate.get("auto_fill_ready")),
                    "manual_slot_count": int(candidate.get("manual_slot_count", 0)),
                    "manual_style_slot_count": int(candidate.get("manual_style_slot_count", 0)),
                    "missing_path_count": int(dependencies.get("missing_path_count", 0)),
                    "missing_path_kinds": dict(sorted(missing_kinds.items())),
                    "remote_resource_id_count": len(dependencies.get("remote_resource_ids", [])),
                    "external_group_count": int(dependencies.get("external_group_count", 0)),
                    "content_sensitive_processing": bool(purpose["content_sensitive_processing"]),
                },
                "reuse_class": reuse_class,
                "reuse_reason": reuse_reason,
                "visual_review_status": "structural_only_unreviewed",
                "exact_file_sha256": source_record.get("exact_file_sha256"),
                "normalized_structure_sha256": source_record.get("normalized_structure_sha256"),
                "exact_duplicate_count": exact_counts[str(source_record.get("exact_file_sha256"))],
                "structural_cluster_count": structure_counts[str(source_record.get("normalized_structure_sha256"))],
            })
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            errors.append({"source_path": relative, "kind": type(exc).__name__, "message": str(exc)})

    category_counts = Counter(record["primary_category"] for record in records)
    reuse_counts = Counter(record["reuse_class"] for record in records)
    version_counts = Counter(record["jianying_8_8_compatibility"] for record in records)
    return {
        "schema": PURPOSE_SCHEMA,
        "preset_root": str(root),
        "summary": {
            "source_records": len(source_records),
            "records": len(records),
            "errors": len(errors),
            "unique_display_names": len({record["display_name"] for record in records}),
            "unique_exact_files": len(exact_counts),
            "structural_clusters": len(structure_counts),
            "previews_present": sum(bool(record["preview_path"]) for record in records),
            "category_counts": dict(sorted(category_counts.items())),
            "empty_categories": sorted(set(PURPOSE_CATEGORIES) - set(category_counts)),
            "reuse_counts": dict(sorted(reuse_counts.items())),
            "compatibility_counts": dict(sorted(version_counts.items())),
        },
        "category_definitions": {
            code: {"label": label, "purpose": purpose}
            for code, (label, purpose) in PURPOSE_CATEGORIES.items()
        },
        "records": records,
        "errors": errors,
    }


def render_purpose_summary(inventory: Mapping[str, Any], examples_per_category: int = 8) -> str:
    summary = inventory.get("summary", {})
    records = [record for record in inventory.get("records", []) if isinstance(record, Mapping)]
    definitions = inventory.get("category_definitions", {})
    lines = [
        "# 剪映预设全量用途分类 v1",
        "",
        "## 扫描口径",
        "",
        f"- 预设文件：{summary.get('records', 0)} / {summary.get('source_records', 0)}（错误 {summary.get('errors', 0)}）",
        f"- 有缩略图：{summary.get('previews_present', 0)}",
        f"- 不重复文件：{summary.get('unique_exact_files', 0)}；不重复结构：{summary.get('structural_clusters', 0)}",
        "- 这里的“1947份”指 1947 个 `preset_draft/draft_content.json`，其中包含大量完全重复或同结构变体。",
        "- 本目录只完成结构与用途分类；`structural_only_unreviewed` 不代表用户已经认可视觉效果。",
        "",
        "## 主用途分类",
        "",
    ]
    category_counts = summary.get("category_counts", {})
    for code, count in sorted(category_counts.items(), key=lambda item: (-int(item[1]), item[0])):
        definition = definitions.get(code, {})
        category_records = [record for record in records if record.get("primary_category") == code]
        category_records.sort(key=lambda record: (
            record.get("jianying_8_8_compatibility") != "le_8_8",
            record.get("reuse_class") != "direct_reuse",
            -int(record.get("structural_cluster_count", 1)),
            str(record.get("display_name", "")),
        ))
        unique_examples: list[str] = []
        for record in category_records:
            display_name = str(record.get("display_name", ""))
            if display_name and display_name not in unique_examples:
                unique_examples.append(display_name)
            if len(unique_examples) >= examples_per_category:
                break
        examples = "、".join(unique_examples) or "无"
        category_reuse = Counter(str(record.get("reuse_class")) for record in category_records)
        reuse_line = "；".join(f"{reuse_code} {reuse_count}" for reuse_code, reuse_count in sorted(category_reuse.items()))
        lines.extend([
            f"### {definition.get('label', code)}（{count}）",
            "",
            str(definition.get("purpose", "")),
            "",
            f"代表：{examples}",
            "",
            f"复用分布：{reuse_line}",
            "",
        ])
    lines.extend(["## 复用等级", ""])
    reuse_explanations = {
        "direct_reuse": "可自动替换明确的文字/素材槽；仍须在剪映 8.8 打开并由用户验收画面。",
        "jianying_recompute": "换素材后要让剪映重新做人物抠像、跟踪或内容相关分析。",
        "manual_step": "需要人工确认槽位、样式映射或版本兼容。",
        "unusable": "当前 8.8 基线下不能安全自动使用，或没有可替换槽位。",
    }
    for code, count in sorted(summary.get("reuse_counts", {}).items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"- `{code}`：{count}。{reuse_explanations.get(code, '')}")
    lines.extend([
        "",
        "## 版本与下一步",
        "",
        *(f"- `{code}`：{count}" for code, count in sorted(summary.get("compatibility_counts", {}).items())),
        f"- 空缺主类别：{'、'.join(definitions.get(code, {}).get('label', code) for code in summary.get('empty_categories', [])) or '无'}",
        "",
        "下一步只从 `le_8_8 + direct_reuse` 中按主用途挑代表预设做用户视觉验收；未验收的预设不得自动进入批量生产。",
        "",
    ])
    return "\n".join(lines)


def write_purpose_inventory(inventory: Mapping[str, Any], json_output: str | Path, markdown_output: str | Path) -> None:
    Path(json_output).write_text(json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    Path(markdown_output).write_text(render_purpose_summary(inventory), encoding="utf-8")


def _candidate_is_usable(candidate: Mapping[str, Any]) -> bool:
    """Keep only templates that can actually receive production inputs.

    A decorative/empty preset is not a parameterized talking-head template.
    Visual-only templates are allowed when an image/video layer is referenced
    by a segment and the candidate explicitly has the visual-material tag.
    """

    text_slots = candidate.get("slots", {}).get("text", [])
    if any(
        slot.get("required") and not slot.get("decorative_locked") and not slot.get("requires_manual_slot_mapping")
        and not slot.get("requires_manual_style_mapping")
        for slot in text_slots
        if isinstance(slot, Mapping)
    ):
        return True
    if "broll_visual" not in candidate.get("semantic_tags", []):
        return False
    for kind in ("image", "video"):
        for slot in candidate.get("slots", {}).get(kind, []):
            locator = slot.get("locator", {}) if isinstance(slot, Mapping) else {}
            if locator.get("segment_refs") and slot.get("required") and locator.get("materials_group") in {"images", "videos"}:
                return True
    return False


def _candidate_family(candidate: Mapping[str, Any]) -> str:
    priority = ("question_hook", "title_hook", "list_3_5", "media_showcase", "single_statement", "explanation_two_line", "word_by_word", "special_overlay", "closing_cta")
    tags = set(candidate.get("semantic_tags", []))
    return next((tag for tag in priority if tag in tags), "other")


def _choose_representative(records: list[Mapping[str, Any]], root: Path) -> dict[str, Any] | None:
    prepared: list[dict[str, Any]] = []
    frequency = len(records)
    for record in records:
        source = root / record["relative_path"]
        if not source.exists():
            continue
        try:
            candidate = _build_candidate(record, source, root, frequency)
            prepared.append({"candidate": candidate})
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
    if not prepared:
        return None
    prepared.sort(key=lambda item: (
        int(item["candidate"].get("auto_fill_ready", False)),
        item["candidate"]["selection_score"]["total"],
        item["candidate"]["selection_score"]["version_8_8"],
        item["candidate"]["selection_score"]["preview"],
        item["candidate"]["selection_score"]["dependency_simplicity"],
        item["candidate"]["source_path"],
    ), reverse=True)
    return prepared[0]["candidate"]


def _coverage_summary(
    selected: list[Mapping[str, Any]],
    representatives: list[Mapping[str, Any]],
    all_representatives: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    # Availability is measured before the 8.8 selection gate so a real but
    # newer-only capability is reported as a version gap, not as nonexistent.
    availability_pool = all_representatives if all_representatives is not None else representatives
    available = Counter(tag for candidate in availability_pool for tag in candidate.get("semantic_tags", []))
    available_8_8 = Counter(tag for candidate in representatives for tag in candidate.get("semantic_tags", []))
    selected_counts = Counter(tag for candidate in selected for tag in candidate.get("semantic_tags", []))
    gaps = []
    for tag, target in SEMANTIC_TARGETS.items():
        if available[tag] == 0:
            if tag == "numbered_list":
                reason = "8.8 兼容且 auto_fill_ready 生产池中没有可自动填充的序号模板"
            else:
                reason = "合格代表中没有可解释的路径/结构事实"
            gaps.append({"tag": tag, "target": target, "selected": 0, "reason": reason})
        elif selected_counts[tag] < target:
            if available_8_8[tag] == 0 and tag == "closing_cta":
                reason = f"有{available[tag]}个CTA但无8.8兼容候选"
            else:
                reason = "候选池中可用且去重后不足配额"
            gaps.append({"tag": tag, "target": target, "selected": selected_counts[tag], "reason": reason})
    has_item_count_5 = any(
        isinstance(candidate.get("min_item_count"), int)
        and isinstance(candidate.get("max_item_count"), int)
        and candidate["min_item_count"] <= 5 <= candidate["max_item_count"]
        for candidate in availability_pool
    )
    if not has_item_count_5:
        gaps.append({"tag": "item_count=5", "target": 1, "selected": 0, "reason": "没有 exact item_count=5 或可解释的 2-5 项目范围候选"})
    return {
        "targets": dict(SEMANTIC_TARGETS),
        "available_representatives": dict(sorted(available.items())),
        "available_8_8_representatives": dict(sorted(available_8_8.items())),
        "selected_counts": dict(sorted(selected_counts.items())),
        "coverage_gap": gaps,
    }


def _quota_repair(selected: list[dict[str, Any]], remaining: list[dict[str, Any]], candidate_limit: int) -> list[dict[str, Any]]:
    """Repair underfilled semantic quotas without breaking hard guarantees."""

    repair_targets = ("single_statement", "explanation_two_line", "word_by_word")
    protected_minimums = {
        "question_hook": SEMANTIC_TARGETS["question_hook"],
        "title_hook": SEMANTIC_TARGETS["title_hook"],
        "keyword_emphasis": SEMANTIC_TARGETS["keyword_emphasis"],
        "quote_opinion": SEMANTIC_TARGETS["quote_opinion"],
        "list_3_5": SEMANTIC_TARGETS["list_3_5"],
    }

    def counts() -> Counter[str]:
        return Counter(tag for item in selected for tag in item.get("semantic_tags", []))

    def preserves_hard_guarantees(candidate: Mapping[str, Any], current_tag: str, prior_repaired_tags: tuple[str, ...]) -> bool:
        after = selected.copy()
        after.remove(candidate)
        after_counts = Counter(tag for item in after for tag in item.get("semantic_tags", []))
        if after_counts["media_showcase"] < SEMANTIC_TARGETS["media_showcase"]:
            return False
        if after_counts["numbered_list"] < SEMANTIC_TARGETS["numbered_list"]:
            return False
        if not any(item.get("min_item_count") == item.get("max_item_count") == 4 for item in after):
            return False
        if any(after_counts[tag] < SEMANTIC_TARGETS[tag] for tag in prior_repaired_tags):
            return False
        return all(after_counts[tag] >= minimum for tag, minimum in protected_minimums.items())

    for repair_index, tag in enumerate(repair_targets):
        while counts()[tag] < SEMANTIC_TARGETS[tag]:
            choices = [
                item for item in remaining
                if tag in item.get("semantic_tags", [])
                and item.get("auto_fill_ready")
                and item.get("jianying_8_8_compatibility") == "le_8_8"
            ]
            if not choices:
                break
            choices.sort(key=lambda item: (item["selection_score"]["total"], item["source_path"]), reverse=True)
            replacement = choices[0]
            removable = [
                item for item in selected
                if tag not in item.get("semantic_tags", [])
                and preserves_hard_guarantees(item, tag, repair_targets[:repair_index])
            ]
            if not removable:
                break
            removable.sort(key=lambda item: (item["selection_score"]["total"], item["source_path"]))
            removed = removable[0]
            selected.remove(removed)
            remaining.remove(replacement)
            remaining.append(removed)
            selected.append(replacement)
    return selected


def build_registry(catalog: Mapping[str, Any], preset_root: str | Path, candidate_limit: int = 25) -> dict[str, Any]:
    """Build a deterministic registry from an existing raw scan catalog."""

    if catalog.get("schema") != CATALOG_SCHEMA:
        raise ValueError("输入不是 preset-catalog v1")
    root = Path(preset_root).expanduser().resolve()
    clustered: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in catalog.get("records", []):
        if isinstance(record, Mapping) and record.get("normalized_structure_sha256"):
            clustered[str(record["normalized_structure_sha256"])].append(record)
    all_representatives = []
    for structure_hash in sorted(clustered):
        candidate = _choose_representative(clustered[structure_hash], root)
        if candidate is not None and _candidate_is_usable(candidate):
            all_representatives.append(candidate)

    representatives = list(all_representatives)

    compatible = [candidate for candidate in representatives if candidate["jianying_8_8_compatibility"] == "le_8_8"]
    compatible_representatives = list(compatible)
    fallback_representatives = [candidate for candidate in representatives if candidate["jianying_8_8_compatibility"] in {"unknown", "mixed", "gt_8_8"}]
    # Version compatibility is a hard first gate.  Newer templates are only
    # considered when the compatible representative pool cannot fill the cap.
    if len(compatible) >= candidate_limit:
        representatives = compatible
    else:
        fallback_representatives.sort(key=lambda candidate: (candidate["jianying_8_8_compatibility"] != "unknown", candidate["jianying_8_8_compatibility"] != "mixed", candidate["source_path"]))
        representatives = compatible + fallback_representatives

    # Prefer templates that can be filled without semantic layer mapping.
    # Manual candidates remain available only when the usable pool cannot fill
    # the requested cap; the runtime selector applies the same policy.
    auto_fill_representatives = [candidate for candidate in representatives if candidate.get("auto_fill_ready")]
    # This is an automatic template library: never re-add a manual candidate
    # merely to satisfy a semantic quota.  If the pool is too small, return
    # fewer candidates and report the capability gap honestly.
    representatives = auto_fill_representatives

    selected: list[dict[str, Any]] = []
    tag_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    semantic_signature_counts: Counter[tuple[str, ...]] = Counter()
    remaining = list(representatives)
    while remaining and len(selected) < candidate_limit:
        broll_selected = tag_counts["broll_visual"]
        broll_remaining = [candidate for candidate in remaining if "broll_visual" in candidate.get("semantic_tags", [])]
        # Reserve the small, explicit visual-template quota before filling
        # broad text categories; this prevents redundant text-only templates
        # from crowding out the two real 素材展示 representatives.
        quota_tags = {"broll_visual"} if broll_selected < SEMANTIC_TARGETS["broll_visual"] and broll_remaining else set()
        fresh_signatures = {
            tuple(sorted(set(candidate.get("semantic_tags", [])).intersection(_SEMANTIC_PRIORITY)))
            for candidate in remaining
            if tuple(sorted(set(candidate.get("semantic_tags", [])).intersection(_SEMANTIC_PRIORITY))) not in semantic_signature_counts
        }
        diversified_pool = [
            candidate for candidate in remaining
            if tuple(sorted(set(candidate.get("semantic_tags", [])).intersection(_SEMANTIC_PRIORITY))) in fresh_signatures
        ]
        pool = diversified_pool if diversified_pool else remaining
        if quota_tags:
            quota_pool = [candidate for candidate in pool if quota_tags.intersection(candidate.get("semantic_tags", []))]
            if not quota_pool:
                quota_pool = broll_remaining
            if quota_pool:
                pool = quota_pool
        showcase_selected = sum(1 for candidate in selected if "media_showcase" in candidate.get("semantic_tags", []))
        showcase_remaining = [candidate for candidate in remaining if "素材展示" in _string(candidate.get("display_name"))]
        if len(selected) >= 2 and showcase_selected < 2 and showcase_remaining:
            showcase_pool = [candidate for candidate in pool if "media_showcase" in candidate.get("semantic_tags", [])]
            pool = showcase_pool or showcase_remaining
        numbered_selected = any("numbered_list" in candidate.get("semantic_tags", []) for candidate in selected)
        numbered_remaining = [candidate for candidate in remaining if "numbered_list" in candidate.get("semantic_tags", [])]
        if not numbered_selected and numbered_remaining:
            numbered_pool = [candidate for candidate in pool if "numbered_list" in candidate.get("semantic_tags", [])]
            pool = numbered_pool or numbered_remaining

        list_selected = [candidate for candidate in selected if "list_3_5" in candidate.get("semantic_tags", [])]
        list_remaining = [candidate for candidate in remaining if "list_3_5" in candidate.get("semantic_tags", [])]
        if len(list_selected) < SEMANTIC_TARGETS["list_3_5"] and list_remaining:
            exact_four_remaining = [candidate for candidate in list_remaining if candidate.get("min_item_count") == candidate.get("max_item_count") == 4]
            numbered_remaining = [candidate for candidate in list_remaining if "numbered_list" in candidate.get("semantic_tags", [])]
            selected_has_exact_four = any(candidate.get("min_item_count") == candidate.get("max_item_count") == 4 for candidate in list_selected)
            selected_has_numbered = any("numbered_list" in candidate.get("semantic_tags", []) for candidate in list_selected)
            preferred_lists = (
                exact_four_remaining if not selected_has_exact_four and exact_four_remaining
                else numbered_remaining if not selected_has_numbered and numbered_remaining
                else list_remaining
            )
            list_pool = [candidate for candidate in pool if candidate in preferred_lists]
            pool = list_pool or preferred_lists

        def rank(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
            tags = set(candidate.get("semantic_tags", []))
            gain = sum(_SEMANTIC_PRIORITY[tag] for tag in tags if tag in SEMANTIC_TARGETS and tag_counts[tag] < SEMANTIC_TARGETS[tag])
            family = _candidate_family(candidate)
            diversity_penalty = family_counts[family]
            signature = tuple(sorted(tags.intersection(_SEMANTIC_PRIORITY)))
            signature_penalty = semantic_signature_counts[signature]
            base = candidate["selection_score"]["total"]
            return gain, -signature_penalty, -diversity_penalty, base, candidate["selection_score"]["version_8_8"], int(candidate.get("preview_path") is not None), candidate["source_path"]
        pool.sort(key=rank, reverse=True)
        candidate = pool[0]
        remaining.remove(candidate)
        family_counts[_candidate_family(candidate)] += 1
        signature = tuple(sorted(set(candidate.get("semantic_tags", [])).intersection(_SEMANTIC_PRIORITY)))
        semantic_signature_counts[signature] += 1
        tag_counts.update(candidate.get("semantic_tags", []))
        candidate["selection_score"] = {**candidate["selection_score"], "coverage_gain": float(sum(1 for tag, target in SEMANTIC_TARGETS.items() if tag in candidate.get("semantic_tags", []) and tag_counts[tag] <= target)), "diversity_penalty": float(family_counts[_candidate_family(candidate)] - 1)}
        candidate["selection_score"]["total"] = round(candidate["selection_score"]["total"] + candidate["selection_score"]["coverage_gain"] - candidate["selection_score"]["diversity_penalty"], 3)
        selected.append(candidate)

    selected = _quota_repair(selected, remaining, candidate_limit)

    for index, candidate in enumerate(selected, 1):
        candidate["template_id"] = f"JIANYING-25-{index:02d}"
    production_88_representatives = [
        candidate for candidate in all_representatives
        if candidate.get("jianying_8_8_compatibility") == "le_8_8" and candidate.get("auto_fill_ready")
    ]
    production_representatives = [candidate for candidate in all_representatives if candidate.get("auto_fill_ready")]
    coverage = _coverage_summary(selected, production_88_representatives, production_representatives)
    return {
        "schema": SCHEMA,
        "version": "candidate_25_v1",
        "source_catalog": "preset_catalog/raw_scan_v1.json",
        "preset_root": str(root),
        "selection_policy": {
            "candidate_limit": candidate_limit,
            "one_representative_per_structure_hash": True,
            "version_hard_gate": "先使用 le_8_8；仅当兼容代表不足 candidate_limit 时按 unknown/mixed/gt_8_8 补位",
            "auto_fill_gate": "最终候选只保留 auto_fill_ready；自动代表不足 candidate_limit 时返回更少并写入 coverage_gap，不用 manual 候选凑数",
            "priority": ["jianying_8_8_compatibility", "coverage_quota", "preview_exists", "dependency_simplicity", "cluster_frequency"],
            "true_transition_rule": "只将 materials.transitions 非空标记为 true_transition；路径词不伪造转场能力",
            "broll_rule": "素材展示叶子名且有被 segment 引用的 video/image 槽可作为真实素材展示候选；普通 video 包装轨不算 B-roll",
            "material_showcase_quota": "优先纳入至少2套叶子名含素材展示且有 required video/image 槽的 8.8 兼容代表",
            "list_diversity_rule": "list_3_5 配额优先 exact item_count=4，并至少纳入一个 numbered_list；3项请求优先 exact 3项模板",
            "slot_fill_rule": "有 segment 引用且非 decorative_locked 的文字槽 required=true；多逻辑槽拆词或歧义重复默认需人工映射，单槽打字/逐字显影可自动填充",
            "manual_slot_policy": "候选优先 auto_fill_ready；运行时默认排除需人工映射的候选，allow_manual=true 才允许回退",
        },
        "summary": {
            "raw_records": len(catalog.get("records", [])),
            "raw_structural_clusters": len(clustered),
            "representatives": len(representatives),
            "unusable_representatives_excluded": len(clustered) - len(all_representatives),
            "selected": len(selected),
            "unique_structure_hashes": len({candidate["structure_hash"] for candidate in selected}),
        },
        "coverage": coverage,
        "coverage_gap": coverage["coverage_gap"],
        "candidates": selected,
    }


def _value_for_slot(slot_values: Mapping[str, Any], kind: str, slot_id: str) -> Any:
    values = slot_values.get(f"{kind}_slots", slot_values.get(kind, {}))
    if isinstance(values, Mapping):
        return values.get(slot_id, _MISSING)
    if isinstance(values, list):
        for item in values:
            if isinstance(item, Mapping) and item.get("slot_id") == slot_id:
                return item.get("value", _MISSING)
    return _MISSING


def _replace_content_text(item: dict[str, Any], replacement: str) -> None:
    content = item.get("content")
    parsed, mode = _parse_content(content)
    if parsed is None:
        if mode == "plain":
            item["content"] = replacement
        return
    styles = parsed.get("styles")
    style_items = styles if isinstance(styles, list) else ([styles] if isinstance(styles, Mapping) else [])
    if len(style_items) > 1:
        raise ValueError("多 style content 无法安全映射新文案，已拒绝替换")
    parsed = copy.deepcopy(dict(parsed))
    parsed["text"] = replacement
    if len(style_items) == 1 and isinstance(style_items[0], Mapping) and isinstance(style_items[0].get("range"), list) and len(style_items[0]["range"]) == 2:
        if isinstance(styles, list):
            parsed["styles"][0]["range"] = [0, len(replacement)]
        else:
            parsed["styles"]["range"] = [0, len(replacement)]
    item["content"] = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


class _Missing:
    pass


_MISSING = _Missing()


def _validate_slot_locator(drafts: list[Mapping[str, Any]], locator: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a registry locator and reject stale/mismatched slot schemas."""

    try:
        draft = drafts[int(locator["inner_draft_index"])]
        materials = draft["materials"]
        items = materials[locator["materials_group"]]
        item = items[int(locator["materials_index"])]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("slot locator 已失效，不能把模板写入当前草稿") from exc
    if not isinstance(materials, Mapping) or not isinstance(items, list) or not isinstance(item, dict):
        raise ValueError("slot locator 指向的 materials 结构无效")
    expected_id = _string(locator.get("material_id"))
    actual_id = _material_id(item)
    if expected_id and actual_id and expected_id != actual_id:
        raise ValueError("slot locator 的 material_id 与当前草稿不一致")
    return draft, item


def apply_template_slots(
    payload: Mapping[str, Any],
    slot_values: Mapping[str, Any],
    candidate: Mapping[str, Any] | None = None,
    slot_schema: Mapping[str, Any] | None = None,
    *,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Return a deep-copied payload with registry-approved text slots applied.

    ``candidate``/``slot_schema`` are the production path.  The two-argument
    form remains as a compatibility helper for local callers, but production
    callers should pass the registry candidate so inner draft names never
    cause a second, potentially different semantic guess.
    """

    result = copy.deepcopy(dict(payload))
    drafts = _inner_drafts(result)
    if candidate is not None and slot_schema is not None:
        raise ValueError("candidate 与 slot_schema 只能显式传一个")
    if candidate is not None:
        if not isinstance(candidate.get("slots"), Mapping):
            raise ValueError("candidate 缺少可写 slots schema")
        slots = candidate["slots"]
    elif slot_schema is not None:
        slots = slot_schema
    else:
        # Legacy helper behavior only; this path is intentionally not used by
        # the generated registry pipeline.
        slots = _extract_slots(result, "")

    text_slots = [slot for slot in slots.get("text", []) if isinstance(slot, Mapping)] if isinstance(slots, Mapping) else []
    pending: list[tuple[Mapping[str, Any], str]] = []
    missing_required: list[str] = []
    for slot in slots["text"]:
        replacement = _value_for_slot(slot_values, "text", slot["slot_id"])
        if replacement is _MISSING:
            if slot.get("required") and not allow_partial:
                missing_required.append(str(slot["slot_id"]))
            continue
        if not isinstance(replacement, str):
            raise TypeError(f"{slot['slot_id']} 必须是字符串")
        if slot.get("decorative_locked"):
            raise ValueError(f"{slot['slot_id']} 是装饰/标点锁定槽，不允许默认替换")
        if slot.get("requires_manual_slot_mapping"):
            raise ValueError(f"{slot['slot_id']} 属于拆词模板，需要人工槽位映射")
        if slot.get("requires_manual_style_mapping"):
            raise ValueError(f"{slot['slot_id']} 含多 style 文案，无法安全自动映射")
        locators = slot.get("locators", [])
        if not isinstance(locators, list) or not locators:
            raise ValueError(f"{slot['slot_id']} 缺少可写 locators")
        for locator in locators:
            _validate_slot_locator(drafts, locator)
        pending.append((slot, replacement))

    if missing_required:
        raise ValueError("缺少 required 文字槽值：" + ", ".join(sorted(missing_required)))

    for slot, replacement in pending:
        for locator in slot.get("locators", []):
            _draft, item = _validate_slot_locator(drafts, locator)
            item["text"] = replacement
            _replace_content_text(item, replacement)
    return result


def _request_tags(request: Mapping[str, Any]) -> set[str]:
    tags: set[str] = set()
    values = request.get("intent", [])
    values = values if isinstance(values, list) else [values]
    aliases = {
        "question": "question_hook", "疑问": "question_hook", "问题": "question_hook", "开头": "title_hook", "标题": "title_hook", "hook": "title_hook",
        "statement": "single_statement", "金句": "quote_opinion", "观点": "quote_opinion", "explanation": "explanation_two_line", "解释": "explanation_two_line",
        "list": "list_3_5", "并列": "list_3_5", "steps": "list_3_5", "步骤": "list_3_5", "word_by_word": "word_by_word", "逐字": "word_by_word",
        "keyword": "keyword_emphasis", "关键词": "keyword_emphasis", "overlay": "special_overlay", "特殊": "special_overlay", "cta": "closing_cta", "结尾": "closing_cta",
        "cover_main_title": "title_hook", "quote_conclusion": "quote_opinion",
        "parallel_list": "list_3_5", "numbered_steps": "numbered_list",
        "multi_line_explanation": "explanation_two_line", "typing_word_reveal": "word_by_word",
        "media_showcase_broll": "media_showcase",
    }
    for value in values:
        text = _string(value).lower()
        if text in aliases:
            tags.add(aliases[text])
        elif text:
            tags.add(text)
    emphasis = _string(request.get("emphasis")).lower()
    if emphasis in {"keyword", "关键词", "重点", "highlight"}:
        tags.add("keyword_emphasis")
    layout = _string(request.get("layout") or request.get("orientation")).lower()
    if layout in {"vertical", "竖排", "直排"}:
        tags.add("vertical")
    if request.get("opening"):
        tags.add("title_hook")
    if request.get("closing"):
        tags.add("closing_cta")
    return tags


def _selection_candidates(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Accept the canonical usage registry as well as existing raw registries."""
    if 'records' not in registry:
        return [dict(c) for c in registry.get('candidates', []) if isinstance(c, Mapping)]
    result = []
    for row in registry['records']:
        contract, gate = row.get('content_contract', {}), row.get('technical_gate', {})
        tags = set(row.get('source', {}).get('semantic_tags', []))
        tags.update(_request_tags({'intent': row.get('primary_category')}))
        features = row.get('visual_features', {})
        if features.get('has_audio'):
            tags.add('sfx_audio')
        result.append({**row, 'semantic_tags': sorted(tags),
                       'min_item_count': contract.get('inferred_item_count_min'),
                       'max_item_count': contract.get('inferred_item_count_max'),
                       'manual_slot_count': contract.get('manual_slot_count', 0),
                       'manual_style_slot_count': contract.get('manual_style_slot_count', 0),
                       'jianying_8_8_compatibility': {
                           'legacy_le_8_8': 'le_8_8', 'modern_gt_8_8': 'gt_8_8',
                           'exact_8_8': 'le_8_8', 'exact_8_8_external_validated': 'le_8_8',
                           'gt_8_8_hold': 'gt_8_8',
                       }.get(gate.get('compatibility_tier'), gate.get('compatibility_tier', 'unknown'))})
    return result


def preset_request_from_plan(plan: Mapping[str, Any], event_id: str) -> dict[str, Any]:
    """Use the same visual events/operations for selection and assembly."""
    events = sorted(plan.get('visual_events', []), key=lambda e: (e['start_us'], e['end_us'], e['id']))
    matches = [i for i, event in enumerate(events) if event.get('id') == event_id]
    if len(matches) != 1:
        raise ValueError('preset-select 需要唯一的当前 visual_event')
    index = matches[0]
    event = events[index]
    request = event.get('preset_request')
    if not isinstance(request, Mapping) or not request.get('intent'):
        raise ValueError('先根据当前表达任务填写 visual_event.preset_request.intent')
    if plan.get('creative_brief') is not None:
        needs = [n for n in event.get('visual_requirements', []) if isinstance(n, Mapping)
                 and n.get('id') == request.get('requirement_id') and n.get('delivery') == 'text']
        if len(needs) != 1:
            raise ValueError('preset_request.requirement_id 必须绑定当前由文字交付的观察需求')
    operations = {op['id']: op for op in plan.get('operations', []) if op.get('id')}
    def template_ids(item):
        refs = {ref for technique in item.get('techniques', []) for ref in technique.get('operation_ids', [])}
        selections = item.get('preset_selections', [])
        if not isinstance(selections, list) or any(not isinstance(row, Mapping) for row in selections):
            raise ValueError(f"{item.get('id')}: preset_selections 必须为列表")
        chosen = {str(row['template_id']) for row in selections if row.get('template_id')}
        return sorted(chosen | {str(op.get('new_template_id') or op.get('template_id')) for ref in refs
                        if (op := operations.get(ref, {})).get('kind') in ('add_preset_group', 'replace_preset_group')
                        and (op.get('new_template_id') or op.get('template_id'))})
    neighbors = [{'event_id': e['id'], 'audience_need': e.get('audience_need'),
                  'composition': e.get('composition'), 'template_ids': template_ids(e),
                  'selection_state': 'selected' if template_ids(e) else 'unselected' if e.get('preset_request') else 'not_requested'}
                 for i in (index - 1, index + 1) if 0 <= i < len(events) for e in [events[i]]]
    prior = [{'event_id': e['id'], 'template_ids': template_ids(e)} for e in events[:index] if e.get('preset_request') or template_ids(e)]
    return {**copy.deepcopy(dict(request)), 'duration_us': event['end_us'] - event['start_us'],
            'creative_brief': copy.deepcopy(plan.get('creative_brief', {})),
            'visual_requirements': [{key: copy.deepcopy(need.get(key)) for key in ('id', 'subject', 'observable', 'delivery', 'time_behavior')}
                                    for need in event.get('visual_requirements', [])],
            'sequence_context': {'event_id': event_id, 'audience_need': event.get('audience_need'),
                                  'expression_role': event.get('expression_role'),
                                  'composition': event.get('composition'),
                                  'transition': copy.deepcopy(event.get('transition', {})),
                                  'current_event_selections': [{key: copy.deepcopy(row.get(key)) for key in ('node_id', 'template_id', 'visual_features')}
                                                               for row in event.get('preset_selections', [])],
                                  'neighbors': neighbors,
                                  'prior_events': prior,
                                  'unresolved_prior_events': [row['event_id'] for row in prior if not row['template_ids']],
                                  'selected_expressions': [dict(event_id=e['id'], **{
                                      key: copy.deepcopy(row.get(key)) for key in ('node_id', 'template_id', 'visual_features')})
                                      for e in events if e['id'] != event_id for row in e.get('preset_selections', [])],
                                  'used_template_ids': [tid for e in events if e['id'] != event_id for tid in template_ids(e)]}}


def attach_preset_selection(plan: Mapping[str, Any], query: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    """Save a creative choice before assembly so the next query sees it.

    A query from an earlier plan cannot silently become a current choice. This
    handoff does not manufacture operations, preview evidence or Writer rights.
    """
    event_id = decision.get('event_id')
    current = preset_request_from_plan(plan, event_id)
    requested = copy.deepcopy(query.get('request', {}))
    requested.pop('top_k', None)
    expected = copy.deepcopy(current)
    expected.pop('top_k', None)
    if requested != expected:
        raise ValueError('预设查询上下文已变化；用当前计划重新 preset-select 后比较选择')
    if current['sequence_context']['unresolved_prior_events']:
        raise ValueError('前面需要预设的事件尚未选择：' + ', '.join(current['sequence_context']['unresolved_prior_events']))
    for field in ('node_id', 'template_id', 'reason'):
        if not isinstance(decision.get(field), str) or not decision[field].strip():
            raise ValueError(f'预设选择需要 {field}')
    rows = [row for row in query.get('top_k', []) if row.get('template_id') == decision['template_id']]
    if len(rows) != 1:
        raise ValueError('所选预设不在当前候选中；扩大同用途查询后再选择')
    candidate = rows[0]
    source = (candidate.get('source', {}).get('resolved_source_path')
              or candidate.get('source', {}).get('source_path') or candidate.get('source_path'))
    review = decision.get('reviewed', {})
    if (not isinstance(review, Mapping) or not isinstance(review.get('observation'), str) or not review['observation'].strip()
            or review.get('scope') not in {'source_structure', 'image', 'video_frames', 'video_segment'}):
        raise ValueError('须记录真实 reviewed.scope 与 observation；结构查看不等于原生播放')
    if not source or not Path(source).is_file() or Path(review.get('source_path', '')).resolve() != Path(source).resolve():
        raise ValueError('预设查看必须绑定所选候选的实际源文件')
    if review['scope'] != 'source_structure' and not Path(review.get('preview_path', '')).is_file():
        raise ValueError('画面查看须绑定实际预览文件；不能把源 JSON 当成已查看画面')
    comparisons = decision.get('compared_with', [])
    if not isinstance(comparisons, list):
        raise ValueError('compared_with 须列出实际比较的候选与取舍')
    offered = {row['template_id'] for row in query.get('top_k', [])}
    for alternative in comparisons:
        if (not isinstance(alternative, Mapping) or alternative.get('template_id') not in offered
                or alternative.get('template_id') == decision['template_id'] or not alternative.get('reason')):
            raise ValueError('比较项须为本次其他候选及具体取舍理由')
    result = copy.deepcopy(dict(plan))
    event = next(e for e in result['visual_events'] if e['id'] == event_id)
    selections = event.setdefault('preset_selections', [])
    row = {key: copy.deepcopy(decision[key]) for key in ('node_id', 'template_id', 'reason', 'reviewed')}
    row.update(compared_with=copy.deepcopy(comparisons), visual_features=copy.deepcopy(candidate.get('visual_features', {})),
               source_path=str(Path(source).resolve()), request=copy.deepcopy(expected),
               requirement_id=current.get('requirement_id'),
               prior_events=copy.deepcopy(current['sequence_context']['prior_events']))
    selections[:] = [old for old in selections if old.get('node_id') != row['node_id']]
    selections.append(row)
    return result


def validate_preset_selections(plan: Mapping[str, Any], *, require_choices: bool = False) -> dict[str, Any]:
    errors, changes = [], []
    operations = {op['id']: op for op in plan.get('operations', []) if isinstance(op, Mapping) and op.get('id')}
    for event in plan.get('visual_events', []):
        if not isinstance(event, Mapping):
            continue
        selections = event.get('preset_selections', [])
        refs = {ref for t in event.get('techniques', []) if isinstance(t, Mapping) for ref in t.get('operation_ids', [])}
        imports = [operations[ref] for ref in refs if operations.get(ref, {}).get('kind') in {'add_preset_group', 'replace_preset_group'}]
        if not selections and not (require_choices and (imports or event.get('preset_request'))):
            continue
        if not event.get('preset_request'):
            errors.append(f"{event.get('id')}: 已选预设缺少按内容提出的 preset_request")
            continue
        try:
            current = preset_request_from_plan(plan, event['id'])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if not isinstance(selections, list) or any(not isinstance(row, Mapping) for row in selections):
            errors.append(f"{event['id']}: preset_selections 必须为选择记录列表")
            continue
        chosen = {(row.get('node_id'), row.get('template_id')) for row in selections}
        actual = {(op.get('node_id'), op.get('new_template_id') or op.get('template_id')) for op in imports}
        if chosen != actual or not chosen:
            errors.append(f"{event['id']}: 实际预设操作与已比较选定的 node/template 不一致")
        for row in selections:
            if row.get('prior_events') != current['sequence_context']['prior_events']:
                errors.append(f"{event['id']}: 之前的预设选择已变化或查询时为空，须在当前上下文复核")
            old = row.get('request', {})
            for key in ('intent', 'requirement_id', 'item_count', 'duration_us', 'preferred_visual_features', 'creative_brief', 'visual_requirements'):
                if old.get(key) != current.get(key):
                    errors.append(f"{event['id']}: 预设选择后的 {key} 已变化，须重新查询")
            for key in ('audience_need', 'expression_role', 'composition', 'transition'):
                if old.get('sequence_context', {}).get(key) != current['sequence_context'].get(key):
                    errors.append(f"{event['id']}: 预设选择后的当前 {key} 已变化，须重新查询")
            if plan.get('creative_brief') is not None:
                needs = [need for need in event.get('visual_requirements', []) if need.get('id') == row.get('requirement_id')]
                bound = {op['id'] for op in imports if (op.get('node_id'), op.get('new_template_id') or op.get('template_id'))
                         == (row.get('node_id'), row.get('template_id'))}
                if len(needs) != 1 or not bound or not bound <= set(needs[0].get('operation_ids', [])):
                    errors.append(f"{event['id']}: 所选预设没有交付原先的文字观察需求")
            if old.get('sequence_context', {}).get('neighbors') != current['sequence_context']['neighbors']:
                changes.append({'event_id': event['id'], 'node_id': row.get('node_id'),
                                'reason': '后续邻段已选定或构图改变；整片复核对照当前组合'})
    if require_choices:
        declared = {ref for e in plan.get('visual_events', []) for t in e.get('techniques', []) for ref in t.get('operation_ids', [])}
        missing = [op['id'] for op in operations.values() if op.get('kind') in {'add_preset_group', 'replace_preset_group'} and op['id'] not in declared]
        if missing:
            errors.append('预设操作没有连接任何当前表达事件：' + ', '.join(missing))
    return {'ok': not errors, 'errors': errors, 'sequence_changes': changes}


def select_template(registry: Mapping[str, Any], semantic_request: Mapping[str, Any]) -> dict[str, Any]:
    """Rank usable expressions with sequence context; selection never grants readiness."""

    candidates = _selection_candidates(registry)
    wanted = _request_tags(semantic_request)
    item_count = semantic_request.get("item_count")
    needs_sfx = semantic_request.get("needs_sfx") is True
    allow_manual = semantic_request.get("allow_manual") is True
    context = semantic_request.get('sequence_context') or {}
    strict = 'records' in registry or 'sequence_context' in semantic_request
    indexed = {c.get('template_id'): c for c in candidates}
    neighboring_ids = [tid for row in context.get('neighbors', []) for tid in row.get('template_ids', [])]
    neighboring_forms = [visual_form_key(indexed.get(tid, {}).get('visual_features', {})) for tid in neighboring_ids]
    neighboring_forms = [form for form in neighboring_forms if form is not None]
    used = Counter(context.get('used_template_ids', []))
    transition = context.get('transition', {}).get('intent')
    transition_axes = context.get('transition', {}).get('axes', ['text'])
    preferred = semantic_request.get('preferred_visual_features') or {}
    requested_role = _request_tags({'intent': semantic_request.get('intent', [])})
    excluded = Counter()
    ranked = []
    for candidate in candidates:
        tags = set(candidate.get("semantic_tags", []))
        candidate_role = (_request_tags({'intent': candidate['primary_category']})
                          if candidate.get('primary_category') else tags)
        if strict and requested_role and not requested_role.intersection(candidate_role):
            excluded['semantic_mismatch'] += 1
            continue
        gate = candidate.get('technical_gate', {})
        if strict and (candidate.get('jianying_8_8_compatibility') == 'gt_8_8'
                       or gate.get('visual_validation') == 'rejected'
                       or str(gate.get('production_gate', '')).startswith('disabled')):
            excluded['known_incompatible_or_rejected'] += 1
            continue
        constraints = candidate.get("selection_constraints", {})
        score = float(candidate.get("selection_score", {}).get("total", 0))
        reasons: list[str] = []
        manual_slot_count = int(candidate.get("manual_slot_count", 0) or 0)
        manual_style_slot_count = int(candidate.get("manual_style_slot_count", 0) or 0)
        manual_total = manual_slot_count + manual_style_slot_count
        if strict and manual_total and not allow_manual:
            excluded['manual_mapping_required'] += 1
            continue
        if manual_total and not allow_manual:
            # Keep a diagnostic entry out of the normal top-k whenever an
            # automatic candidate exists; the large penalty also handles
            # legacy registries that predate an explicit filtered pool.
            score -= 1000.0
            reasons.append(f"需人工映射 {manual_total} 个文字槽，默认排除")
        elif manual_total:
            reasons.append(f"允许人工映射 {manual_total} 个文字槽")
        matched = sorted(wanted.intersection(tags))
        score += 25.0 * len(matched)
        if matched:
            reasons.append("语义标签命中：" + ", ".join(matched))
        if isinstance(item_count, int):
            minimum = constraints.get("item_count", {}).get("min", candidate.get("min_item_count", 0))
            maximum = constraints.get("item_count", {}).get("max", candidate.get("max_item_count", 0))
            if minimum is None or maximum is None:
                score -= 18.0
                reasons.append("叶子名没有可靠 item_count 证据，不命中项目数请求")
            elif minimum <= item_count <= maximum or (minimum == maximum == 0 and item_count == 0):
                score += 18.0
                reasons.append(f"item_count={item_count} 落在槽位范围内")
                if "list_3_5" in tags and minimum == maximum == item_count:
                    score += 12.0
                    reasons.append(f"列表模板 exact item_count={item_count}")
            else:
                if strict:
                    excluded['item_count_mismatch'] += 1
                    continue
                score -= 24.0
                reasons.append(f"item_count={item_count} 超出槽位范围 {minimum}-{maximum}")
        if needs_sfx:
            if "sfx_audio" in tags:
                score += 12.0
                reasons.append("需要音效且候选含音频事实")
            else:
                score -= 12.0
                reasons.append("需要音效但候选没有音频事实")
        compatibility = candidate.get("jianying_8_8_compatibility", "unknown")
        if compatibility == "le_8_8":
            score += 8.0
            reasons.append("兼容剪映 8.8")
        elif compatibility == "gt_8_8":
            score -= 8.0
            reasons.append("版本高于剪映 8.8，降权")
        features = candidate.get('visual_features', {})
        form = visual_form_key(features)
        if strict:
            reasons.append('当前字体、画布、资源与实际文案仍须按需预检')
            available = semantic_request.get('duration_us')
            native_seconds = candidate.get('timing_contract', {}).get('native_duration_seconds')
            if type(available) is int and available > 0 and type(native_seconds) in (int, float) and native_seconds > 0:
                if native_seconds * 1_000_000 > available:
                    score -= 24
                    reasons.append('原生时序长于当前事件；须核对能否按合同调整，不能截掉入场或阅读期')
                else:
                    reasons.append('原生时序可装入当前事件，阅读停留仍按实际文案核对')
            if form is None:
                reasons.append('实际表现特征未知，不能把未知项算作新的表现方式')
            for key in ('layout', 'information_order'):
                if preferred.get(key) and features.get(key) == preferred[key] and form is not None:
                    score += 20
                    reasons.append(f'符合所需 {key}={preferred[key]}')
            if transition == 'change' and 'text' in transition_axes:
                if candidate.get('template_id') in neighboring_ids:
                    score -= 16
                    reasons.append('与相邻段使用同一预设，当前需要变化')
                if form is not None and form in neighboring_forms:
                    score -= 24
                    reasons.append('布局、信息顺序及进退场机制与相邻段相同')
                score -= min(used[candidate.get('template_id')], 5) * 3
            elif transition == 'hold' and form is not None and form in neighboring_forms:
                score += 24
                reasons.append('延续相邻表达机制，符合当前连续性设计')
        if not reasons:
            reasons.append("无直接语义命中，作为兼容回退候选")
        comparisons = []
        for row in context.get('selected_expressions', []):
            comparison = compare_expression_features(features, row.get('visual_features', {}))
            comparisons.append({'event_id': row.get('event_id'), 'template_id': row.get('template_id'), **comparison})
            if transition == 'change' and 'sound' in transition_axes and row.get('template_id') in neighboring_ids:
                if comparison['same_audio_content'] or comparison['shared_audio_references']:
                    score -= 12
                    reasons.append('当前要求声音变化，但与相邻段复用同一音频内容或源引用')
        ranked.append({"template_id": candidate.get("template_id"), "score": round(score, 3), "reasons": reasons,
                       "comparison_to_selected": comparisons, "candidate": candidate})
    ranked.sort(key=lambda item: (-item["score"], str(item["template_id"])))
    top_k = semantic_request.get("top_k", 5)
    try:
        top_k = max(1, int(top_k))
    except (TypeError, ValueError):
        top_k = 5
    top = ranked[:top_k]
    if strict and ranked:
        # Animation IDs alone must not fill the shortlist with the same layout
        # and reveal order. Keep full motion fingerprints for other comparisons.
        def shortlist_form(row):
            key = visual_form_key(row['candidate'].get('visual_features', {}))
            return key[:2] if key and all(v not in (None, '', 'unknown') for v in key[:2]) else None

        top, remaining = [], list(ranked)
        while remaining and len(top) < top_k:
            seen_forms = {shortlist_form(r) for r in top}
            selected = max(remaining, key=lambda r: (
                r['score'] - (14 if (key := shortlist_form(r))
                             is not None and key in seen_forms else 0),
                -ranked.index(r)))
            top.append(selected)
            remaining.remove(selected)
    fallback = None
    if ranked:
        fallback_item = min(
            ranked,
            key=lambda item: (
                1 if ((item["candidate"].get("manual_slot_count", 0) or item["candidate"].get("manual_style_slot_count", 0)) and not allow_manual) else 0,
                0 if item["candidate"].get("jianying_8_8_compatibility") == "le_8_8" else 1,
                -item["score"],
                str(item["template_id"]),
            ),
        )
        fallback = {"template_id": fallback_item["template_id"], "reason": "无完全匹配时选择确定性最高的兼容候选"}
    if strict:
        fallback = ({'template_id': top[0]['template_id'],
                     'reason': '同表达候选仍须核对实际效果及资源；不可静默更换表达职责'} if top else None)
    fields = ('template_id', 'score', 'reasons')
    rows = [{**{key: item[key] for key in fields},
             **({'visual_features': item['candidate'].get('visual_features', {'evidence': 'unknown'}),
                 'comparison_to_selected': item['comparison_to_selected'],
                 'source': item['candidate'].get('source', {'source_path': item['candidate'].get('source_path')}),
                 'timing_contract': item['candidate'].get('timing_contract', {}),
                 'content_contract': item['candidate'].get('content_contract', {}),
                 'technical_gate': item['candidate'].get('technical_gate', {})} if strict else {})} for item in top]
    return {"request": dict(semantic_request), "top_k": rows, "fallback": fallback,
            'excluded_counts': dict(excluded), 'current_video_ready': False,
            'gap': ('没有同表达的可用候选；检查相同职责的资源修复或等效实现，保留当前需求' if not top else None)}


def write_registry(registry: Mapping[str, Any], output: str | Path) -> None:
    Path(output).write_text(json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 raw_scan_v1.json 生成剪映候选模板注册表")
    parser.add_argument("catalog", type=Path)
    parser.add_argument("preset_root", type=Path)
    parser.add_argument("--output", "-o", type=Path, required=True)
    args = parser.parse_args(argv)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8-sig"))
    write_registry(build_registry(catalog, args.preset_root), args.output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
