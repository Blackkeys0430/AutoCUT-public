from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .hashing import object_sha256


IdFactory = Callable[[], str]
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


@dataclass(frozen=True)
class KeywordSpan:
    start: int
    end: int
    color: str = "#FFD600"
    size_scale: float = 1.12


@dataclass(frozen=True)
class SubtitleCard:
    start_us: int
    duration_us: int
    zh: str
    en: str
    zh_keywords: tuple[KeywordSpan, ...] = ()
    en_keywords: tuple[KeywordSpan, ...] = ()


@dataclass(frozen=True)
class SubtitlePlan:
    zh_template_track: str
    en_template_track: str
    cards: tuple[SubtitleCard, ...]
    zh_track_name: str = "JY_ZH_SUBTITLES"
    en_track_name: str = "JY_EN_SUBTITLES"


@dataclass(frozen=True)
class SubtitlePatchResult:
    content: dict[str, Any]
    zh_track_id: str
    en_track_id: str
    card_count: int
    before_object_sha256: str
    after_object_sha256: str


def _keyword(value: dict[str, Any]) -> KeywordSpan:
    return KeywordSpan(
        start=int(value["start"]),
        end=int(value["end"]),
        color=str(value.get("color", "#FFD600")),
        size_scale=float(value.get("size_scale", 1.12)),
    )


def load_subtitle_plan(path: str | Path) -> SubtitlePlan:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("subtitle plan must be a JSON object")
    cards_value = value.get("cards")
    if not isinstance(cards_value, list):
        raise ValueError("subtitle plan cards must be an array")
    cards = tuple(
        SubtitleCard(
            start_us=int(item["start_us"]),
            duration_us=int(item["duration_us"]),
            zh=str(item["zh"]),
            en=str(item["en"]),
            zh_keywords=tuple(_keyword(span) for span in item.get("zh_keywords", [])),
            en_keywords=tuple(_keyword(span) for span in item.get("en_keywords", [])),
        )
        for item in cards_value
    )
    plan = SubtitlePlan(
        zh_template_track=str(value["zh_template_track"]),
        en_template_track=str(value["en_template_track"]),
        zh_track_name=str(value.get("zh_track_name", "JY_ZH_SUBTITLES")),
        en_track_name=str(value.get("en_track_name", "JY_EN_SUBTITLES")),
        cards=cards,
    )
    validate_subtitle_plan(plan)
    return plan


def _validate_keywords(text: str, spans: Sequence[KeywordSpan], label: str) -> None:
    previous_end = 0
    for span in sorted(spans, key=lambda item: (item.start, item.end)):
        if span.start < 0 or span.end <= span.start or span.end > len(text):
            raise ValueError(f"{label} keyword range is outside text: [{span.start}, {span.end})")
        if span.start < previous_end:
            raise ValueError(f"{label} keyword ranges overlap")
        if not _COLOR.fullmatch(span.color):
            raise ValueError(f"{label} keyword color must be #RRGGBB")
        if span.size_scale <= 0:
            raise ValueError(f"{label} keyword size_scale must be positive")
        previous_end = span.end


def validate_subtitle_plan(plan: SubtitlePlan) -> None:
    if not plan.zh_template_track or not plan.en_template_track:
        raise ValueError("both template tracks are required")
    if plan.zh_template_track == plan.en_template_track:
        raise ValueError("Chinese and English template tracks must differ")
    if not plan.cards:
        raise ValueError("subtitle plan must contain at least one card")
    previous_end = 0
    for index, card in enumerate(sorted(plan.cards, key=lambda item: item.start_us)):
        if card.start_us < 0 or card.duration_us <= 0:
            raise ValueError(f"card {index} has an invalid time range")
        if card.start_us < previous_end:
            raise ValueError("subtitle cards overlap")
        if not card.zh.strip() or not card.en.strip():
            raise ValueError(f"card {index} requires Chinese and English text")
        _validate_keywords(card.zh, card.zh_keywords, f"card {index} Chinese")
        _validate_keywords(card.en, card.en_keywords, f"card {index} English")
        previous_end = card.start_us + card.duration_us


def _collect_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "id" and isinstance(item, str) and item:
                found.add(item)
            found.update(_collect_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_collect_ids(item))
    return found


