"""Lossless structural segmentation of a clean frozen rough-cut A-roll."""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Mapping

from .aroll_motion import _integer, _number


def split_video_track(draft: dict[str, Any], spec: Mapping[str, Any], _context: Any) -> Mapping[str, Any]:
    """Split only at explicit absolute timeline cuts, with caller-frozen IDs.

    No media is cut or reencoded. The concatenated source/target intervals are
    identical and every untouched native/audio field is copied byte-for-byte
    as JSON data. Complex or time-dependent input is deliberately refused.
    """
    fields = {"id", "kind", "track_name", "segment_id", "cut_points_us", "segment_ids"}
    if set(spec) != fields or spec.get("kind") != "split_video_track" or spec.get("track_name") != "JY_ROUGH_CUT_VIDEO":
        raise ValueError("split_video_track requires explicit JY_ROUGH_CUT_VIDEO, segment_id, cut_points_us and segment_ids")
    for field in ("id", "segment_id"):
        if not isinstance(spec[field], str) or not spec[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    tracks = [t for t in draft.get("tracks", []) if isinstance(t, dict) and t.get("name") == spec["track_name"]]
    if len(tracks) != 1 or tracks[0].get("type") != "video":
        raise ValueError("split requires one unique A-roll video track")
    segments = tracks[0].get("segments")
    if not isinstance(segments, list) or any(not isinstance(s, dict) for s in segments):
        raise ValueError("split requires a valid A-roll segment list")
    selected = [(position, s) for position, s in enumerate(segments) if s.get("id") == spec["segment_id"]]
    if len(selected) != 1:
        raise ValueError("split requires one uniquely selected clean segment")
    selected_position, segment = selected[0]
    source, target = segment.get("source_timerange"), segment.get("target_timerange")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        raise ValueError("split requires explicit source/target ranges")
    source_start = _integer(source.get("start"), "source start")
    start = _integer(target.get("start"), "target start")
    duration = _integer(target.get("duration"), "target duration", minimum=1)
    if _integer(source.get("duration"), "source duration", minimum=1) != duration:
        raise ValueError("split requires equal source/target duration")
    if _number(segment.get("speed"), "speed", positive=True) != 1 or segment.get("reverse"):
        raise ValueError("split requires original-speed forward playback")
    for field in ("common_keyframes", "keyframe_refs", "animations", "transition", "transitions", "curve_speed", "audio_fade", "fade_in_duration", "fade_out_duration"):
        if segment.get(field):
            raise ValueError(f"split cannot duplicate existing time-dependent {field}")
    materials = draft.get("materials")
    if not isinstance(materials, Mapping):
        raise ValueError("split requires materials")
    index: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for bucket, items in materials.items():
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping) and item.get("id"):
                    index.setdefault(str(item["id"]), []).append((bucket, item))
    media = index.get(str(segment.get("material_id")), [])
    if len(media) != 1 or media[0][0] != "videos" or not media[0][1].get("path"):
        raise ValueError("split requires one unambiguous frozen video material")
    refs = segment.get("extra_material_refs", [])
    if not isinstance(refs, list) or any(not isinstance(r, str) or not r for r in refs) or len(refs) != len(set(refs)):
        raise ValueError("split has malformed material references")
    fade = None
    for ref in refs:
        matches = index.get(ref, [])
        if len(matches) != 1:
            raise ValueError("split has missing or ambiguous material reference")
        bucket, item = matches[0]
        if bucket == "speeds":
            if _number(item.get("speed"), "speed material", positive=True) != 1 or item.get("curve_speed"):
                raise ValueError("split conflicts with speed material")
        elif bucket == "material_animations" and not item.get("animations"):
            pass
        elif bucket == "audio_fades":
            if fade is not None or item.get("type") != "audio_fade" or item.get("fade_type") != 0:
                raise ValueError("split supports only one simple endpoint audio fade")
            if set(item) - {"id", "type", "fade_type", "fade_in_duration", "fade_out_duration"}:
                raise ValueError("split refuses unknown audio fade behavior")
            fade_in = _integer(item.get("fade_in_duration"), "fade in")
            fade_out = _integer(item.get("fade_out_duration"), "fade out")
            if fade_in + fade_out > duration:
                raise ValueError("split cannot preserve overlapping endpoint fades")
            fade = (ref, item, fade_in, fade_out)
        else:
            raise ValueError(f"split refuses unverified/time-dependent material: {bucket}")
    cuts = spec["cut_points_us"]
    if not isinstance(cuts, list) or not cuts:
        raise ValueError("cut_points_us must be a nonempty list of internal timeline times")
    previous = start
    for value in cuts:
        cut = _integer(value, "cut point")
        if cut <= previous or cut >= start + duration:
            raise ValueError("cut points must strictly increase inside target range")
        previous = cut
    if fade and (cuts[0] - start < fade[2] or start + duration - cuts[-1] < fade[3]):
        raise ValueError("split cut cannot intersect an existing endpoint fade")
    ids = spec["segment_ids"]
    if not isinstance(ids, list) or len(ids) != len(cuts) + 1 or any(not isinstance(s, str) or not s.strip() for s in ids) or len(ids) != len(set(ids)):
        raise ValueError("segment_ids must give one unique nonempty ID per output segment")
    other_ids = {s.get("id") for t in draft.get("tracks", []) if isinstance(t, Mapping)
                 for s in t.get("segments", []) if isinstance(s, Mapping) and s is not segment}
    if any(identifier in other_ids for identifier in ids):
        raise ValueError("output segment ID collides with another draft segment")
    boundaries = [start, *cuts, start + duration]
    output, new_fades = [], []
    for position, (identifier, left, right) in enumerate(zip(ids, boundaries, boundaries[1:])):
        item = copy.deepcopy(segment)
        item["id"] = identifier
        item["source_timerange"] = {**source, "start": source_start + left - start, "duration": right - left}
        item["target_timerange"] = {**target, "start": left, "duration": right - left}
        if fade:
            fade_id, original_fade, fade_in, fade_out = fade
            item["extra_material_refs"] = [r for r in refs if r != fade_id]
            in_duration = fade_in if position == 0 else 0
            out_duration = fade_out if position == len(ids) - 1 else 0
            if in_duration or out_duration:
                copied_fade = copy.deepcopy(original_fade)
                copied_fade["id"] = "split_fade_" + hashlib.sha256((fade_id + "\0" + identifier).encode()).hexdigest()[:24]
                if copied_fade["id"] in index:
                    raise ValueError("split audio fade ID collides with an existing material")
                copied_fade.update(fade_in_duration=in_duration, fade_out_duration=out_duration)
                new_fades.append(copied_fade)
                item["extra_material_refs"].append(copied_fade["id"])
        output.append(item)
    if new_fades:
        # Existing materials remain available to other segments. Only independent
        # endpoint fades are appended; interior cuts never acquire a new fade.
        materials["audio_fades"].extend(new_fades)
    segments[selected_position:selected_position + 1] = output
    return {"track_name": spec["track_name"], "input_segment_id": segment["id"], "segment_ids": list(ids),
            "cut_points_us": list(cuts), "segment_count": len(output),
            "source_range_us": [source_start, source_start + duration], "target_range_us": [start, start + duration],
            "source_target_coverage_unchanged": True, "audio_continuity_preserved": True,
            "endpoint_fades_redistributed": bool(fade),
            "native_visual_qa": "pending"}
