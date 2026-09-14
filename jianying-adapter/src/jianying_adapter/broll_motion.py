"""Explicit linear transforms for one declared, original-speed B-roll segment."""
from __future__ import annotations

import copy
from typing import Any, Mapping

from .aroll_motion import _integer, _number, _native_keyframe_model


def animate_broll_transform(draft: dict[str, Any], spec: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Write native position/scale points only after every check has passed."""
    node, name = spec.get("node_id"), spec.get("track_name")
    if not isinstance(node, str) or not node.strip() or not isinstance(name, str) or not name.startswith("JY_BROLL_"):
        raise ValueError("B-roll motion requires explicit node_id and JY_BROLL_ track_name")
    declarations = [item for item in context.plan.get("brolls", [])
                    if item.get("node_id") == node and item.get("track_name") == name]
    if len(declarations) != 1:
        raise ValueError("B-roll motion must match one declared node_id/track_name")
    if spec.get("position_space") != "normalized" or spec.get("interpolation") != "linear":
        raise ValueError("B-roll motion supports normalized positions and linear interpolation only")
    tracks = [track for track in draft.get("tracks", []) if track.get("name") == name]
    if len(tracks) != 1 or tracks[0].get("type") != "video":
        raise ValueError("B-roll motion requires one unique video track")
    segments = tracks[0].get("segments")
    if not isinstance(segments, list) or len(segments) != 1 or not isinstance(segments[0], dict):
        raise ValueError("B-roll motion requires exactly one segment; split explicitly before use")
    segment = segments[0]
    source, target = segment.get("source_timerange"), segment.get("target_timerange")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        raise ValueError("B-roll motion requires explicit source and target timeranges")
    start = _integer(target.get("start"), "target start")
    duration = _integer(target.get("duration"), "target duration", minimum=1)
    _integer(source.get("start"), "source start")
    if _integer(source.get("duration"), "source duration", minimum=1) != duration:
        raise ValueError("B-roll motion requires equal source and target duration")
    declaration = declarations[0]
    if (declaration.get("start_us"), declaration.get("end_us"), declaration.get("segment_count")) != (start, start + duration, 1):
        raise ValueError("B-roll motion segment timing/count differs from its declaration")
    if _number(segment.get("speed"), "segment speed", positive=True) != 1.0 or segment.get("reverse"):
        raise ValueError("B-roll motion requires original speed and forward playback")
    clip = segment.get("clip") or {}
    if _number(clip.get("rotation", 0), "clip rotation") != 0:
        raise ValueError("B-roll motion does not support rotation")
    uniform = segment.get("uniform_scale") or {}
    if uniform.get("on") and _number(uniform.get("value", 1), "uniform scale", positive=True) != 1:
        raise ValueError("B-roll motion cannot compose a non-default uniform_scale")
    references = set(segment.get("extra_material_refs") or [])
    materials = draft.get("materials") or {}
    for material in materials.get("speeds") or []:
        if material.get("id") in references and (
            _number(material.get("speed"), "speed material", positive=True) != 1 or material.get("curve_speed")
        ):
            raise ValueError("B-roll motion conflicts with speed material")
    for material in materials.get("material_animations") or []:
        if material.get("id") in references and material.get("animations"):
            raise ValueError("B-roll motion conflicts with native animation")
    existing = segment.get("common_keyframes", [])
    if not isinstance(existing, list) or any(not isinstance(group, Mapping) or group.get("property_type") != "KFTypeVolume" for group in existing):
        raise ValueError("B-roll motion conflicts with existing visual keyframes")
    if segment.get("keyframe_refs") or segment.get("animations"):
        raise ValueError("B-roll motion conflicts with existing motion references")
    frames = spec.get("keyframes")
    if not isinstance(frames, list) or len(frames) < 2:
        raise ValueError("B-roll motion requires at least two explicit keyframes")
    properties = ("scale_x", "scale_y", "position_x", "position_y")
    checked, previous = [], -1
    for frame in frames:
        if not isinstance(frame, Mapping) or set(frame) != {"time_offset_us", *properties}:
            raise ValueError("B-roll keyframe requires time_offset_us and all four transform values")
        time = _integer(frame["time_offset_us"], "time_offset_us")
        if time <= previous or time > duration:
            raise ValueError("B-roll keyframe times must strictly increase within segment duration")
        checked.append((time, {p: _number(frame[p], p, positive=p.startswith("scale")) for p in properties}))
        previous = time
    if checked[0][0] != 0:
        raise ValueError("B-roll first keyframe must be at zero")
    model = _native_keyframe_model()
    generated = []
    for prop in properties:
        group = model.KeyframeList(getattr(model.KeyframeProperty, prop))
        for time, values in checked:
            group.add_keyframe(time, values[prop])
        generated.append(group.export_json())
    updated_clip = copy.deepcopy(clip)
    first = checked[0][1]
    updated_clip["scale"] = {"x": first["scale_x"], "y": first["scale_y"]}
    updated_clip["transform"] = {"x": first["position_x"], "y": first["position_y"]}
    segment["clip"] = updated_clip
    segment["common_keyframes"] = copy.deepcopy(existing) + generated
    segment["uniform_scale"] = {"on": False, "value": 1.0}
    return {"node_id": node, "track_name": name, "segment_id": segment.get("id"),
            "keyframe_count_per_property": len(checked), "position_space": "normalized",
            "interpolation": "linear", "timing_and_audio_unchanged": True, "native_visual_qa": "pending"}
