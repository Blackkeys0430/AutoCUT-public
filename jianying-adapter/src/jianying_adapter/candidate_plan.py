# Copyright (c) 2026 Blackkeys0430 — AutoCUT original project code.
# Origin: https://github.com/Blackkeys0430/AutoCUT-public
# SPDX-License-Identifier: LicenseRef-AutoCUT-Personal-Use-1.0
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

from .preset_preflight import declared_track_texts, preflight_selection
from .project_state import validate_project_state
from .visual_planning import validate_visual_events
from .jianying_launch import exact_process_identifier
from .semantic_evidence.content_gate import (
    validate_caption_coverage,
    validate_semantic_evidence,
    validate_retake_cuts,
)


Operation = Callable[
    [dict[str, Any], Mapping[str, Any], "CandidateBuildContext"],
    tuple[dict[str, Any], Mapping[str, Any]] | Mapping[str, Any] | None,
]
FinalValidator = Callable[[dict[str, Any], "CandidateBuildContext"], Mapping[str, Any]]
SHARED_OPERATION_REGISTRY = "jianying_adapter.shared-operations.v1"
_REGISTRY_TOKEN = object()


def get_shared_operation_registry() -> "OperationRegistry":
    """Return the only production operation registry.

    The import is deliberately lazy so the registry implementation can use
    the small data helpers in this module without creating an import cycle.
    Callers must use this factory; a per-video callback mapping is never a
    production execution path.
    """
    from .shared_operations import shared_operation_handlers

    return OperationRegistry(
        SHARED_OPERATION_REGISTRY,
        shared_operation_handlers(),
        _token=_REGISTRY_TOKEN,
    )


@dataclass(frozen=True)
class OperationRegistry:
    """Named shared handler set; plain per-video callback dictionaries are rejected."""

    name: str
    handlers: Mapping[str, Operation]
    _token: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.name != SHARED_OPERATION_REGISTRY:
            raise ValueError(f"unsupported operation registry: {self.name!r}")
        if self._token is not _REGISTRY_TOKEN:
            raise TypeError(
                "OperationRegistry 只能由 get_shared_operation_registry() 创建；"
                "禁止同名注册表注入单视频 callback"
            )
        from .shared_operations import shared_operation_handlers

        expected = shared_operation_handlers()
        if set(self.handlers) != set(expected) or any(
            self.handlers.get(name) is not handler
            for name, handler in expected.items()
        ):
            raise ValueError(
                "OperationRegistry 必须是共享操作的完整原生实现；"
                "禁止替换、删减或注入 callback"
            )


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根对象不是 object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


_LATE_EVIDENCE_KEYS = frozenset({
    "current_video_evidence",
    "current_video_ready",
    "frontend_proof",
})


def _build_contract_value(value: Any) -> Any:
    """Return the immutable build portion of a CandidatePlan.

    Runtime visual evidence is deliberately allowed to be added between the
    workspace preview and seal.  Every declarative input (media, operations,
    copy, timing, placement and scale) remains in this digest.
    """
    if isinstance(value, Mapping):
        return {
            str(key): _build_contract_value(item)
            for key, item in value.items()
            if str(key) not in _LATE_EVIDENCE_KEYS
        }
    if isinstance(value, list):
        return [_build_contract_value(item) for item in value]
    return value


def build_contract_sha256(plan: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _build_contract_value(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def resolve_path(value: Any, plan_path: Path) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else plan_path.parent / path


def _timerange(segment: Mapping[str, Any]) -> tuple[int, int] | None:
    value = segment.get("target_timerange")
    if not isinstance(value, Mapping):
        return None
    start = value.get("start")
    duration = value.get("duration")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration <= 0
    ):
        return None
    return start, start + duration


def _material_index(draft: Mapping[str, Any], group: str) -> dict[str, Mapping[str, Any]]:
    materials = (draft.get("materials") or {}).get(group) or []
    return {
        str(item.get("id") or item.get("material_id") or ""): item
        for item in materials
        if isinstance(item, Mapping) and (item.get("id") or item.get("material_id"))
    }


def _material_text(material: Mapping[str, Any]) -> str:
    content = material.get("content")
    if isinstance(content, str) and content.strip():
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping) and "text" in payload:
            return str(payload.get("text") or "")
    return str(material.get("text") or "")


def _close_us(actual: int, expected: int, tolerance_us: int) -> bool:
    return abs(actual - expected) <= tolerance_us


def _same_path(first: Any, second: Any) -> bool:
    if not first or not second:
        return False
    try:
        return str(Path(str(first)).resolve(strict=False)).casefold() == str(
            Path(str(second)).resolve(strict=False)
        ).casefold()
    except OSError:
        return False


def _actual_caption_items(draft: Mapping[str, Any], caption_track_name: str | None = None) -> list[dict[str, Any]]:
    """Extract the caption track from the actual assembled draft.

    The project uses ``JY_ZH_SUBTITLES`` for ordinary captions.  A plan may
    provide another explicit caption track name; arbitrary longest-track
    inference would confuse preset text with subtitles.
    """
    tracks = [
        item for item in draft.get("tracks") or []
        if isinstance(item, Mapping) and item.get("type") == "text"
    ]
    preferred = str(caption_track_name or "JY_ZH_SUBTITLES").strip()
    named = [item for item in tracks if str(item.get("name") or "") == preferred]
    if not named and not caption_track_name:
        named = [item for item in tracks if "caption" in str(item.get("name") or "").casefold() or "subtitle" in str(item.get("name") or "").casefold()]
    if not named:
        return []
    track = named[0]
    materials = {
        str(item.get("id")): item
        for item in (draft.get("materials") or {}).get("texts") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    rows: list[dict[str, Any]] = []
    for index, segment in enumerate(track.get("segments") or []):
        if not isinstance(segment, Mapping):
            continue
        timerange = segment.get("target_timerange") or {}
        start_us, duration_us = timerange.get("start"), timerange.get("duration")
        try:
            start, end = float(start_us) / 1_000_000.0, (float(start_us) + float(duration_us)) / 1_000_000.0
        except (TypeError, ValueError):
            continue
        material = materials.get(str(segment.get("material_id") or ""), {})
        content = material.get("content") if isinstance(material, Mapping) else None
        text = material.get("text", "") if isinstance(material, Mapping) else ""
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, Mapping):
                text = payload.get("text", text)
        rows.append({"id": str(segment.get("id") or index), "start": start, "end": end, "text": str(text or "")})
    return rows


def _strict_caption_coverage(
    speech: Any,
    captions: Any,
    replacement_windows: Any = None,
    *,
    tolerance_seconds: float = 0.04,
) -> dict[str, Any]:
    """Compatibility wrapper around the shared exact caption gate."""
    return validate_caption_coverage(
        speech,
        captions,
        replacement_windows,
        tolerance_seconds=tolerance_seconds,
    )


def _actual_content_coverage(draft: Mapping[str, Any], plan: Mapping[str, Any], content: Mapping[str, Any]) -> dict[str, Any]:
    from .caption_replacement import bind_replacement_windows

    binding = bind_replacement_windows(draft, plan, content)
    coverage = _strict_caption_coverage(
        content.get("final_retained_speech"),
        _actual_caption_items(draft, str(content.get("ordinary_caption_track_name") or plan.get("ordinary_caption_track_name") or "JY_ZH_SUBTITLES")),
        binding["windows"],
    )
    return {**coverage, "ok": binding["ok"] and coverage["ok"],
            "errors": binding["errors"] + coverage["errors"], "actual_preset_replacement_binding": binding}


def validate_actual_content(draft: Mapping[str, Any], plan: Mapping[str, Any], *, plan_path: Path | None = None) -> dict[str, Any]:
    """Use the same source-bound caption check at assembly and native readback."""
    content = plan.get('content_gate')
    if not isinstance(content, Mapping) and plan.get('semantic_gate'):
        try:
            path = resolve_path(plan['semantic_gate'], plan_path) if plan_path is not None else Path(plan['semantic_gate'])
            content = read_json(path).get('content_gate')
        except (OSError, ValueError, TypeError) as error:
            return {'ok': False, 'errors': ['content_gate: ' + str(error)]}
    if not isinstance(content, Mapping):
        return {'ok': False, 'errors': ['content_gate missing']}
    from .material_library import validate_plan_materials
    coverage = _actual_content_coverage(draft, plan, content)
    materials = validate_plan_materials(plan, plan_path or Path.cwd() / "candidate_plan.json", draft)
    cuts = validate_actual_retake_cuts(draft, plan, plan_path=plan_path)
    return {**coverage, "ok": coverage["ok"] and materials["ok"] and cuts["ok"],
            "errors": coverage["errors"] + materials["errors"] + cuts["errors"],
            "material_library": materials, "retake_cuts": cuts}


def validate_actual_retake_cuts(draft: Mapping[str, Any], plan: Mapping[str, Any], *, plan_path: Path | None = None) -> dict[str, Any]:
    """Read deletion decisions from the existing edit plan and check native ranges."""
    try:
        context = plan_path or Path.cwd() / "candidate_plan.json"
        edit_path = plan.get("edit_plan")
        if not edit_path and plan.get("project_state"):
            state = read_json(resolve_path(plan["project_state"], context))
            edit_path = state.get("artifacts", {}).get("edit_plan")
        if not edit_path:
            return {"ok": True, "errors": [], "count": 0}
        edit = read_json(resolve_path(edit_path, context))
        cuts = edit.get("retake_cuts", [])
        errors = validate_retake_cuts(cuts, edit.get("timeline_segments", []))
        media = {item["id"]: item.get("path", "") for kind in ("videos", "audios")
                 for item in draft.get("materials", {}).get(kind, [])}
        segments = []
        for track in draft.get("tracks", []):
            if track.get("type") not in {"video", "audio"}:
                continue
            for segment in track.get("segments", []):
                source = segment.get("source_timerange") or {}
                segments.append({"source_path": media.get(segment.get("material_id"), ""),
                                 "source_start_us": int(source.get("start", 0)),
                                 "source_end_us": int(source.get("start", 0)) + int(source.get("duration", 0))})
        errors.extend(validate_retake_cuts(cuts, segments))
        return {"ok": not errors, "errors": errors, "count": len(cuts)}
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {"ok": False, "errors": ["retake_cuts: " + str(error)]}


