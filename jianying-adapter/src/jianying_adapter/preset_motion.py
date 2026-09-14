"""Explicit second-beat group motion after a native preset has entered."""
from __future__ import annotations

import copy
from typing import Any, Mapping

from .aroll_motion import _integer, _number, _native_keyframe_model


def animate_preset_group_transform(draft: dict[str, Any], spec: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Preserve native layout and animations; move the complete stable group."""
    from .shared_operations import (
        _declared_selection, _explicit_point, _native_animation_index,
        _position_from_px, _position_to_px, _preset_group_entries,
        _require_supported_canvas, _timerange,
    )
    canvas = _require_supported_canvas(context, "animate_preset_group_transform", draft)
    template, node = str(spec.get("template_id") or ""), str(spec.get("node_id") or "")
    selection = _declared_selection(context, template, node)
    if spec.get("interpolation") != "linear":
        raise ValueError("preset group motion supports only linear interpolation")
    anchor = _explicit_point(spec.get("anchor_px"), "anchor_px")
    raw_frames = spec.get("keyframes")
    if not isinstance(raw_frames, list) or len(raw_frames) < 2:
        raise ValueError("preset group motion requires at least two explicit keyframes")
    frames = []
    previous = -1
    for frame in raw_frames:
        if not isinstance(frame, Mapping) or set(frame) != {"timeline_time_us", "group_scale", "shift_x_px", "shift_y_px"}:
            raise ValueError("preset group keyframe requires timeline_time_us, group_scale and shift_x_px/shift_y_px")
        time = _integer(frame["timeline_time_us"], "timeline_time_us")
        if time <= previous:
            raise ValueError("preset group keyframe times must strictly increase")
        frames.append((time, _number(frame["group_scale"], "group_scale", positive=True),
                       _number(frame["shift_x_px"], "shift_x_px"), _number(frame["shift_y_px"], "shift_y_px")))
        previous = time
    if frames[0][1:] != (1.0, 0.0, 0.0):
        raise ValueError("first preset group keyframe must be neutral to preserve the native entrance")
    node_start = _integer(selection.get("start_us"), "node start")
    node_end = _integer(selection.get("end_us"), "node end", minimum=node_start + 1)
    if frames[0][0] < node_start or frames[-1][0] > node_end:
        raise ValueError("preset group keyframes exceed declared node")
    entries = _preset_group_entries(draft, template, node)
    animation_index = _native_animation_index(draft)
    model = _native_keyframe_model()
    updates = []
    for name, segment, _material in entries:
        start, end = _timerange(segment)
        existing = segment.get("common_keyframes", [])
        if not isinstance(existing, list) or any(not isinstance(g, Mapping) or g.get("property_type") != "KFTypeVolume" for g in existing):
            raise ValueError(f"preset group motion conflicts with existing visual keyframes: {name}")
        if segment.get("keyframe_refs") or segment.get("animations"):
            raise ValueError(f"preset group motion conflicts with motion references: {name}")
        stable_start, stable_end = start, end
        for reference in segment.get("extra_material_refs") or []:
            animation = animation_index.get(str(reference))
            if animation is None:
                continue
            for item in animation.get("animations") or []:
                if not isinstance(item, Mapping) or item.get("type") not in {"in", "out"}:
                    raise ValueError("preset group motion rejects loop/combined native animation")
                offset = _integer(item.get("start"), "native animation start")
                duration = _integer(item.get("duration"), "native animation duration", minimum=1)
                if offset + duration > end - start:
                    raise ValueError("native animation exceeds preset segment")
                if item["type"] == "in":
                    stable_start = max(stable_start, start + offset + duration)
                else:
                    stable_end = min(stable_end, start + offset)
        if frames[0][0] < stable_start or frames[-1][0] > stable_end:
            raise ValueError(f"preset group motion must fit every track's stable interval: {name}")
        clip = segment.get("clip") or {}
        scale = clip.get("scale") or {"x": 1.0, "y": 1.0}
        position = clip.get("transform") or {"x": 0.0, "y": 0.0}
        base_scale = {a: _number(scale.get(a), f"clip scale {a}", positive=True) for a in ("x", "y")}
        base_position = {a: _position_to_px(a, _number(position.get(a), f"clip position {a}"), canvas) for a in ("x", "y")}
        uniform = segment.get("uniform_scale") or {}
        if uniform.get("on") and _number(uniform.get("value", 1.0), "uniform_scale value", positive=True) != 1.0:
            raise ValueError("non-default native uniform_scale cannot be safely composed")
        points = [(start, 1.0, 0.0, 0.0)] if frames[0][0] > start else []
        points += frames
        generated = []
        for prop in ("scale_x", "scale_y", "position_x", "position_y"):
            group = model.KeyframeList(getattr(model.KeyframeProperty, prop))
            axis = prop[-1]
            i = 0 if axis == "x" else 1
            for time, factor, dx, dy in points:
                value = base_scale[axis] * factor if prop.startswith("scale") else _position_from_px(
                    axis, anchor[i] + (base_position[axis] - anchor[i]) * factor + (dx if i == 0 else dy), canvas)
                group.add_keyframe(time - start, value)
            generated.append(group.export_json())
        updates.append((segment, copy.deepcopy(existing) + generated))
    # Commit only after every track passes, including late auxiliary tracks.
    for segment, groups in updates:
        segment["common_keyframes"] = groups
        segment["uniform_scale"] = {"on": False, "value": 1.0}
    return {"template_id": template, "node_id": node, "segment_count": len(updates),
            "anchor_px": list(anchor), "keyframe_count": len(frames),
            "motion_start_us": frames[0][0], "motion_end_us": frames[-1][0],
            "native_visual_qa": "pending", "native_animations_and_timing_unchanged": True}
