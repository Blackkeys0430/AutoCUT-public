"""One locally frozen native text entrance on an existing candidate segment."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .animation_recipes import AnimationReference, _ensure_vendored_import_paths
from .animation_writer import apply_animation_reference


PREFIX = "JY_TEXT_ANIM_"
RESOURCE_SCHEMA = "jianying-adapter.text-animation-resource.v1"


def _json(path: Path) -> dict:
    value = json.loads(path.read_text("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("text animation JSON must be an object")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("text animation requires a SHA256 digest")
    return value.lower()


def _time(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("text animation time must be nonnegative integer microseconds")
    return value


def _path(value: Any, plan_path: Path | str | None = None) -> Path:
    path = Path(str(value or ""))
    return Path(plan_path).parent / path if not path.is_absolute() and plan_path is not None else path


def _resource(spec: Mapping[str, Any]) -> dict:
    manifest_path = _path(spec.get("resource_manifest_path"))
    if not manifest_path.is_absolute() or not manifest_path.is_file():
        raise ValueError("text animation manifest must be an existing absolute file")
    if _digest(manifest_path) != _hash(spec.get("resource_manifest_sha256")):
        raise ValueError("text animation resource manifest hash mismatch")
    manifest = _json(manifest_path)
    fields = {"schema", "enum", "member", "effect_id", "resource_id", "resource_path", "files"}
    if not fields <= set(manifest) or set(manifest) - fields - {"provenance"} or manifest["schema"] != RESOURCE_SCHEMA:
        raise ValueError("unsupported text animation resource manifest schema or fields")
    for key in ("enum", "member", "effect_id", "resource_id"):
        if manifest[key] != spec[key]:
            raise ValueError("text animation resource identity differs: " + key)
    root = _path(manifest["resource_path"])
    files = manifest["files"]
    if not root.is_absolute() or not root.is_dir() or not isinstance(files, dict) or not files:
        raise ValueError("text animation requires a frozen directory and complete file inventory")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("text animation resource cannot contain filesystem links")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files):
        raise ValueError("text animation resource file inventory mismatch")
    for relative, digest in files.items():
        if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
                or relative.startswith("/") or any(p in {"", ".", ".."} for p in relative.split("/"))):
            raise ValueError("text animation resource needs safe relative POSIX paths")
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("text animation resource escapes its frozen directory") from exc
        if not path.is_file() or _digest(path) != _hash(digest):
            raise ValueError("text animation resource missing or hash mismatch: " + relative)
    return {"resource_path": str(root.resolve()), "file_count": len(files), "resources_verified": True}


def _checked(spec: Mapping[str, Any]) -> tuple[AnimationReference, dict]:
    fields = {"id", "kind", "track_name", "segment_id", "enum", "member", "effect_id", "resource_id",
              "duration_us", "resource_manifest_path", "resource_manifest_sha256", "authorization_source", "visual_event_id"}
    if not fields <= set(spec) or set(spec) - fields - {"current_video_evidence"} or spec.get("kind") != "apply_text_animation":
        raise ValueError("apply_text_animation requires the explicit supported fields")
    for key in fields - {"duration_us"}:
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise ValueError("text animation requires nonempty " + key)
    if spec["enum"] != "TextIntro":
        raise ValueError("only native TextIntro entrances are supported")
    duration = _time(spec["duration_us"])
    if duration == 0:
        raise ValueError("text entrance duration must be positive")
    reference = AnimationReference("TextIntro", spec["member"], "in", duration)
    _ensure_vendored_import_paths()
    from pyJianYingDraft.metadata.text_intro import TextIntro
    if spec["member"] not in TextIntro.__members__:
        raise ValueError("member must exactly name a TextIntro enum member")
    meta = reference.metadata
    if meta.is_vip:
        raise ValueError("VIP text animations are not authorized")
    if spec["effect_id"] != meta.effect_id or spec["resource_id"] != meta.resource_id:
        raise ValueError("text animation IDs differ from the exact SDK enum")
    return reference, _resource(spec)


def _authorize(plan: Mapping[str, Any], spec: Mapping[str, Any], plan_path: Path | str | None = None) -> dict:
    source = spec.get("authorization_source")
    option = (plan.get("options") or {}).get("text_animation") or {}
    if not source or option.get("enabled") is not True or option.get("authorization_source") != source:
        raise ValueError("text_animation option authorization mismatch")
    state = _json(_path(plan.get("project_state"), plan_path))
    if not any(a.get("option") == "text_animation" and a.get("enabled") is True and a.get("source") == source
               and a.get("status") not in {"denied", "revoked"} for a in state.get("current_authorizations", [])):
        raise ValueError("missing current text_animation authorization")
    return state


def _target(draft: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[dict, dict, dict, int, int]:
    matches = [t for t in draft.get("tracks", []) if t.get("name") == spec["track_name"]]
    if len(matches) != 1 or matches[0].get("type") != "text":
        raise ValueError("text animation requires exactly one named text track")
    track = matches[0]
    matches = [s for s in track.get("segments", []) if s.get("id") == spec["segment_id"]]
    if len(matches) != 1:
        raise ValueError("text animation target segment must exist exactly once")
    segment = matches[0]
    tr = segment.get("target_timerange") or {}
    start, duration = _time(tr.get("start")), _time(tr.get("duration"))
    if not duration or spec["duration_us"] > duration:
        raise ValueError("text entrance exceeds the selected segment duration")
    materials = (draft.get("materials") or {}).get("texts", [])
    matches = [m for m in materials if m.get("id") == segment.get("material_id")]
    if len(matches) != 1:
        raise ValueError("text animation requires exactly one actual text material")
    content = json.loads(matches[0].get("content", ""))
    if not isinstance(content, dict) or not isinstance(content.get("text"), str) or not content["text"]:
        raise ValueError("text animation target has no readable native text")
    if not isinstance(segment.get("extra_material_refs", []), list):
        raise ValueError("text animation material references must be a list")
    return track, segment, content, start, duration


def _event(plan: Mapping[str, Any], spec: Mapping[str, Any], interval: tuple[int, int] | None = None) -> None:
    events = [v for v in plan.get("visual_events", []) if v.get("id") == spec["visual_event_id"]]
    if len(events) != 1:
        raise ValueError("text animation requires one declared visual_event")
    event = events[0]
    bindings = [op_id for t in event.get("techniques", []) if t.get("kind") == "text_animation"
                for op_id in t.get("operation_ids", []) if op_id == spec["id"]]
    if len(bindings) != 1:
        raise ValueError("text animation must bind its own visual_event technique exactly once")
    if interval is not None and not (_time(event["start_us"]) <= interval[0] < interval[1] <= _time(event["end_us"])):
        raise ValueError("text entrance interval lies outside its visual event")


def _native_material(spec: Mapping[str, Any], reference: AnimationReference, resource: dict, text: str, duration: int) -> dict:
    from pyJianYingDraft import TextSegment, trange
    # Only take the animation container from this SDK object. The actual text,
    # fonts, native styles and placement remain owned by the existing segment.
    temporary = TextSegment(text, trange(0, duration))
    apply_animation_reference(temporary, reference, duration_us=spec["duration_us"])
    material = temporary.animations_instance.export_json()
    material["id"] = PREFIX + spec["id"]
    material["animations"][0]["path"] = resource["resource_path"]
    return material


def _containers(draft: Mapping[str, Any], segment: Mapping[str, Any]) -> list[dict]:
    refs = segment.get("extra_material_refs", [])
    return [m for m in (draft.get("materials") or {}).get("material_animations", []) if m.get("id") in refs]


def apply_text_animation(draft: dict, spec: Mapping[str, Any], context: Any):
    reference, resource = _checked(spec)
    _authorize(context.plan, spec, getattr(context, "plan_path", None))
    _, segment, content, start, duration = _target(draft, spec)
    _event(context.plan, spec, (start, start + spec["duration_us"]))
    containers = _containers(draft, segment)
    if any(m.get("animations") for m in containers):
        raise ValueError("text segment already has a native animation; replacement is not implicit")
    identifier = PREFIX + spec["id"]
    if any(m.get("id") == identifier for entries in draft["materials"].values() if isinstance(entries, list)
           for m in entries if isinstance(m, dict)):
        raise ValueError("duplicate text animation material")
    material = _native_material(spec, reference, resource, content["text"], duration)
    result = copy.deepcopy(draft)
    _, target, _, _, _ = _target(result, spec)
    empty_ids = {m["id"] for m in containers}
    target["extra_material_refs"] = [r for r in target.get("extra_material_refs", []) if r not in empty_ids] + [identifier]
    result["materials"].setdefault("material_animations", []).append(material)
    return result, {"track_name": spec["track_name"], "segment_id": spec["segment_id"], "resource": resource,
                    "intro_start_us": start, "intro_end_us": start + spec["duration_us"],
                    "text_style_layout_timing_unchanged": True, "native_visual_qa": "pending"}


def _validate_one(plan: Mapping[str, Any], draft: Mapping[str, Any], spec: Mapping[str, Any], plan_path=None) -> dict:
    reference, resource = _checked(spec)
    _authorize(plan, spec, plan_path)
    track, segment, content, start, duration = _target(draft, spec)
    expected = _native_material(spec, reference, resource, content["text"], duration)
    containers = _containers(draft, segment)
    if len(containers) != 1 or containers[0].get("id") != expected["id"]:
        raise ValueError("text animation requires its single declared native container")
    actual = containers[0]
    if any(actual.get(key) != value for key, value in expected.items()):
        raise ValueError("native text entrance identity, resource, duration or parameters differs")
    references = [s for t in draft.get("tracks", []) for s in t.get("segments", [])
                  for ref in s.get("extra_material_refs", []) if ref == expected["id"]]
    if len(references) != 1 or references[0] is not segment:
        raise ValueError("text animation container has missing or duplicate/wrong-target references")
    if (segment.get("visible") is not True or segment.get("state", 0) != 0
            or segment.get("track_attribute", 0) != 0 or track.get("attribute", 0) != 0 or track.get("flag", 0) != 0):
        raise ValueError("text animation target is not visible")
    animation = actual["animations"][0]
    intro_start = start + _time(animation["start"])
    intro_end = intro_start + _time(animation["duration"])
    _event(plan, spec, (intro_start, intro_end))
    return {"id": spec["id"], "track_name": spec["track_name"], "segment_id": spec["segment_id"],
            "intro_start_us": intro_start, "intro_end_us": intro_end, "segment_end_us": start + duration,
            "resource": resource, "native_visual_qa": "pending"}


def _preset_intro(plan: Mapping[str, Any], draft: Mapping[str, Any], spec: Mapping[str, Any]) -> dict:
    """Read only an explicitly declared single-track preset; preflight owns its resources."""
    template, node = spec.get("template_id"), spec.get("node_id")
    if not all(isinstance(v, str) and v.strip() for v in (template, node)):
        raise ValueError("preset entrance anchor requires explicit template_id and node_id")
    selections = [p for p in plan.get("presets", [])
                  if p.get("template_id") == template and p.get("node_id") == node]
    operations = [o for o in plan.get("operations", []) if o.get("kind") == "add_preset_group"
                  and o.get("template_id") == template and o.get("node_id") == node]
    if len(selections) != 1 or len(operations) != 1:
        raise ValueError("preset entrance anchor requires one declared preset and operation")
    declared = selections[0].get("actual_text_tracks") or []
    prefix = f"JY_PRESET_{template}__NODE__{node}_"
    if (len(declared) != 1 or declared[0].get("segment_count") != 1
            or not str(declared[0].get("track_name", "")).startswith(prefix)):
        raise ValueError("preset entrance anchor requires one explicitly named single-segment text track")
    group = [t for t in draft.get("tracks", []) if str(t.get("name", "")).startswith(prefix)]
    # The native 152/396 importer retains an empty auxiliary video track.
    # Empty non-text tracks have no motion target; populated auxiliaries do.
    tracks = [t for t in group if t.get("type") == "text"]
    if (any(t.get("segments") for t in group if t.get("type") != "text")
            or len(tracks) != 1 or tracks[0].get("name") != declared[0]["track_name"]
            or len(tracks[0].get("segments", [])) != 1):
        raise ValueError("preset entrance anchor cannot select among missing or multiple tracks/segments")
    track = tracks[0]
    segment = track["segments"][0]
    texts = [m for m in (draft.get("materials") or {}).get("texts", [])
             if m.get("id") == segment.get("material_id")]
    if len(texts) != 1 or not json.loads(texts[0].get("content", "{}")).get("text"):
        raise ValueError("preset entrance anchor requires actual native text material")
    if (segment.get("visible") is not True or segment.get("state", 0) != 0
            or segment.get("track_attribute", 0) != 0 or track.get("attribute", 0) != 0 or track.get("flag", 0) != 0):
        raise ValueError("preset entrance anchor target is not visible")
    containers = _containers(draft, segment)
    if (len(containers) != 1 or containers[0].get("type") != "sticker_animation"
            or len(containers[0].get("animations", [])) != 1
            or containers[0]["animations"][0].get("type") != "in"):
        raise ValueError("preset entrance anchor requires one container with exactly one in animation")
    refs = [s for t in draft.get("tracks", []) for s in t.get("segments", [])
            for ref in s.get("extra_material_refs", []) if ref == containers[0].get("id")]
    if len(refs) != 1 or refs[0] is not segment:
        raise ValueError("preset entrance anchor animation has duplicate/wrong-target references")
    timing = segment.get("target_timerange") or {}
    start, span = _time(timing.get("start")), _time(timing.get("duration"))
    animation = containers[0]["animations"][0]
    offset, duration = _time(animation.get("start")), _time(animation.get("duration"))
    if not duration or offset + duration > span:
        raise ValueError("preset entrance anchor duration exceeds its text segment")
    return {"intro_start_us": start + offset, "intro_end_us": start + offset + duration}


def resolve_text_intro_anchor(plan: Mapping[str, Any], draft: Mapping[str, Any], operation_id: str, anchor: str, *, plan_path=None) -> int:
    """Return a checked time from actual compiled material, never plan arithmetic."""
    if anchor not in {"text_intro_start", "text_intro_end"}:
        raise ValueError("unsupported text entrance action anchor")
    specs = [s for s in plan.get("operations", []) if s.get("id") == operation_id]
    if len(specs) != 1:
        raise ValueError("text entrance anchor must reference one operation")
    if specs[0].get("kind") == "apply_text_animation":
        actual = _validate_one(plan, draft, specs[0], plan_path)
    elif specs[0].get("kind") == "add_preset_group":
        actual = _preset_intro(plan, draft, specs[0])
    else:
        raise ValueError("text entrance anchor requires apply_text_animation or add_preset_group")
    return actual["intro_start_us" if anchor == "text_intro_start" else "intro_end_us"]


def validate_text_animations(plan: Mapping[str, Any], draft: Mapping[str, Any], *, plan_path=None) -> dict:
    specs = [s for s in plan.get("operations", []) if s.get("kind") == "apply_text_animation"]
    materials = draft.get("materials") or {}
    marked = [(bucket, m) for bucket, entries in materials.items() if isinstance(entries, list)
              for m in entries if isinstance(m, dict) and str(m.get("id", "")).startswith(PREFIX)]
    refs = [r for t in draft.get("tracks", []) for s in t.get("segments", []) for r in s.get("extra_material_refs", [])
            if isinstance(r, str) and r.startswith(PREFIX)]
    if not specs:
        errors = ["undeclared shared text animation"] if marked or refs else []
        return {"ok": not errors, "errors": errors, "declared": False}
    errors, reports = [], []
    expected = [PREFIX + s["id"] for s in specs]
    if len(set(expected)) != len(expected) or sorted(m["id"] for _, m in marked) != sorted(expected):
        errors.append("text animation container set missing, duplicate or undeclared")
    if any(bucket != "material_animations" for bucket, _ in marked):
        errors.append("shared text animation in wrong native material bucket")
    for spec in specs:
        try:
            reports.append(_validate_one(plan, draft, spec, plan_path))
        except (ValueError, KeyError, TypeError, OSError, LookupError) as exc:
            errors.append("text animation " + str(spec.get("id")) + ": " + str(exc))
    return {"ok": not errors, "errors": errors, "declared": True, "animations": reports,
            "native_visual_verified": False}


def validate_text_animation_plan(plan: Mapping[str, Any], *, plan_path: Path, require_native_evidence: bool) -> dict:
    errors = []
    specs = [s for s in plan.get("operations", []) if isinstance(s, Mapping) and s.get("kind") == "apply_text_animation"]
    for spec in specs:
        try:
            _checked(spec)
            state = _authorize(plan, spec, plan_path)
            _event(plan, spec)
            if require_native_evidence:
                from .candidate_plan import build_contract_sha256
                from .preset_preflight import _phase_status, _phase_timing_contract, _distinct_phase_content
                evidence = spec.get("current_video_evidence") or {}
                if (evidence.get("current_video_ready") is not True or evidence.get("native_visual_verified") is not True
                        or evidence.get("project_id") != state.get("project_id") or evidence.get("operation_id") != spec["id"]
                        or evidence.get("build_contract_sha256", "").lower() != build_contract_sha256(plan).lower()):
                    raise ValueError("text animation seal needs current project/operation native evidence")
                preview = _path(plan.get("preview_output"), plan_path)
                if not preview.is_file() or _digest(preview) != _hash(evidence.get("candidate_sha256")):
                    raise ValueError("text animation native evidence must bind the actual preview candidate")
                actual = _validate_one(plan, _json(preview), spec, plan_path)
                for phase in ("entry", "stable", "exit"):
                    _phase_status(evidence, phase, verify_snapshot_files=True, errors=errors)
                _phase_timing_contract({"start_us": actual["intro_start_us"], "end_us": actual["segment_end_us"]}, evidence, errors)
                _distinct_phase_content(evidence, verify_snapshot_files=True, errors=errors)
        except (ValueError, KeyError, TypeError, OSError, LookupError) as exc:
            errors.append("text animation " + str(spec.get("id")) + ": " + str(exc))
    return {"ok": not errors, "errors": errors, "declared": bool(specs),
            "native_visual_evidence_required": require_native_evidence}
