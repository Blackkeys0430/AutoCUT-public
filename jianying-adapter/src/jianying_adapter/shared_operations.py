# Copyright (c) 2026 Blackkeys0430 — AutoCUT original project code.
# Origin: https://github.com/Blackkeys0430/AutoCUT-public
# SPDX-License-Identifier: LicenseRef-AutoCUT-Personal-Use-1.0
"""Canonical, data-only CandidatePlan operations.

The production entry point resolves this module through
``get_shared_operation_registry``.  Operations receive only the draft,
declarative operation spec and build context; no video directory script may
inject a callback or mutate module globals.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from .preset_preflight import normalize_text_role
from .aroll_motion import animate_aroll_transform
from .broll_motion import animate_broll_transform
from .preset_motion import animate_preset_group_transform
from .caption_replacement import replace_ordinary_captions_with_presets
from .video_mask import apply_video_mask
from .video_split import split_video_track
from .video_effects import add_video_effect
from .text_animation import apply_text_animation
from .text_style_variant import apply_text_style_variant
from .action_audio import mix_action_audio
from .preset_simplification import simplify_preset_motion
from .preset_timing import retime_preset_text_tracks
from .text_layout import reflow_caption, bbox_intersection, transformed_text_bbox, union_bbox, measure_reading_glyphs

CANVAS = (1080, 1920)
SUPPORTED_CANVASES = (CANVAS, (1920, 1080))
UNIT_TO_PX = 5.0
SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, SAFE_BOTTOM = 54.0, 1026.0, 70.0, 1280.0
_PRESET = re.compile(r"^JY_PRESET_(?P<template>.+?)__NODE__(?P<node>.+?)_(?P<slot>\d+)$")


def _text_tracks(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [item for item in draft.get("tracks") or [] if isinstance(item, dict) and item.get("type") == "text"]


def _match_preset_track(name: str) -> re.Match[str] | None:
    return _PRESET.match(name)


def _timerange(segment: Mapping[str, Any]) -> tuple[int, int]:
    value = segment.get("target_timerange") or {}
    start = int(value.get("start") or 0)
    duration = int(value.get("duration") or 0)
    if duration <= 0:
        raise ValueError("preset segment must have positive target_timerange.duration")
    return start, start + duration


def _font_path(material: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    candidates = [material.get("font_path")]
    for style in payload.get("styles") or []:
        if isinstance(style, Mapping):
            font = style.get("font") or {}
            if isinstance(font, Mapping):
                candidates.append(font.get("path"))
    return next((str(value) for value in candidates if value and Path(str(value)).is_file()), None)


def _max_keyframe_scale(segment: Mapping[str, Any], axis: str, base: float) -> float:
    kind = "KFTypeScaleX" if axis == "x" else "KFTypeScaleY"
    values = [abs(base)]
    for group in segment.get("common_keyframes") or []:
        if group.get("property_type") == kind:
            for frame in group.get("keyframe_list") or []:
                values.extend(abs(float(value)) for value in frame.get("values") or [])
    return max(values)


def _measure_segment(segment: Mapping[str, Any], material: Mapping[str, Any], *, canvas: tuple[int, int] = CANVAS,
                     text_unit_to_px: float = UNIT_TO_PX) -> dict[str, Any]:
    payload = _payload(material)
    styles = payload.get("styles") or []
    if not styles:
        raise ValueError("preset text material has no styles")
    max_size = max(float(style.get("size") or material.get("font_size") or 14.0) for style in styles)
    clip = segment.get("clip") or {}
    transform = clip.get("transform") or {"x": 0.0, "y": 0.0}
    scale = clip.get("scale") or {"x": 1.0, "y": 1.0}
    def measure(font_path):
        return transformed_text_bbox(
        str(payload.get("text") or ""),
        font_path=font_path,
        pixel_size=max_size * text_unit_to_px,
        canvas_width=canvas[0], canvas_height=canvas[1],
        transform_x=float(transform.get("x") or 0.0),
        transform_y=float(transform.get("y") or 0.0),
        scale_x=_max_keyframe_scale(segment, "x", float(scale.get("x") or 1.0)),
        scale_y=_max_keyframe_scale(segment, "y", float(scale.get("y") or 1.0)),
        rotation_degrees=float(clip.get("rotation") or 0.0),
        letter_spacing_px=float(material.get("letter_spacing") or 0.0) * text_unit_to_px,
        )
    measured = measure(_font_path(material, payload))
    # Mixed runs can contain a wider font than the material's default. Measuring
    # the full string at the largest size in each bound font is conservative.
    fonts = {s.get("font", {}).get("path") for s in styles}
    boxes = [measured["bbox"]]
    for font in fonts:
        if font and Path(font).is_file():
            boxes.append(measure(font)["bbox"])
    measured["bbox"] = list(union_bbox(boxes))
    return measured


def _preset_groups(draft: Mapping[str, Any]) -> dict[tuple[str, str], list[tuple[str, dict[str, Any]]]]:
    groups: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    captions = _caption_track(draft)
    caption_id = str(captions.get("id") or "")
    for track in _text_tracks(draft):
        if str(track.get("id") or "") == caption_id:
            continue
        matched = _match_preset_track(str(track.get("name") or ""))
        if not matched:
            continue
        key = (matched.group("template"), matched.group("node"))
        for segment in track.get("segments") or []:
            if isinstance(segment, dict):
                groups.setdefault(key, []).append((str(track.get("name") or ""), segment))
    return groups


def _position_to_px(axis: str, value: float, canvas: tuple[int, int] = CANVAS) -> float:
    return canvas[0] * (0.5 + value / 2.0) if axis == "x" else canvas[1] * (0.5 - value / 2.0)


def _position_from_px(axis: str, value: float, canvas: tuple[int, int] = CANVAS) -> float:
    return 2.0 * (value / canvas[0] - 0.5) if axis == "x" else 2.0 * (0.5 - value / canvas[1])


def _scale_position(axis: str, value: float, anchor_px: float, factor: float, canvas: tuple[int, int] = CANVAS) -> float:
    return _position_from_px(axis, anchor_px + (_position_to_px(axis, value, canvas) - anchor_px) * factor, canvas)


def _transform_group(entries: list[tuple[str, dict[str, Any], dict[str, Any]]], *, anchor_px: tuple[float, float], scale_factor: float, shift_px: tuple[float, float] = (0.0, 0.0), canvas: tuple[int, int] = CANVAS) -> None:
    anchor_x, anchor_y = anchor_px
    dx, dy = shift_px
    for _name, segment, material in entries:
        is_text_material = bool(material)
        if scale_factor != 1.0:
            payload = _payload(material)
            styles = payload.get("styles") or []
            if is_text_material and styles:
                for style in styles:
                    style["size"] = float(style.get("size") or material.get("font_size") or 14.0) * scale_factor
                _set_payload(material, payload)
        clip = segment.setdefault("clip", {})
        if scale_factor != 1.0 and not is_text_material:
            clip_scale = clip.setdefault("scale", {"x": 1.0, "y": 1.0})
            if not isinstance(clip_scale, dict):
                raise ValueError("辅助轨 clip.scale 不是对象，无法进行整组缩放，已明确拒绝")
            for axis in ("x", "y"):
                value = clip_scale.get(axis)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ValueError(f"辅助轨 clip.scale.{axis} 不可解析，已明确拒绝")
                clip_scale[axis] = float(value) * scale_factor
        transform = clip.setdefault("transform", {"x": 0.0, "y": 0.0})
        transform["x"] = _scale_position("x", float(transform.get("x") or 0.0), anchor_x, scale_factor, canvas) + 2.0 * dx / canvas[0]
        transform["y"] = _scale_position("y", float(transform.get("y") or 0.0), anchor_y, scale_factor, canvas) - 2.0 * dy / canvas[1]
        for group in segment.get("common_keyframes") or []:
            axis = "x" if group.get("property_type") == "KFTypePositionX" else ("y" if group.get("property_type") == "KFTypePositionY" else "")
            if axis:
                shift = 2.0 * dx / canvas[0] if axis == "x" else -2.0 * dy / canvas[1]
                anchor = anchor_x if axis == "x" else anchor_y
                for frame in group.get("keyframe_list") or []:
                    frame["values"] = [_scale_position(axis, float(value), anchor, scale_factor, canvas) + shift for value in frame.get("values") or []]
                continue
            if not is_text_material and group.get("property_type") in {"KFTypeScaleX", "KFTypeScaleY", "UNIFORM_SCALE"}:
                for frame in group.get("keyframe_list") or []:
                    frame["values"] = [float(value) * scale_factor for value in frame.get("values") or []]


def _preset_group_entries(draft: Mapping[str, Any], template_id: str, node_id: str) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    prefix = f"JY_PRESET_{template_id}__NODE__{node_id}_"
    materials = _texts(draft)
    entries: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict) or not str(track.get("name") or "").startswith(prefix):
            continue
        track_type = str(track.get("type") or "")
        if track_type not in {"text", "video", "image"}:
            continue
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                raise ValueError(f"预设 {template_id}/{node_id} 含非法辅助片段，已明确拒绝")
            material = materials.get(str(segment.get("material_id") or ""), {}) if track_type == "text" else {}
            if track_type == "text" and not material:
                raise ValueError(f"预设 {template_id}/{node_id} 文字片段缺少素材，已明确拒绝")
            entries.append((str(track.get("name")), segment, material))
    if not entries:
        raise ValueError(f"预设 {template_id}/{node_id} 没有可定位的文字或辅助轨")
    return entries


def _explicit_point(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} 必须显式包含 x/y")
    try:
        x, y = float(value["x"]), float(value["y"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{label} 必须显式包含数字 x/y") from None
    if not all(math.isfinite(v) for v in (x, y)):
        raise ValueError(f"{label} 必须是有限数字")
    return x, y


def place_preset_group(draft: dict[str, Any], spec: Mapping[str, Any], context: Any) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Apply an explicit group placement while preserving internal timing/layout."""
    canvas = _require_supported_canvas(context, "place_preset_group", draft)
    template_id = str(spec.get("template_id") or "")
    node_id = str(spec.get("node_id") or "")
    if not template_id or not node_id:
        raise ValueError("place_preset_group requires template_id and node_id")
    selection = _declared_selection(context, template_id, node_id)
    if "segment_groups" in spec:
        return _place_preset_segment_groups(draft, spec, template_id, node_id, canvas)
    target_value = spec.get("position_px", spec.get("final_position", selection.get("final_position")))
    position_space = str(spec.get("position_space") or "px")
    target = _explicit_point(target_value, "final_position")
    if position_space == "normalized":
        target = (_position_to_px("x", target[0], canvas), _position_to_px("y", target[1], canvas))
    elif position_space != "px":
        raise ValueError("place_preset_group 的 position_space 必须是 px 或 normalized")
    raw_scale = spec.get("group_scale", spec.get("final_group_scale", selection.get("final_group_scale")))
    try:
        scale_factor = float(raw_scale)
    except (TypeError, ValueError):
        raise ValueError("place_preset_group 必须显式声明正数 final_group_scale") from None
    if not math.isfinite(scale_factor) or scale_factor <= 0:
        raise ValueError("place_preset_group 的 final_group_scale 必须是正数")
    entries = _preset_group_entries(draft, template_id, node_id)
    source_value = spec.get("source_anchor_px")
    if source_value is None:
        first_clip = entries[0][1].get("clip") or {}
        first_transform = first_clip.get("transform") or {"x": 0.0, "y": 0.0}
        source = (
            _position_to_px("x", float(first_transform.get("x") or 0.0), canvas),
            _position_to_px("y", float(first_transform.get("y") or 0.0), canvas),
        )
    else:
        source_space = str(spec.get("source_anchor_space") or "px")
        source = _explicit_point(source_value, "source_anchor_px")
        if source_space == "normalized":
            source = (_position_to_px("x", source[0], canvas), _position_to_px("y", source[1], canvas))
        elif source_space != "px":
            raise ValueError("place_preset_group 的 source_anchor_space 必须是 px 或 normalized")
    shift = (target[0] - source[0], target[1] - source[1])
    _transform_group(entries, anchor_px=source, scale_factor=scale_factor, shift_px=shift, canvas=canvas)
    return draft, {
        "template_id": template_id,
        "node_id": node_id,
        "position_space": "px",
        "source_anchor_px": list(source),
        "target_position_px": list(target),
        "shift_px": list(shift),
        "group_scale": scale_factor,
        "transformed_track_count": len({name for name, _segment, _material in entries}),
        "transformed_segment_count": len(entries),
    }