def _mapping_contains(actual: Any, expected: Mapping[str, Any]) -> bool:
    if not isinstance(actual, Mapping):
        return False
    return all(
        key in actual
        and (
            _mapping_contains(actual[key], value)
            if isinstance(value, Mapping)
            else actual[key] == value
        )
        for key, value in expected.items()
    )


def validate_frontend_proof(
    proof: Mapping[str, Any],
    *,
    expected_exe: Path,
    project_id: str,
    roughcut_sha256: str,
) -> dict[str, Any]:
    errors: list[str] = []
    if proof.get("schema") != "jianying-adapter.computer-use-proof.v1":
        errors.append("frontend_proof schema 不正确")
    if proof.get("stage") != "after_launch":
        errors.append("frontend_proof.stage 必须是 after_launch")
    if str(proof.get("project_id") or "") != project_id:
        errors.append("frontend_proof.project_id 未绑定当前视频")
    if str(proof.get("roughcut_sha256") or "").upper() != roughcut_sha256.upper():
        errors.append("frontend_proof.roughcut_sha256 未绑定当前粗剪")
    observations = proof.get("runtime_observations") or {}
    if not isinstance(observations, Mapping):
        observations = {}
    window = observations.get("cua_state_window") or {}
    if not isinstance(window, Mapping):
        window = {}
    actual_app = str(proof.get("window_app") or window.get("app") or "")
    expected_app = exact_process_identifier(expected_exe)
    if actual_app.casefold() != expected_app.casefold():
        errors.append(f"frontend_proof 窗口app不是精确8.8目标: {actual_app!r}")
    if proof.get("blocking_popup") or observations.get("blocking_popup"):
        errors.append("frontend_proof 仍存在阻塞弹窗")
    return {
        "ok": not errors,
        "errors": errors,
        "expected_window_app": expected_app,
        "actual_window_app": actual_app,
        "trust_boundary": "agent_tool_layer_attestation",
        "runtime_calls_machine_proven_by_this_json": False,
    }


def validate_timeline_equivalence(
    plan: Mapping[str, Any],
    draft: Mapping[str, Any],
    *,
    plan_path: Path | None = None,
) -> dict[str, Any]:
    """Compare declared preset/B-roll intent with the actual candidate timeline."""

    tolerance_us = int(plan.get("timeline_tolerance_us") or 2)
    errors: list[str] = []
    render_order_report: dict[str, Any] = {"declared": False}
    if "declared_render_order" in plan:
        ordered = plan["declared_render_order"]
        visual_tracks = [
            (index, track) for index, track in enumerate(draft.get("tracks") or [])
            if isinstance(track, Mapping) and track.get("type") in {"video", "text"}
            and track.get("segments")
        ]
        actual_order = [track.get("name") for _, track in visual_tracks]
        valid_order = (
            isinstance(ordered, list) and bool(ordered)
            and all(isinstance(name, str) and bool(name) for name in ordered)
            and len(ordered) == len(set(ordered))
        )
        if not valid_order or actual_order != ordered:
            errors.append("declared_render_order 与实际可见轨道数组顺序不一致")
        index_errors: list[str] = []
        for index, track in visual_tracks:
            segments = track.get("segments")
            if not isinstance(segments, list):
                index_errors.append(str(track.get("name")))
                continue
            for segment in segments:
                if (
                    not isinstance(segment, Mapping)
                    or type(segment.get("render_index")) is not int
                    or segment.get("render_index") != index
                    or type(segment.get("track_render_index")) is not int
                    or segment.get("track_render_index") != 0
                ):
                    index_errors.append(str(track.get("name")))
                    break
        if index_errors:
            errors.append(f"render_index 必须与实际完整轨道数组位置一致: {index_errors}")
        render_order_report = {
            "declared": True, "expected": ordered, "actual": actual_order,
            "index_errors": index_errors,
            "ok": valid_order and actual_order == ordered and not index_errors,
            "native_visual_verified": False,
        }
    tracks = [item for item in draft.get("tracks") or [] if isinstance(item, Mapping)]
    tracks_by_name = {str(item.get("name") or ""): item for item in tracks}
    text_materials = _material_index(draft, "texts")
    video_materials = _material_index(draft, "videos")
    preset_reports: list[dict[str, Any]] = []
    declared_preset_prefixes: set[str] = set()
    declared_preset_text_names: set[str] = set()

    for selection in plan.get("presets") or []:
        if not isinstance(selection, Mapping):
            continue
        node_id = str(selection.get("node_id") or "")
        template_id = str(selection.get("template_id") or "")
        start_us = selection.get("start_us")
        end_us = selection.get("end_us")
        prefix = f"JY_PRESET_{template_id}__NODE__{node_id}_"
        declared_preset_prefixes.add(prefix)
        matched = [track for track in tracks if str(track.get("name") or "").startswith(prefix)]
        ranges = [
            value
            for track in matched
            for segment in track.get("segments") or []
            if isinstance(segment, Mapping)
            for value in [_timerange(segment)]
            if value is not None
        ]
        actual_start = min((value[0] for value in ranges), default=None)
        actual_end = max((value[1] for value in ranges), default=None)
        timing_ok = (
            isinstance(start_us, int)
            and not isinstance(start_us, bool)
            and isinstance(end_us, int)
            and not isinstance(end_us, bool)
            and actual_start is not None
            and actual_end is not None
            and _close_us(actual_start, start_us, tolerance_us)
            and _close_us(actual_end, end_us, tolerance_us)
        )
        if not timing_ok:
            errors.append(
                f"preset {node_id or template_id} 时点不等价: "
                f"planned=({start_us},{end_us}), actual=({actual_start},{actual_end})"
            )
        text_checks: list[dict[str, Any]] = []
        for expected in selection.get("actual_text_tracks") or []:
            if not isinstance(expected, Mapping):
                continue
            track_name = str(expected.get("track_name") or "")
            track = tracks_by_name.get(track_name)
            actual_texts: list[str] = []
            actual_ranges: list[tuple[int, int]] = []
            if track is not None:
                for segment in track.get("segments") or []:
                    if not isinstance(segment, Mapping):
                        continue
                    material = text_materials.get(str(segment.get("material_id") or ""))
                    if material is not None:
                        actual_texts.append(_material_text(material))
                    value = _timerange(segment)
                    if value is not None:
                        actual_ranges.append(value)
            try:
                expected_texts = declared_track_texts(expected)
            except ValueError as error:
                errors.append(f"preset {node_id or template_id} {track_name}: {error}")
                expected_texts = []
            expected_text = expected_texts if "texts" in expected else str(expected.get("text") or "")
            expected_track_start = expected.get("start_us")
            expected_track_end = expected.get("end_us")
            expected_segment_count = expected.get("segment_count")
            text_ok = bool(actual_texts) and bool(expected_texts) and (
                actual_texts == expected_texts if "texts" in expected
                else all(value == expected_text for value in actual_texts)
            )
            if not text_ok:
                errors.append(
                    f"preset {node_id or template_id} 文字不等价: "
                    f"track={track_name!r}, planned={expected_text!r}, actual={actual_texts!r}"
                )
            track_timing_ok = (
                isinstance(expected_track_start, int)
                and not isinstance(expected_track_start, bool)
                and isinstance(expected_track_end, int)
                and not isinstance(expected_track_end, bool)
                and actual_ranges
                and _close_us(min(value[0] for value in actual_ranges), expected_track_start, tolerance_us)
                and _close_us(max(value[1] for value in actual_ranges), expected_track_end, tolerance_us)
                and isinstance(expected_segment_count, int)
                and not isinstance(expected_segment_count, bool)
                and len(actual_ranges) == expected_segment_count
            )
            if not track_timing_ok:
                errors.append(
                    f"preset {node_id or template_id} 逐轨时点不等价: track={track_name!r}, "
                    f"planned=({expected_track_start},{expected_track_end},count={expected_segment_count}), "
                    f"actual=({min((v[0] for v in actual_ranges), default=None)},"
                    f"{max((v[1] for v in actual_ranges), default=None)},count={len(actual_ranges)})"
                )
            declared_preset_text_names.add(track_name)
            text_checks.append({
                "track_name": track_name,
                "planned": expected_text,
                "actual": actual_texts,
                "ok": text_ok,
                "timing_ok": track_timing_ok,
            })
        preset_reports.append({
            "node_id": node_id,
            "template_id": template_id,
            "planned_start_us": start_us,
            "planned_end_us": end_us,
            "actual_start_us": actual_start,
            "actual_end_us": actual_end,
            "hold_span_us": actual_end - actual_start if actual_start is not None and actual_end is not None else None,
            "timing_ok": timing_ok,
            "text_tracks": text_checks,
        })

    actual_preset_tracks = {
        str(track.get("name") or "")
        for track in tracks
        if str(track.get("name") or "").startswith("JY_PRESET_")
    }
    undeclared_preset_groups = sorted(
        name
        for name in actual_preset_tracks
        if not any(name.startswith(prefix) for prefix in declared_preset_prefixes)
    )
    actual_preset_text_names = {
        str(track.get("name") or "")
        for track in tracks
        if str(track.get("name") or "").startswith("JY_PRESET_")
        and track.get("type") == "text"
    }
    undeclared_preset_text = sorted(actual_preset_text_names - declared_preset_text_names)
    if undeclared_preset_groups:
        errors.append(f"候选包含未在 CandidatePlan 声明的预设组轨道: {undeclared_preset_groups}")
    if undeclared_preset_text:
        errors.append(f"候选包含未在 actual_text_tracks 声明的预设文字轨: {undeclared_preset_text}")

    broll_reports: list[dict[str, Any]] = []
    declared_broll_names: set[str] = set()
    segment_ids = Counter(
        str(segment.get("id") or "").strip()
        for track in tracks for segment in track.get("segments") or []
        if isinstance(segment, Mapping)
    )
    track_ids = Counter(str(track.get("id") or "").strip() for track in tracks)
    for item in plan.get("brolls") or []:
        if not isinstance(item, Mapping):
            continue
        node_id = str(item.get("node_id") or "")
        track_name = str(item.get("track_name") or "")
        declared_broll_names.add(track_name)
        track = tracks_by_name.get(track_name)
        ranges: list[tuple[int, int]] = []
        material_paths: list[str] = []
        clip_checks: list[bool] = []
        identity_ok = bool(track is not None and str(track.get("id") or "").strip())
        if track is not None:
            identity_ok = identity_ok and track_ids[str(track.get("id") or "").strip()] == 1
        fixed_ids = [operation["segment_id"] for operation in plan.get("operations") or []
                     if isinstance(operation, Mapping) and operation.get("kind") == "add_broll"
                     and operation.get("node_id") == node_id and "segment_id" in operation]
        if "segment_id" in item:
            fixed_ids.append(item["segment_id"])
        if fixed_ids:
            identity_ok = identity_ok and all(isinstance(value, str) and value.strip() for value in fixed_ids)
            identity_ok = identity_ok and all(value == fixed_ids[0] for value in fixed_ids)
            identity_ok = identity_ok and track is not None and [
                segment.get("id") for segment in track.get("segments") or []
                if isinstance(segment, Mapping)
            ] == [fixed_ids[0]]
        if track is not None:
            for segment in track.get("segments") or []:
                if not isinstance(segment, Mapping):
                    continue
                segment_id = str(segment.get("id") or "").strip()
                identity_ok = identity_ok and bool(segment_id) and segment_ids[segment_id] == 1
                value = _timerange(segment)
                if value is not None:
                    ranges.append(value)
                material = video_materials.get(str(segment.get("material_id") or ""))
                if material is not None:
                    material_paths.append(str(material.get("path") or material.get("media_path") or ""))
                expected_clip = item.get("clip")
                if isinstance(expected_clip, Mapping):
                    actual_clip = segment.get("clip") or {}
                    clip_checks.append(_mapping_contains(actual_clip, expected_clip))
                if "source_crop" in item:
                    from .media_crop import crop_rectangle, material_crop
                    try:
                        clip_checks.append(material is not None and
                                           material_crop(material) == crop_rectangle(item["source_crop"]))
                    except ValueError:
                        clip_checks.append(False)
        actual_start = min((value[0] for value in ranges), default=None)
        actual_end = max((value[1] for value in ranges), default=None)
        start_us = item.get("start_us")
        end_us = item.get("end_us")
        timing_ok = (
            isinstance(start_us, int)
            and not isinstance(start_us, bool)
            and isinstance(end_us, int)
            and not isinstance(end_us, bool)
            and actual_start is not None
            and actual_end is not None
            and _close_us(actual_start, start_us, tolerance_us)
            and _close_us(actual_end, end_us, tolerance_us)
        )
        declared_source = (
            resolve_path(item.get("source_path"), plan_path)
            if plan_path is not None
            else item.get("source_path")
        )
        source_ok = bool(material_paths) and all(
            _same_path(path, declared_source) for path in material_paths
        )
        expected_segment_count = item.get("segment_count")
        segment_count_ok = (
            isinstance(expected_segment_count, int)
            and not isinstance(expected_segment_count, bool)
            and len(ranges) == expected_segment_count
        )
        layout_ok = bool(clip_checks) and all(clip_checks)
        if not identity_ok:
            errors.append(f"broll {node_id or track_name} requires non-empty unique track/segment id")
        if not timing_ok:
            errors.append(
                f"broll {node_id or track_name} 时点不等价: "
                f"planned=({start_us},{end_us}), actual=({actual_start},{actual_end})"
            )
        if not source_ok:
            errors.append(
                f"broll {node_id or track_name} 素材不等价: "
                f"planned={item.get('source_path')!r}, actual={material_paths!r}"
            )
        if not layout_ok:
            errors.append(f"broll {node_id or track_name} clip 布局参数与计划不一致")
        if not segment_count_ok:
            errors.append(
                f"broll {node_id or track_name} segment_count 不等价: "
                f"planned={expected_segment_count}, actual={len(ranges)}"
            )
        broll_reports.append({
            "node_id": node_id,
            "track_name": track_name,
            "planned_start_us": start_us,
            "planned_end_us": end_us,
            "actual_start_us": actual_start,
            "actual_end_us": actual_end,
            "source_ok": source_ok,
            "layout_ok": layout_ok,
            "timing_ok": timing_ok,
            "segment_count_ok": segment_count_ok,
            "identity_ok": identity_ok,
        })
    actual_broll_names = {
        str(track.get("name") or "")
        for track in tracks
        if str(track.get("name") or "").startswith("JY_BROLL_")
    }
    undeclared = sorted(actual_broll_names - declared_broll_names)
    if undeclared:
        errors.append(f"候选包含未在 CandidatePlan 声明的 B-roll 轨道: {undeclared}")
    from .action_audio import validate_action_audio
    audio_report = validate_action_audio(plan, draft, plan_path=plan_path)
    errors.extend(audio_report["errors"])
    from .preset_audio import validate_preset_audio
    preset_audio_report = validate_preset_audio(plan, draft, plan_path=plan_path)
    errors.extend(preset_audio_report["errors"])
    from .video_effects import validate_video_effects
    effect_report = validate_video_effects(plan, draft, plan_path=plan_path)
    errors.extend(effect_report["errors"])
    from .visual_planning import validate_required_visual_techniques
    required_visuals = validate_required_visual_techniques(
        plan, draft=draft, plan_path=plan_path, effect_report=effect_report)
    errors.extend(required_visuals["errors"])
    from .text_animation import validate_text_animations
    text_animation_report = validate_text_animations(plan, draft, plan_path=plan_path)
    errors.extend(text_animation_report["errors"])
    from .visual_planning import validate_visual_execution
    visual_execution = validate_visual_execution(plan, draft, plan_path=plan_path)
    errors.extend(visual_execution['errors'])
    return {
        "ok": not errors,
        "errors": errors,
        "tolerance_us": tolerance_us,
        "render_order": render_order_report,
        "presets": preset_reports,
        "brolls": broll_reports,
        "action_audio": audio_report,
        "preset_audio": preset_audio_report,
        "video_effects": effect_report,
        "required_visual_techniques": required_visuals,
        "text_animations": text_animation_report,
        "visual_execution": visual_execution,
        "undeclared_preset_group_tracks": undeclared_preset_groups,
        "undeclared_preset_text_tracks": undeclared_preset_text,
        "undeclared_broll_tracks": undeclared,
    }


