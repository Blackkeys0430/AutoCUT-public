"""Attach a built-in native mask to one explicitly selected video segment."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .animation_recipes import _ensure_vendored_import_paths


def _number(value: Any, label: str, low: float, high: float, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if not low <= value <= high or (positive and value <= 0):
        raise ValueError(f"{label} is outside the supported range")
    return float(value)


def _native_segment_model() -> Any:
    _ensure_vendored_import_paths()
    from pyJianYingDraft.video_segment import VideoSegment, MaskType
    return VideoSegment, MaskType


def _visible_segment(segment: Mapping[str, Any], *, reference: bool = True) -> None:
    if reference and ("enable_video_mask" in segment or segment.get("enable_adjust_mask") is not False):
        raise ValueError("8.8 mask requires enable_video_mask absent and enable_adjust_mask=false")
    if not reference and any(k in segment and not isinstance(segment[k], bool)
                             for k in ("enable_video_mask", "enable_adjust_mask")):
        raise ValueError("existing mask switches must be boolean when present")
    clip = segment.get("clip")
    if not isinstance(clip, Mapping):
        raise ValueError("mask target requires native clip settings")
    _number(clip.get("alpha"), "static alpha", 0, 1, positive=True)
    frames = segment.get("common_keyframes", [])
    if not isinstance(frames, list) or any(not isinstance(k, Mapping) or
            ("mask" in str(k.get("property_type", "")).lower()) or
            k.get("property_type") == "KFTypeAlpha" for k in frames):
        raise ValueError("unverified Alpha/mask keyframes conflict with a new static mask")
    if segment.get("keyframe_refs"):
        raise ValueError("unresolved keyframe references cannot establish absence of Alpha/mask animation")


def _reference_mask(value: Any, shape: str) -> tuple[dict[str, Any], str]:
    """Read a frozen, already decoded 8.8 source; never import old build code."""
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "segment_id"}:
        raise ValueError("native_reference requires path, sha256 and segment_id")
    if any(not isinstance(value[k], str) or not value[k] for k in value):
        raise ValueError("native_reference fields must be non-empty strings")
    path = Path(value["path"])
    if not path.is_absolute() or not path.is_file() or path.name == "project_state.json":
        raise ValueError("native_reference must be an explicit frozen draft JSON file")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest.lower() != value["sha256"].lower():
        raise ValueError("native_reference SHA256 mismatch")
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("native_reference must contain a draft object")
    reference = data.get("draft", data)
    if not isinstance(reference, dict) or any(not isinstance(reference.get(k), Mapping) or
            reference[k].get("app_version") != "8.8.0" for k in ("platform", "last_modified_platform")):
        raise ValueError("native_reference requires both platform versions to be 8.8.0")
    segments = [s for t in reference.get("tracks", []) if t.get("type") == "video"
                for s in t.get("segments", []) if s.get("id") == value["segment_id"]]
    if len(segments) != 1:
        raise ValueError("native_reference segment must exist exactly once")
    _visible_segment(segments[0])
    refs = segments[0].get("extra_material_refs", [])
    if not isinstance(refs, list) or any(not isinstance(r, str) or not r for r in refs) or len(refs) != len(set(refs)):
        raise ValueError("native_reference has duplicate or malformed references")
    all_items = [m for items in reference.get("materials", {}).values() if isinstance(items, list)
                 for m in items if isinstance(m, Mapping)]
    if any(sum(m.get("id") == ref for m in all_items) != 1 for ref in refs):
        raise ValueError("native_reference has missing or ambiguous material references")
    matches = [(bucket, m) for bucket, items in reference.get("materials", {}).items()
               if isinstance(items, list) for m in items if isinstance(m, dict) and m.get("id") in refs
               and (bucket in ("masks", "mask", "common_mask") or m.get("type") == "mask")]
    if len(matches) != 1 or matches[0][0] != "common_mask":
        raise ValueError("native_reference must reference exactly one common_mask")
    mask = matches[0][1]
    if mask.get("resource_type") != shape or mask.get("type") != "mask" or not mask.get("resource_id"):
        raise ValueError("native_reference mask identity/shape mismatch")
    if not isinstance(mask.get("config"), dict) or not {"width", "height"} <= set(mask["config"]):
        raise ValueError("native_reference lacks mask geometry")
    if not isinstance(mask.get("path"), str) or not Path(mask["path"]).is_dir():
        raise ValueError("native_reference mask resource directory is unavailable")
    return copy.deepcopy(mask), digest


def apply_video_mask(draft: dict[str, Any], spec: Mapping[str, Any], _context: Any) -> Mapping[str, Any]:
    """Reuse VideoSegment.add_mask; never reconstruct or retime the segment.

    Coordinates are offsets from the source-material centre, in source pixels.
    size_ratio is mask height / source height; rectangle width_ratio is mask
    width / source width. Native roundness/feather use a 0..100 input range.
    All validation and native construction precede draft mutation.
    This proves model compatibility only: current native visual QA is pending.
    """
    required = {"id", "kind", "track_name", "segment_id", "shape", "center_x_px", "center_y_px",
                "size_ratio", "rotation_deg", "feather_percent", "invert", "native_reference"}
    shape = spec.get("shape")
    if shape == "rectangle":
        required |= {"width_ratio", "round_corner_percent"}
    if shape not in ("circle", "rectangle") or set(spec) != required or spec.get("kind") != "apply_video_mask":
        raise ValueError("apply_video_mask requires explicit circle/rectangle parameters; unknown fields are rejected")
    for key in ("id", "track_name", "segment_id"):
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if not isinstance(spec["invert"], bool):
        raise ValueError("invert must be boolean")
    tracks = [t for t in draft.get("tracks", []) if isinstance(t, Mapping) and t.get("name") == spec["track_name"]]
    if len(tracks) != 1 or tracks[0].get("type") != "video":
        raise ValueError("mask target track must be unique and have type video")
    segments = [s for s in tracks[0].get("segments", []) if isinstance(s, dict) and s.get("id") == spec["segment_id"]]
    if len(segments) != 1:
        raise ValueError("mask target segment must exist exactly once in the selected track")
    segment = segments[0]
    _visible_segment(segment, reference=False)
    materials = draft.get("materials")
    if not isinstance(materials, dict):
        raise ValueError("materials must be an object")
    masks = materials.get("common_mask", [])
    if not isinstance(masks, list):
        raise ValueError("materials.common_mask must be an array")
    index: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for bucket, items in materials.items():
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping) and item.get("id"):
                    index.setdefault(str(item["id"]), []).append((bucket, item))
    material_matches = index.get(str(segment.get("material_id")), [])
    if len(material_matches) != 1 or material_matches[0][0] != "videos":
        raise ValueError("mask target must reference one unambiguous video material")
    material = material_matches[0][1]
    width = _number(material.get("width"), "source width", 0, math.inf, positive=True)
    height = _number(material.get("height"), "source height", 0, math.inf, positive=True)
    refs = segment.get("extra_material_refs", [])
    if not isinstance(refs, list) or any(not isinstance(r, str) or not r for r in refs) or len(set(refs)) != len(refs):
        raise ValueError("extra_material_refs must contain unique non-empty IDs")
    for ref in refs:
        matches = index.get(ref, [])
        if len(matches) != 1:
            raise ValueError("existing extra material reference is missing or ambiguous")
        bucket, item = matches[0]
        if bucket in ("masks", "mask", "common_mask") or item.get("type") == "mask":
            raise ValueError("target segment already has a mask")
    if segment.get("mask") or segment.get("masks"):
        raise ValueError("target segment already declares a mask")
    kwargs = {
        "center_x": _number(spec["center_x_px"], "center_x_px", -width / 2, width / 2),
        "center_y": _number(spec["center_y_px"], "center_y_px", -height / 2, height / 2),
        "size": _number(spec["size_ratio"], "size_ratio", 0, 1, positive=True),
        "rotation": _number(spec["rotation_deg"], "rotation_deg", -360, 360),
        "feather": _number(spec["feather_percent"], "feather_percent", 0, 100),
        "invert": spec["invert"],
    }
    if shape == "rectangle":
        kwargs.update(rect_width=_number(spec["width_ratio"], "width_ratio", 0, 1, positive=True),
                      round_corner=_number(spec["round_corner_percent"], "round_corner_percent", 0, 100))
    mask, reference_digest = _reference_mask(spec["native_reference"], shape)
    VideoSegment, MaskType = _native_segment_model()
    # add_mask only consumes these three fields. Avoid VideoMaterial probing and
    # VideoSegment.export_json, which would replace the existing native segment.
    native = VideoSegment.__new__(VideoSegment)
    native.material_size = (width, height)
    native.mask = None
    native.extra_material_refs = []
    native.add_mask(MaskType.圆形 if shape == "circle" else MaskType.矩形, **kwargs)
    computed = native.mask.export_json()
    # The vendor computes geometry, but its resource identity and 'masks' bucket
    # differ from observed 8.8 common_mask. Preserve the reference's native shell.
    defaults = {"centerX": 0, "centerY": 0, "rotation": 0, "feather": 0,
                "invert": False, "roundCorner": 0, "expansion": 0, "aspectRatio": 1}
    for key, number in computed["config"].items():
        if key in mask["config"]:
            mask["config"][key] = number
        elif key not in defaults or number != defaults[key]:
            raise ValueError(f"native_reference does not establish requested config field: {key}")
    mask["id"] = computed["id"]
    if mask["id"] in index:
        raise ValueError("generated mask ID conflicts with an existing material")
    normalized = {}
    if "enable_video_mask" in segment:
        normalized["enable_video_mask"] = {"before": segment["enable_video_mask"], "after": "absent"}
    if segment.get("enable_adjust_mask") is not False:
        normalized["enable_adjust_mask"] = {"before": segment.get("enable_adjust_mask", "absent"), "after": False}
    materials["common_mask"] = masks + [mask]
    segment["extra_material_refs"] = refs + native.extra_material_refs
    # Existing native base segments carry vendor defaults. With no active mask,
    # normalize only these switches to the verified 8.8 reference after all checks.
    segment.pop("enable_video_mask", None)
    segment["enable_adjust_mask"] = False
    return {"track_name": spec["track_name"], "segment_id": spec["segment_id"], "shape": shape,
            "mask_id": mask["id"], "native_visual_qa": "pending",
            "native_reference_sha256": reference_digest,
            "normalized_mask_switches": normalized,
            "native_model": "pyJianYingDraft.VideoSegment.add_mask/Mask.export_json",
            "media_timing_audio_and_keyframes_unchanged": True}