def _place_preset_segment_groups(draft, spec, template_id, node_id, canvas):
    """Place an explicit partition of one node's visual segments atomically."""
    if set(spec) - {"id", "kind", "template_id", "node_id", "segment_groups"}:
        raise ValueError("segment_groups cannot mix whole-group placement fields")
    groups = spec["segment_groups"]
    if not isinstance(groups, list) or not groups:
        raise ValueError("segment_groups requires a nonempty explicit partition")
    working = copy.deepcopy(draft)
    entries = _preset_group_entries(working, template_id, node_id)
    entry_by_identity = {id(segment): (name, segment, material) for name, segment, material in entries}
    available, track_names = {}, set()
    for track in working.get("tracks") or []:
        name = track.get("name") if isinstance(track, dict) else None
        if name not in {entry[0] for entry in entries}:
            continue
        if name in track_names:
            raise ValueError("segment_groups requires unique track names")
        track_names.add(name)
        for index, segment in enumerate(track.get("segments") or []):
            available[(name, index)] = entry_by_identity[id(segment)]
    seen, prepared = set(), []
    for group in groups:
        if not isinstance(group, Mapping) or set(group) != {"segments", "source_anchor_px", "target_anchor_px", "scale_factor"}:
            raise ValueError("each segment group requires segments, source_anchor_px, target_anchor_px and scale_factor")
        points = []
        for label in ("source_anchor_px", "target_anchor_px"):
            point = group[label]
            if not isinstance(point, Mapping) or set(point) != {"x", "y"} or any(type(v) not in (int, float) for v in point.values()):
                raise ValueError(f"{label} requires numeric pixel x/y")
            points.append(_explicit_point(point, label))
        factor = group["scale_factor"]
        if type(factor) not in (int, float) or not math.isfinite(factor) or factor <= 0:
            raise ValueError("segment group scale_factor must be finite and positive")
        selectors = group["segments"]
        if not isinstance(selectors, list) or not selectors:
            raise ValueError("each segment group requires nonempty segments")
        selected = []
        for selector in selectors:
            if not isinstance(selector, Mapping) or set(selector) != {"track_name", "segment_index"}:
                raise ValueError("segment selector requires track_name and segment_index")
            name, index = selector["track_name"], selector["segment_index"]
            if not isinstance(name, str) or type(index) is not int or index < 0:
                raise ValueError("segment selector requires a track name and nonnegative integer index")
            key = (name, index)
            if key not in available or key in seen:
                raise ValueError("segment_groups has missing, duplicate or cross-node segment selection")
            seen.add(key)
            selected.append(available[key])
        # Font size lives in the material: scaling a shared text would also
        # change another segment or scale it twice. Require an independent one.
        if factor != 1:
            for _name, segment, material in selected:
                if material and sum(1 for track in working.get("tracks", []) for other in track.get("segments", [])
                                    if other.get("material_id") == segment.get("material_id")) != 1:
                    raise ValueError("segment group scaling requires independent text materials")
        source, target = points
        prepared.append((selected, source, target, factor))
    if seen != set(available):
        raise ValueError("segment_groups must cover every visual segment of the node exactly once")
    reports = []
    for selected, source, target, factor in prepared:
        shift = (target[0] - source[0], target[1] - source[1])
        _transform_group(selected, anchor_px=source, scale_factor=factor, shift_px=shift, canvas=canvas)
        reports.append({"source_anchor_px": list(source), "target_anchor_px": list(target),
                        "scale_factor": factor, "transformed_segment_count": len(selected)})
    draft.clear()
    draft.update(working)
    return draft, {"template_id": template_id, "node_id": node_id, "segment_groups": reports,
                   "transformed_track_count": len(track_names), "transformed_segment_count": len(entries)}


def _stable_effective_font_height_px(segment: Mapping[str, Any], material: Mapping[str, Any], *, text_unit_to_px: float = UNIT_TO_PX) -> float:
    return measure_reading_glyphs(segment, material, text_unit_to_px=text_unit_to_px)['minimum_height_px']