def _registry_role(registry: Mapping[str, Any], template_id: str) -> str:
    """Return the semantic role from the canonical usage registry."""
    for key in ("records", "candidates"):
        rows = registry.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or str(row.get("template_id") or "") != template_id:
                continue
            return str(
                row.get("semantic_role")
                or row.get("primary_category")
                or row.get("category")
                or ""
            ).strip()
    return ""


def _validate_replacement_semantics(
    plan: Mapping[str, Any],
    usage_registry: Mapping[str, Any],
) -> list[str]:
    """Fail the plan before writing when a replacement crosses semantic roles."""
    selections = {
        str(item.get("template_id") or ""): item
        for item in plan.get("presets") or []
        if isinstance(item, Mapping) and item.get("template_id")
    }
    errors: list[str] = []
    for operation in plan.get("operations") or []:
        if not isinstance(operation, Mapping) or operation.get("kind") != "replace_preset_group":
            continue
        operation_id = str(operation.get("id") or "<missing-id>")
        old_id = str(operation.get("old_template_id") or "")
        new_id = str(operation.get("new_template_id") or "")
        if not old_id or not new_id:
            errors.append(f"replacement {operation_id}: old/new template_id 必须存在")
            continue
        old_role = next(
            (
                str(operation.get(key) or "").strip()
                for key in ("old_semantic_role", "source_semantic_role", "old_category")
                if str(operation.get(key) or "").strip()
            ),
            "",
        )
        new_role = next(
            (
                str(operation.get(key) or "").strip()
                for key in ("new_semantic_role", "target_semantic_role", "new_category")
                if str(operation.get(key) or "").strip()
            ),
            "",
        )
        shared_role = str(operation.get("semantic_role") or "").strip()
        if shared_role:
            old_role = old_role or shared_role
            new_role = new_role or shared_role
        old_role = old_role or _registry_role(usage_registry, old_id)
        target = selections.get(new_id)
        new_role = new_role or (
            str(target.get("semantic_role") or target.get("category") or "").strip()
            if isinstance(target, Mapping)
            else ""
        )
        new_role = new_role or _registry_role(usage_registry, new_id)
        registry_role = _registry_role(usage_registry, new_id)
        if not old_role or not new_role:
            errors.append(
                f"replacement {operation_id}: {old_id}->{new_id} requires explicit/resolvable semantic_role"
            )
            continue
        if old_role != new_role:
            errors.append(
                f"replacement {operation_id}: semantic_role mismatch: "
                f"{old_id}={old_role!r}, {new_id}={new_role!r}"
            )
        if not registry_role:
            errors.append(
                f"replacement {operation_id}: candidate {new_id} has no semantic_role in usage registry"
            )
        elif registry_role != new_role:
            errors.append(
                f"replacement {operation_id}: candidate semantic_role mismatch: "
                f"declared={new_role!r}, registry={registry_role!r}"
            )
    return errors


