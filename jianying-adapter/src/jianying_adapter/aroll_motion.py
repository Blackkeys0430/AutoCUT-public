"""Small, explicit transform-keyframe operation for a frozen rough-cut segment."""

from __future__ import annotations

import copy
import importlib.util
import math
from pathlib import Path
from typing import Any, Mapping


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")
    return float(value)


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _native_keyframe_model() -> Any:
    # Load the dependency-free, vendored model itself. Do not duplicate its schema
    # or import the package's GUI/media dependencies for this JSON-only operation.
    path = Path(__file__).resolve().parents[2] / "vendor/pyJianYingDraft-source/pyJianYingDraft/keyframe.py"
    spec = importlib.util.spec_from_file_location("jianying_native_keyframe_model", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"native keyframe model unavailable: {path}")
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)
    return model


def animate_aroll_transform(draft: dict[str, Any], spec: Mapping[str, Any], _context: Any) -> Mapping[str, Any]:
    """Add absolute normalized transform values; leave media and audio untouched.

    Without segment_id retain the legacy one-segment, source-zero contract.
    An explicit segment_id selects one original-speed segment; keyframe times
    remain relative to that segment, not to the draft timeline.
    All checks precede mutation, so a rejected operation leaves its input intact.
    """
    if spec.get("track_name") != "JY_ROUGH_CUT_VIDEO":
        raise ValueError("animate_aroll_transform requires explicit JY_ROUGH_CUT_VIDEO track_name")
    if spec.get("position_space") != "normalized" or spec.get("interpolation") != "linear":
        raise ValueError("animate_aroll_transform supports only normalized positions and linear interpolation")
    matches = [t for t in draft.get("tracks", []) if t.get("name") == spec["track_name"]]
    if len(matches) != 1 or matches[0].get("type") != "video":
        raise ValueError("A-roll track must be unique and have type video")
    segments = matches[0].get("segments")
    selected_id = spec.get("segment_id")
    if selected_id is None:
        if not isinstance(segments, list) or len(segments) != 1 or not isinstance(segments[0], dict):
            raise ValueError("A-roll transform v1 requires exactly one segment without segment_id")
        segment = segments[0]
    else:
        if not isinstance(selected_id, str) or not selected_id.strip() or not isinstance(segments, list):
            raise ValueError("segment_id must explicitly identify one A-roll segment")
        selected = [s for s in segments if isinstance(s, dict) and s.get("id") == selected_id]
        if len(selected) != 1:
            raise ValueError("segment_id must identify exactly one A-roll segment")
        segment = selected[0]
    source, target = segment.get("source_timerange"), segment.get("target_timerange")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        raise ValueError("A-roll requires explicit source and target timeranges")
    _integer(target.get("start"), "target start")
    duration = _integer(target.get("duration"), "target duration", minimum=1)
    source_start = _integer(source.get("start"), "source start")
    if (selected_id is None and source_start != 0) or _integer(source.get("duration"), "source duration", minimum=1) != duration:
        raise ValueError("A-roll transform requires equal source/target duration; legacy entry requires source start zero")
    if _number(segment.get("speed"), "segment speed", positive=True) != 1.0 or segment.get("reverse"):
        raise ValueError("A-roll transform requires original speed and forward playback")
    materials = draft.get("materials") or {}
    references = set(segment.get("extra_material_refs") or [])
    for item in materials.get("speeds") or []:
        if item.get("id") in references and (_number(item.get("speed"), "speed material", positive=True) != 1.0 or item.get("curve_speed")):
            raise ValueError("A-roll transform conflicts with speed material")
    for item in materials.get("material_animations") or []:
        if item.get("id") in references and item.get("animations"):
            raise ValueError("A-roll transform conflicts with native animation")
    existing = segment.get("common_keyframes", [])
    if not isinstance(existing, list) or any(not isinstance(g, Mapping) or g.get("property_type") != "KFTypeVolume" for g in existing):
        raise ValueError("A-roll transform conflicts with existing visual keyframes")
    if segment.get("keyframe_refs") or segment.get("animations"):
        raise ValueError("A-roll transform conflicts with existing motion references")
    frames = spec.get("keyframes")
    if not isinstance(frames, list) or len(frames) < 2:
        raise ValueError("A-roll requires at least two explicit keyframes")
    properties = ("scale_x", "scale_y", "position_x", "position_y")
    checked = []
    previous = -1
    for frame in frames:
        if not isinstance(frame, Mapping) or set(frame) != {"time_offset_us", *properties}:
            raise ValueError("keyframe requires exactly time_offset_us and all four transform values")
        time = _integer(frame["time_offset_us"], "time_offset_us")
        if time <= previous or time > duration:
            raise ValueError("keyframe times must strictly increase within segment duration")
        checked.append((time, {p: _number(frame[p], p, positive=p.startswith("scale")) for p in properties}))
        previous = time
    if checked[0][0] != 0:
        raise ValueError("first keyframe must be at zero to define the initial state")
    model = _native_keyframe_model()
    generated = []
    for prop in properties:
        group = model.KeyframeList(getattr(model.KeyframeProperty, prop))
        for time, values in checked:
            group.add_keyframe(time, values[prop])
        generated.append(group.export_json())
    clip = copy.deepcopy(segment.get("clip") or {})
    first = checked[0][1]
    clip["scale"] = {"x": first["scale_x"], "y": first["scale_y"]}
    clip["transform"] = {"x": first["position_x"], "y": first["position_y"]}
    segment["clip"] = clip
    segment["common_keyframes"] = copy.deepcopy(existing) + generated
    # Matches VisualSegment.add_keyframe(scale_x/scale_y).export_json().
    segment["uniform_scale"] = {"on": False, "value": 1.0}
    return {"track_name": spec["track_name"], "segment_id": segment.get("id"),
            "keyframe_count_per_property": len(checked), "property_count": 4,
            "position_space": "normalized", "interpolation": "linear",
            "native_visual_qa": "pending", "timing_and_audio_unchanged": True}