def _preset_track_roles(context: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for preset in getattr(context, "plan", {}).get("presets") or []:
        if not isinstance(preset, Mapping):
            continue
        for track in preset.get("actual_text_tracks") or []:
            if isinstance(track, Mapping) and track.get("track_name"):
                result[str(track["track_name"])] = normalize_text_role(track.get("role") or "editable")
    return result


def _overlapping_caption_height_px(preset_segment: Mapping[str, Any], caption_entries: list[tuple[dict[str, Any], dict[str, Any]]], *, text_unit_to_px: float = UNIT_TO_PX) -> float | None:
    # Preserve the full-film baseline even when a local caption has yielded.
    values = [measure_reading_glyphs(segment, material, text_unit_to_px=text_unit_to_px)['maximum_height_px'] for segment, material in caption_entries]
    return max(values) if values else None


def _texts(draft: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id")): item
        for item in (draft.get("materials") or {}).get("texts") or []
        if isinstance(item, Mapping) and item.get("id")
    }


def _payload(material: Mapping[str, Any]) -> dict[str, Any]:
    value = material.get("content")
    if isinstance(value, str):
        try:
            result = json.loads(value)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    return {"text": str(material.get("text") or ""), "styles": []}


def _set_payload(material: dict[str, Any], value: Mapping[str, Any]) -> None:
    material["content"] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    styles = value.get("styles") or []
    if styles:
        material["font_size"] = float(styles[0].get("size") or material.get("font_size") or 0)


def _caption_track(draft: Mapping[str, Any]) -> dict[str, Any]:
    tracks = [item for item in draft.get("tracks") or [] if isinstance(item, dict) and item.get("type") == "text"]
    if not tracks:
        raise ValueError("draft has no text track")
    named = [item for item in tracks if item.get("name") == "JY_ZH_SUBTITLES"]
    if len(named) > 1:
        raise ValueError("JY_ZH_SUBTITLES track name must be unique")
    if named:
        return named[0]
    return max(tracks, key=lambda item: len(item.get("segments") or []))


def reflow_ordinary_captions(draft: dict[str, Any], spec: Mapping[str, Any], _context: Any) -> Mapping[str, Any]:
    canvas = _require_supported_canvas(_context, "reflow_ordinary_captions", draft)
    plan = getattr(_context, "plan", {}) or {}
    unit_to_px = (plan.get("layout_checks") or {}).get("text_unit_to_px", UNIT_TO_PX)
    if type(unit_to_px) not in (int, float) or not math.isfinite(unit_to_px) or unit_to_px <= 0:
        raise ValueError("text_unit_to_px must be a finite positive number")
    preferred_breaks = spec.get("preferred_breaks", {})
    if not isinstance(preferred_breaks, Mapping):
        raise ValueError("preferred_breaks must map logical caption text to character offsets")
    final_size = spec.get("font_size")
    if final_size is not None:
        if isinstance(final_size, bool) or not isinstance(final_size, (int, float)) or not math.isfinite(float(final_size)) or float(final_size) <= 0:
            raise ValueError("reflow_ordinary_captions font_size must be a finite positive number")
        final_size = float(final_size)
    materials = _texts(draft)
    track = _caption_track(draft)
    width = canvas[0] * float(spec.get("safe_width_ratio") or 0.82)
    reports = []
    for segment in track.get("segments") or []:
        material = materials.get(str(segment.get("material_id") or ""))
        if material is None:
            raise ValueError("ordinary caption references missing text material")
        payload = _payload(material)
        styles = payload.get("styles") or []
        if len(styles) != 1:
            raise ValueError("ordinary caption must have exactly one final style")
        style = styles[0]
        if final_size is not None:
            style["size"] = final_size
        scale_x = _max_keyframe_scale(segment, "x", float(
            ((segment.get("clip") or {}).get("scale") or {}).get("x", 1.0)))
        if not math.isfinite(scale_x) or scale_x <= 0:
            raise ValueError("ordinary caption horizontal scale must be finite and nonzero")
        logical_text = str(payload.get("text") or "").replace("\r", "").replace("\n", "")
        result = reflow_caption(
            str(payload.get("text") or ""),
            font_path=_font_path(material, payload),
            pixel_size=float(style.get("size") or material.get("font_size") or 14.0) * unit_to_px,
            safe_width_px=width / scale_x,
            letter_spacing_px=float(material.get("letter_spacing") or 0.0) * unit_to_px,
            preferred_breaks=preferred_breaks.get(logical_text),
        )
        # Font metrics use the unscaled native style; report final canvas widths.
        result["safe_width_px"] = round(width, 3)
        for line in result["lines"]:
            line["width_px"] = round(line["width_px"] * scale_x, 3)
        result["text_unit_to_px"] = unit_to_px
        result["measurement_scale_x"] = scale_x
        payload["text"] = result["text"]
        style["range"] = [0, len(result["text"])]
        _set_payload(material, payload)
        reports.append({"segment_id": segment.get("id"), **result})
    return {"caption_count": len(reports), "captions": reports}


def _load_native_preset_builder() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_five_template_trial.py"
    name = "huoke_unified_preset_builder_shared"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load native preset importer: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _resolve(value: Any, context: Any) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else Path(context.plan_path).parent / path


def _declared_selection(context: Any, template_id: str, node_id: str) -> Mapping[str, Any]:
    matches: list[Mapping[str, Any]] = []
    for item in context.plan.get("presets") or []:
        if (
            isinstance(item, Mapping)
            and str(item.get("template_id") or "") == template_id
            and str(item.get("node_id") or "") == node_id
        ):
            matches.append(item)
    if len(matches) != 1:
        raise ValueError(f"preset {template_id}/{node_id} 必须在 CandidatePlan 中唯一声明")
    return matches[0]


def _require_supported_canvas(context: Any, operation: str, draft: Mapping[str, Any] | None = None) -> tuple[int, int]:
    target = (getattr(context, "plan", {}) or {}).get("target") or {}
    actual = (draft or {}).get("canvas_config")
    declared = target if "width" in target or "height" in target else actual
    canvas = (declared.get("width"), declared.get("height")) if declared is not None else CANVAS
    if any(type(value) is not int for value in canvas) or canvas not in SUPPORTED_CANVASES:
        raise ValueError(
            f"{operation} 目前只支持 1080x1920 或 1920x1080；当前画布为 {canvas[0]}x{canvas[1]}，已明确拒绝"
        )
    if actual is not None and (actual.get("width"), actual.get("height")) != canvas:
        raise ValueError(f"{operation} draft canvas does not match target canvas {canvas}")
    return canvas


def _strict_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} 必须大于等于 {minimum}")
    return value


def _native_animation_index(draft: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("id")): item
        for item in ((draft.get("materials") or {}).get("material_animations") or [])
        if isinstance(item, Mapping) and item.get("id")
    }


def _hold_keyframe_list(keyframe_group: dict[str, Any], *, old_duration: int, new_duration: int) -> bool:
    """Extend a property's terminal value without stretching its animation."""
    keyframes = keyframe_group.get("keyframe_list")
    if not isinstance(keyframes, list) or not keyframes:
        raise ValueError("稳定态延长遇到缺少 keyframe_list 的关键帧组，已明确拒绝")
    normalized: list[tuple[int, dict[str, Any]]] = []
    for frame in keyframes:
        if not isinstance(frame, dict):
            raise ValueError("稳定态延长遇到非法关键帧，已明确拒绝")
        offset = _strict_int(frame.get("time_offset"), "关键帧 time_offset", minimum=0)
        if offset > old_duration:
            raise ValueError("关键帧超出原片段时长，无法判定稳定态，已明确拒绝")
        values = frame.get("values")
        if not isinstance(values, list) or not values or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in values
        ):
            raise ValueError("关键帧终态值不可解析，无法判定稳定态，已明确拒绝")
        normalized.append((offset, frame))
    normalized.sort(key=lambda item: item[0])
    if normalized[-1][0] > new_duration:
        raise ValueError("终端关键帧超出声明结束时点，已明确拒绝")
    last_offset, last_frame = normalized[-1]
    if last_offset == new_duration:
        return False
    hold = copy.deepcopy(last_frame)
    old_id = str(hold.get("id") or "")
    seed = f"{old_id}:{keyframe_group.get('property_type', '')}:{new_duration}"
    hold["id"] = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    hold["time_offset"] = new_duration
    keyframes.append(hold)
    keyframes.sort(key=lambda item: int(item.get("time_offset") or 0))
    return True


