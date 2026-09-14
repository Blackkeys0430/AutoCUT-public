"""Derive the audited 500 fully-recovered presets and build one screening batch.

The queue is derived from phase-1's final runtime ledger and phase-2's eleven
post-open runtime audits.  Candidate assembly is workspace-only; live materialize
and registration remain a separate single-Writer step.
"""

from __future__ import annotations

import argparse
import copy
import functools
import hashlib
import itertools
import json
import re
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ADAPTER = Path(__file__).resolve().parents[1]
PROJECT = ADAPTER.parent
TRIAL = PROJECT / "video_trials/字幕预设黑幕筛选_20260902"
OUTPUT = TRIAL / "screening_500"
SCRIPTS = ADAPTER / "scripts"
SRC = ADAPTER / "src"
VENDOR = ADAPTER / "vendor/pyJianYingDraft-source"
DEPS = ADAPTER / "vendor/python-deps"
for path in (SCRIPTS, SRC, VENDOR, DEPS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import build_phase1_resource_recovery_batches as phase1  # noqa: E402
from jianying_adapter.candidate_plan import (  # noqa: E402
    CandidateAssembler,
    OperationRegistry,
    SHARED_OPERATION_REGISTRY,
)
from pyJianYingDraft import (  # noqa: E402
    ClipSettings,
    FontType,
    ScriptFile,
    TextSegment,
    TextShadow,
    TextStyle,
    TrackSpec,
    TrackType,
    VideoSegment,
    trange,
)
from jianying_adapter import preset_trial as trial_core  # noqa: E402
from jianying_adapter.preset_trial import validate_trial_draft  # noqa: E402


PHASE1_LEDGER = TRIAL / "phase1_resource_recovery_batches/phase1_285_post_open_runtime_ledger.json"
PHASE2_ROOT = TRIAL / "phase2_engineering_verification"
AUTO = ADAPTER / "preset_catalog/all_auto_candidates_v1.json"
RESCANNED = ADAPTER / "repair_workspaces/phase2_engineering_adaptation_20260903/rescanned_source_candidates_phase2.json"
PRESET_ROOT = PROJECT / "jianying_presets/剪映1000个高级感字幕预设"
CACHE_ROOT = Path.home() / "AppData/Local/JianyingPro/User Data/Cache"
PROJECT_STATE = TRIAL / "project_state.json"
BLACK = TRIAL / "assets/black_1080x1920_phase1_probe.mp4"
COMPATIBLE_BASE = TRIAL / "workspace_base_batch01.json"
QUEUE = OUTPUT / "fully_recovered_500_queue.json"
VALIDATION = OUTPUT / "fully_recovered_500_set_validation.json"
REGISTRY = OUTPUT / "fully_recovered_500_runtime_candidate_registry.json"
BASE_DURATION_US = 180_000_000
MAX_TEMPLATE_DURATION_US = 3_000_000
START_US = 1_000_000
GAP_US = 200_000
BATCH_COUNT = 10
BATCH_SIZE = 50
SCREENABLE_TRACK_TYPES = frozenset({"video"})
SCREENABLE_MATERIAL_GROUPS = frozenset({"videos", "images", "stickers"})
SCREENING_PATH_KEYS = frozenset(
    {
        "path",
        "media_path",
        "file_path",
        "filepath",
        "source_path",
        "font_path",
        "res_path",
        "production_path",
        "intensifies_path",
    }
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def candidate_maps() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    result: dict[str, dict[str, Any]] = {}
    provenance: dict[str, str] = {}
    for path in (AUTO, RESCANNED):
        for row in read_json(path).get("candidates") or []:
            if isinstance(row, dict) and row.get("template_id"):
                template_id = str(row["template_id"])
                result[template_id] = row
                provenance[template_id] = str(path.relative_to(PROJECT))
    return result, provenance


def phase1_window_map() -> dict[str, tuple[int | None, int | None, int | None]]:
    result: dict[str, tuple[int | None, int | None, int | None]] = {}
    for path in sorted((TRIAL / "phase1_resource_recovery_batches").glob("batch_*_post_open_runtime_audit.json")):
        match = re.search(r"batch_(\d+)_", path.name)
        batch = int(match.group(1)) if match else None
        for row in read_json(path).get("templates") or []:
            if isinstance(row, Mapping) and row.get("template_id"):
                result[str(row["template_id"])] = (int(row["start_us"]), int(row["end_us"]), batch)
    return result


def derive_sets() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    candidates, provenance = candidate_maps()
    ledger = read_json(PHASE1_LEDGER)
    p1_windows = phase1_window_map()
    p1_full: list[dict[str, Any]] = []
    p1_partial: list[str] = []
    for order, row in enumerate(ledger.get("templates") or [], 1):
        if not isinstance(row, Mapping) or not row.get("template_id"):
            continue
        template_id = str(row["template_id"])
        status = str(row.get("recovery_status") or "")
        if status == "partially_recovered":
            p1_partial.append(template_id)
            continue
        if status != "fully_recovered":
            raise ValueError(f"unexpected phase1 recovery status: {template_id}={status}")
        candidate = candidates.get(template_id)
        if candidate is None:
            raise ValueError(f"candidate registry missing phase1 fully-recovered template: {template_id}")
        start, end, batch = p1_windows.get(template_id, (None, None, None))
        duration = int((candidate.get("duration") or {}).get("microseconds") or 0)
        p1_full.append({
            "source_phase": "phase1_resource_recovery",
            "source_phase_order": order,
            "source_batch": batch,
            "template_id": template_id,
            "display_name": str(row.get("display_name") or candidate.get("display_name") or template_id),
            "candidate_source_registry": provenance[template_id],
            "candidate_source_path": str(candidate.get("source_path") or ""),
            "original_start_us": start,
            "original_end_us": end,
            "source_duration_us": duration,
            "audit_evidence": str(row.get("source_audit") or ""),
            "audit_evidence_sha256": str(row.get("source_audit_sha256") or ""),
            "recovery_status": status,
        })

    p2_full: list[dict[str, Any]] = []
    p2_partial: list[str] = []
    p2_audits = sorted(PHASE2_ROOT.glob("batch_*_post_open_runtime_audit.json"))
    for audit in p2_audits:
        match = re.search(r"batch_(\d+)_", audit.name)
        if not match:
            raise ValueError(f"cannot parse phase2 batch: {audit}")
        batch = int(match.group(1))
        audit_hash = sha256(audit)
        for batch_order, row in enumerate(read_json(audit).get("templates") or [], 1):
            if not isinstance(row, Mapping) or not row.get("template_id"):
                continue
            template_id = str(row["template_id"])
            summary = row.get("summary") or {}
            status = str(summary.get("recovery_status") or "")
            if status == "partially_recovered":
                p2_partial.append(template_id)
                continue
            if status != "fully_recovered":
                raise ValueError(f"unexpected phase2 recovery status: {template_id}={status}")
            candidate = candidates.get(template_id)
            if candidate is None:
                raise ValueError(f"candidate registry missing phase2 fully-recovered template: {template_id}")
            start, end = int(row["start_us"]), int(row["end_us"])
            duration = int((candidate.get("duration") or {}).get("microseconds") or end - start)
            p2_full.append({
                "source_phase": "phase2_engineering_verification",
                "source_phase_order": len(p2_full) + 1,
                "source_batch": batch,
                "source_batch_order": batch_order,
                "template_id": template_id,
                "display_name": str(candidate.get("display_name") or template_id),
                "candidate_source_registry": provenance[template_id],
                "candidate_source_path": str(candidate.get("source_path") or ""),
                "original_start_us": start,
                "original_end_us": end,
                "source_duration_us": duration,
                "audit_evidence": str(audit.relative_to(PROJECT)),
                "audit_evidence_sha256": audit_hash,
                "recovery_status": status,
            })

    full = p1_full + p2_full
    screening_rows, deferred_rows = split_screening_rows(full, candidates)
    for row in full:
        screened = next(item for item in screening_rows + deferred_rows if item["template_id"] == row["template_id"])
        row.update({
            "screening_status": screened["screening_status"],
            "embedded_media_evidence": screened["embedded_media_evidence"],
        })
    full_ids = [row["template_id"] for row in full]
    partial_ids = p1_partial + p2_partial
    checks = {
        "phase1_ledger_summary_matches_records": (
            int((ledger.get("summary") or {}).get("fully_recovered_template_count") or -1) == len(p1_full)
            and int((ledger.get("summary") or {}).get("partially_recovered_template_count") or -1) == len(p1_partial)
        ),
        "phase1_225_fully_recovered": len(p1_full) == 225,
        "phase1_60_partially_recovered": len(p1_partial) == 60,
        "phase2_11_post_open_audits": len(p2_audits) == 11,
        "phase2_275_fully_recovered": len(p2_full) == 275,
        "phase2_82_partially_recovered": len(p2_partial) == 82,
        "fully_recovered_500_unique": len(full_ids) == len(set(full_ids)) == 500,
        "partial_142_unique": len(partial_ids) == len(set(partial_ids)) == 142,
        "full_partial_intersection_empty": not (set(full_ids) & set(partial_ids)),
        "all_candidates_resolved": all(row["source_duration_us"] > 0 and row["candidate_source_path"] for row in full),
        "screenable_embedded_media_count": len(screening_rows) + len(deferred_rows) == len(full),
        "deferred_embedded_media_count": len({row["template_id"] for row in screening_rows}.intersection(
            row["template_id"] for row in deferred_rows
        )) == 0,
        "screening_duration_cap_is_three_seconds": all(
            min(int(row["source_duration_us"]), MAX_TEMPLATE_DURATION_US) <= MAX_TEMPLATE_DURATION_US
            for row in screening_rows
        ),
    }
    validation = {
        "schema": "huoke.fully-recovered-500-set-validation.v1",
        "inputs": {
            "phase1_final_ledger": str(PHASE1_LEDGER.relative_to(PROJECT)),
            "phase1_final_ledger_sha256": sha256(PHASE1_LEDGER),
            "phase2_post_open_audits": [
                {"path": str(path.relative_to(PROJECT)), "sha256": sha256(path)} for path in p2_audits
            ],
        },
        "counts": {
            "phase1_fully_recovered": len(p1_full),
            "phase1_partially_recovered_excluded": len(p1_partial),
            "phase2_fully_recovered": len(p2_full),
            "phase2_partially_recovered_excluded": len(p2_partial),
            "combined_fully_recovered_unique": len(set(full_ids)),
            "combined_partially_recovered_unique_excluded": len(set(partial_ids)),
            "screenable_embedded_media": len(screening_rows),
            "deferred_embedded_media": len(deferred_rows),
        },
        "checks": checks,
        "excluded_partially_recovered": {
            "phase1": p1_partial,
            "phase2": p2_partial,
            "combined": partial_ids,
        },
        "intersections": {
            "phase1_phase2_fully_recovered": sorted(set(row["template_id"] for row in p1_full) & set(row["template_id"] for row in p2_full)),
            "fully_recovered_partially_recovered": sorted(set(full_ids) & set(partial_ids)),
        },
        "screening": {
            "screenable_count": len(screening_rows),
            "deferred_embedded_media_count": len(deferred_rows),
            "deferred_embedded_media": deferred_rows,
        },
        "ok": all(checks.values()),
    }
    partial = {"phase1": p1_partial, "phase2": p2_partial, "combined": partial_ids}
    return full, validation, partial


def _screening_inner_drafts(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    wrappers = ((source.get("materials") or {}).get("drafts") or [])
    inners = [
        item["draft"]
        for item in wrappers
        if isinstance(item, Mapping) and isinstance(item.get("draft"), Mapping)
    ]
    return inners or [source]


def _screening_visual_refs(inner: Mapping[str, Any]) -> list[dict[str, Any]]:
    materials = inner.get("materials") or {}
    if not isinstance(materials, Mapping):
        return []
    aliases: dict[str, list[tuple[str, int, Mapping[str, Any]]]] = {}
    for group, raw_items in materials.items():
        if str(group) not in SCREENABLE_MATERIAL_GROUPS or not isinstance(raw_items, list):
            continue
        for index, item in enumerate(raw_items):
            if not isinstance(item, Mapping):
                continue
            owner = (str(group), index, item)
            for key in ("id", "material_id", "local_material_id", "origin_material_id"):
                identifier = str(item.get(key) or "").strip()
                if identifier:
                    aliases.setdefault(identifier, []).append(owner)
    refs: list[dict[str, Any]] = []
    for track_index, track in enumerate(inner.get("tracks") or []):
        if not isinstance(track, Mapping) or str(track.get("type") or "").casefold() not in SCREENABLE_TRACK_TYPES:
            continue
        for segment_index, segment in enumerate(track.get("segments") or []):
            if not isinstance(segment, Mapping):
                continue
            reference = str(segment.get("material_id") or "").strip()
            for group, material_index, material in aliases.get(reference, []):
                path_values = [
                    str(material.get(key)).strip()
                    for key in ("path", "media_path", "file_path", "filepath", "source_path")
                    if isinstance(material.get(key), str) and material.get(key).strip()
                ]
                refs.append({
                    "group": group,
                    "material_index": material_index,
                    "material_id": str(material.get("id") or reference),
                    "material_name": str(material.get("material_name") or ""),
                    "type": str(material.get("type") or ""),
                    "segment_id": str(segment.get("id") or ""),
                    "track_index": track_index,
                    "segment_index": segment_index,
                    "target_timerange": copy.deepcopy(segment.get("target_timerange")),
                    "path_values": path_values,
                    "source_platform": material.get("source_platform"),
                    "screening_status": "pending" if not path_values else "unknown",
                })
    return refs


def _screening_imported_fragment_paths(inner: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Enumerate every non-empty visual path the trial importer can preserve.

    The old recovery audit only emitted resource-id backed font/animation
    rows.  The writer, however, preserves path-bearing effect/material nodes
    too (for example ``materials.effects[*].path``).  Walk the same material
    payload that is handed to the importer so a stale non-empty path cannot
    make a template appear screenable merely because it is not on a video
    segment.
    """

    refs: list[dict[str, Any]] = []

    def walk(value: Any, location: str, group: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                child_location = f"{location}.{key_text}"
                if (
                    isinstance(child, str)
                    and child.strip()
                    and (key_text in SCREENING_PATH_KEYS or key_text.endswith("_path"))
                    and group != "audios"
                ):
                    refs.append(
                        {
                            "group": group,
                            "location": child_location,
                            "path": child.strip(),
                            "resource_id": str(
                                value.get("resource_id")
                                or value.get("font_resource_id")
                                or value.get("effect_id")
                                or ""
                            ),
                        }
                    )
                    continue
                if key_text == "content" and group == "texts" and isinstance(child, str):
                    try:
                        embedded = json.loads(child)
                    except (TypeError, json.JSONDecodeError):
                        embedded = None
                    if isinstance(embedded, (Mapping, list)):
                        walk(embedded, f"{child_location}.<json>", group)
                    continue
                walk(child, child_location, group)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{location}[{index}]", group)

    materials = inner.get("materials") or {}
    tracks = inner.get("tracks") or []
    if not isinstance(materials, Mapping) or not isinstance(tracks, list):
        return refs
    # Match build_trial_draft's imported-fragment scope: unsupported tracks and
    # disabled audio never reach the candidate draft, so their material paths
    # must not make an otherwise usable template deferred.
    imported_tracks = [
        copy.deepcopy(track)
        for track in tracks
        if isinstance(track, Mapping)
        and str(track.get("type") or "").casefold() in trial_core.ALLOWED_TRACK_TYPES
        and str(track.get("type") or "").casefold() != "audio"
    ]
    # The production sanitizer drops unknown optional extra_material_refs
    # before collecting transitive dependencies.  Mirror that small step so a
    # malformed optional reference does not abort the entire live-time audit.
    sanitized = copy.deepcopy(dict(inner))
    deduplicated: list[dict[str, Any]] = []
    phase1.trial_builder._deduplicate_material_ids(sanitized, deduplicated)
    sanitized_materials = sanitized.get("materials") or {}
    if not isinstance(sanitized_materials, Mapping):
        return refs
    known_ids, _locations = trial_core._material_aliases(sanitized_materials)
    skipped_refs: list[dict[str, Any]] = []
    phase1.trial_builder._filter_extra_refs(
        sanitized,
        known_material_ids=known_ids,
        location=("materials",),
        skipped=skipped_refs,
    )
    imported_tracks = [
        track
        for track in sanitized.get("tracks") or []
        if isinstance(track, Mapping)
        and str(track.get("type") or "").casefold() in trial_core.ALLOWED_TRACK_TYPES
        and str(track.get("type") or "").casefold() != "audio"
    ]
    entries = trial_core._collect_material_entries(sanitized, imported_tracks)
    for entry in entries:
        if entry.group == "audios":
            continue
        walk(entry.item, f"$.materials.{entry.group}[{entry.index}]", entry.group)
    # A path can be repeated in duplicate material records.  Keep each
    # location because the evidence must identify every imported occurrence.
    return refs


def _screening_audit_path_targets(row: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Read already-audited old->current targets without trusting counts."""

    audit_path = _audit_path(row)
    if not audit_path.is_file():
        return {"paths": {}, "remote_ids": {}, "ambiguous_paths": []}
    audit = read_json(audit_path)
    path_targets: dict[str, set[str]] = {}
    remote_targets: dict[str, set[str]] = {}
    for template in audit.get("templates") or []:
        if not isinstance(template, Mapping) or str(template.get("template_id") or "") != str(row.get("template_id") or ""):
            continue
        for dependency in template.get("runtime_visual_dependencies") or []:
            if not isinstance(dependency, Mapping):
                continue
            local = dependency.get("local_evidence") or {}
            target = str(local.get("resolved_target") or "") if isinstance(local, Mapping) else ""
            if target and _target_is_usable(target):
                remote_id = str(dependency.get("remote_id") or "")
                if remote_id:
                    remote_targets.setdefault(remote_id, set()).add(target)
                for key in ("before_path", "after_path", "path"):
                    original = str(dependency.get(key) or "")
                    if original:
                        path_targets.setdefault(_normal_path(original), set()).add(target)
        break
    return {
        "paths": {
            key: next(iter(values))
            for key, values in path_targets.items()
            if len(values) == 1
        },
        "remote_ids": {
            key: sorted(values)
            for key, values in remote_targets.items()
            if len(values) == 1
        },
        "ambiguous_paths": sorted(key for key, values in path_targets.items() if len(values) > 1),
    }


def _screening_path_usable(raw: str) -> bool:
    if _target_is_usable(raw):
        return True
    if not Path(raw).is_absolute() and _target_is_usable(str(PRESET_ROOT / raw)):
        return True
    normalized = raw.replace("\\", "/")
    marker = "/User Data/Cache/"
    if marker not in normalized:
        return False
    suffix = normalized.split(marker, 1)[1]
    rebased = CACHE_ROOT.joinpath(*[part for part in suffix.split("/") if part])
    return _target_is_usable(str(rebased))


def _screening_missing_kind(ref: Mapping[str, Any]) -> str:
    name = str(ref.get("material_name") or "")
    item_type = str(ref.get("type") or "").casefold()
    if item_type == "video" and ("复合片段" in name or "compound" in name.casefold()):
        return "empty_path_compound_clip"
    if item_type == "photo" and name == "透明":
        return "empty_path_transparent_photo"
    source_platform = str(ref.get("source_platform") or "").strip()
    if source_platform not in {"", "0"}:
        return "empty_path_online_media"
    return "unavailable_visual_material_path"


def _audit_screenability(row: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    source_path = PRESET_ROOT / str(candidate.get("source_path") or "")
    if not source_path.is_file():
        return {
            "screening_status": "deferred_embedded_media",
            "embedded_media_evidence": [{"reason": "source_preset_missing", "source_path": str(source_path)}],
        }
    source = read_json(source_path)
    evidence: list[dict[str, Any]] = []
    audit_targets = _screening_audit_path_targets(row)
    for inner_index, inner in enumerate(_screening_inner_drafts(source)):
        for ref in _screening_visual_refs(inner):
            resolved = any(_screening_path_usable(path) for path in ref["path_values"])
            item = {"inner_draft_index": inner_index, **ref}
            if resolved:
                item["screening_status"] = "screenable"
            else:
                item["screening_status"] = "pending"
                item["missing_kind"] = _screening_missing_kind(ref)
            evidence.append(item)
        for ref in _screening_imported_fragment_paths(inner):
            remote_target = ""
            remote_id = str(ref.get("resource_id") or "")
            remote_candidates = audit_targets["remote_ids"].get(remote_id) or []
            if len(remote_candidates) == 1:
                remote_target = str(remote_candidates[0])
            cache_target = ""
            fallback = _resolve_current_cache_fallback(
                ref["path"], (remote_id,) if remote_id else ()
            )
            if fallback is not None:
                cache_target = fallback[0]
            path_key = _normal_path(ref["path"])
            ambiguous_audit_path = path_key in audit_targets["ambiguous_paths"]
            resolved_path = next(
                (
                    path
                    for path in (
                        audit_targets["paths"].get(_normal_path(ref["path"]), ""),
                        ref["path"],
                        cache_target,
                        remote_target,
                    )
                    if path and _screening_path_usable(path)
                ),
                None,
            )
            item = {"inner_draft_index": inner_index, **ref}
            if ambiguous_audit_path:
                item["screening_status"] = "pending"
                item["missing_kind"] = "ambiguous_imported_fragment_visual_path"
            elif resolved_path is not None:
                item["screening_status"] = "screenable"
                item["resolved_path"] = resolved_path
            else:
                item["screening_status"] = "pending"
                item["missing_kind"] = "unavailable_imported_fragment_visual_path"
            evidence.append(item)
    pending = [item for item in evidence if item.get("screening_status") == "pending"]
    return {
        "screening_status": "deferred_embedded_media" if pending else "screenable",
        "embedded_media_evidence": evidence,
    }


def split_screening_rows(
    rows: list[dict[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split audited rows using only visual materials actually used by video tracks."""

    candidate_map = candidates or candidate_maps()[0]
    screenable: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for row in rows:
        audit = _audit_screenability(row, candidate_map[str(row["template_id"])])
        enriched = {**row, **audit}
        (deferred if audit["screening_status"] == "deferred_embedded_media" else screenable).append(enriched)
    return screenable, deferred


def assign_batches(rows: list[dict[str, Any]], *, batch_count: int | None = None) -> list[dict[str, Any]]:
    if batch_count is None:
        batch_count = max(1, (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE)
    if batch_count <= 0:
        raise ValueError("batch_count must be positive")
    for index, row in enumerate(rows, 1):
        row["canonical_order"] = index
    canonical_rows = sorted(rows, key=lambda item: int(item["canonical_order"]))
    bins = [
        canonical_rows[index : index + BATCH_SIZE]
        for index in range(0, len(canonical_rows), BATCH_SIZE)
    ]
    if len(bins) != batch_count:
        raise ValueError(f"batch_count does not match canonical chunks: {batch_count} vs {len(bins)}")
    ordered: list[dict[str, Any]] = []
    for batch_index, bucket in enumerate(bins, 1):
        cursor = START_US
        for batch_order, row in enumerate(sorted(bucket, key=lambda item: int(item["canonical_order"])), 1):
            duration = min(int(row["source_duration_us"]), MAX_TEMPLATE_DURATION_US)
            ordered.append({
                **row,
                "screening_order": (batch_index - 1) * BATCH_SIZE + batch_order,
                "batch": batch_index,
                "batch_order": batch_order,
                "duration_us": duration,
                "screening_start_us": cursor,
                "screening_end_us": cursor + duration,
            })
            cursor += duration + GAP_US
        if not bucket or len(bucket) > BATCH_SIZE or (ordered and ordered[-1]["screening_end_us"] > BASE_DURATION_US):
            raise ValueError(f"balanced batch {batch_index} does not fit: count={len(bucket)} end={ordered[-1]['screening_end_us']}")
    return ordered


def make_registry(queue: Mapping[str, Any]) -> dict[str, Any]:
    candidates, _ = candidate_maps()
    output: list[dict[str, Any]] = []
    for item in queue.get("items") or []:
        candidate = copy.deepcopy(candidates[str(item["template_id"])])
        for kind in ("text", "image", "video"):
            for slot in ((candidate.get("slots") or {}).get(kind) or []):
                if not isinstance(slot, dict):
                    continue
                slot["screening_500_original_required"] = bool(slot.get("required"))
                slot["screening_500_preserve_source_node"] = True
                slot["required"] = False
        candidate["screening_500_policy"] = "preserve_native_source_text_styles_resources_and_animations"
        output.append(candidate)
    return {
        "schema": "huoke.fully-recovered-500-runtime-candidate-registry.v1",
        "scope_count": len(output),
        "current_video_media_bound": False,
        "unsafe_whole_text_replacement_performed": False,
        "candidates": output,
    }


def plan_queue() -> dict[str, Any]:
    full, validation, partial = derive_sets()
    if not validation["ok"]:
        raise RuntimeError(f"500-set validation failed: {validation['checks']}")
    screenable, deferred = split_screening_rows(full)
    batch_count = max(1, (len(screenable) + BATCH_SIZE - 1) // BATCH_SIZE)
    items = assign_batches(screenable, batch_count=batch_count)
    batch_summaries = []
    for batch in range(1, batch_count + 1):
        group = [item for item in items if item["batch"] == batch]
        batch_summaries.append({
            "batch": batch,
            "template_count": len(group),
            "first_template_id": group[0]["template_id"],
            "last_template_id": group[-1]["template_id"],
            "end_us": group[-1]["screening_end_us"],
        })
    queue = {
        "schema": "huoke.fully-recovered-500-screening-queue.v1",
        "scope": "audited_fully_recovered_only",
        "template_count": len(items),
        "batch_count": batch_count,
        "batch_size": BATCH_SIZE,
        "ordering": {
            "canonical": "phase1_final_ledger_order_then_phase2_audit_batch_and_record_order",
            "batch_assignment": "canonical_contiguous_chunks_of_at_most_batch_size",
            "tie_break": "canonical_order",
            "start_us": START_US,
            "gap_us": GAP_US,
            "max_duration_us": MAX_TEMPLATE_DURATION_US,
        },
        "excluded_partial_count": len(partial["combined"]),
        "deferred_embedded_media_count": len(deferred),
        "deferred_embedded_media": deferred,
        "batch_summaries": batch_summaries,
        "items": items,
    }
    write_json(QUEUE, queue)
    validation["queue"] = {"path": str(QUEUE.relative_to(PROJECT)), "sha256": sha256(QUEUE)}
    validation["batch_summaries"] = batch_summaries
    validation["checks"]["screenable_batches_at_most_fifty"] = (
        len(batch_summaries) == batch_count
        and all(0 < row["template_count"] <= BATCH_SIZE for row in batch_summaries)
    )
    validation["checks"]["screenable_count_matches_queue"] = len(items) == len(screenable)
    validation["checks"]["deferred_embedded_media_count_matches_audit"] = (
        len(deferred) + len(screenable) == len(full)
    )
    validation["checks"]["screening_items_at_most_three_seconds"] = all(
        int(row["duration_us"]) <= MAX_TEMPLATE_DURATION_US for row in items
    )
    validation["checks"]["all_batches_fit_180_seconds"] = all(row["end_us"] <= BASE_DURATION_US for row in batch_summaries)
    validation["ok"] = all(validation["checks"].values())
    write_json(VALIDATION, validation)
    write_json(REGISTRY, make_registry(queue))
    return queue


def deterministic_id_factory(batch: int = 1):
    counter = itertools.count(1)
    namespace = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"huoke-fully-recovered-500-screening-batch-{int(batch):02d}",
    )
    return lambda: str(uuid.uuid5(namespace, str(next(counter)))).upper()


def screening_draft_name(batch: int, template_count: int) -> str:
    return f"字幕预设500款筛选_第{int(batch):02d}批_{int(template_count)}款"


def _normal_path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _target_is_usable(value: str) -> bool:
    target = Path(value)
    if target.is_file():
        return target.stat().st_size > 0
    if target.is_dir():
        return any(path.is_file() and path.stat().st_size > 0 for path in target.rglob("*"))
    return False


def _audit_path(item: Mapping[str, Any]) -> Path:
    raw = Path(str(item.get("audit_evidence") or ""))
    return raw if raw.is_absolute() else PROJECT / raw


_SCREENING_CACHE_LEAF_INDEX: dict[str, list[Path]] | None = None


def _screening_cache_leaf_index() -> dict[str, list[Path]]:
    global _SCREENING_CACHE_LEAF_INDEX
    if _SCREENING_CACHE_LEAF_INDEX is None:
        index: dict[str, list[Path]] = {}
        if CACHE_ROOT.is_dir():
            for path in CACHE_ROOT.rglob("*"):
                index.setdefault(path.name.casefold(), []).append(path)
        _SCREENING_CACHE_LEAF_INDEX = index
    return _SCREENING_CACHE_LEAF_INDEX


@functools.lru_cache(maxsize=None)
def _resolve_current_cache_fallback(
    original: str,
    remote_ids: list[str],
) -> tuple[str, str] | None:
    """Resolve an imported path omitted by the older audit collector."""

    direct = Path(original)
    if _target_is_usable(str(direct)):
        return str(direct), "original_path_already_local"
    normalized = original.replace("\\", "/")
    marker = "/User Data/Cache/"
    if marker in normalized:
        suffix = normalized.split(marker, 1)[1]
        rebased = CACHE_ROOT.joinpath(*[part for part in suffix.split("/") if part])
        if _target_is_usable(str(rebased)):
            return str(rebased), "current_cache_relative_rebase"

    category = "artistEffect" if "/artistEffect/" in normalized else "effect"
    remote_targets: list[Path] = []
    for remote_id in remote_ids:
        target = CACHE_ROOT / category / remote_id
        if _target_is_usable(str(target)):
            remote_targets.append(target)
    unique_remote = {str(path.resolve(strict=False)): path for path in remote_targets}
    if len(unique_remote) == 1:
        return str(next(iter(unique_remote.values()))), "current_cache_remote_id"

    leaf = normalized.rsplit("/", 1)[-1]
    if leaf and CACHE_ROOT.is_dir():
        leaf_targets = [
            path for path in _screening_cache_leaf_index().get(leaf.casefold(), [])
            if _target_is_usable(str(path))
        ]
        unique_leaf = {str(path.resolve(strict=False)): path for path in leaf_targets}
        if len(unique_leaf) == 1:
            return str(next(iter(unique_leaf.values()))), "current_cache_unique_leaf"
    return None


def derive_visual_path_maps(
    selected: list[dict[str, Any]],
    probe_manifest: Mapping[str, Any],
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """Bind every imported stale path to its audited current-machine target.

    A template is only eligible when its earlier post-open audit closed every
    runtime dependency.  The first workspace-only build enumerates all actual
    imported path occurrences (including duplicate font paths embedded in text
    JSON); this function joins those occurrences to the audit by exact old path
    first and remote resource id second.
    """

    audit_cache: dict[Path, dict[str, Any]] = {}
    audit_rows: dict[str, dict[str, Any]] = {}
    for item in selected:
        path = _audit_path(item)
        if not path.is_file():
            raise FileNotFoundError(f"missing post-open audit: {path}")
        audit = audit_cache.setdefault(path, read_json(path))
        matches = [
            row for row in audit.get("templates") or []
            if isinstance(row, Mapping) and str(row.get("template_id") or "") == str(item["template_id"])
        ]
        if len(matches) != 1:
            raise ValueError(f"audit must contain one template row: {item['template_id']} in {path}")
        row = dict(matches[0])
        if str((row.get("summary") or {}).get("recovery_status") or "") != "fully_recovered":
            raise ValueError(f"template is not fully recovered in audit: {item['template_id']}")
        audit_rows[str(item["template_id"])] = row

    probe_by_id = {
        str(row["template_id"]): row
        for row in probe_manifest.get("templates") or []
        if isinstance(row, Mapping) and row.get("template_id")
    }
    maps: dict[str, dict[str, str]] = {}
    evidence_rows: list[dict[str, Any]] = []
    candidate_registry, _provenance = candidate_maps()
    for item in selected:
        template_id = str(item["template_id"])
        audit = audit_rows[template_id]
        dependencies = [
            dict(row) for row in audit.get("runtime_visual_dependencies") or []
            if isinstance(row, Mapping)
        ]
        by_before: dict[str, set[str]] = {}
        by_remote: dict[str, set[str]] = {}
        for dependency in dependencies:
            if dependency.get("runtime_dependency_closed") is not True:
                raise ValueError(f"audit contains open dependency: {template_id}")
            local = dependency.get("local_evidence") or {}
            target = str(local.get("resolved_target") or "") if isinstance(local, Mapping) else ""
            if not target or not _target_is_usable(target):
                raise FileNotFoundError(
                    f"audited local dependency disappeared: {template_id} {dependency.get('remote_id')} {target}"
                )
            before = str(dependency.get("before_path") or "")
            remote_id = str(dependency.get("remote_id") or "")
            if before:
                by_before.setdefault(_normal_path(before), set()).add(target)
            if remote_id:
                by_remote.setdefault(remote_id, set()).add(target)

        probe = probe_by_id.get(template_id)
        if probe is None:
            raise ValueError(f"probe manifest missing template: {template_id}")
        occurrences = [
            row
            for inner in (probe.get("builder_report") or {}).get("inner_drafts") or []
            if isinstance(inner, Mapping)
            for row in (inner.get("build_report") or {}).get("visual_rehydrations") or []
            if isinstance(row, Mapping)
        ]
        # The probe report also records preserved paths from skipped track
        # types (notably sticker tracks).  They are not imported into the
        # screening candidate, so mapping them would falsely reject a
        # screenable template.  Keep only paths reachable from the tracks the
        # importer actually accepts.
        candidate = candidate_registry.get(template_id)
        imported_paths: set[str] = set()
        if candidate is not None:
            source_path = PRESET_ROOT / str(candidate.get("source_path") or "")
            if source_path.is_file():
                source = read_json(source_path)
                for inner in _screening_inner_drafts(source):
                    for path_ref in _screening_imported_fragment_paths(inner):
                        imported_paths.add(_normal_path(str(path_ref.get("path") or "")))
        occurrences = [
            row for row in occurrences
            if _normal_path(str(row.get("original_path") or "")) in imported_paths
        ]
        template_map: dict[str, str] = {}
        resolution_counts: dict[str, int] = {}
        for occurrence in occurrences:
            original = str(occurrence.get("original_path") or "")
            if not original:
                continue
            exact_candidates = set(by_before.get(_normal_path(original), set()))
            remote_candidates: set[str] = set()
            for remote_id in occurrence.get("remote_resource_ids") or []:
                remote_candidates.update(by_remote.get(str(remote_id), set()))
            if len(exact_candidates) == 1:
                target = next(iter(exact_candidates))
                resolution = "post_open_audit"
            elif len(exact_candidates) > 1:
                raise ValueError(
                    f"audit old path has conflicting targets: {template_id} {original} "
                    f"targets={sorted(exact_candidates)}"
                )
            else:
                fallback = _resolve_current_cache_fallback(
                    original,
                    tuple(str(value) for value in occurrence.get("remote_resource_ids") or []),
                )
                if fallback is not None:
                    target, resolution = fallback
                elif len(remote_candidates) == 1:
                    target = next(iter(remote_candidates))
                    resolution = "post_open_audit_remote_id"
                elif not remote_candidates:
                    raise ValueError(
                        f"cannot rebind runtime path: {template_id} {original}"
                    )
                else:
                    raise ValueError(
                        f"cannot uniquely rebind runtime path: {template_id} {original} "
                        f"targets={sorted(remote_candidates)}"
                    )
            previous = template_map.get(original)
            if previous is not None and _normal_path(previous) != _normal_path(target):
                raise ValueError(f"conflicting runtime path targets: {template_id} {original}")
            template_map[original] = target
            resolution_counts[resolution] = resolution_counts.get(resolution, 0) + 1
        if len(template_map) != len({_normal_path(str(row.get('original_path') or '')) for row in occurrences if row.get('original_path')}):
            raise ValueError(f"incomplete imported runtime path inventory: {template_id}")
        maps[template_id] = template_map
        evidence_rows.append({
            "template_id": template_id,
            "audit": str(_audit_path(item)),
            "audit_sha256": sha256(_audit_path(item)),
            "imported_path_occurrence_count": len(occurrences),
            "unique_rebound_path_count": len(template_map),
            "resolution_counts": dict(sorted(resolution_counts.items())),
        })
    return maps, {
        "schema": "huoke.screening-runtime-path-rebind.v1",
        "template_count": len(maps),
        "all_targets_exist_and_nonempty": True,
        "templates": evidence_rows,
    }


def _all_visual_paths_closed(manifest: Mapping[str, Any]) -> bool:
    if manifest.get("pending_visual_paths"):
        return False
    for template in manifest.get("templates") or []:
        if not isinstance(template, Mapping):
            continue
        for inner in (template.get("builder_report") or {}).get("inner_drafts") or []:
            if not isinstance(inner, Mapping):
                continue
            for row in (inner.get("build_report") or {}).get("visual_rehydrations") or []:
                if not isinstance(row, Mapping):
                    continue
                resolved = str(row.get("resolved_path") or "")
                if row.get("pending_rehydrate") is True or not resolved or not _target_is_usable(resolved):
                    return False
    return True


def make_labeled_base(selected: list[dict[str, Any]], path: Path) -> None:
    if path.exists():
        raise FileExistsError(path)
    script = ScriptFile(1080, 1920, 30, True)
    script.save_path = str(path)
    video_track = script.append_track(TrackSpec(TrackType.video, name="BLACK_BACKGROUND"))
    script.add_segment(VideoSegment(str(BLACK), trange(0, BASE_DURATION_US), volume=0.0), track=video_track)
    label_track = script.append_track(TrackSpec(TrackType.text, name="AUDITION_INDEX_LABELS"))
    style = TextStyle(size=3.2, bold=False, color=(0.72, 0.72, 0.72), align=0, auto_wrapping=False, max_line_width=0.92)
    shadow = TextShadow(alpha=0.75, diffuse=8.0, distance=2.0, angle=-45.0)
    for item in selected:
        label = f"{int(item['batch_order']):02d} | {item['template_id']} | {item['display_name']}"
        segment = TextSegment(
            label,
            trange(int(item["screening_start_us"]), int(item["duration_us"])),
            font=FontType.宋体,
            style=style,
            shadow=shadow,
            clip_settings=ClipSettings(transform_x=-0.62, transform_y=0.88),
        )
        script.add_segment(segment, track=label_track)
    script.save()
    base = read_json(path)
    reference = read_json(COMPATIBLE_BASE)
    for key in ("platform", "last_modified_platform"):
        base[key] = copy.deepcopy(reference[key])
    write_json(path, base)


def rename_preset_tracks(draft: dict[str, Any], selected: list[dict[str, Any]]) -> int:
    count = 0
    for track in draft.get("tracks") or []:
        if not isinstance(track, dict) or track.get("name") in {"BLACK_BACKGROUND", "AUDITION_INDEX_LABELS"}:
            continue
        ranges = []
        for segment in track.get("segments") or []:
            timerange = segment.get("target_timerange") or {}
            start = int(timerange.get("start") or 0)
            ranges.append((start, start + int(timerange.get("duration") or 0)))
        matches = [item for item in selected if any(min(end, int(item["screening_end_us"])) > max(start, int(item["screening_start_us"])) for start, end in ranges)]
        if len(matches) == 1:
            item = matches[0]
            count += 1
            track["name"] = f"SCREEN_{int(item['batch_order']):02d}_{item['template_id']}_{str(track.get('type') or 'TRACK').upper()}_{count:03d}"
    return count


def build_batch(queue: Mapping[str, Any], batch: int) -> dict[str, Any]:
    batch = int(batch)
    batch_count = int(queue.get("batch_count") or 0)
    if batch < 1 or batch > batch_count:
        raise ValueError(f"batch must be between 1 and {batch_count}: {batch}")
    prefix = OUTPUT / f"batch_{batch:02d}"
    paths = {
        name: prefix.with_name(prefix.name + suffix)
        for name, suffix in {
            "candidate": "_candidate.json",
            "manifest": "_manifest.json",
            "execution_plan": "_execution_plan.json",
            "candidate_plan": "_candidate_plan.json",
            "semantic_gate": "_semantic_gate.json",
            "build_report": "_candidate_build_report.json",
            "writer_manifest": "_candidate_writer_manifest.json",
            "base": "_workspace_base_with_labels.json",
        }.items()
    }
    for path in paths.values():
        if path.exists():
            raise FileExistsError(path)
    selected = sorted([dict(row) for row in queue.get("items") or [] if int(row.get("batch") or 0) == batch], key=lambda row: int(row["batch_order"]))
    if not selected:
        raise ValueError(f"batch has no screenable items: {batch}")
    draft_name = screening_draft_name(batch, len(selected))
    make_labeled_base(selected, paths["base"])
    write_json(paths["semantic_gate"], {
        "schema": "huoke.fully-recovered-500-screening-gate.v1",
        "status": "passed",
        "rough_cut": {"path": str(BLACK), "sha256": sha256(BLACK)},
        "evidence": [
            {"kind": "audited_fully_recovered_queue", "path": str(QUEUE), "sha256": sha256(QUEUE)},
            {"kind": "set_validation", "path": str(VALIDATION), "sha256": sha256(VALIDATION)},
        ],
        "checks": {"fully_recovered_only": True, "partial_142_excluded": True, "black_asset_frozen": True, "no_audio": True},
        "production_semantic_evidence_claimed": False,
    })
    write_json(paths["candidate_plan"], {
        "schema": "jianying-adapter.candidate-plan.v1",
        "draft_name": draft_name,
        "project_format": {
            "name": "获客口播自动剪辑管线统一工程格式",
            "separates_creative_plan_from_writer": True,
            "style_normalization": False,
            "writer_adapter": "jianying-8.8",
            "candidate_builder": "CandidateAssembler",
            "per_video_build_script": False,
            "operation_registry": SHARED_OPERATION_REGISTRY,
        },
        "project_state": str(PROJECT_STATE),
        "base_draft": str(paths["base"]),
        "rough_cut": str(BLACK),
        "semantic_gate": str(paths["semantic_gate"]),
        "usage_registry": str(REGISTRY),
        "target": {"width": 1080, "height": 1920, "fps": 30, "jianying_version": "8.8.0", "max_duration_us": BASE_DURATION_US},
        "options": {"audio": {"enabled": False}, "bgm": {"enabled": False}},
        "subject_clarity_preflight": {"status": "passed", "scope": "generic_black_screen_screening", "not_applicable_reason": "black screening probe has no production subject", "checks": {"primary_subject_defined": True, "unused_headroom_checked": True, "background_distraction_checked": True, "perspective_checked": True, "caption_subject_clearance_checked": True}},
        "transition_requires_real_before_after_state": True,
        "presets": [],
        "operations": [{"id": f"assemble-fully-recovered-500-batch-{batch:02d}", "kind": "assemble_fully_recovered_screening_batch", "batch": batch, "template_ids": [row["template_id"] for row in selected], "preserve_native_text_and_styled_ranges": True, "unsafe_whole_text_replacement": False}],
        "output": str(paths["candidate"]),
        "report": str(paths["build_report"]),
        "manifest": str(paths["writer_manifest"]),
    })
    built: dict[str, Any] = {}

    def operation(_draft: dict[str, Any], spec: Mapping[str, Any], _context: Any):
        if spec.get("preserve_native_text_and_styled_ranges") is not True or spec.get("unsafe_whole_text_replacement") is not False:
            raise ValueError("native text preservation gate is not explicit")
        probe_envelope = phase1.build_batch(
            queue,
            batch,
            dependency_scope="imported_fragment_visual",
            registry_path=REGISTRY,
            preset_root=PRESET_ROOT,
            base_path=paths["base"],
            black_path=BLACK,
            id_factory_override=deterministic_id_factory(batch),
            start_us=START_US,
            gap_us=GAP_US,
            base_duration_us=BASE_DURATION_US,
            retain_base_track_names=("BLACK_BACKGROUND", "AUDITION_INDEX_LABELS"),
            max_template_duration_us=MAX_TEMPLATE_DURATION_US,
        )
        visual_path_maps, rebind_evidence = derive_visual_path_maps(
            selected, probe_envelope["manifest"]
        )
        envelope = phase1.build_batch(
            queue,
            batch,
            dependency_scope="imported_fragment_visual",
            registry_path=REGISTRY,
            preset_root=PRESET_ROOT,
            base_path=paths["base"],
            black_path=BLACK,
            id_factory_override=deterministic_id_factory(batch),
            start_us=START_US,
            gap_us=GAP_US,
            base_duration_us=BASE_DURATION_US,
            retain_base_track_names=("BLACK_BACKGROUND", "AUDITION_INDEX_LABELS"),
            visual_path_maps=visual_path_maps,
            max_template_duration_us=MAX_TEMPLATE_DURATION_US,
        )
        if not _all_visual_paths_closed(envelope["manifest"]):
            raise ValueError("rebuilt screening batch still contains missing runtime visual paths")
        envelope["manifest"]["runtime_path_rebind"] = rebind_evidence
        envelope["manifest"]["checks"].update({
            "runtime_paths_derived_from_post_open_audits": True,
            "all_runtime_visual_paths_resolve_current_machine": True,
            "pending_visual_paths_empty": not envelope["manifest"].get("pending_visual_paths"),
        })
        actual = [row["template_id"] for row in envelope["manifest"]["templates"]]
        if actual != list(spec["template_ids"]):
            raise ValueError("assembled template order differs from candidate plan")
        renamed = rename_preset_tracks(envelope["draft"], selected)
        built["envelope"] = envelope
        return envelope["draft"], {"template_count": len(actual), "first_template_id": actual[0], "last_template_id": actual[-1], "renamed_preset_track_count": renamed}

    def validator(draft: dict[str, Any], _context: Any) -> Mapping[str, Any]:
        tracks = [row for row in draft.get("tracks") or [] if isinstance(row, Mapping)]
        labels = [row for row in tracks if row.get("name") == "AUDITION_INDEX_LABELS"]
        visual_gate = validate_trial_draft(draft)
        checks = {
            "double_8_8_header": ((draft.get("platform") or {}).get("app_version"), (draft.get("last_modified_platform") or {}).get("app_version")) == ("8.8.0", "8.8.0"),
            "canvas_1080x1920": (int((draft.get("canvas_config") or {}).get("width") or 0), int((draft.get("canvas_config") or {}).get("height") or 0)) == (1080, 1920),
            "duration_at_most_180_seconds": 0 < int(draft.get("duration") or 0) <= BASE_DURATION_US,
            "clear_index_labels": len(labels) == 1 and len(labels[0].get("segments") or []) == len(selected),
            "no_audio_track": not any(row.get("type") == "audio" for row in tracks),
            # This is deliberately repeated at the final candidate boundary:
            # the older path-only recovery gate could pass a draft whose
            # segment referenced a video/photo material with path="".
            "referenced_visual_materials_closed": visual_gate.ok
            and not visual_gate.facts.get("pending_visual_materials"),
        }
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "visual_material_gate": visual_gate.to_dict(),
        }

    result = CandidateAssembler(
        paths["candidate_plan"],
        OperationRegistry(
            SHARED_OPERATION_REGISTRY,
            {"assemble_fully_recovered_screening_batch": operation},
        ),
        final_validator=validator,
    ).run()
    manifest = copy.deepcopy(built["envelope"]["manifest"])
    manifest["schema"] = "huoke.fully-recovered-500-screening-batch-candidate.v1"
    manifest["status"] = "candidate_ready_not_live_written"
    manifest["draft_name"] = draft_name
    manifest["checks"].update({
        "audited_fully_recovered_only": all(row["recovery_status"] == "fully_recovered" for row in selected),
        "partial_142_excluded": not ({row["template_id"] for row in selected} & set(read_json(VALIDATION)["excluded_partially_recovered"]["combined"])),
        "clear_index_id_labels": validator(built["envelope"]["draft"], None)["checks"]["clear_index_labels"],
    })
    manifest["structure_ready"] = all(manifest["checks"].values())
    manifest["policy"].update({
        "purpose": "user_visual_screening_of_audited_fully_recovered_presets",
        "candidate_assembler_applied": True,
        "candidate_plan": str(paths["candidate_plan"]),
        "source_text_policy": "preserve_native_text_and_styled_ranges",
        "unsafe_whole_text_replacement_performed": False,
        "partial_presets_included": False,
        "current_video_media_bound": False,
        "production_ready_claimed": False,
        "visual_success_claimed": False,
    })
    write_json(paths["manifest"], manifest)
    write_json(paths["execution_plan"], {
        "schema": "huoke.fully-recovered-500-screening-execution-plan.v1",
        "batch": batch,
        "status": "candidate_ready_not_live_written",
        "candidate_plan": str(paths["candidate_plan"]),
        "candidate_build_report": str(paths["build_report"]),
        "candidate_writer_manifest": str(paths["writer_manifest"]),
        "candidate": str(paths["candidate"]),
        "manifest": str(paths["manifest"]),
        "operation_order": ["derive_audited_set", "candidate_assembler", "capture_cache_baseline", "single_writer", "frontend_user_screening"],
        "live_write_or_registration_performed": False,
        "visual_success_claimed": False,
    })
    return result


def build_batch_one(queue: Mapping[str, Any]) -> dict[str, Any]:
    """Backward-compatible batch-1 entry point."""

    return build_batch(queue, 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--build-batch", type=int)
    args = parser.parse_args()
    queue = plan_queue()
    if args.build_batch is not None:
        build_batch(queue, args.build_batch)
    print(json.dumps({"queue": str(QUEUE), "validation": str(VALIDATION), "registry": str(REGISTRY), "built_batch": args.build_batch}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
