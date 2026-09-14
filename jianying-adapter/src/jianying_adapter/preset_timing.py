"""Align explicit preset text tracks with current speech, without retiming motion."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path


def _inline_keyframe_end(segment, target_duration):
    """Keep bounded, self-contained native text keyframes at their offsets."""
    groups = segment.get("common_keyframes") or []
    if not isinstance(groups, list):
        raise ValueError("preset timing requires explicit inline keyframe groups")
    supported = {"KFTypeScaleX": 1, "KFTypePositionX": 1,
                 "KFTypePositionY": 1, "KFTypeTextColor": 4}
    old_duration = segment["target_timerange"]["duration"]
    latest = 0
    for group in groups:
        if (not isinstance(group, dict) or group.get("material_id")
                or group.get("property_type") not in supported):
            raise ValueError("preset timing cannot resolve external or unsupported keyframes")
        points = group.get("keyframe_list")
        if not isinstance(points, list) or not points:
            raise ValueError("preset timing requires explicit native keyframe points")
        previous = -1
        for point in points:
            if not isinstance(point, dict):
                raise ValueError("invalid native keyframe point")
            offset, values = point.get("time_offset"), point.get("values")
            if (type(offset) is not int or not previous < offset <= old_duration
                    or offset > target_duration):
                raise ValueError("preset timing keyframe offset is unordered or out of bounds")
            if (point.get("graphID") or point.get("string_value")
                    or point.get("curveType") != "Line"
                    or not isinstance(values, list)
                    or len(values) != supported[group["property_type"]]
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)):
                raise ValueError("preset timing requires self-contained numeric linear keyframes")
            previous = offset
            latest = max(latest, offset)
    return latest


def retime_preset_text_tracks(draft, spec, context):
    """Move one-segment text tracks and change only their stable hold duration.

    Entrance-only native animations keep their original relative time/duration.
    Bounded inline numeric keyframes keep their offsets and final hold state.
    Tracks with looping, exit, externally linked or unresolved motion are unsupported.
    Declarations must agree with the current CandidatePlan; source presets and
    animation materials remain unchanged.
    """
    authorization = spec.get("authorization_source")
    state = json.loads(Path(context.plan["project_state"]).read_text("utf-8-sig"))
    if not authorization or not spec.get("design_reason") or not any(
        a.get("option") == "visual_packaging_revision" and a.get("enabled") is True
        and a.get("source") == authorization for a in state.get("current_authorizations", [])
    ):
        raise ValueError("preset timing requires current visual authorization and design reason")
    template, node = spec.get("template_id"), spec.get("node_id")
    selected = [p for p in context.plan.get("presets", [])
                if p.get("template_id") == template and p.get("node_id") == node]
    if not template or not node or len(selected) != 1:
        raise ValueError("preset timing requires one declared template/node")
    selection = selected[0]
    declarations = selection.get("actual_text_tracks", [])
    requests = spec.get("tracks")
    if not isinstance(requests, list) or not requests:
        raise ValueError("preset timing requires explicit tracks")
    result = copy.deepcopy(draft)
    animations = {a["id"]: a for a in result["materials"].get("material_animations", [])}
    seen, reports = set(), []
    for request in requests:
        if not isinstance(request, dict) or set(request) != {"track_name", "start_us", "end_us"}:
            raise ValueError("timing row requires only track_name/start_us/end_us")
        name, start, end = (request[k] for k in ("track_name", "start_us", "end_us"))
        if not isinstance(name, str) or name in seen or not name.startswith(f"JY_PRESET_{template}__NODE__{node}_"):
            raise ValueError("preset timing requires unique exact tracks in the selected node")
        seen.add(name)
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ValueError("preset timing requires a positive integer interval")
        if not selection["start_us"] <= start < end <= selection["end_us"]:
            raise ValueError("preset timing exceeds declared node window")
        declared = [t for t in declarations if t.get("track_name") == name]
        if len(declared) != 1 or any(declared[0].get(k) != v for k, v in
                                    (("start_us", start), ("end_us", end), ("segment_count", 1))):
            raise ValueError("preset timing must match actual_text_tracks declaration")
        tracks = [t for t in result["tracks"] if t.get("type") == "text" and t.get("name") == name]
        if len(tracks) != 1 or len(tracks[0].get("segments", [])) != 1:
            raise ValueError("preset timing supports exactly one segment per text track")
        segment = tracks[0]["segments"][0]
        if any(segment.get(k) for k in ("keyframe_refs", "animations")):
            raise ValueError("preset timing cannot retime keyframed or unresolved motion")
        keyframe_end = _inline_keyframe_end(segment, end - start)
        motion_end = 0
        for ref in segment.get("extra_material_refs", []):
            if ref not in animations:
                continue
            sequence = animations[ref].get("animations")
            if not isinstance(sequence, list):
                raise ValueError("invalid native animation sequence")
            for animation in sequence:
                offset, duration = animation.get("start", 0), animation.get("duration")
                if (animation.get("type") != "in" or type(offset) is not int or offset < 0
                        or type(duration) is not int or duration <= 0):
                    raise ValueError("preset timing only supports explicit entrance-only animations")
                motion_end = max(motion_end, offset + duration)
        if end - start < motion_end:
            raise ValueError("preset timing would truncate native entrance animation")
        old = copy.deepcopy(segment["target_timerange"])
        segment["target_timerange"] = {"start": start, "duration": end - start}
        reports.append({"track_name": name, "before": old, "after": segment["target_timerange"],
                        "entrance_end_offset_us": motion_end,
                        "keyframe_end_offset_us": keyframe_end,
                        "inline_keyframes_unchanged": True})
    from .preset_audio import sync_preset_audio
    audio_report = sync_preset_audio(draft, result, prefix=f"JY_PRESET_{template}__NODE__{node}_",
                                    start_us=selection['start_us'], end_us=selection['end_us'],
                                    bindings=spec.get('audio_bindings', []))
    return result, {"tracks": reports, "original_audio": audio_report, "animation_materials_unchanged": True,
                    "native_visual_verified": False}