def _extend_segment_final_state(draft: Mapping[str, Any], segment: dict[str, Any], target_end_us: int) -> dict[str, Any]:
    """Extend one visual segment by holding its terminal state.

    Intro animations and existing keyframe timing stay at their original
    offsets.  Out animations are moved to the new tail; loop/group animations
    are refused because their terminal visual state cannot be inferred.
    """
    timerange = segment.get("target_timerange")
    if not isinstance(timerange, dict):
        raise ValueError("稳定态延长目标片段缺少 target_timerange，已明确拒绝")
    start = _strict_int(timerange.get("start"), "片段 start", minimum=0)
    duration = _strict_int(timerange.get("duration"), "片段 duration", minimum=1)
    old_end = start + duration
    if target_end_us < old_end:
        raise ValueError("声明结束时点早于原生片段结束时点，拒绝非法缩短")
    if target_end_us == old_end:
        return {"old_end_us": old_end, "new_end_us": old_end, "extended_us": 0, "out_animation_count": 0, "keyframe_hold_count": 0}
    if segment.get("source_timerange"):
        raise ValueError("稳定态延长目标含 source_timerange，无法保证不空白停留，已明确拒绝")

    animation_index = _native_animation_index(draft)
    # Text can reference static glow or an explicitly inactive bubble slot.
    # Preserve these refs; unidentified and timed effects still fail closed.
    static_text_effects = {
        str(effect.get("id"))
        for effect in ((draft.get("materials") or {}).get("effects") or [])
        if isinstance(effect, Mapping) and effect.get("id")
        and ((effect.get("type") == "bloom" and effect.get("panel_id") == "text_glow"
              and isinstance(effect.get("bloom_params"), Mapping) and effect.get("bloom_params"))
             or (effect.get("type") == "text_shape" and effect.get("sub_type") == "none"
                 and effect.get("category_id") == "bubble"
                 and all(not effect.get(key) for key in (
                     "path", "effect_id", "resource_id", "third_resource_id", "adjust_params",
                     "algorithm_artifact_path", "lumi_hub_path", "bloom_params"))))
        and all(not effect.get(key) for key in ("time_range", "animations", "common_keyframes", "keyframe_refs"))
    }
    # This native hollow-white SDF resource has no script-driven or timed
    # animation. Match its complete inspected package, not all text_effect
    # materials: an unknown or changed resource must still fail closed.
    for effect in (draft.get("materials") or {}).get("effects", []):
        if (not isinstance(effect, Mapping) or effect.get("type") != "text_effect"
                or effect.get("resource_id") != "6896144074487696654"
                or any(effect.get(key) for key in (
                    "time_range", "animations", "common_keyframes", "keyframe_refs", "adjust_params"))):
            continue
        package = Path(str(effect.get("path") or ""))
        if not package.is_absolute() or not package.is_dir():
            continue
        files = sorted(path for path in package.rglob("*") if path.is_file())
        if not files or any(path.is_symlink() for path in files):
            continue
        entries = sorted(path.relative_to(package).as_posix() + ":" +
                         hashlib.sha256(path.read_bytes()).hexdigest() for path in files)
        digest = hashlib.sha256("\n".join(entries).encode()).hexdigest()
        if digest == "3f625bd1a66212c21e3e1cee497fd60495447ca43d732c37b0ab8eb22932dfa8":
            static_text_effects.add(str(effect.get("id")))
    out_count = 0
    for ref in segment.get("extra_material_refs") or []:
        animation = animation_index.get(str(ref))
        if animation is None:
            if str(ref) in static_text_effects:
                continue
            raise ValueError(f"稳定态延长缺少动画素材 {ref}，已明确拒绝")
        entries = animation.get("animations")
        if not isinstance(entries, list):
            raise ValueError("稳定态延长遇到缺失或非法 animations 字段，无法判定稳定态，已明确拒绝")
        # A native fixed symbol may reference a valid container with an
        # explicit empty animations list. It has no animation to extend;
        # preserve the container and reference and hold the static segment.
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("稳定态延长遇到非法动画条目，已明确拒绝")
            animation_type = str(item.get("type") or "").lower()
            anim_start = _strict_int(item.get("start"), "动画 start", minimum=0)
            anim_duration = _strict_int(item.get("duration"), "动画 duration", minimum=1)
            if anim_start + anim_duration > duration:
                raise ValueError("动画超出原生片段时长，无法判定稳定态，已明确拒绝")
            if animation_type in {"loop", "group"}:
                raise ValueError(f"{animation_type} 动画没有可判定的稳定终态，已明确拒绝")
            if animation_type == "out":
                if anim_duration > target_end_us - start:
                    raise ValueError("出场动画长于声明片段，无法后移，已明确拒绝")
                item["start"] = target_end_us - start - anim_duration
                out_count += 1
            elif animation_type != "in":
                raise ValueError(f"未知动画类型 {animation_type}，无法判定稳定态，已明确拒绝")

    keyframe_holds = 0
    for group in segment.get("common_keyframes") or []:
        if not isinstance(group, dict):
            raise ValueError("稳定态延长遇到非法关键帧组，已明确拒绝")
        if _hold_keyframe_list(group, old_duration=duration, new_duration=target_end_us - start):
            keyframe_holds += 1
    timerange["duration"] = target_end_us - start
    return {
        "old_end_us": old_end,
        "new_end_us": target_end_us,
        "extended_us": target_end_us - old_end,
        "out_animation_count": out_count,
        "keyframe_hold_count": keyframe_holds,
    }


def _resize_loop_segment(draft: Mapping[str, Any], segment: dict[str, Any], target_end_us: int) -> dict[str, Any]:
    """Change a native pure-loop text span while preserving its cycle period.

    Native loop animation.duration is the cycle duration, not the text span.
    Mixed entrance/exit, keyframes and source-driven clips require another
    timing policy; this branch must not stretch or silently discard them.
    """
    start, old_end = _timerange(segment)
    duration = _strict_int(target_end_us - start, "loop text duration", minimum=1)
    if (segment.get("source_timerange") or segment.get("speed", 1) != 1
            or any(segment.get(key) for key in ("common_keyframes", "keyframe_refs", "animations"))):
        raise ValueError("preserve_loop_period requires unretimed native loop text without keyframes")
    animations = _native_animation_index(draft)
    entries = []
    for ref in segment.get("extra_material_refs") or []:
        container = animations.get(str(ref))
        if not container or not isinstance(container.get("animations"), list):
            raise ValueError("preserve_loop_period has an unresolved animation reference")
        entries.extend(container["animations"])
    if len(entries) != 1 or entries[0].get("type") != "loop":
        raise ValueError("preserve_loop_period supports exactly one pure loop animation")
    animation = entries[0]
    period = _strict_int(animation.get("duration"), "loop cycle duration", minimum=1)
    if _strict_int(animation.get("start"), "loop start", minimum=0) != 0 or period > duration:
        raise ValueError("preserve_loop_period requires start=0 and at least one complete cycle")
    segment["target_timerange"]["duration"] = duration
    return {"old_end_us": old_end, "new_end_us": target_end_us,
            "duration_delta_us": target_end_us - old_end, "cycle_duration_us": period,
            "speed_unchanged": True, "animation_materials_unchanged": True,
            "native_visual_verified": False}