def validate_semantic_gate(gate: Mapping[str, Any], rough_cut: Path, *, gate_path: Path | None = None) -> dict[str, Any]:
    errors: list[str] = []
    evidence = gate.get("evidence")
    actual = file_sha256(rough_cut) if rough_cut.is_file() else ""
    raw_values = gate.get("evidence_paths")
    if not isinstance(raw_values, list):
        raw_values = gate.get("evidence") if isinstance(gate.get("evidence"), list) else []
    evidence_paths: list[Path] = []
    for item in raw_values:
        value = item.get("path", item.get("evidence_path")) if isinstance(item, Mapping) else item
        if value:
            path = Path(str(value))
            if not path.is_absolute() and gate_path is not None:
                path = gate_path.parent / path
            evidence_paths.append(path.resolve())
    semantic_report: dict[str, Any] = {}
    if len(evidence_paths) != 2:
        errors.append("语义门禁必须通过两路真实 evidence JSON 文件；仅填写 status/checks/evidence 摘要不算证据")
    elif actual:
        semantic_report = validate_semantic_evidence(rough_cut, evidence_paths, expected_rough_cut_sha256=actual)
        errors.extend(semantic_report.get("errors") or [])
    checks = gate.get("checks")
    if gate.get("status") != "passed":
        errors.append("语义门禁状态不是 passed")
    if not isinstance(checks, Mapping) or not checks:
        errors.append("语义门禁缺少机器检查证据")
    elif not all(value is True for value in checks.values()):
        errors.append("语义门禁存在未通过检查")
    rough = gate.get("rough_cut") or {}
    expected = str(rough.get("sha256") or "").upper()
    if not actual:
        errors.append(f"粗剪文件不存在: {rough_cut}")
    elif actual != expected:
        errors.append(f"粗剪 SHA256 不匹配: {actual} != {expected}")
    content_payload = gate.get("content_gate")
    content_report: dict[str, Any] = {}
    if not isinstance(content_payload, Mapping):
        errors.append("语义门禁必须包含 content_gate；不能省略最终语音字幕覆盖检查")
    else:
        required_content_keys = (
            "final_retained_speech",
            "ordinary_subtitles",
            "preset_replacement_windows",
        )
        missing_content_keys = [key for key in required_content_keys if key not in content_payload]
        errors.extend(f"content_gate 缺少 {key}" for key in missing_content_keys)
        if not missing_content_keys:
            adjudicated_text = str(semantic_report.get("adjudication_final_text") or "")
            if adjudicated_text:
                retained = content_payload.get("final_retained_speech")
                if isinstance(retained, Mapping):
                    retained = retained.get("segments", retained.get("items", []))
                retained_text = "".join(str(item.get("text") or "") for item in retained or [] if isinstance(item, Mapping))
                normalize = lambda value: re.sub(r"[\s，。！？、；：,.!?;:]", "", value).casefold()
                if normalize(retained_text) != normalize(adjudicated_text):
                    errors.append("content_gate.final_retained_speech 与实际裁决后的文字不一致")
            # Keep the shared report for compatibility, then apply the strict
            # equality check here.  Substring containment is unsafe for final
            # speech (for example, a one-character caption can appear in a
            # longer unrelated line).
            content_report = {
                "ok": True,
                "errors": [],
                "caption_coverage": validate_caption_coverage(
                    content_payload.get("final_retained_speech"),
                    content_payload.get("ordinary_subtitles"),
                    content_payload.get("preset_replacement_windows"),
                ),
            }
            errors.extend(content_report["caption_coverage"].get("errors") or [])
            strict_report = _strict_caption_coverage(
                content_payload.get("final_retained_speech"),
                content_payload.get("ordinary_subtitles"),
                content_payload.get("preset_replacement_windows"),
            )
            content_report["strict_caption_coverage"] = strict_report
            errors.extend(strict_report.get("errors") or [])
    return {
        "ok": not errors,
        "errors": errors,
        "rough_cut": str(rough_cut),
        "rough_cut_sha256": actual,
        "checks": dict(checks) if isinstance(checks, Mapping) else {},
        "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
        "semantic_evidence": semantic_report,
        "content_gate": content_report,
    }


SUBJECT_CLARITY_CHECKS = (
    "primary_subject_defined",
    "unused_headroom_checked",
    "background_distraction_checked",
    "perspective_checked",
    "caption_subject_clearance_checked",
)


def validate_visual_rules(plan: Mapping[str, Any], *, require_native_clearance: bool = True,
                          content_gate: Any = None) -> dict[str, Any]:
    errors: list[str] = []
    visual_events = validate_visual_events(plan, content_gate=content_gate)
    errors.extend(visual_events["errors"])

    subject = plan.get("subject_clarity_preflight")
    subject_checks: dict[str, Any] = {}
    if not isinstance(subject, Mapping):
        errors.append("缺少 subject_clarity_preflight")
    else:
        if subject.get("status") not in ({"passed"} if require_native_clearance else {"passed", "pending"}):
            errors.append("subject_clarity_preflight.status 必须是 passed")
        raw_checks = subject.get("checks")
        if not isinstance(raw_checks, Mapping):
            errors.append("subject_clarity_preflight.checks 必须是对象")
        else:
            subject_checks = dict(raw_checks)
            for check_name in SUBJECT_CLARITY_CHECKS:
                if not require_native_clearance and check_name == "caption_subject_clearance_checked":
                    continue
                if raw_checks.get(check_name) is not True:
                    errors.append(f"subject_clarity_preflight.checks.{check_name} 必须为 true")

    budget = plan.get("text_style_budget")
    budget_report: dict[str, Any] = {}
    if budget is not None and not isinstance(budget, Mapping):
        errors.append("text_style_budget 必须是对象")
    elif isinstance(budget, Mapping):
        budget_report = dict(budget)
        display_families = budget.get("max_display_families")
        palette_roles = budget.get("max_palette_roles")
        if (
            not isinstance(display_families, int)
            or isinstance(display_families, bool)
            or display_families < 1
        ):
            errors.append("text_style_budget.max_display_families 必须是正整数")
        if (
            not isinstance(palette_roles, int)
            or isinstance(palette_roles, bool)
            or palette_roles < 1
        ):
            errors.append("text_style_budget.max_palette_roles 必须是正整数")
        if not isinstance(budget.get("full_screen_text"), bool):
            errors.append("text_style_budget.full_screen_text 必须是布尔值")

    transition_gate = plan.get("transition_requires_real_before_after_state")
    if transition_gate is not True:
        errors.append("transition_requires_real_before_after_state 必须为 true")
    transition_reports: list[dict[str, Any]] = []
    operations = plan.get("operations")
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, Mapping) or operation.get("visual_role") != "transition":
                continue
            operation_id = str(operation.get("id") or "")
            before_state_id = str(operation.get("before_state_id") or "").strip()
            after_state_id = str(operation.get("after_state_id") or "").strip()
            boundary_kind = str(operation.get("boundary_kind") or "")
            operation_errors: list[str] = []
            if not before_state_id:
                operation_errors.append("缺少 before_state_id")
            if not after_state_id:
                operation_errors.append("缺少 after_state_id")
            if before_state_id and before_state_id == after_state_id:
                operation_errors.append("before_state_id 与 after_state_id 不能相同")
            if boundary_kind not in {"shot_change", "composition_change"}:
                operation_errors.append("boundary_kind 必须是 shot_change 或 composition_change")
            errors.extend(f"transition operation {operation_id or '<missing-id>'}: {item}" for item in operation_errors)
            transition_reports.append(
                {
                    "id": operation_id,
                    "before_state_id": before_state_id,
                    "after_state_id": after_state_id,
                    "boundary_kind": boundary_kind,
                    "ok": not operation_errors,
                }
            )

    return {
        "ok": not errors,
        "errors": errors,
        "subject_clarity_preflight": {
            "status": subject.get("status") if isinstance(subject, Mapping) else None,
            "checks": subject_checks,
        },
        "text_style_budget": budget_report,
        "transition_requires_real_before_after_state": transition_gate,
        "transition_operations": transition_reports,
        "visual_events": visual_events,
    }


@dataclass
class CandidatePlanReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    project_state: dict[str, Any] = field(default_factory=dict)
    semantic_gate: dict[str, Any] = field(default_factory=dict)
    frontend_proof: dict[str, Any] = field(default_factory=dict)
    visual_rules: dict[str, Any] = field(default_factory=dict)
    preset_preflight: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "project_state": self.project_state,
            "semantic_gate": self.semantic_gate,
            "frontend_proof": self.frontend_proof,
            "visual_rules": self.visual_rules,
            "preset_preflight": self.preset_preflight,
        }