def _collect_id_list(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "id" and isinstance(item, str) and item:
                found.append(item)
            found.extend(_collect_id_list(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_id_list(item))
    return found


def _find_track(content: dict[str, Any], selector: str) -> dict[str, Any]:
    matches = [
        track
        for track in content.get("tracks") or []
        if track.get("id") == selector or track.get("name") == selector
    ]
    if len(matches) != 1:
        raise ValueError(f"template track selector must match exactly once: {selector!r}")
    return matches[0]


def _template(content: dict[str, Any], selector: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    track = _find_track(content, selector)
    segments = track.get("segments") or []
    if track.get("type") != "text" or len(segments) != 1:
        raise ValueError(f"template track must be one-segment text track: {selector!r}")
    segment = segments[0]
    material_id = segment.get("material_id")
    texts = (content.get("materials") or {}).get("texts") or []
    materials = [material for material in texts if material.get("id") == material_id]
    if len(materials) != 1:
        raise ValueError(f"template text material is missing or duplicated: {material_id!r}")
    references = [
        item
        for item_track in content.get("tracks") or []
        for item in item_track.get("segments") or []
        if item.get("material_id") == material_id
    ]
    if len(references) != 1:
        raise ValueError(f"template material must have exactly one reference: {material_id!r}")
    return track, segment, materials[0]


def _rgb(color: str) -> list[float]:
    return [int(color[index : index + 2], 16) / 255.0 for index in (1, 3, 5)]


def _styled_content(template: dict[str, Any], text: str, keywords: Sequence[KeywordSpan]) -> str:
    authored = json.loads(str(template.get("content", "{}")))
    styles = authored.get("styles") or []
    if not styles:
        raise ValueError(f"template material has no text style: {template.get('id')}")
    base = copy.deepcopy(styles[0])
    boundaries = {0, len(text)}
    for span in keywords:
        boundaries.update((span.start, span.end))
    ordered = sorted(boundaries)
    result: list[dict[str, Any]] = []
    for start, end in zip(ordered, ordered[1:]):
        if start == end:
            continue
        keyword = next((span for span in keywords if span.start <= start and end <= span.end), None)
        style = copy.deepcopy(base)
        style["range"] = [start, end]
        if keyword is not None:
            if isinstance(style.get("size"), (int, float)):
                style["size"] = float(style["size"]) * keyword.size_scale
            solid = style.setdefault("fill", {}).setdefault("content", {}).setdefault("solid", {})
            solid["color"] = _rgb(keyword.color)
            solid["alpha"] = 1.0
        result.append(style)
    authored["text"] = text
    authored["styles"] = result
    return json.dumps(authored, ensure_ascii=False, separators=(",", ":"))


def _new_id(existing: set[str], id_factory: IdFactory) -> str:
    value = str(id_factory())
    if not value or value in existing:
        raise ValueError(f"generated subtitle ID is empty or duplicated: {value!r}")
    existing.add(value)
    return value


def build_bilingual_subtitles(
    content: dict[str, Any],
    plan: SubtitlePlan,
    *,
    id_factory: IdFactory = lambda: str(uuid.uuid4()).upper(),
) -> SubtitlePatchResult:
    validate_subtitle_plan(plan)
    zh_track, zh_segment, zh_material = _template(content, plan.zh_template_track)
    en_track, en_segment, en_material = _template(content, plan.en_template_track)
    if zh_track.get("id") == en_track.get("id") or zh_material.get("id") == en_material.get("id"):
        raise ValueError("Chinese and English templates must be independent")

    candidate = copy.deepcopy(content)
    existing = _collect_ids(candidate)
    template_track_ids = {zh_track["id"], en_track["id"]}
    template_material_ids = {zh_material["id"], en_material["id"]}
    candidate["tracks"] = [track for track in candidate.get("tracks") or [] if track.get("id") not in template_track_ids]
    texts = (candidate.setdefault("materials", {}).setdefault("texts", []))
    candidate["materials"]["texts"] = [item for item in texts if item.get("id") not in template_material_ids]

    zh_track_new = copy.deepcopy(zh_track)
    en_track_new = copy.deepcopy(en_track)
    zh_track_new["id"] = _new_id(existing, id_factory)
    en_track_new["id"] = _new_id(existing, id_factory)
    zh_track_new["name"] = plan.zh_track_name
    en_track_new["name"] = plan.en_track_name
    zh_track_new["segments"] = []
    en_track_new["segments"] = []

    generated_materials: list[dict[str, Any]] = []
    for card in sorted(plan.cards, key=lambda item: item.start_us):
        for language, text, keywords, template_material, template_segment, output_track in (
            ("zh", card.zh, card.zh_keywords, zh_material, zh_segment, zh_track_new),
            ("en", card.en, card.en_keywords, en_material, en_segment, en_track_new),
        ):
            material = copy.deepcopy(template_material)
            material_id = _new_id(existing, id_factory)
            material["id"] = material_id
            material["content"] = _styled_content(template_material, text, keywords)
            segment = copy.deepcopy(template_segment)
            segment["id"] = _new_id(existing, id_factory)
            segment["material_id"] = material_id
            segment["target_timerange"] = {"start": card.start_us, "duration": card.duration_us}
            generated_materials.append(material)
            output_track["segments"].append(segment)

    candidate["materials"]["texts"].extend(generated_materials)
    candidate["tracks"].extend((zh_track_new, en_track_new))
    candidate_ids = _collect_id_list(candidate)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise AssertionError("candidate subtitle IDs are not unique")

    untouched_before = [track for track in content.get("tracks") or [] if track.get("id") not in template_track_ids]
    untouched_after = candidate["tracks"][: len(untouched_before)]
    if untouched_before != untouched_after:
        raise AssertionError("non-template tracks changed")
    return SubtitlePatchResult(
        content=candidate,
        zh_track_id=zh_track_new["id"],
        en_track_id=en_track_new["id"],
        card_count=len(plan.cards),
        before_object_sha256=object_sha256(content),
        after_object_sha256=object_sha256(candidate),
    )


__all__ = [
    "KeywordSpan",
    "SubtitleCard",
    "SubtitlePatchResult",
    "SubtitlePlan",
    "build_bilingual_subtitles",
    "load_subtitle_plan",
    "validate_subtitle_plan",
]