def _extend_declared_text_tracks(
    draft: dict[str, Any], selection: Mapping[str, Any], *, start_us: int, end_us: int,
    duration_policy: str = "extend_final_state_only",
) -> list[dict[str, Any]]:
    declarations = selection.get("actual_text_tracks")
    if not isinstance(declarations, list) or not declarations:
        raise ValueError("hold_policy=extend_final_state_only 必须声明 actual_text_tracks，已明确拒绝")
    text_tracks = {
        str(track.get("name")): track
        for track in draft.get("tracks") or []
        if isinstance(track, dict) and track.get("type") == "text" and track.get("name")
    }
    if len(text_tracks) != sum(1 for track in draft.get("tracks") or [] if isinstance(track, dict) and track.get("type") == "text" and track.get("name")):
        raise ValueError("稳定态延长遇到重复文字轨名称，已明确拒绝")
    reports: list[dict[str, Any]] = []
    declared_names: set[str] = set()
    for declaration in declarations:
        if not isinstance(declaration, Mapping):
            raise ValueError("actual_text_tracks 条目必须是对象，已明确拒绝")
        name = str(declaration.get("track_name") or "")
        if not name or name in declared_names:
            raise ValueError("actual_text_tracks 必须包含唯一 track_name，已明确拒绝")
        declared_names.add(name)
        track = text_tracks.get(name)
        if track is None:
            raise ValueError(f"声明文字轨不存在: {name}")
        declared_start = _strict_int(declaration.get("start_us"), f"{name}.start_us", minimum=0)
        declared_end = _strict_int(declaration.get("end_us"), f"{name}.end_us", minimum=1)
        declared_count = _strict_int(declaration.get("segment_count"), f"{name}.segment_count", minimum=1)
        if declared_start < start_us or declared_start >= end_us or declared_end != end_us:
            raise ValueError(f"{name} 的 actual_text_tracks 时点与节点声明不一致，已明确拒绝")
        segments = track.get("segments")
        if not isinstance(segments, list) or len(segments) != declared_count:
            raise ValueError(f"{name} 的 segment_count 与原生导入不一致，已明确拒绝")
        ranges: list[tuple[int, int, dict[str, Any]]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError(f"{name} 含非法文字片段，已明确拒绝")
            seg_start, seg_end = _timerange(segment)
            if seg_start < start_us or seg_end > end_us:
                raise ValueError(f"{name} 的原生片段越过声明节点窗口，已明确拒绝")
            ranges.append((seg_start, seg_end, segment))
        actual_start = min(item[0] for item in ranges)
        if actual_start != declared_start:
            raise ValueError(f"{name} 的首段开始时点不等价，已明确拒绝")
        terminal_end = max(item[1] for item in ranges)
        terminal = max(ranges, key=lambda item: (item[1], item[0]))[2]
        if duration_policy == "preserve_loop_period":
            if len(segments) != 1:
                raise ValueError("preserve_loop_period requires one segment per declared text track")
            hold_report = _resize_loop_segment(draft, terminal, declared_end)
        else:
            hold_report = _extend_segment_final_state(draft, terminal, declared_end)
        if hold_report["new_end_us"] != declared_end:
            raise ValueError(f"{name} 未形成声明终点的稳定停留，已明确拒绝")
        reports.append({
            "track_name": name,
            "segment_count": declared_count,
            "declared_timerange_us": [declared_start, declared_end],
            "native_timerange_us": [actual_start, terminal_end],
            "terminal_segment_id": terminal.get("id"),
            **hold_report,
        })
    return reports


def _append_native_group(draft: dict[str, Any], spec: Mapping[str, Any], context: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    template_id = str(spec.get("template_id") or spec.get("new_template_id") or "")
    node_id = str(spec.get("node_id") or "")
    start_us = int(spec.get("start_us") or 0)
    end_us = int(spec.get("end_us") or spec.get("target_end_us") or 0)
    if not template_id or not node_id or end_us <= start_us:
        raise ValueError("add_preset_group requires template_id, node_id and positive start/end")
    if start_us < 0 or end_us > _strict_int(draft.get("duration"), "draft duration", minimum=1):
        raise ValueError("native preset node exceeds the unchanged base duration")
    selection = _declared_selection(context, template_id, node_id)
    slots = tuple(str(value) for value in spec.get("slots") or selection.get("slots") or [])
    if not slots:
        raise ValueError("add_preset_group requires non-empty slots")
    registry = _resolve(spec.get("registry") or context.plan.get("preset_registry"), context)
    preset_root = _resolve(spec.get("preset_root") or context.plan.get("preset_root"), context)
    if not registry.is_file() or not preset_root.exists():
        raise ValueError("add_preset_group native registry/preset_root is missing")
    existing_ids = {str(track.get("id") or "") for track in draft.get("tracks") or [] if isinstance(track, Mapping)}
    prefix = f"JY_PRESET_{template_id}__NODE__{node_id}_"
    if any(str(track.get("name") or "").startswith(prefix) for track in draft.get("tracks") or []):
        raise ValueError("native preset node already exists; use replace_preset_group instead of importing it twice")
    audio_path_map = dict(context.plan.get("audio_path_map") or {})
    audio_path_map.update(selection.get("audio_path_map") or {})
    audio_path_map.update(spec.get("audio_path_map") or {})
    audio_path_map = {source: str(_resolve(target, context)) for source, target in audio_path_map.items()}
    audio_cache = spec.get("audio_cache_root") or selection.get("audio_cache_root") or context.plan.get("audio_cache_root")
    builder = _load_native_preset_builder()
    envelope = builder.build_five_template_trial(
        copy.deepcopy(draft), registry, preset_root, (), disable_audio=False,
        audio_path_map=audio_path_map,
        audio_cache_root=_resolve(audio_cache, context) if audio_cache else None,
        template_ids=(template_id,), template_offsets_us={template_id: start_us},
        slot_copy_table={template_id: slots},
        max_template_duration_us=end_us - start_us,
    )
    if any(sampling.get("dropped_segment_count", 0)
           for template in envelope["manifest"]["templates"]
           for sampling in template.get("sampling", {}).values()):
        raise ValueError("native preset node would drop source segments")
    result = envelope["draft"]
    added = [track for track in result.get("tracks") or [] if str(track.get("id") or "") not in existing_ids]
    text_index = 0
    auxiliary_counts: dict[str, int] = {}
    for track in added:
        if track.get("type") == "text":
            text_index += 1
            track["name"] = f"JY_PRESET_{template_id}__NODE__{node_id}_{text_index:02d}"
        else:
            kind = str(track.get("type") or "AUX").upper()
            auxiliary_counts[kind] = auxiliary_counts.get(kind, 0) + 1
            track["name"] = f"JY_PRESET_{template_id}__NODE__{node_id}_AUX_{kind}_{auxiliary_counts[kind]:02d}"
    # Native importer may extend terminal segments to its native duration.
    # Clip groups are allowed to hold only inside the declared node window.
    for track in added:
        for segment in track.get("segments") or []:
            timerange = segment.get("target_timerange") or {}
            start = int(timerange.get("start") or 0)
            duration = int(timerange.get("duration") or 0)
            if start + duration > end_us:
                if track.get("type") == "audio":
                    raise ValueError("native preset audio exceeds declared node; original sound must not be clipped")
                if start >= end_us:
                    raise ValueError("native preset segment starts after declared node end")
                timerange["duration"] = end_us - start
    # Sampling must only shorten a hold, never cut off actual native motion.
    animations = _native_animation_index(result)
    for track in added:
        for segment in track.get("segments") or []:
            span = _strict_int(segment["target_timerange"]["duration"], "preset duration", minimum=1)
            for ref in segment.get("extra_material_refs") or []:
                for animation in animations.get(str(ref), {}).get("animations", []):
                    motion_end = (_strict_int(animation.get("start"), "animation start", minimum=0)
                                  + _strict_int(animation.get("duration"), "animation duration", minimum=1))
                    if motion_end > span:
                        raise ValueError("native preset animation exceeds clipped node segment")
            for group in segment.get("common_keyframes") or []:
                if any(_strict_int(key.get("time_offset"), "keyframe time", minimum=0) > span
                       for key in group.get("keyframe_list", [])):
                    raise ValueError("native preset keyframe exceeds clipped node segment")
    hold_policy = str(spec.get("hold_policy") or selection.get("hold_policy") or "")
    hold_reports: list[dict[str, Any]] = []
    if hold_policy in {"extend_final_state_only", "preserve_loop_period"}:
        before_hold = copy.deepcopy(result)
        hold_reports = _extend_declared_text_tracks(result, selection, start_us=start_us, end_us=end_us,
                                                    duration_policy=hold_policy)
        from .preset_audio import sync_preset_audio
        sync_preset_audio(before_hold, result, prefix=prefix, start_us=start_us, end_us=end_us,
                          bindings=spec.get("audio_bindings", selection.get("audio_bindings", [])))
    elif hold_policy:
        raise ValueError(f"不支持的 hold_policy: {hold_policy}")
    from .preset_audio import audio_rows
    audio_rows({"materials": result["materials"], "tracks": added})
    return result, {
        "template_id": template_id,
        "node_id": node_id,
        "added_track_count": len(added),
        "original_audio_track_count": sum(track.get("type") == "audio" for track in added),
        "original_audio_segment_count": sum(len(track.get("segments", [])) for track in added if track.get("type") == "audio"),
        "hold_policy": hold_policy or None,
        "hold_reports": hold_reports,
    }


def add_preset_group(draft: dict[str, Any], spec: Mapping[str, Any], context: Any) -> tuple[dict[str, Any], Mapping[str, Any]]:
    return _append_native_group(draft, spec, context)


def replace_preset_group(draft: dict[str, Any], spec: Mapping[str, Any], context: Any) -> tuple[dict[str, Any], Mapping[str, Any]]:
    old_id = str(spec.get("old_template_id") or "")
    new_id = str(spec.get("new_template_id") or spec.get("template_id") or "")
    if not old_id or not new_id or old_id == new_id:
        raise ValueError("replace_preset_group requires distinct old/new template ids")
    node_id = str(spec.get("node_id") or "").strip()
    if not node_id:
        raise ValueError("replace_preset_group 必须显式声明 node_id，禁止按 template_id 删除整组节点")
    old_role = str(spec.get("old_semantic_role") or spec.get("old_category") or "").strip()
    new_role = str(spec.get("new_semantic_role") or spec.get("new_category") or "").strip()
    if old_role and new_role and old_role != new_role:
        raise ValueError("semantic_role mismatch: replacement refused")
    clean = copy.deepcopy(draft)
    prefix = f"JY_PRESET_{old_id}__NODE__{node_id}_"
    removed = [track for track in clean.get("tracks") or [] if str(track.get("name") or "").startswith(prefix)]
    if not removed:
        raise ValueError(f"old preset tracks not found: {old_id}")
    clean["tracks"] = [track for track in clean.get("tracks") or [] if track not in removed]
    replacement = dict(spec)
    replacement["template_id"] = new_id
    replacement["node_id"] = node_id
    replacement.setdefault("end_us", spec.get("target_end_us"))
    result, evidence = _append_native_group(clean, replacement, context)
    return result, {"old_template_id": old_id, "new_template_id": new_id, **evidence, "removed_track_count": len(removed)}


def add_broll(draft: dict[str, Any], spec: Mapping[str, Any], context: Any) -> tuple[dict[str, Any], Mapping[str, Any]]:
    from .material_library import check_asset_binding
    media_binding = check_asset_binding(spec, Path(context.plan_path))
    node_id = str(spec.get("node_id") or "")
    track_name = str(spec.get("track_name") or (f"JY_BROLL_{node_id}" if node_id else ""))
    source = _resolve(spec.get("source_path"), context)
    start_us, end_us = int(spec.get("start_us") or 0), int(spec.get("end_us") or 0)
    if not node_id or not track_name.startswith("JY_BROLL_") or not source.is_file() or end_us <= start_us:
        raise ValueError("add_broll requires node_id, JY_BROLL_ track, existing source and positive range")
    fixed_segment_id = spec.get("segment_id")
    if "segment_id" in spec:
        if not isinstance(fixed_segment_id, str) or not fixed_segment_id.strip():
            raise ValueError("add_broll segment_id must be a non-empty string")
        if any(segment.get("id") == fixed_segment_id for track in draft.get("tracks", [])
               for segment in track.get("segments", []) if isinstance(segment, Mapping)):
            raise ValueError("add_broll segment_id must be unique")
    from .media_crop import crop_rectangle, native_crop
    crop = crop_rectangle(spec["source_crop"]) if "source_crop" in spec else None
    identity = str(source) + (json.dumps(crop) if crop is not None else "")
    material_id = str(spec.get("material_id") or f"broll_{hashlib.sha256(identity.encode()).hexdigest()[:16]}")
    # Build the complete native local-video material from the real file.  A
    # hand-written {id,path,name} record is accepted by the JSON schema but is
    # not importable/playable by Jianying (it lacks type, dimensions, duration,
    # crop and the other native material fields).
    vendor_root = Path(__file__).resolve().parents[2] / "vendor" / "pyJianYingDraft-source"
    deps_root = vendor_root.parent / "python-deps"
    for import_root in (deps_root, vendor_root):
        if str(import_root) not in sys.path:
            sys.path.insert(0, str(import_root))
    try:
        from pyJianYingDraft import VideoMaterial, VideoSegment, trange
        from pyJianYingDraft.track import Track, TrackType
        native_material = VideoMaterial(str(source.resolve()), material_name=source.name)
        if crop is not None:
            from pyJianYingDraft.local_materials import CropSettings
            native_material.crop_settings = CropSettings(**native_crop(crop))
        native_material.material_id = material_id
        material = native_material.export_json()
    except Exception as exc:
        raise ValueError(f"add_broll 无法从真实素材生成原生VideoMaterial: {source}: {exc}") from exc
    material["id"] = material_id
    material["material_id"] = material_id
    for previous in (draft.get("materials") or {}).get("videos", []):
        if previous.get("id") == material_id and previous.get("crop") != material.get("crop"):
            raise ValueError("add_broll material_id already binds a different source_crop")
    overrides = spec.get("segment") or {}
    if not isinstance(overrides, Mapping):
        raise ValueError("add_broll segment must be an object")
    source_timerange = spec.get("source_timerange") or overrides.get("source_timerange")
    if not isinstance(source_timerange, Mapping):
        raise ValueError("add_broll requires explicit source_timerange")
    source_start = int(source_timerange.get("start") or 0)
    source_duration = int(source_timerange.get("duration") or 0)
    if source_start < 0 or source_duration <= 0 or source_start + source_duration > int(material["duration"]):
        raise ValueError("add_broll source_timerange exceeds probed native material duration")
    target_duration = end_us - start_us
    speed = float(overrides.get("speed", 1.0))
    if not math.isfinite(speed) or speed <= 0 or round(source_duration / speed) != target_duration:
        raise ValueError("add_broll source/target duration must match explicit speed; implicit retiming is forbidden")
    volume = float(overrides.get("volume", 0.0))
    if not math.isfinite(volume) or volume < 0:
        raise ValueError("add_broll volume must be finite and non-negative")
    clip = spec.get("clip")
    if not isinstance(clip, Mapping) or not clip:
        raise ValueError("add_broll requires explicit clip layout")
    # Both the segment and its speed material must come from the native model.
    # Exporting only VideoMaterial left anonymous, incomplete timeline segments
    # which schema-only checks could not distinguish from usable native clips.
    native_segment = VideoSegment(
        native_material, trange(start_us, target_duration),
        source_timerange=trange(source_start, source_duration), speed=speed,
        volume=volume, change_pitch=bool(overrides.get("is_tone_modify", False)),
    )
    native_track = Track(TrackType.video, track_name, 0, mute=volume == 0)
    native_track.add_segment(native_segment)
    track = native_track.export_json()
    if spec.get("track_id"):
        track["id"] = str(spec["track_id"])
    if any(t.get("id") == track["id"] for t in draft.get("tracks", [])):
        raise ValueError("add_broll track_id must be unique")
    segment = track["segments"][0]
    if fixed_segment_id is not None:
        segment["id"] = fixed_segment_id
    # Keep supported declarative visual fields, but never reuse an old segment
    # identity, material binding, timing, or speed reference from a copied spec.
    managed = {"id", "material_id", "source_timerange", "target_timerange", "clip", "speed", "extra_material_refs"}
    unsupported = set(overrides) - set(segment) - {"render_index"}
    if unsupported:
        raise ValueError(f"add_broll unsupported segment fields: {sorted(unsupported)}")
    segment.update({key: copy.deepcopy(value) for key, value in overrides.items() if key not in managed})
    segment["volume"] = volume
    segment["clip"] = copy.deepcopy(dict(clip))
    existing_materials = {
        str(item.get("id")): item
        for items in (draft.get("materials") or {}).values() if isinstance(items, list)
        for item in items if isinstance(item, Mapping) and item.get("id")
    }
    refs = overrides.get("extra_material_refs", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in existing_materials for ref in refs):
        raise ValueError("add_broll extra_material_refs must resolve to existing materials")
    segment["extra_material_refs"] = [native_segment.speed.global_id] + [
        ref for ref in dict.fromkeys(refs) if existing_materials[ref].get("type") != "speed"
    ]
    materials = draft.setdefault("materials", {})
    videos = materials.setdefault("videos", [])
    for index, item in enumerate(videos):
        if isinstance(item, Mapping) and str(item.get("id") or "") == material_id:
            videos[index] = material
            break
    else:
        videos.append(material)
    materials.setdefault("speeds", []).append(native_segment.speed.export_json())
    draft.setdefault("tracks", []).append(track)
    return draft, {"track_name": track_name, "source_path": str(source.resolve()), "segment_count": 1,
                   "media_asset": media_binding}


def set_track_render_order(draft: dict[str, Any], spec: Mapping[str, Any], _context: Any) -> Mapping[str, Any]:
    """Set visual stacking without promoting a background into the main track."""
    ordered = spec.get("ordered_track_names")
    if not isinstance(ordered, list) or any(not isinstance(name, str) or not name for name in ordered):
        raise ValueError("set_track_render_order requires ordered_track_names as a non-empty string list")
    if len(ordered) != len(set(ordered)):
        raise ValueError("set_track_render_order ordered_track_names must be unique")
    visual = []
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict) or track.get("type") not in {"video", "text"}:
            continue
        segments = track.get("segments")
        if segments in (None, []):
            continue
        if not isinstance(segments, list):
            raise ValueError("set_track_render_order visual track segments must be a list")
        visual.append(track)
    names = [str(track.get("name") or "") for track in visual]
    if any(not name for name in names):
        raise ValueError("set_track_render_order populated visual tracks require unique names")
    if len(names) != len(set(names)):
        raise ValueError("set_track_render_order populated visual track names must be unique")
    if set(ordered) != set(names) or len(ordered) != len(names):
        raise ValueError("set_track_render_order must cover exactly all populated video/text tracks")
    # Validate every segment before touching any track, preserving atomicity.
    for track in visual:
        if any(not isinstance(segment, dict) for segment in track["segments"]):
            raise ValueError("set_track_render_order encountered a non-object segment")
    video_tracks = [track for track in draft.get("tracks") or []
                    if isinstance(track, dict) and track.get("type") == "video"]
    if any(type(track.get("flag", 0)) is not int or track.get("flag", 0) < 0 for track in video_tracks):
        raise ValueError("set_track_render_order video flag must be a non-negative integer")
    populated_video = [track for track in video_tracks if track.get("segments")]
    main_track = None
    if populated_video:
        # 8.8 native roundtrips mark auxiliary video tracks with flag bit 2.
        # All-zero vendor exports let the first reordered background become
        # the magnetic main track and snap its first clip to zero. Bind the
        # original base main track before changing array/render order.
        original_tracks = populated_video
        plan = getattr(_context, "plan", {})
        if plan.get("base_draft"):
            base = json.loads(_resolve(plan["base_draft"], _context).read_text("utf-8-sig"))
            original_tracks = [track for track in base.get("tracks") or []
                               if isinstance(track, dict) and track.get("type") == "video" and track.get("segments")]
        original_main = next((track for track in original_tracks if not (int(track.get("flag", 0)) & 2)), None)
        if original_main is None:
            raise ValueError("set_track_render_order cannot identify the original main video track")
        matches = [track for track in populated_video if track.get("name") == original_main.get("name")]
        if len(matches) != 1:
            raise ValueError("set_track_render_order original main video track must remain uniquely present")
        main_track = matches[0]
    # All validation is complete before touching the draft. Keep audio, empty,
    # and other tracks in their original slots; only populated visual slots move.
    tracks = draft.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("set_track_render_order draft tracks must be a list")
    visual_slots = [index for index, track in enumerate(tracks) if track in visual]
    by_name = {str(track.get("name")): track for track in visual}
    for slot, name in zip(visual_slots, ordered):
        tracks[slot] = by_name[name]
    if main_track is not None:
        for track in video_tracks:
            track["flag"] = track.get("flag", 0) & ~2 if track is main_track else track.get("flag", 0) | 2
    for slot, name in zip(visual_slots, ordered):
        track = tracks[slot]
        for segment in track["segments"]:
            segment["render_index"] = slot
            segment["track_render_index"] = 0
    return {"ordered_track_names": list(ordered), "track_count": len(ordered), "segment_count": sum(len(track["segments"]) for track in visual),
            "main_video_track_name": main_track.get("name") if main_track is not None else None,
            "native_main_track_roundtrip_verified": False}


def fit_template_text_bounds(draft: dict[str, Any], spec: Mapping[str, Any], _context: Any) -> Mapping[str, Any]:
    """Uniformly fit each declared preset group into the calibrated safe area."""
    canvas = _require_supported_canvas(_context, "fit_template_text_bounds", draft)
    materials = _texts(draft)
    unit_to_px = (getattr(_context, 'plan', {}).get('layout_checks') or {}).get('text_unit_to_px', UNIT_TO_PX)
    if type(unit_to_px) not in (int, float) or not math.isfinite(unit_to_px) or unit_to_px <= 0:
        raise ValueError('text_unit_to_px must be a finite positive number')
    default_safe = ([SAFE_LEFT, SAFE_TOP, SAFE_RIGHT, SAFE_BOTTOM] if canvas == CANVAS
                    else [SAFE_LEFT, SAFE_TOP, canvas[0] - SAFE_LEFT, canvas[1] - SAFE_TOP])
    raw_safe = spec.get("safe_area_px", default_safe)
    if not isinstance(raw_safe, (list, tuple)) or len(raw_safe) != 4:
        raise ValueError("fit_template_text_bounds requires four safe_area_px coordinates")
    safe = list(raw_safe)
    if "safe_bottom_px" in spec:
        safe[3] = spec["safe_bottom_px"]
    if (len(safe) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in safe)
            or not (0 <= safe[0] < safe[2] <= canvas[0] and 0 <= safe[1] < safe[3] <= canvas[1])):
        raise ValueError("fit_template_text_bounds requires a safe_area_px inside the target canvas")
    safe_left, safe_top, safe_right, safe_bottom = safe
    minimum_scale = float(spec.get("minimum_group_scale") or 0.55)
    maximum_scale = float(spec.get("maximum_group_scale") or 1.5)
    minimum_height = max(56.0, float(spec.get("minimum_readable_text_height_px") or 56.0))
    overrides = spec.get("template_scale_overrides") if isinstance(spec.get("template_scale_overrides"), Mapping) else {}
    caption = _caption_track(draft)
    caption_entries = [(s, materials[str(s.get("material_id") or "")]) for s in caption.get("segments") or [] if str(s.get("material_id") or "") in materials]
    reports: list[dict[str, Any]] = []
    for (template_id, node_id), raw in _preset_groups(draft).items():
        entries = [(name, segment, materials.get(str(segment.get("material_id") or ""))) for name, segment in raw]
        if any(material is None for _name, _segment, material in entries):
            raise ValueError(f"preset {template_id}/{node_id} references missing text material")
        typed = [(name, segment, material) for name, segment, material in entries if material is not None]
        before = union_bbox([_measure_segment(segment, material, canvas=canvas, text_unit_to_px=unit_to_px)["bbox"] for _name, segment, material in typed])
        width, height = max(1.0, before[2] - before[0]), max(1.0, before[3] - before[1])
        requested = float(overrides.get(template_id, spec.get("group_scale") or 1.0))
        hierarchy_required = 0.0
        for name, segment, material in typed:
            if not any(char.isalnum() for char in str(_payload(material).get('text', ''))):
                continue
            caption_height = _overlapping_caption_height_px(segment, caption_entries, text_unit_to_px=unit_to_px)
            ratio = max(1.3, float(spec.get("minimum_emphasis_to_caption_ratio") or 1.3))
            if caption_height and ratio > 0:
                hierarchy_required = max(hierarchy_required, ratio * caption_height / max(1.0, _stable_effective_font_height_px(segment, material, text_unit_to_px=unit_to_px)))
        requested = max(requested, hierarchy_required)
        if requested <= 0 or requested > maximum_scale:
            raise ValueError(f"{template_id} requested group scale out of range: {requested}")
        applied = min(requested, (safe_right - safe_left) / width, (safe_bottom - safe_top) / height)
        if applied < minimum_scale:
            raise ValueError(f"{template_id} requires group scale {applied:.3f}, below readable minimum {minimum_scale:.3f}")
        anchor = ((before[0] + before[2]) / 2.0, (before[1] + before[3]) / 2.0)
        _transform_group(typed, anchor_px=anchor, scale_factor=applied, canvas=canvas)
        boxes = [_measure_segment(segment, material, canvas=canvas, text_unit_to_px=unit_to_px)["bbox"] for _name, segment, material in typed]
        union = union_bbox(boxes)
        correction = min(1.0, (safe_right - safe_left) / max(1.0, union[2] - union[0]), (safe_bottom - safe_top) / max(1.0, union[3] - union[1]))
        if correction < 1.0:
            if applied * correction < minimum_scale:
                raise ValueError(f"{template_id} corrected group scale below readable minimum")
            _transform_group(typed, anchor_px=((union[0] + union[2]) / 2.0, (union[1] + union[3]) / 2.0), scale_factor=correction, canvas=canvas)
            applied *= correction
            boxes = [_measure_segment(segment, material, canvas=canvas, text_unit_to_px=unit_to_px)["bbox"] for _name, segment, material in typed]
            union = union_bbox(boxes)
        dx = max(safe_left - union[0], min(0.0, safe_right - union[2]))
        dy = max(safe_top - union[1], min(0.0, safe_bottom - union[3]))
        if dx or dy:
            _transform_group(typed, anchor_px=anchor, scale_factor=1.0, shift_px=(dx, dy), canvas=canvas)
            boxes = [_measure_segment(segment, material, canvas=canvas, text_unit_to_px=unit_to_px)["bbox"] for _name, segment, material in typed]
            union = union_bbox(boxes)
        tracks = [{"track_name": name, "segment_id": str(segment.get("id") or ""), "text": str(_payload(material).get("text") or ""), "bbox": _measure_segment(segment, material, canvas=canvas, text_unit_to_px=unit_to_px)["bbox"]} for (name, segment, material) in typed]
        inside = union[0] >= safe_left - .01 and union[2] <= safe_right + .01 and union[1] >= safe_top - .01 and union[3] <= safe_bottom + .01
        readings = [measure_reading_glyphs(segment, material, text_unit_to_px=unit_to_px) for _name, segment, material in typed]
        caption_height = _overlapping_caption_height_px(typed[0][1], caption_entries, text_unit_to_px=unit_to_px) or 0.0
        readable = all(not reading['informative'] or reading['minimum_height_px'] + .01 >= max(
            minimum_height, caption_height * max(1.3, float(spec.get('minimum_emphasis_to_caption_ratio') or 1.3)))
            for reading in readings)
        if not inside or not readable:
            raise ValueError(f"{template_id} fitted group failed bounds/readability")
        reports.append({"template_id": template_id, "node_id": node_id, "track_count": len(tracks), "applied_group_scale": applied, "group_shift_px": [dx, dy], "final_union_bbox": list(union), "canvas_bounds": "passed", "readability": "passed", "tracks": tracks})
    declared_groups = {
        (str(item.get("template_id") or ""), str(item.get("node_id") or ""))
        for item in _context.plan.get("presets") or []
        if isinstance(item, Mapping)
    }
    measured_groups = {(str(item["template_id"]), str(item["node_id"])) for item in reports}
    if declared_groups - measured_groups:
        raise ValueError(f"fit_template_text_bounds 未找到已声明节点: {sorted(declared_groups - measured_groups)}")
    return {"template_group_count": len(reports), "template_text_track_count": sum(item["track_count"] for item in reports), "resize_policy": "uniform_group_only", "groups": reports, "safe_area_px": safe}