def validate_candidate_plan(
    plan: Mapping[str, Any],
    *,
    plan_path: Path,
    verify_snapshot_files: bool = True,
    mode: str = "canonical",
) -> CandidatePlanReport:
    if mode not in {"canonical", "preview", "seal"}:
        raise ValueError(f"unknown candidate plan validation mode: {mode!r}")
    require_evidence = mode != "preview"
    errors: list[str] = []
    warnings: list[str] = []
    from .material_library import validate_plan_materials
    errors.extend(validate_plan_materials(plan, plan_path)["errors"])
    if plan.get("schema") != "jianying-adapter.candidate-plan.v1":
        errors.append("candidate plan schema 必须是 jianying-adapter.candidate-plan.v1")
    project_format = plan.get("project_format")
    if not isinstance(project_format, Mapping):
        errors.append("project_format 必须是对象")
    else:
        if project_format.get("separates_creative_plan_from_writer") is not True:
            errors.append("project_format 必须把创意计划与 Writer 分离")
        if project_format.get("style_normalization") is not False:
            errors.append("project_format.style_normalization 必须为 false，禁止统一视觉模板")
        if project_format.get("writer_adapter") != "jianying-8.8":
            errors.append("project_format.writer_adapter 必须是 jianying-8.8")
        if project_format.get("candidate_builder") != "CandidateAssembler":
            errors.append("project_format.candidate_builder 必须是 CandidateAssembler")
        if project_format.get("per_video_build_script") is not False:
            errors.append("project_format.per_video_build_script 必须为 false")
        if project_format.get("operation_registry") != SHARED_OPERATION_REGISTRY:
            errors.append(
                f"project_format.operation_registry 必须是 {SHARED_OPERATION_REGISTRY}"
            )
    required_paths = ("project_state", "base_draft", "rough_cut", "semantic_gate", "usage_registry")
    paths = {key: resolve_path(plan.get(key), plan_path) for key in required_paths}
    for key, path in paths.items():
        if not path.is_file():
            errors.append(f"{key} 文件不存在: {path}")
    state_report: dict[str, Any] = {}
    if paths["project_state"].is_file():
        state_value = read_json(paths["project_state"])
        minimum_visual_version = state_value.get("visual_planning_min_version", 1 if state_value.get("visual_planning_required") is True else 0)
        if (type(minimum_visual_version) is not int or minimum_visual_version not in (0, 1, 2)
                or ("visual_planning_min_version" in state_value and minimum_visual_version == 0)):
            errors.append("project_state.visual_planning_min_version 必须为 1 或 2")
        else:
            minimum_visual_version = max(minimum_visual_version, 1 if state_value.get("visual_planning_required") is True else 0)
            declared_visual_version = project_format.get("visual_planning_version") if isinstance(project_format, Mapping) else None
            if minimum_visual_version and (type(declared_visual_version) is not int or declared_visual_version < minimum_visual_version):
                errors.append(f"当前项目要求画面安排：不得省略 visual_planning_version>={minimum_visual_version} 与 visual_events，也不得降级")
        result = validate_project_state(
            state_value,
            state_path=paths["project_state"],
            require_canonical_status=bool(plan.get("require_canonical_status", True)),
        )
        state_report = result.to_dict()
        from .visual_planning import validate_required_visual_techniques
        required_visuals = validate_required_visual_techniques(plan, state=state_value, plan_path=plan_path)
        state_report["required_visual_techniques"] = required_visuals
        errors.extend(required_visuals["errors"])
        errors.extend(f"project_state: {item}" for item in result.errors)
        warnings.extend(f"project_state: {item}" for item in result.warnings)
        accepted_statuses = {"candidate_ready"} if mode == "canonical" else {"planning"}
        if result.canonical_status not in accepted_statuses:
            errors.append(
                "project_state.status 不允许进入当前候选阶段；preview/seal 需要 planning，正式构建需要 candidate_ready"
            )
        project_scripts = sorted(
            str(path.relative_to(paths["project_state"].parent))
            for path in paths["project_state"].parent.rglob("*.py")
        )
        if project_scripts:
            errors.append(
                "当前视频目录存在 Python 脚本；正式候选目录只能提交数据计划，"
                "不得靠改名或放入子目录绕过: "
                + ", ".join(project_scripts)
            )
    semantic_report: dict[str, Any] = {}
    semantic_payload = read_json(paths["semantic_gate"]) if paths["semantic_gate"].is_file() else {}
    if paths["semantic_gate"].is_file() and paths["rough_cut"].is_file():
        semantic_report = validate_semantic_gate(semantic_payload, paths["rough_cut"], gate_path=paths["semantic_gate"])
        errors.extend(f"semantic_gate: {item}" for item in semantic_report["errors"])
    if paths["base_draft"].is_file():
        retakes = validate_actual_retake_cuts(read_json(paths["base_draft"]), plan, plan_path=plan_path)
        errors.extend(retakes["errors"])
    target = plan.get("target") or {}
    width = int(target.get("width") or 0)
    height = int(target.get("height") or 0)
    if width <= 0 or height <= 0:
        errors.append("target.width/height 必须为正整数")
    if str(target.get("jianying_version") or "") != "8.8.0":
        errors.append("当前生产候选必须显式声明 jianying_version=8.8.0")
    expected_exe = resolve_path(target.get("expected_exe"), plan_path)
    if not expected_exe.is_file() or expected_exe.name.casefold() != "jianyingpro.exe":
        errors.append(f"target.expected_exe 必须是存在的剪映8.8 JianyingPro.exe: {expected_exe}")
    else:
        try:
            exact_process_identifier(expected_exe)
        except (FileNotFoundError, ValueError) as error:
            errors.append(f"target.expected_exe 版本门禁失败: {error}")
    for option_name, option in (plan.get("options") or {}).items():
        if isinstance(option, Mapping) and option.get("enabled") is True and not str(option.get("authorization_source") or "").strip():
            errors.append(f"启用 {option_name} 时必须提供 authorization_source")
    from .creative_plan import validate_creative_plan
    creative_plan = validate_creative_plan(plan, plan_path=plan_path,
        require=(plan.get('project_format') or {}).get('visual_planning_version') == 2)
    errors.extend(f'creative_plan: {item}' for item in creative_plan['errors'])
    from .preset_registry import validate_preset_selections
    preset_choices = validate_preset_selections(plan, require_choices=plan.get('creative_brief') is not None)
    errors.extend(f'preset_choices: {item}' for item in preset_choices['errors'])
    visual_rules = validate_visual_rules(plan, require_native_clearance=require_evidence,
        content_gate=plan.get("content_gate") or semantic_payload.get("content_gate"))
    errors.extend(f"visual_rules: {item}" for item in visual_rules["errors"])
    from .video_effects import validate_video_effect_plan
    effect_plan = validate_video_effect_plan(plan, plan_path=plan_path, require_native_evidence=require_evidence)
    errors.extend(effect_plan["errors"])
    from .text_animation import validate_text_animation_plan
    text_animation_plan = validate_text_animation_plan(plan, plan_path=plan_path, require_native_evidence=require_evidence)
    errors.extend(text_animation_plan["errors"])
    preset_reports: list[dict[str, Any]] = []
    if paths["usage_registry"].is_file() and width > 0 and height > 0:
        registry = read_json(paths["usage_registry"])
        errors.extend(
            f"replacement_semantics: {item}"
            for item in _validate_replacement_semantics(plan, registry)
        )
        state_value = read_json(paths["project_state"]) if paths["project_state"].is_file() else {}
        expected_binding = {
            "project_id": str(state_value.get("project_id") or ""),
            "roughcut_sha256": str(semantic_report.get("rough_cut_sha256") or "").upper(),
            "project_root": paths["project_state"].parent,
        }
        preset_keys: set[tuple[str, str]] = set()
        for selection in plan.get("presets") or []:
            if not isinstance(selection, Mapping):
                errors.append("presets 只能包含对象")
                continue
            node_id = str(selection.get("node_id") or "").strip()
            template_id = str(selection.get("template_id") or "").strip()
            start_us = selection.get("start_us")
            end_us = selection.get("end_us")
            duration_us = selection.get("node_duration_us")
            if not node_id:
                errors.append(f"preset {template_id}: 缺少 node_id")
            key = (template_id, node_id)
            if key in preset_keys:
                errors.append(f"preset 节点身份重复: {key}")
            preset_keys.add(key)
            if (
                not isinstance(start_us, int)
                or isinstance(start_us, bool)
                or start_us < 0
                or not isinstance(end_us, int)
                or isinstance(end_us, bool)
                or end_us <= start_us
            ):
                errors.append(f"preset {node_id or template_id}: start_us/end_us 必须声明有效目标时点")
            elif duration_us != end_us - start_us:
                errors.append(
                    f"preset {node_id or template_id}: node_duration_us 必须等于 end_us-start_us"
                )
            final_texts: list[str] = []
            actual_text_tracks = selection.get("actual_text_tracks") or []
            for track_index, track in enumerate(actual_text_tracks):
                if not isinstance(track, Mapping):
                    continue
                track_name = str(track.get("track_name") or "").strip()
                track_start = track.get("start_us")
                track_end = track.get("end_us")
                segment_count = track.get("segment_count")
                expected_prefix = f"JY_PRESET_{template_id}__NODE__{node_id}_"
                if not track_name.startswith(expected_prefix):
                    errors.append(
                        f"preset {node_id or template_id}: actual_text_tracks[{track_index}].track_name "
                        "必须绑定当前 template_id+node_id"
                    )
                if (
                    not isinstance(track_start, int)
                    or isinstance(track_start, bool)
                    or not isinstance(track_end, int)
                    or isinstance(track_end, bool)
                    or track_end <= track_start
                ):
                    errors.append(
                        f"preset {node_id or template_id}: actual_text_tracks[{track_index}] "
                        "必须声明有效 start_us/end_us"
                    )
                elif (
                    isinstance(start_us, int)
                    and isinstance(end_us, int)
                    and (track_start < start_us or track_end > end_us)
                ):
                    errors.append(
                        f"preset {node_id or template_id}: actual_text_tracks[{track_index}] 时点超出节点"
                    )
                if (
                    not isinstance(segment_count, int)
                    or isinstance(segment_count, bool)
                    or segment_count < 1
                ):
                    errors.append(
                        f"preset {node_id or template_id}: actual_text_tracks[{track_index}].segment_count "
                        "必须是正整数"
                    )
                if isinstance(track.get("texts"), list):
                    final_texts.extend(str(value) for value in track["texts"])
                elif "text" in track:
                    final_texts.append(str(track.get("text") or ""))
            if not final_texts:
                final_texts = [str(value) for value in selection.get("slots") or []]
            selection_binding = {
                **expected_binding,
                "final_texts": final_texts,
                "final_position": selection.get("final_position"),
                "final_group_scale": selection.get("final_group_scale"),
            }
            report = preflight_selection(
                selection,
                registry,
                target_canvas=(width, height),
                verify_snapshot_files=verify_snapshot_files,
                expected_binding=selection_binding,
                require_current_video_evidence=require_evidence,
            )
            preset_reports.append(report.to_dict())
            errors.extend(f"preset {report.template_id}: {item}" for item in report.errors)
            warnings.extend(f"preset {report.template_id}: {item}" for item in report.warnings)
    frontend_report: dict[str, Any] = {}
    if (plan.get("presets") or plan.get("brolls")) and expected_exe.is_file() and require_evidence:
        proof_value = plan.get("frontend_proof")
        proof_path = resolve_path(proof_value, plan_path)
        if proof_value and proof_path.is_file():
            try:
                state_value = read_json(paths["project_state"]) if paths["project_state"].is_file() else {}
                frontend_report = validate_frontend_proof(
                    read_json(proof_path),
                    expected_exe=expected_exe,
                    project_id=str(state_value.get("project_id") or ""),
                    roughcut_sha256=str(semantic_report.get("rough_cut_sha256") or ""),
                )
            except Exception as exc:
                frontend_report = {
                    "ok": False,
                    "errors": [f"无法读取或解析诊断证明: {type(exc).__name__}: {exc}"],
                }
            warnings.extend(f"frontend_proof (diagnostic): {value}" for value in frontend_report["errors"])
        elif proof_value:
            warnings.append(f"frontend_proof 未找到，继续依赖当前原生画面证据: {proof_path}")
    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        errors.append("operations 必须是非空数组")
    else:
        ids = [str(item.get("id") or "") for item in operations if isinstance(item, Mapping)]
        if any(not item for item in ids) or len(ids) != len(set(ids)):
            errors.append("每个 operation 必须有唯一非空 id")
        operation_kinds = {
            str(item.get("kind") or "") for item in operations if isinstance(item, Mapping)
        }
        if any(kind.startswith("add_broll") for kind in operation_kinds) and not plan.get("brolls"):
            errors.append("存在 add_broll 操作时必须在 CandidatePlan.brolls 显式声明每个节点")
    brolls = plan.get("brolls") or []
    if not isinstance(brolls, list):
        errors.append("brolls 必须是数组")
    else:
        broll_names: set[str] = set()
        for index, item in enumerate(brolls):
            if not isinstance(item, Mapping):
                errors.append(f"brolls[{index}] 必须是对象")
                continue
            node_id = str(item.get("node_id") or "").strip()
            track_name = str(item.get("track_name") or "").strip()
            start_us = item.get("start_us")
            end_us = item.get("end_us")
            source_path = resolve_path(item.get("source_path"), plan_path)
            if not node_id or not track_name.startswith("JY_BROLL_"):
                errors.append(f"brolls[{index}] 必须声明 node_id 和唯一 JY_BROLL_ track_name")
            if track_name in broll_names:
                errors.append(f"B-roll track_name 重复: {track_name}")
            broll_names.add(track_name)
            if (
                not isinstance(start_us, int)
                or isinstance(start_us, bool)
                or start_us < 0
                or not isinstance(end_us, int)
                or isinstance(end_us, bool)
                or end_us <= start_us
            ):
                errors.append(f"broll {node_id or index}: start_us/end_us 必须声明有效目标时点")
            if not source_path.is_file():
                errors.append(f"broll {node_id or index}: 冻结素材不存在: {source_path}")
            segment_count = item.get("segment_count")
            if (
                not isinstance(segment_count, int)
                or isinstance(segment_count, bool)
                or segment_count < 1
            ):
                errors.append(f"broll {node_id or index}: segment_count 必须是正整数")
            clip = item.get("clip")
            if not isinstance(clip, Mapping) or not clip:
                errors.append(f"broll {node_id or index}: 必须显式声明最终 clip 布局")
    for key in ("output", "report", "manifest"):
        if not str(plan.get(key) or "").strip():
            errors.append(f"缺少候选输出路径: {key}")
    preview_paths = {
        key: resolve_path(plan.get(key), plan_path)
        for key in ("preview_output", "preview_report", "preview_manifest")
    }
    output_paths = [resolve_path(plan.get(key), plan_path).resolve(strict=False)
                    for key in ("output", "report", "manifest", "preview_output", "preview_report", "preview_manifest")]
    if len(set(output_paths)) != len(output_paths):
        errors.append("候选、报告与 manifest 的六个输出路径必须互不相同")
    for key, path in preview_paths.items():
        if not str(plan.get(key) or "").strip():
            errors.append(f"缺少隔离预览输出路径: {key}")
        elif path in {
            resolve_path(plan.get("output"), plan_path),
            resolve_path(plan.get("report"), plan_path),
            resolve_path(plan.get("manifest"), plan_path),
        }:
            errors.append(f"预览路径 {key} 必须与正式候选输出路径分离")
    if not str(plan.get("draft_name") or "").strip():
        errors.append("缺少 draft_name")
    return CandidatePlanReport(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        project_state=state_report,
        semantic_gate=semantic_report,
        frontend_proof=frontend_report,
        visual_rules=visual_rules,
        preset_preflight=preset_reports,
    )


