"""Frozen local scene effects through the vendored native SDK.

Only a static whole-segment attachment or one global effect interval is
supported. Resource checks and the Writer's readback use the same contract.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .action_audio import _native


PREFIX = "JY_VFX_"
RESOURCE_SCHEMA = "jianying-adapter.video-effect-resource.v1"


def _json(path: Path) -> dict:
    value = json.loads(path.read_text("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("video effect JSON must be an object")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError("video effect requires a SHA256 digest")
    return value.lower()


def _time(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("video effect time must be nonnegative integer microseconds")
    return value


def _relative(value: Any) -> PurePosixPath:
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or value.startswith("/") or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ValueError("effect dependency must be a safe relative POSIX path")
    return PurePosixPath(value)


def _inside(root: Path, relative: str) -> Path:
    path = root.joinpath(*_relative(relative).parts)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("effect dependency escapes resource directory") from exc
    return path


def _resource(spec: Mapping[str, Any], meta: Any) -> dict:
    manifest_path = Path(str(spec.get("resource_manifest_path") or ""))
    if not manifest_path.is_absolute() or not manifest_path.is_file():
        raise ValueError("effect resource manifest must be an existing absolute file")
    if _sha(manifest_path) != _hash(spec.get("resource_manifest_sha256")):
        raise ValueError("effect resource manifest hash mismatch")
    manifest = _json(manifest_path)
    required = {"schema", "effect_name", "effect_id", "resource_id", "resource_path", "files"}
    if set(manifest) - required - {"engine_min_version"} or not required <= set(manifest):
        raise ValueError("effect resource manifest fields differ from the supported schema")
    if manifest["schema"] != RESOURCE_SCHEMA:
        raise ValueError("unsupported effect resource manifest schema")
    for key in ("effect_name", "effect_id", "resource_id"):
        if manifest[key] != spec[key]:
            raise ValueError("effect resource identity mismatch: " + key)
    root = Path(str(manifest["resource_path"]))
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("effect resource_path must be an existing absolute directory")
    files = manifest["files"]
    if not isinstance(files, dict) or not {"config.json", "extra.json"} <= set(files):
        raise ValueError("effect files must include config.json and extra.json")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("effect resource cannot contain filesystem links")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files):
        raise ValueError("effect resource file inventory mismatch")
    for relative, digest in files.items():
        path = _inside(root, relative)
        if not path.is_file() or _sha(path) != _hash(digest):
            raise ValueError("effect resource file missing or hash mismatch: " + relative)
    extra = _json(root / "extra.json")
    observed = (extra.get("setting") or {}).get("effect_adjust_params")
    expected = [{"effect_key": p.name, "default": p.default_value,
                 "min": p.min_value, "max": p.max_value} for p in meta.params]
    if observed != expected:
        raise ValueError("effect local parameter metadata differs from SDK")
    config = _json(root / "config.json")
    links = (config.get("effect") or {}).get("Link")
    if not isinstance(links, list) or not links:
        raise ValueError("effect config requires local feature links")
    dependencies, engines = [], []
    for link in links:
        if not isinstance(link, dict) or link.get("type") != "AmazingFeature":
            raise ValueError("unsupported effect resource feature type")
        feature = _inside(root, str(link.get("path") or "").rstrip("/"))
        for filename in ("main.scene", "sticker.config"):
            if not (feature / filename).is_file():
                raise ValueError("effect feature dependency missing: " + filename)
        engine = _json(feature / "sticker.config").get("min_version")
        if engine is not None:
            engines.append(engine)
        # Native serialized assets store relative references as length-prefixed
        # UTF-8 strings. Check those references without interpreting shaders.
        extensions = rb"(?:lua|scene|prefab|material|xshader|vert|frag|mesh|rt|png|jpg|jpeg|webp|texture)"
        pattern = rb"[A-Za-z0-9_./-]+\." + extensions
        for asset in feature.rglob("*"):
            if not asset.is_file():
                continue
            data = asset.read_bytes()
            if data.startswith(b"%SerializedFormat%@"):
                for match in re.finditer(pattern, data):
                    start, end = match.span()
                    if start < 4 or int.from_bytes(data[start - 4:start], "little") != end - start:
                        continue
                    reference = match.group().decode("utf-8")
                    dependency = _inside(feature, reference)
                    if not dependency.is_file():
                        raise ValueError("effect serialized dependency missing: " + reference)
                    dependencies.append(dependency.relative_to(root).as_posix())
            elif asset.name.endswith("-meta.json"):
                # FileAbsPath is editor provenance, not a runtime dependency.
                def visit(value):
                    if isinstance(value, dict):
                        if value.get("FilePath"):
                            dependency = _inside(feature, value["FilePath"])
                            if not dependency.is_file():
                                raise ValueError("effect metadata dependency missing: " + value["FilePath"])
                            dependencies.append(dependency.relative_to(root).as_posix())
                        for child in value.values():
                            visit(child)
                    elif isinstance(value, list):
                        for child in value:
                            visit(child)
                visit(json.loads(data.decode("utf-8-sig")))
    if "engine_min_version" in manifest and engines != [manifest["engine_min_version"]]:
        raise ValueError("effect engine_min_version does not match local sticker.config")
    return {"resource_path": str(root.resolve()), "file_count": len(files),
            "checked_dependencies": sorted(set(dependencies)), "engine_min_versions": engines,
            "engine_version_is_not_app_version": True}


def _checked(spec: Mapping[str, Any]) -> tuple[Any, list, dict]:
    common = {"id", "kind", "scope", "effect_name", "effect_id", "resource_id", "parameters",
              "resource_manifest_path", "resource_manifest_sha256", "authorization_source", "visual_event_id"}
    scope = spec.get("scope")
    fields = common | ({"start_us", "end_us"} if scope == "global" else {"track_name", "segment_id"})
    if (scope not in {"global", "segment"} or not fields <= set(spec)
            or set(spec) - fields - {"current_video_evidence"} or spec.get("kind") != "add_video_effect"):
        raise ValueError("unsupported add_video_effect scope or fields")
    for key in common - {"parameters", "kind"}:
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise ValueError("video effect requires nonempty " + key)
    native = _native()
    try:
        effect = native.VideoSceneEffectType[spec["effect_name"]]
    except KeyError as exc:
        raise ValueError("effect_name must exactly name a VideoSceneEffectType member") from exc
    meta = effect.value
    if meta.is_vip or any(getattr(p, "is_extra", False) for p in meta.params):
        raise ValueError("VIP and extra effect parameters are unsupported")
    if spec["effect_id"] != meta.effect_id or spec["resource_id"] != meta.resource_id:
        raise ValueError("effect IDs must match the exact SDK enum")
    values = spec["parameters"]
    names = [p.name for p in meta.params]
    if not isinstance(values, dict) or set(values) - set(names):
        raise ValueError("unknown or extra video effect parameter")
    for value in values.values():
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError("effect parameter must be finite and between 0 and 100")
    if scope == "global":
        if _time(spec["end_us"]) <= _time(spec["start_us"]):
            raise ValueError("global effect requires a positive interval")
    elif any(not isinstance(spec[k], str) or not spec[k].strip() for k in ("track_name", "segment_id")):
        raise ValueError("segment effect requires explicit track_name and segment_id")
    return effect, [values.get(name) for name in names], _resource(spec, meta)


def _authorize(plan: Mapping[str, Any], spec: Mapping[str, Any], plan_path: Path | None = None) -> dict:
    source = spec.get("authorization_source")
    option = (plan.get("options") or {}).get("video_effects") or {}
    if not source or option.get("enabled") is not True or option.get("authorization_source") != source:
        raise ValueError("video_effects option authorization mismatch")
    state_path = Path(str(plan.get("project_state") or ""))
    if not state_path.is_absolute() and plan_path is not None:
        state_path = plan_path.parent / state_path
    state = _json(state_path)
    if not any(a.get("option") == "video_effects" and a.get("enabled") is True
               and a.get("source") == source and a.get("status") not in {"denied", "revoked"}
               for a in state.get("current_authorizations", [])):
        raise ValueError("missing current video_effects authorization")
    return state


def _event(plan: Mapping[str, Any], spec: Mapping[str, Any], interval: tuple[int, int] | None = None) -> None:
    matches = [v for v in plan.get("visual_events", []) if v.get("id") == spec["visual_event_id"]]
    if len(matches) != 1:
        raise ValueError("video effect requires one visual_event")
    matches = [v for v in plan.get("visual_events", []) for t in v.get("techniques", [])
               if t.get("kind") == "video_effect" for operation_id in t.get("operation_ids", [])
               if operation_id == spec["id"] and v.get("id") == spec["visual_event_id"]]
    if len(matches) != 1:
        raise ValueError("video effect must bind its exact visual_event technique once")
    event = matches[0]
    if interval is not None and not (_time(event["start_us"]) <= interval[0] < interval[1] <= _time(event["end_us"])):
        raise ValueError("video effect interval outside visual_event")


def _target(draft: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[dict | None, dict | None, tuple[int, int]]:
    tracks = draft.get("tracks")
    if not isinstance(tracks, list) or not isinstance(draft.get("materials"), dict):
        raise ValueError("effect requires native tracks and materials")
    if spec["scope"] == "global":
        interval = (_time(spec["start_us"]), _time(spec["end_us"]))
        duration = _time(draft.get("duration"))
        if interval[1] > duration:
            raise ValueError("global effect extends beyond draft duration")
        return None, None, interval
    selected = [t for t in tracks if t.get("name") == spec["track_name"]]
    if len(selected) != 1 or selected[0].get("type") != "video":
        raise ValueError("effect requires a unique video target track")
    track = selected[0]
    selected = [s for s in track.get("segments", []) if s.get("id") == spec["segment_id"]]
    if len(selected) != 1:
        raise ValueError("effect requires a unique explicit target segment")
    segment = selected[0]
    timerange = segment.get("target_timerange") or {}
    start, duration = _time(timerange.get("start")), _time(timerange.get("duration"))
    if not duration or start + duration > _time(draft.get("duration")):
        raise ValueError("effect target segment has invalid timing")
    if not isinstance(segment.get("extra_material_refs", []), list):
        raise ValueError("effect target material references must be a list")
    return track, segment, (start, start + duration)


def _material_id(spec: Mapping[str, Any]) -> str:
    # Like split_fade_ IDs, this reserved prefix makes otherwise invisible
    # segment attachments discoverable even after their declaration is removed.
    return PREFIX + str(spec["id"])


def _planned_segment_interval(plan: Mapping[str, Any], spec: Mapping[str, Any], plan_path: Path | None) -> tuple[int, int]:
    """Recover the exact target from the base, split or same-plan B-roll."""
    base_path = Path(str(plan.get("base_draft") or ""))
    if not base_path.is_absolute() and plan_path is not None:
        base_path = plan_path.parent / base_path
    base = _json(base_path)
    splits = [o for o in plan.get("operations", []) if o.get("kind") == "split_video_track"
              and o.get("track_name") == spec["track_name"] and spec["segment_id"] in o.get("segment_ids", [])]
    additions = [o for o in plan.get("operations", []) if o.get("kind") == "add_broll"
                 and o.get("segment_id") == spec["segment_id"]]
    if additions:
        if (len(additions) != 1 or splits
                or additions[0].get("track_name") != spec["track_name"]
                or any(s.get("id") == spec["segment_id"] for t in base.get("tracks", [])
                       for s in t.get("segments", []))):
            raise ValueError("effect target has ambiguous B-roll declaration")
        addition = additions[0]
        start, end = _time(addition.get("start_us")), _time(addition.get("end_us"))
        if end <= start or end > _time(base.get("duration")):
            raise ValueError("effect B-roll target has invalid declared timing")
        return start, end
    if len(splits) > 1:
        raise ValueError("effect target has ambiguous split declarations")
    if not splits:
        return _target(base, spec)[2]
    split = splits[0]
    selected = {**spec, "segment_id": split["segment_id"]}
    interval = _target(base, selected)[2]
    cuts = split.get("cut_points_us")
    ids = split.get("segment_ids")
    if (not isinstance(cuts, list) or not isinstance(ids, list) or len(ids) != len(cuts) + 1
            or len(set(ids)) != len(ids)):
        raise ValueError("effect target split declaration is invalid")
    boundaries = [interval[0], *[_time(t) for t in cuts], interval[1]]
    if any(a >= b for a, b in zip(boundaries, boundaries[1:])):
        raise ValueError("effect target split cuts are outside the original interval")
    index = ids.index(spec["segment_id"])
    return boundaries[index], boundaries[index + 1]


def _native_output(spec: Mapping[str, Any], effect: Any, params: list, resource: dict, interval: tuple[int, int]):
    native = _native()
    if spec["scope"] == "global":
        effect_segment = native.EffectSegment(effect, native.trange(interval[0], interval[1] - interval[0]), params)
        material = effect_segment.effect_inst.export_json()
        segment = effect_segment.export_json()
        segment.update(material_id=_material_id(spec), source_timerange=None)
    else:
        from pyJianYingDraft.video_segment import VideoEffect
        material = VideoEffect(effect, params, apply_target_type=0).export_json()
        segment = None
    material.update(id=_material_id(spec), path=resource["resource_path"])
    return material, segment


def add_video_effect(draft: dict, spec: Mapping[str, Any], context: Any):
    """Prepare every check before touching the original draft."""
    effect, params, resource = _checked(spec)
    _authorize(context.plan, spec, getattr(context, "plan_path", None))
    _, _, interval = _target(draft, spec)
    if spec["scope"] == "segment" and interval != _planned_segment_interval(context.plan, spec, getattr(context, "plan_path", None)):
        raise ValueError("effect target timing differs from base/split/B-roll declaration")
    _event(context.plan, spec, interval)
    identifier = _material_id(spec)
    if any(m.get("id") == identifier for bucket in draft["materials"].values()
           if isinstance(bucket, list) for m in bucket if isinstance(m, dict)):
        raise ValueError("duplicate video effect material")
    if any(t.get("name") == PREFIX + spec["id"] for t in draft["tracks"]):
        raise ValueError("duplicate video effect track")
    material, segment = _native_output(spec, effect, params, resource, interval)
    result = copy.deepcopy(draft)
    result["materials"].setdefault("video_effects", []).append(material)
    if spec["scope"] == "global":
        result["tracks"].append({"id": uuid.uuid4().hex, "name": PREFIX + spec["id"], "type": "effect",
                                 "attribute": 0, "flag": 0, "segments": [segment]})
    else:
        _, target, _ = _target(result, spec)
        target.setdefault("extra_material_refs", []).append(identifier)
    return result, {"effect_id": spec["effect_id"], "resource_id": spec["resource_id"],
                    "scope": spec["scope"], "interval_us": list(interval), "resource": resource,
                    "native_visual_qa": "pending", "source_timing_audio_mask_keyframes_unchanged": True}


def _preset_effect_rows(draft: Mapping[str, Any]) -> tuple[dict, set[str]]:
    """Compare inherited effects by native facts and owners, never a name alone."""
    from .preset_audio import _without_local_ids

    materials = {}
    effects = {}
    for bucket, entries in draft.get("materials", {}).items():
        if not isinstance(entries, list):
            continue
        for material in entries:
            if not isinstance(material, dict) or not material.get("id"):
                continue
            identifier = material["id"]
            if identifier in materials:
                raise ValueError("duplicate inherited effect/dependency material ID")
            materials[identifier] = material
            if bucket == "video_effects" and not identifier.startswith(PREFIX):
                effects[identifier] = material

    def material_facts(material):
        facts = _without_local_ids(material)
        for key in ("render_index", "track_render_index"):
            facts.pop(key, None)
        for key in ("path", "algorithm_artifact_path", "lumi_hub_path"):
            if material.get(key):
                path = Path(material[key])
                if not path.is_absolute() or not path.exists():
                    raise ValueError("original preset effect resource missing: " + key)
                facts[key] = str(path.resolve())
        return facts

    rows, used, track_names = {}, set(), set()
    for track in draft.get("tracks", []):
        name = str(track.get("name", ""))
        for index, segment in enumerate(track.get("segments", [])):
            references = [segment.get("material_id"), *(segment.get("extra_material_refs") or [])]
            for position, identifier in enumerate(references):
                if identifier not in effects:
                    continue
                if not name.startswith("JY_PRESET_") or "__NODE__" not in name:
                    raise ValueError("unmarked video effect is not owned by a selected preset")
                key = (name, index, position)
                if key in rows:
                    raise ValueError("duplicate original preset effect owner")
                used.add(identifier)
                if track.get("type") == "effect":
                    owner = _without_local_ids(segment)
                    for field in ("render_index", "track_render_index", "extra_material_refs"):
                        owner.pop(field, None)
                    dependencies = segment.get("extra_material_refs") or []
                    if any(ref not in materials for ref in dependencies):
                        raise ValueError("original preset effect has a dangling dependency")
                    owner["dependencies"] = [material_facts(materials[ref]) for ref in dependencies]
                else:
                    # Placement/style operations can change a text/video owner.
                    # Preserve its attachment and authored interval; the existing
                    # layout/motion checks cover the separately declared changes.
                    owner = {field: segment.get(field) for field in (
                        "target_timerange", "visible", "state", "track_attribute")}
                rows[key] = {"track": {field: track.get(field) for field in (
                    "type", "visible", "attribute", "flag")},
                    "owner": owner, "material": material_facts(effects[identifier])}
        if any(key[0] == name for key in rows):
            if name in track_names:
                raise ValueError("duplicate original preset effect track")
            track_names.add(name)
    if used != set(effects):
        raise ValueError("unbound or undeclared original preset effect material")
    return rows, used


def _validate_preset_effects(plan, draft, *, plan_path=None):
    from .preset_audio import rebuild_preset_groups

    expected = rebuild_preset_groups(plan, draft, plan_path=plan_path) or {"materials": {}, "tracks": []}
    wanted, expected_ids = _preset_effect_rows(expected)
    actual, actual_ids = _preset_effect_rows(draft)
    if wanted != actual or list(wanted) != list(actual) or len(expected_ids) != len(actual_ids):
        raise ValueError("original preset video effects differ from selected native sources")
    return {"material_count": len(actual_ids), "attachment_count": len(actual),
            "native_visual_verified": False}


def validate_video_effects(plan: Mapping[str, Any], draft: Mapping[str, Any], *, plan_path: Path | None = None) -> dict:
    """Validate actual native material, references and visibility after Writer."""
    specs = [s for s in plan.get("operations", []) if s.get("kind") == "add_video_effect"]
    tracks = draft.get("tracks", [])
    marked_tracks = [t for t in tracks if str(t.get("name", "")).startswith(PREFIX)]
    materials = draft.get("materials") or {}
    marked_materials = [(bucket, m) for bucket, entries in materials.items() if isinstance(entries, list)
                        for m in entries if isinstance(m, dict) and str(m.get("id", "")).startswith(PREFIX)]
    marked_refs = [r for t in tracks for s in t.get("segments", [])
                   for r in [s.get("material_id"), *(s.get("extra_material_refs") or [])]
                   if isinstance(r, str) and r.startswith(PREFIX)]
    if not specs:
        errors = ["undeclared JY_VFX effect track or attachment"] if marked_tracks or marked_materials or marked_refs else []
        return {"ok": not errors, "errors": errors, "declared": False}
    errors, reports = [], []
    expected_ids = [_material_id(s) for s in specs]
    if len(set(expected_ids)) != len(expected_ids):
        errors.append("duplicate video effect operation IDs")
    actual_materials = materials.get("video_effects") or []
    actual_marked_ids = [m.get("id", "") for m in actual_materials if str(m.get("id", "")).startswith(PREFIX)]
    if sorted(actual_marked_ids) != sorted(expected_ids):
        errors.append("video effect material set missing, duplicate or undeclared")
    inherited_report = {"material_count": 0, "attachment_count": 0, "native_visual_verified": False}
    if (len(actual_materials) != len(actual_marked_ids)
            or any(op.get("kind") in {"add_preset_group", "replace_preset_group"} for op in plan.get("operations", []))):
        try:
            inherited_report = _validate_preset_effects(plan, draft, plan_path=plan_path)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append("original preset video effect verification failed: " + str(exc))
    if any(bucket != "video_effects" for bucket, _ in marked_materials):
        errors.append("JY_VFX material in incorrect native bucket")
    expected_tracks = [PREFIX + s["id"] for s in specs if s.get("scope") == "global"]
    if sorted(t.get("name", "") for t in marked_tracks) != sorted(expected_tracks):
        errors.append("JY_VFX track set missing, duplicate or undeclared")
    for spec in specs:
        try:
            effect, params, resource = _checked(spec)
            _authorize(plan, spec, plan_path)
            track, target, interval = _target(draft, spec)
            if spec["scope"] == "segment" and interval != _planned_segment_interval(plan, spec, plan_path):
                raise ValueError("effect target timing differs from base/split/B-roll declaration")
            _event(plan, spec, interval)
            identifier = _material_id(spec)
            found = [m for m in actual_materials if m.get("id") == identifier]
            if len(found) != 1:
                raise ValueError("video effect requires exactly one native material")
            expected, expected_segment = _native_output(spec, effect, params, resource, interval)
            material = found[0]
            if any(material.get(k) != value for k, value in expected.items()):
                raise ValueError("native video effect identity, scope, parameters or resource differs")
            if material.get("visible", True) is not True or material.get("enable", True) is not True or material.get("disabled", False):
                raise ValueError("native video effect material is disabled")
            if any(material.get(k) for k in ("effect_mask", "enable_mask", "bind_segment_id",
                                             "algorithm_artifact_path", "transparent_params")):
                raise ValueError("native video effect has an undeclared mask, binding or alternate render path")
            references = [(t, s, position) for t in tracks for s in t.get("segments", [])
                          for position, ref in enumerate([s.get("material_id"), *(s.get("extra_material_refs") or [])])
                          if ref == identifier]
            if len(references) != 1:
                raise ValueError("video effect reference missing, duplicated or applied to another segment")
            owner_track, owner, position = references[0]
            video_owner = owner_track.get("type") == "video"
            # Video track attribute bit 1 is mute; flag bit 2 marks auxiliary
            # video tracks after native ordering. Both are expected on B-roll.
            if (owner_track.get("visible") is False
                    or owner_track.get("attribute", 0) not in ((0, 1) if video_owner else (0,))
                    or owner_track.get("flag", 0) not in ((0, 2) if video_owner else (0,))
                    or owner.get("visible") is not True or owner.get("track_attribute", 0) != 0
                    or owner.get("state", 0) != 0):
                raise ValueError("video effect owner is not visible")
            if spec["scope"] == "global":
                if owner_track.get("name") != PREFIX + spec["id"] or owner_track.get("type") != "effect" or position != 0:
                    raise ValueError("global effect must own its declared independent effect track")
                if len(owner_track.get("segments", [])) != 1:
                    raise ValueError("global effect must contain exactly one effect segment")
                # SDK IDs and render indices are assigned by the surrounding
                # native track; every time/visibility/motion field is preserved.
                ignored = {"id", "track_render_index"}
                if any(owner.get(k) != value for k, value in expected_segment.items() if k not in ignored):
                    raise ValueError("global effect segment timing, visibility or motion differs")
            elif owner_track is not track or owner is not target or position == 0:
                raise ValueError("segment effect is attached to the wrong video target")
            reports.append({"id": spec["id"], "scope": spec["scope"], "interval_us": list(interval),
                            "resource": resource, "native_visual_qa": "pending"})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append("video effect " + str(spec.get("id")) + ": " + str(exc))
    return {"ok": not errors, "errors": errors, "declared": True, "effects": reports,
            "inherited_preset_effects": inherited_report, "native_visual_verified": False}


def validate_video_effect_plan(plan: Mapping[str, Any], *, plan_path: Path, require_native_evidence: bool) -> dict:
    """Preview may defer native evidence; seal and production must bind it."""
    errors = []
    specs = [s for s in plan.get("operations", []) if isinstance(s, Mapping) and s.get("kind") == "add_video_effect"]
    for spec in specs:
        try:
            _checked(spec)
            state = _authorize(plan, spec, plan_path)
            _event(plan, spec, (spec["start_us"], spec["end_us"]) if spec["scope"] == "global" else None)
            if require_native_evidence:
                from .candidate_plan import build_contract_sha256, resolve_path
                from .preset_preflight import _phase_status, _distinct_phase_content
                evidence = spec.get("current_video_evidence") or {}
                if (evidence.get("current_video_ready") is not True or evidence.get("native_visual_verified") is not True
                        or evidence.get("project_id") != state.get("project_id")
                        or evidence.get("operation_id") != spec["id"]
                        or evidence.get("build_contract_sha256", "").lower() != build_contract_sha256(plan).lower()):
                    raise ValueError("video effect seal requires native evidence bound to this project and operation")
                candidate = resolve_path(plan.get("preview_output"), plan_path)
                if not candidate.is_file() or _sha(candidate) != _hash(evidence.get("candidate_sha256")):
                    raise ValueError("video effect native evidence must bind the actual preview candidate")
                for phase in ("entry", "stable", "exit"):
                    _phase_status(evidence, phase, verify_snapshot_files=True, errors=errors)
                _distinct_phase_content(evidence, verify_snapshot_files=True, errors=errors)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append("video effect " + str(spec.get("id")) + ": " + str(exc))
    return {"ok": not errors, "errors": errors, "declared": bool(specs),
            "native_visual_evidence_required": require_native_evidence}