def measure_preset_internal_collisions(draft: dict[str, Any], spec: Mapping[str, Any], _context: Any) -> Mapping[str, Any]:
    canvas = _require_supported_canvas(_context, "measure_preset_internal_collisions", draft)
    materials = _texts(draft)
    minimum_gap = float(spec.get("minimum_gap_px") or 0.0)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for track in _text_tracks(draft):
        matched = _match_preset_track(str(track.get("name") or ""))
        if not matched:
            continue
        for segment in track.get("segments") or []:
            material = materials.get(str(segment.get("material_id") or ""))
            if material is None:
                raise ValueError("preset text track references missing material")
            start, end = _timerange(segment)
            grouped.setdefault((matched.group("template"), matched.group("node")), []).append({"track_name": str(track.get("name") or ""), "segment_id": str(segment.get("id") or ""), "timerange_us": [start, end], "bbox": _measure_segment(segment, material, canvas=canvas)["bbox"]})
    allowed: dict[frozenset[str], Mapping[str, Any]] = {}
    for item in spec.get("allowed_track_pairs") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("tracks"), list) or len(item["tracks"]) != 2 or item.get("authorized_by") != "accepted_template_behavior" or not str(item.get("design_basis") or "").strip():
            raise ValueError("allowed preset overlap requires two tracks, accepted_template_behavior and design_basis")
        allowed[frozenset(str(v) for v in item["tracks"])] = item
    collisions, allowed_overlaps = [], []
    pairs = 0
    for (template, node), entries in grouped.items():
        for index, first in enumerate(entries):
            for second in entries[index + 1:]:
                start, end = max(first["timerange_us"][0], second["timerange_us"][0]), min(first["timerange_us"][1], second["timerange_us"][1])
                if end <= start:
                    continue
                pairs += 1
                spatial = bbox_intersection(first["bbox"], second["bbox"], minimum_gap_px=minimum_gap)
                if spatial["collides"]:
                    detail = {"template_id": template, "node_id": node, "visible_overlap_us": [start, end], "first": first, "second": second, "spatial": spatial}
                    authorization = allowed.get(frozenset((first["track_name"], second["track_name"])))
                    (allowed_overlaps if authorization else collisions).append({**detail, **({"authorization": authorization} if authorization else {})})
    declared_groups = {
        (str(item.get("template_id") or ""), str(item.get("node_id") or ""))
        for item in _context.plan.get("presets") or []
        if isinstance(item, Mapping)
    }
    if declared_groups - set(grouped):
        raise ValueError(f"measure_preset_internal_collisions 未找到已声明节点: {sorted(declared_groups - set(grouped))}")
    return {"ok": not collisions, "minimum_gap_px": minimum_gap, "preset_count": len(grouped), "checked_visible_pair_count": pairs, "collision_count": len(collisions), "collisions": collisions, "allowed_overlap_count": len(allowed_overlaps), "allowed_overlaps": allowed_overlaps}