@dataclass
class CandidateBuildContext:
    plan_path: Path
    plan: Mapping[str, Any]
    plan_report: CandidatePlanReport
    operation_reports: list[dict[str, Any]] = field(default_factory=list)


def _advance_state_after_seal(
    state_path: Path,
    *,
    candidate_plan_path: Path,
    structure_report_path: Path,
) -> None:
    """Atomically record the preview-to-sealed transition.

    A state module may provide a stricter project-specific implementation;
    this small fallback keeps the CandidatePlan contract usable in isolated
    tests and still refuses any state other than canonical ``planning``.
    """
    from . import project_state as state_module

    advance = getattr(state_module, "advance_state_after_candidate_seal", None)
    if callable(advance):
        advance(
            state_path,
            candidate_plan_path=candidate_plan_path,
            structure_report_path=structure_report_path,
        )
        return
    state = read_json(state_path)
    report = validate_project_state(state, state_path=state_path, require_canonical_status=True)
    if not report.ok or report.canonical_status != "planning":
        raise RuntimeError("seal 只能从 canonical planning 状态推进")
    updated = copy.deepcopy(state)
    updated["status"] = "candidate_ready"
    artifacts = dict(updated.get("artifacts") or {})
    artifacts.update({
        "candidate_plan": str(candidate_plan_path),
        "structure_report": str(structure_report_path),
    })
    updated["artifacts"] = artifacts
    temporary = state_path.with_name(state_path.name + ".seal.tmp")
    write_json(temporary, updated)
    temporary.replace(state_path)