def shared_operation_handlers() -> dict[str, Any]:
    return {
        "add_video_effect": add_video_effect,
        "apply_text_animation": apply_text_animation,
        "mix_action_audio": mix_action_audio,
        "simplify_preset_motion": simplify_preset_motion,
        "retime_preset_text_tracks": retime_preset_text_tracks,
        "apply_text_style_variant": apply_text_style_variant,
        "split_video_track": split_video_track,
        "apply_video_mask": apply_video_mask,
        "animate_aroll_transform": animate_aroll_transform,
        "animate_broll_transform": animate_broll_transform,
        "animate_preset_group_transform": animate_preset_group_transform,
        "reflow_ordinary_captions": reflow_ordinary_captions,
        "replace_ordinary_captions_with_presets": replace_ordinary_captions_with_presets,
        "replace_preset_group": replace_preset_group,
        "add_preset_group": add_preset_group,
        "place_preset_group": place_preset_group,
        "add_broll": add_broll,
        "set_track_render_order": set_track_render_order,
        "fit_template_text_bounds": fit_template_text_bounds,
        "measure_preset_internal_collisions": measure_preset_internal_collisions,
    }


def shared_final_validator(draft: Mapping[str, Any], _context: Any) -> Mapping[str, Any]:
    """Validate the minimum native draft shape and every referenced material."""
    checks = {
        "draft_object": isinstance(draft, Mapping),
        "tracks_array": isinstance(draft.get("tracks"), list),
        "materials_object": isinstance(draft.get("materials"), Mapping),
    }
    errors = []
    if checks["tracks_array"] and checks["materials_object"]:
        materials = draft.get("materials") or {}
        indexes = {str(item.get("id")) for group in materials.values() if isinstance(group, list) for item in group if isinstance(item, Mapping) and item.get("id")}
        for track in draft.get("tracks") or []:
            for segment in track.get("segments") or []:
                if str(segment.get("material_id") or "") not in indexes:
                    errors.append(f"track {track.get('name') or track.get('id')} references missing material")
    return {"ok": all(checks.values()) and not errors, "checks": checks, "errors": errors}