class CandidateAssembler:
    """Execute one explicit candidate plan without importing older video versions.

    A named shared registry supplies operations.  Plain per-video callback
    dictionaries are deliberately rejected.  The assembler owns the fail-closed
    preflight, operation order, output lifecycle and final report.
    """

    def __init__(
        self,
        plan_path: Path,
        operations: OperationRegistry,
        *,
        final_validator: FinalValidator,
        verify_snapshot_files: bool = True,
    ) -> None:
        if not isinstance(operations, OperationRegistry):
            raise TypeError(
                "CandidateAssembler 只接受 OperationRegistry；禁止单视频脚本直接注入 callback 字典"
            )
        self.plan_path = plan_path.resolve()
        self.plan = read_json(self.plan_path)
        declared_registry = str((self.plan.get("project_format") or {}).get("operation_registry") or "")
        if declared_registry != operations.name:
            raise ValueError(
                f"CandidatePlan operation_registry 与执行注册表不一致: "
                f"{declared_registry!r} != {operations.name!r}"
            )
        self.operations = dict(operations.handlers)
        declared_kinds = {
            str(spec.get("kind") or "")
            for spec in self.plan.get("operations") or []
            if isinstance(spec, Mapping)
        }
        missing = sorted(declared_kinds - set(self.operations))
        if missing:
            raise KeyError(f"共享操作注册表缺少计划操作: {missing}")
        self.final_validator = final_validator
        self.verify_snapshot_files = verify_snapshot_files

    def run(self) -> dict[str, Any]:
        """Reject the former single-phase writer bypass.

        Production callers must obtain a non-writable preview and seal it only
        after current-video evidence has been captured.  Keeping this method
        as an explicit failure gives old callers a useful migration error and
        cannot create a writer-allowed manifest accidentally.
        """
        raise RuntimeError("CandidateAssembler.run() 已停用；请按 preview() -> seal() 执行")

    def preview(self) -> dict[str, Any]:
        """Build an isolated workspace preview from the clean base draft.

        A preview is useful for obtaining runtime screenshots, but its
        manifest is deliberately non-writable and never advances project
        state.
        """
        return self._build(mode="preview", writer_allowed=False)

    def seal(self) -> dict[str, Any]:
        """Seal a previously built preview after real current-video evidence.

        This method starts from ``planning`` and validates the final plan,
        evidence and preview structure together.  Only then is the state
        atomically advanced to ``candidate_ready``.
        """
        state_path = resolve_path(self.plan["project_state"], self.plan_path)
        state = read_json(state_path)
        state_status = str(state.get("status") or "")
        if state_status != "planning":
            raise RuntimeError(f"seal 只能从 planning 启动，当前为 {state_status!r}")
        preview = self._preview_paths()
        if not all(path.is_file() for path in preview.values()):
            raise FileNotFoundError("seal 需要先完成 preview，并找到 preview candidate/report")
        preview_manifest = read_json(preview["manifest"])
        expected_plan_hash = file_sha256(self.plan_path)
        expected_build_contract_hash = build_contract_sha256(self.plan)
        expected_base_path = resolve_path(self.plan["base_draft"], self.plan_path)
        expected_base_hash = file_sha256(expected_base_path)
        if preview_manifest.get("writer_allowed") is not False or preview_manifest.get("phase") != "preview":
            raise RuntimeError("seal 拒绝缺少 writer_allowed=false 的隔离 preview manifest")
        if preview_manifest.get("blockers") != ["current_video_evidence_pending_seal"]:
            raise RuntimeError("seal 拒绝 preview manifest blockers 被修改")
        if str(preview_manifest.get("build_contract_sha256") or "").upper() != expected_build_contract_hash:
            raise RuntimeError("seal 拒绝：preview 与当前不可变构建合同不一致（素材/操作/文案/时点/布局已改变）")
        if str(preview_manifest.get("base_draft_sha256") or "").upper() != expected_base_hash:
            raise RuntimeError("seal 拒绝：preview 与当前 base draft 哈希不一致")
        preview_hash = file_sha256(preview["candidate"])
        if str(preview_manifest.get("candidate_sha256") or "").upper() != preview_hash:
            raise RuntimeError("seal 拒绝：preview candidate 已被修改")
        preflight = validate_candidate_plan(
            self.plan,
            plan_path=self.plan_path,
            verify_snapshot_files=self.verify_snapshot_files,
            mode="seal",
        )
        if not preflight.ok:
            raise RuntimeError(json.dumps(preflight.to_dict(), ensure_ascii=False))
        preview_report = read_json(preview["report"])
        if preview_report.get("ok") is not True:
            raise RuntimeError("seal 拒绝未通过的 preview report")
        draft = read_json(preview["candidate"])
        context = CandidateBuildContext(self.plan_path, self.plan, preflight)
        final = self._run_final_validation(
            draft, context, audio_measurement=(preview_report.get("final_validation") or {}).get("audio_measurement")
        )
        timeline_equivalence = validate_timeline_equivalence(self.plan, draft, plan_path=self.plan_path)
        final["timeline_equivalence"] = timeline_equivalence
        final_errors = [str(value) for value in final.get("errors") or []]
        final_errors.extend(timeline_equivalence["errors"])
        final["errors"] = final_errors
        final["ok"] = bool(final.get("ok")) and timeline_equivalence["ok"]
        report = {
            "schema": "jianying-adapter.candidate-build-report.v1",
            "ok": bool(final["ok"]),
            "phase": "seal",
            "plan": str(self.plan_path),
            "preview": {key: str(value) for key, value in preview.items()},
            "preflight": preflight.to_dict(),
            "operations": [],
            "final_validation": final,
        }
        paths = self._canonical_paths()
        if any(path.exists() for path in paths.values()):
            raise FileExistsError("正式候选输出已存在；禁止原地覆盖")
        if not report["ok"]:
            write_json(paths["report"], report)
            raise RuntimeError(f"候选封版最终验证失败: {paths['report']}")
        state_before = resolve_path(self.plan["project_state"], self.plan_path).read_bytes()
        created: list[Path] = []
        try:
            write_json(paths["candidate"], draft)
            created.append(paths["candidate"])
            candidate_hash = file_sha256(paths["candidate"])
            report["candidate_sha256"] = candidate_hash
            report["plan_sha256"] = expected_plan_hash
            report["build_contract_sha256"] = expected_build_contract_hash
            report["base_draft_sha256"] = expected_base_hash
            write_json(paths["report"], report)
            created.append(paths["report"])
            manifest = {
                "schema": "jianying-adapter.candidate-writer-manifest.v1",
                "status": "candidate_ready_for_single_writer",
                "writer_allowed": True,
                "blockers": [],
                "candidate": str(paths["candidate"]),
                "structure_report": str(paths["report"]),
                "candidate_plan": str(self.plan_path),
                "source": str(resolve_path(self.plan["rough_cut"], self.plan_path)),
                "project_state": str(resolve_path(self.plan["project_state"], self.plan_path)),
                "registered_live_draft": False,
                "subjective_visual_qa": "pending_user",
                "phase": "sealed",
                "preview_manifest": str(preview["manifest"]),
                "candidate_sha256": candidate_hash,
                "plan_sha256": expected_plan_hash,
                "build_contract_sha256": expected_build_contract_hash,
                "base_draft_sha256": expected_base_hash,
            }
            write_json(paths["manifest"], manifest)
            created.append(paths["manifest"])
            _advance_state_after_seal(
                resolve_path(self.plan["project_state"], self.plan_path),
                candidate_plan_path=self.plan_path,
                structure_report_path=paths["report"],
            )
        except Exception as error:
            # Seal failures are recoverable evidence.  Preserve any newly
            # written candidate/report and replace the writer manifest with a
            # non-writable failure record instead of deleting artifacts.
            write_json(paths["manifest"], {
                "schema": "jianying-adapter.candidate-writer-manifest.v1",
                "status": "seal_failed",
                "writer_allowed": False,
                "blockers": ["seal_failed", f"{type(error).__name__}: {error}"],
                "candidate": str(paths["candidate"]),
                "structure_report": str(paths["report"]),
                "candidate_plan": str(self.plan_path),
                "project_state": str(resolve_path(self.plan["project_state"], self.plan_path)),
                "registered_live_draft": False,
                "phase": "seal_failed",
                "plan_sha256": expected_plan_hash,
                "build_contract_sha256": expected_build_contract_hash,
                "base_draft_sha256": expected_base_hash,
                "recoverable_artifacts": [str(path) for path in created],
            })
            resolve_path(self.plan["project_state"], self.plan_path).write_bytes(state_before)
            raise
        return {"candidate": str(paths["candidate"]), "report": str(paths["report"]), "manifest": str(paths["manifest"])}

    def _preview_paths(self) -> dict[str, Path]:
        return {
            "candidate": resolve_path(self.plan.get("preview_output"), self.plan_path),
            "report": resolve_path(self.plan.get("preview_report"), self.plan_path),
            "manifest": resolve_path(self.plan.get("preview_manifest"), self.plan_path),
        }

    def _canonical_paths(self) -> dict[str, Path]:
        return {
            "candidate": resolve_path(self.plan["output"], self.plan_path),
            "report": resolve_path(self.plan["report"], self.plan_path),
            "manifest": resolve_path(self.plan["manifest"], self.plan_path),
        }

    def _run_final_validation(
        self, draft: Mapping[str, Any], context: CandidateBuildContext, *, mode: str = "canonical",
        audio_measurement: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run mandatory shared structure/content checks before plan-specific checks."""
        from .shared_operations import shared_final_validator

        shared = dict(shared_final_validator(draft, context))
        errors = [str(value) for value in shared.get("errors") or []]
        actual_coverage = validate_actual_content(draft, self.plan, plan_path=self.plan_path)
        errors.extend(str(value) for value in actual_coverage.get("errors") or [])
        try:
            custom = dict(self.final_validator(draft, context))
        except Exception as error:
            custom = {"ok": False, "errors": [f"final_validator failed: {type(error).__name__}: {error}"]}
        errors.extend(str(value) for value in custom.get("errors") or [])
        from .final_layout import validate_final_layout
        layout = validate_final_layout(draft, self.plan, preview=mode == "preview")
        errors.extend(layout.get("errors", []))
        from .action_audio import measure_candidate_audio, validate_audio_measurement
        if not errors:
            if audio_measurement is None:
                audio_measurement = measure_candidate_audio(self.plan, draft, plan_path=self.plan_path)
            errors.extend(audio_measurement.get("errors", []))
            errors.extend(validate_audio_measurement(draft, audio_measurement, plan=self.plan, plan_path=self.plan_path))
        return {
            **custom,
            "ok": shared.get("ok") is True and actual_coverage.get("ok") is True and custom.get("ok") is True and layout["ok"] and not errors,
            "errors": errors,
            "shared_final_validation": shared,
            "actual_caption_coverage": actual_coverage,
            "final_layout": layout,
            "audio_measurement": audio_measurement,
        }

    def _build(
        self,
        *,
        mode: str,
        writer_allowed: bool,
        preview_paths: Mapping[str, Path] | None = None,
    ) -> dict[str, Any]:
        preflight = validate_candidate_plan(
            self.plan,
            plan_path=self.plan_path,
            verify_snapshot_files=self.verify_snapshot_files,
            mode=mode,
        )
        if not preflight.ok:
            raise RuntimeError(json.dumps(preflight.to_dict(), ensure_ascii=False))
        base_path = resolve_path(self.plan["base_draft"], self.plan_path)
        paths = preview_paths if mode == "seal" and preview_paths else (
            self._preview_paths() if mode == "preview" else self._canonical_paths()
        )
        output_path, report_path, manifest_path = paths["candidate"], paths["report"], paths["manifest"]
        if any(path.exists() for path in (output_path, report_path, manifest_path)):
            raise FileExistsError("候选输出已存在；禁止原地覆盖")
        draft = copy.deepcopy(read_json(base_path))
        context = CandidateBuildContext(self.plan_path, self.plan, preflight)
        for spec in self.plan["operations"]:
            kind = str(spec.get("kind") or "")
            handler = self.operations.get(kind)
            if handler is None:
                raise KeyError(f"未注册候选操作: {kind}")
            try:
                result = handler(draft, spec, context)
            except Exception as error:
                context.operation_reports.append({
                    "id": str(spec["id"]),
                    "kind": kind,
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                })
                failure_report = {
                    "schema": "jianying-adapter.candidate-build-report.v1",
                    "ok": False,
                    "plan": str(self.plan_path),
                    "preflight": preflight.to_dict(),
                    "operations": context.operation_reports,
                    "final_validation": {
                        "ok": False,
                        "errors": [f"operation {spec['id']} failed: {type(error).__name__}: {error}"],
                    },
                }
                write_json(report_path, failure_report)
                raise RuntimeError(f"候选操作失败，已写入报告: {report_path}") from error
            try:
                evidence: Mapping[str, Any] = {}
                if isinstance(result, tuple):
                    if len(result) != 2 or not isinstance(result[0], dict) or not isinstance(result[1], Mapping):
                        raise ValueError("返回值必须是 (draft, evidence) 二元组")
                    draft, evidence = result
                elif isinstance(result, Mapping):
                    evidence = result
                else:
                    raise ValueError("未返回 draft/evidence")
                if evidence.get("ok") is False:
                    raise ValueError("返回 ok=false；操作未通过")
            except Exception as error:
                context.operation_reports.append({
                    "id": str(spec["id"]),
                    "kind": kind,
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                    "evidence": dict(evidence),
                })
                failure_report = {
                    "schema": "jianying-adapter.candidate-build-report.v1",
                    "ok": False,
                    "plan": str(self.plan_path),
                    "preflight": preflight.to_dict(),
                    "operations": context.operation_reports,
                    "final_validation": {"ok": False, "errors": [f"operation {spec['id']} failed: {error}"]},
                }
                write_json(report_path, failure_report)
                raise RuntimeError(f"候选操作失败，已写入报告: {report_path}") from error
            context.operation_reports.append(
                {"id": str(spec["id"]), "kind": kind, "status": "passed", "evidence": dict(evidence)}
            )
        final = self._run_final_validation(draft, context, mode=mode)
        timeline_equivalence = validate_timeline_equivalence(
            self.plan, draft, plan_path=self.plan_path
        )
        final["timeline_equivalence"] = timeline_equivalence
        final_errors = [str(value) for value in final.get("errors") or []]
        final_errors.extend(timeline_equivalence["errors"])
        final["errors"] = final_errors
        final["ok"] = bool(final.get("ok")) and timeline_equivalence["ok"]
        report = {
            "schema": "jianying-adapter.candidate-build-report.v1",
            "ok": final.get("ok") is True,
            "plan": str(self.plan_path),
            "preflight": preflight.to_dict(),
            "operations": context.operation_reports,
            "final_validation": final,
        }
        if mode == "preview":
            write_json(output_path, draft)
            candidate_hash = file_sha256(output_path)
            report["candidate_sha256"] = candidate_hash
            report["plan_sha256"] = file_sha256(self.plan_path)
            report["build_contract_sha256"] = build_contract_sha256(self.plan)
            report["base_draft_sha256"] = file_sha256(base_path)
        write_json(report_path, report)
        if not report["ok"]:
            raise RuntimeError(f"候选最终验证失败: {report_path}")
        if mode != "preview":
            write_json(output_path, draft)
        manifest = {
            "schema": "jianying-adapter.candidate-writer-manifest.v1",
            "status": "candidate_ready_for_single_writer" if writer_allowed else "preview_only",
            "writer_allowed": writer_allowed,
            "blockers": [] if writer_allowed else ["current_video_evidence_pending_seal"],
            "candidate": str(output_path),
            "structure_report": str(report_path),
            "candidate_plan": str(self.plan_path),
            "source": str(resolve_path(self.plan["rough_cut"], self.plan_path)),
            "project_state": str(resolve_path(self.plan["project_state"], self.plan_path)),
            "registered_live_draft": False,
            "subjective_visual_qa": "pending_user",
            "phase": "sealed" if mode == "seal" else mode,
        }
        if mode == "preview":
            manifest.update({
                "plan_sha256": report["plan_sha256"],
                "build_contract_sha256": report["build_contract_sha256"],
                "base_draft_sha256": report["base_draft_sha256"],
                "candidate_sha256": report["candidate_sha256"],
            })
        write_json(manifest_path, manifest)
        if mode == "seal":
            _advance_state_after_seal(
                resolve_path(self.plan["project_state"], self.plan_path),
                candidate_plan_path=self.plan_path,
                structure_report_path=report_path,
            )
        return {"candidate": str(output_path), "report": str(report_path), "manifest": str(manifest_path)}


def validate_writer_contract(
    plan: Mapping[str, Any],
    *,
    plan_path: Path,
    verify_snapshot_files: bool = True,
    preview_test: bool = False,
    profile_kind: str = "production",
) -> dict[str, Any]:
    """Revalidate every immutable candidate gate immediately before the single Writer."""

    errors: list[str] = []
    if preview_test and profile_kind != "test":
        errors.append("原生预览只允许写入 test profile，禁止生产登记")
    if preview_test:
        preview_config = plan.get("test_preview") or {}
        state_file = resolve_path(plan.get("project_state"), plan_path)
        state = read_json(state_file) if state_file.is_file() else {}
        grant = state.get("test_preview_authorization") or {}
        if (preview_config.get("enabled") is not True or grant.get("enabled") is not True
                or not grant.get("source") or preview_config.get("authorization_source") != grant.get("source")):
            errors.append("测试预览缺少与当前状态绑定的用户授权")
        preview_name = str(preview_config.get("draft_name") or "").strip()
        if not preview_name or Path(preview_name).name != preview_name or preview_name == plan.get("draft_name"):
            errors.append("测试预览必须使用独立的单层草稿名")
    paths = {
        "candidate": resolve_path(plan.get("preview_output" if preview_test else "output"), plan_path),
        "report": resolve_path(plan.get("preview_report" if preview_test else "report"), plan_path),
        "manifest": resolve_path(plan.get("preview_manifest" if preview_test else "manifest"), plan_path),
    }
    for key, path in paths.items():
        if not path.is_file():
            errors.append(f"{key} 文件不存在: {path}")
    plan_report = validate_candidate_plan(
        plan,
        plan_path=plan_path,
        verify_snapshot_files=verify_snapshot_files,
        mode="preview" if preview_test else "canonical",
    )
    errors.extend(f"candidate_plan: {value}" for value in plan_report.errors)
    report: dict[str, Any] = {}
    manifest: dict[str, Any] = {}
    candidate: dict[str, Any] = {}
    if paths["report"].is_file():
        report = read_json(paths["report"])
        if report.get("ok") is not True:
            errors.append("候选总结构检查未通过")
        final = report.get("final_validation")
        if not isinstance(final, Mapping) or final.get("ok") is not True:
            errors.append("候选最终结构检查未通过")
        elif not isinstance(final.get("timeline_equivalence"), Mapping) or (
            final.get("timeline_equivalence") or {}
        ).get("ok") is not True:
            errors.append("候选报告缺少通过的中心化时点等价检查")
    if paths["manifest"].is_file():
        manifest = read_json(paths["manifest"])
        if manifest.get("schema") != "jianying-adapter.candidate-writer-manifest.v1":
            errors.append("writer manifest schema 不正确")
        expected_status = "preview_only" if preview_test else "candidate_ready_for_single_writer"
        if manifest.get("status") != expected_status:
            errors.append(f"writer manifest 状态不是 {expected_status}")
        if manifest.get("writer_allowed") is not (False if preview_test else True):
            errors.append("writer_manifest.writer_allowed 与当前写入阶段不符")
        if preview_test and manifest.get("phase") != "preview":
            errors.append("测试 Writer 只接受原生 preview manifest")
        blockers = manifest.get("blockers")
        expected_blockers = ["current_video_evidence_pending_seal"] if preview_test else []
        if blockers != expected_blockers:
            errors.append(f"writer manifest blockers 与阶段不符，当前为 {blockers!r}")
        if manifest.get("registered_live_draft") is not False:
            errors.append("候选已写入或 registered_live_draft 未显式为 false")
        if not _same_path(manifest.get("candidate"), paths["candidate"]):
            errors.append("writer manifest candidate 与计划不一致")
        if not _same_path(manifest.get("structure_report"), paths["report"]):
            errors.append("writer manifest structure_report 与计划不一致")
        if not _same_path(manifest.get("candidate_plan"), plan_path):
            errors.append("writer manifest candidate_plan 与当前计划不一致")
    timeline: dict[str, Any] = {"ok": False, "errors": ["candidate missing"]}
    if paths["candidate"].is_file():
        candidate = read_json(paths["candidate"])
        from .action_audio import validate_audio_measurement
        errors.extend(validate_audio_measurement(candidate, (report.get("final_validation") or {}).get("audio_measurement"), plan=plan, plan_path=plan_path))
        from .shared_operations import shared_final_validator

        shared = dict(shared_final_validator(candidate, None))
        from .final_layout import validate_final_layout
        layout = validate_final_layout(candidate, plan, preview=preview_test and profile_kind == "test")
        errors.extend(f"final_layout: {value}" for value in layout.get("errors", []))
        if shared.get("ok") is not True:
            errors.extend(f"shared_final_validation: {value}" for value in shared.get("errors") or [])
        actual_coverage = validate_actual_content(candidate, plan, plan_path=plan_path)
        if actual_coverage.get("ok") is not True:
            errors.extend(f"actual_caption_coverage: {value}" for value in actual_coverage.get("errors") or [])
        timeline = validate_timeline_equivalence(plan, candidate, plan_path=plan_path)
        errors.extend(f"timeline: {value}" for value in timeline["errors"])
        candidate_hash = file_sha256(paths["candidate"])
        if manifest and str(manifest.get("candidate_sha256") or "").upper() != candidate_hash:
            errors.append("writer manifest candidate_sha256 与当前 candidate 不一致")
        if manifest and str(manifest.get("plan_sha256") or "").upper() != file_sha256(plan_path):
            errors.append("writer manifest plan_sha256 与当前 CandidatePlan 不一致")
        if report and str(report.get("candidate_sha256") or "").upper() != candidate_hash:
            errors.append("candidate build report candidate_sha256 与当前 candidate 不一致")
        if report and str(report.get("plan_sha256") or "").upper() != file_sha256(plan_path):
            errors.append("candidate build report plan_sha256 与当前 CandidatePlan 不一致")
        if preview_test and manifest and str(manifest.get("build_contract_sha256") or "").upper() != build_contract_sha256(plan):
            errors.append("测试预览的构建合同与当前计划不一致")
        base_path = resolve_path(plan.get("base_draft"), plan_path)
        if manifest and str(manifest.get("base_draft_sha256") or "").upper() != file_sha256(base_path):
            errors.append("writer manifest base_draft_sha256 与当前 base draft 不一致")
    return {
        "ok": not errors,
        "errors": errors,
        "paths": {key: str(path) for key, path in paths.items()},
        "plan": plan_report.to_dict(),
        "timeline_equivalence": timeline,
        "manifest": manifest,
    }
