"""Build an isolated engineering-adaptation plan for the remaining 357 presets.

The v1 catalog, the phase-1 workbench, source presets, project state, and
Memos are read-only inputs.  This command only writes a v3 candidate ledger
and auditable manifests; it never edits a draft or claims that an adaptation
is current-video ready without evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

_ADAPTER_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _ADAPTER_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from jianying_adapter.preset_catalog import _record_from_file
from jianying_adapter.preset_registry import _build_candidate, _inner_drafts

try:
    from scripts.repair_preset_resources_phase1 import (
        CURRENT_CACHE,
        SOURCE_ROOT,
        dependency_decisions,
        local_file_index,
    )
except ModuleNotFoundError:  # direct ``python scripts/<tool>.py`` invocation
    from repair_preset_resources_phase1 import (  # type: ignore[no-redef]
        CURRENT_CACHE,
        SOURCE_ROOT,
        dependency_decisions,
        local_file_index,
    )


ADAPTER = _ADAPTER_ROOT
PROJECT = ADAPTER.parent
CATALOG = ADAPTER / "preset_catalog"
V1_PATH = CATALOG / "standardized_preset_catalog_v1.json"
AUTO_PATH = CATALOG / "all_auto_candidates_v1.json"
PHASE1_MANIFEST = ADAPTER / "repair_workspaces" / "phase1_resource_repair_20260903" / "repair_manifest_phase1.json"
WORKBENCH = ADAPTER / "repair_workspaces" / "phase2_engineering_adaptation_20260903"
MANIFEST_PATH = WORKBENCH / "adaptation_manifest_phase2.json"
AUDIT_PATH = WORKBENCH / "phase2_audit.json"
V3_PATH = WORKBENCH / "standardized_preset_catalog_v3_candidate.json"
RECOVERY_PATH = WORKBENCH / "recovery.md"
COMPLETE_PATH = WORKBENCH / "fully_repaired_phase2.json"
PARTIAL_PATH = WORKBENCH / "partially_adapted_phase2.json"
FAILED_PATH = WORKBENCH / "failed_phase2.json"
RECLASSIFIED_PATH = WORKBENCH / "reclassified_non_text_phase2.json"
RESCANNED_CANDIDATES_PATH = WORKBENCH / "rescanned_source_candidates_phase2.json"

TARGET_CANVAS = {"width": 1080, "height": 1920}
EXCLUDED_REASON_SUFFIXES = ("_hold",)


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


def candidate_rows(value: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(row.get("template_id")): row
        for row in value.get("candidates", [])
        if isinstance(row, Mapping) and row.get("template_id")
    }


def phase2_targets(ledger: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for record in ledger.get("records", []):
        if not isinstance(record, Mapping) or record.get("production", {}).get("status") != "blocked":
            continue
        reasons = [str(reason) for reason in record.get("production", {}).get("blocked_reasons") or []]
        if any(reason.endswith(EXCLUDED_REASON_SUFFIXES) for reason in reasons):
            continue
        if "visual_rejected" in reasons or "source_package_missing" in reasons:
            continue
        if reasons == ["resource_missing"]:
            continue
        rows.append(record)
    return sorted(rows, key=lambda row: str(row.get("template_id")))


def canvas_rows(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in record.get("canvas") or [] if isinstance(row, Mapping)]


def _candidate_recovery(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    value = candidate.get("phase2_source_rescan")
    return value if isinstance(value, Mapping) else {}


def canvas_plan(record: Mapping[str, Any], candidate: Mapping[str, Any] | None = None) -> dict[str, Any]:
    rows = canvas_rows(record)
    if not rows and candidate:
        rows = [
            row
            for row in _candidate_recovery(candidate).get("canvases") or []
            if isinstance(row, Mapping)
        ]
    if not rows:
        return {
            "status": "failed_unresolved",
            "source_canvas": [],
            "variant": None,
            "proof": {"inner_bounds_proven": False, "reason": "canvas_unknown"},
        }
    source = rows[0]
    width = int(source.get("width") or 0)
    height = int(source.get("height") or 0)
    if width <= 0 or height <= 0:
        return {
            "status": "failed_unresolved",
            "source_canvas": [dict(row) for row in rows],
            "variant": None,
            "proof": {"inner_bounds_proven": False, "reason": "canvas_unknown"},
        }
    if width == TARGET_CANVAS["width"] and height == TARGET_CANVAS["height"]:
        return {
            "status": "no_canvas_change",
            "source_canvas": [dict(row) for row in rows],
            "variant": {"width": 1080, "height": 1920, "scale": 1.0, "translate": {"x": 0.0, "y": 0.0}},
            "proof": {"inner_bounds_proven": True, "reason": "already_target_canvas"},
        }
    scale = min(TARGET_CANVAS["width"] / width, TARGET_CANVAS["height"] / height)
    scaled_width = width * scale
    scaled_height = height * scale
    return {
        "status": "adaptation_pending",
        "source_canvas": [dict(row) for row in rows],
        "variant": {
            "width": 1080,
            "height": 1920,
            "operation": "group_scale_translate_only",
            "scale": round(scale, 12),
            "translate": {
                "x": round((1080 - scaled_width) / 2, 6),
                "y": round((1920 - scaled_height) / 2, 6),
            },
            "preserve_internal_ratio": True,
            "preserve_animation_structure": True,
        },
        "proof": {
            "inner_bounds_proven": False,
            "reason": "no_inner_draft_render_or_bounds_evidence",
            "boundary_policy": "adaptation_pending_until_bounds_proven",
        },
    }


def _text_tracks_from_rescanned_candidate(candidate: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    slots = [
        row
        for row in (candidate.get("slots") or {}).get("text") or []
        if isinstance(row, Mapping)
    ]
    if not slots:
        return [], ["inner_text_tracks_not_enumerable"]
    reasons: list[str] = []
    rows: list[dict[str, Any]] = []
    editable_index = 0
    for index, slot in enumerate(slots, 1):
        default_text = str(slot.get("default_text") or "")
        locked = bool(slot.get("decorative_locked"))
        if locked:
            role = "fixed_symbol" if default_text.strip() and not any(ch.isalnum() for ch in default_text) else "decorative"
        else:
            role = "main_emphasis" if editable_index == 0 else "supporting"
            editable_index += 1
        locators = slot.get("locators") or ([slot.get("locator")] if isinstance(slot.get("locator"), Mapping) else [])
        if not locators:
            reasons.append(f"text_locator_missing:{index}")
        synchronized = bool(slot.get("requires_manual_slot_mapping"))
        styled_parts = bool(slot.get("requires_manual_style_mapping"))
        rows.append({
            "track_index": index,
            "slot_id": slot.get("slot_id") or f"text_{index:02d}",
            "role": role,
            "editable": not locked,
            "fixed": locked,
            "capacity_max_chars": len(default_text.strip()) or 1,
            "capacity_source": "default_text_length_lower_bound",
            "default_text": default_text,
            "locators": locators,
            "slot_mapping_mode": "synchronized_locator_group" if synchronized else "direct_locator",
            "style_mapping_mode": "explicit_styled_parts_required" if styled_parts else "whole_text",
            "mapping_contract_required": synchronized or styled_parts,
        })
    if not any(row["role"] == "main_emphasis" for row in rows):
        reasons.append("main_emphasis_missing")
    return rows, list(dict.fromkeys(reasons))


def text_mapping_plan(record: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    tracks = [row for row in record.get("text_tracks") or [] if isinstance(row, Mapping)]
    slots = [row for row in (candidate.get("slots") or {}).get("text") or [] if isinstance(row, Mapping)]
    reasons: list[str] = []
    recovered_from_source = False
    if not tracks and _candidate_recovery(candidate).get("status") == "rescanned":
        recovered_tracks, recovered_reasons = _text_tracks_from_rescanned_candidate(candidate)
        if recovered_tracks:
            recovered_from_source = True
            pending_contracts: list[str] = []
            for track in recovered_tracks:
                if track.get("slot_mapping_mode") == "synchronized_locator_group":
                    pending_contracts.append("synchronized_group_binding_pending")
                if track.get("style_mapping_mode") == "explicit_styled_parts_required":
                    pending_contracts.append("styled_parts_binding_pending")
            return {
                "status": "enumerated_pending_capacity_measurement" if not recovered_reasons else "failed_unresolved",
                "enumeration_source": "read_only_source_rescan_inner_draft",
                "tracks": recovered_tracks,
                "reasons": recovered_reasons,
                "capacity_proven": False,
                "recovered_from_source": True,
                "pending_contracts": list(dict.fromkeys(pending_contracts)),
            }
    if not tracks or not slots:
        reasons.append("inner_text_tracks_not_enumerable")
    elif len(tracks) != len(slots):
        reasons.append("text_track_count_mismatch")
    enumerated: list[dict[str, Any]] = []
    for index, track in enumerate(tracks):
        role = str(track.get("role") or "")
        capacity = track.get("max_chars")
        if role not in {"main_emphasis", "supporting", "fixed_symbol", "decorative"}:
            reasons.append(f"unknown_text_role:{index + 1}")
        capacity_pending = capacity is None or int(capacity or 0) <= 0
        slot = slots[index] if index < len(slots) else {}
        enumerated.append({
            "track_index": track.get("track_index", index + 1),
            "slot_id": track.get("slot_id") or slot.get("slot_id") or f"text_{index + 1:02d}",
            "role": role or None,
            "editable": bool(track.get("editable")),
            "fixed": not bool(track.get("editable")),
            "capacity_max_chars": int(capacity) if capacity is not None and str(capacity).isdigit() else capacity,
            "capacity_source": "measured_contract" if not capacity_pending else "pending_measurement",
            "default_text": track.get("default_text", slot.get("default_text")),
            "locators": track.get("locators") or slot.get("locators") or [],
        })
    if not recovered_from_source and "manual_mapping_required" in (record.get("production", {}).get("blocked_reasons") or []):
        reasons.append("manual_mapping_required")
    return {
        "status": (
            "failed_unresolved"
            if reasons
            else "enumerated_pending_capacity_measurement"
            if any(row.get("capacity_source") == "pending_measurement" for row in enumerated)
            else "enumerated"
        ),
        "enumeration_source": "standardized_v1_inner_draft_projection",
        "tracks": enumerated,
        "reasons": list(dict.fromkeys(reasons)),
        "capacity_proven": not any(row.get("capacity_source") == "pending_measurement" for row in enumerated),
        "recovered_from_source": False,
        "pending_contracts": [],
    }


def is_non_text_visual_candidate(record: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    if (record.get("text_tracks") or []) or ((candidate.get("slots") or {}).get("text") or []):
        return False
    display_name = str(candidate.get("display_name") or record.get("display_name") or "")
    visual_slots = sum(len((candidate.get("slots") or {}).get(kind) or []) for kind in ("image", "video"))
    tags = {str(value) for value in candidate.get("semantic_tags") or []}
    return "背景" in display_name or visual_slots > 0 or "broll_visual" in tags


def rescan_source_candidate(record: Mapping[str, Any], source_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source = record.get("source") or {}
    source_value = source.get("source_path_absolute") or source.get("source_path")
    if not source_value:
        return {}, {"status": "not_attempted", "reason": "source_path_missing"}
    path = Path(str(source_value))
    if not path.is_absolute():
        path = source_root / path
    if not path.is_file():
        return {}, {"status": "failed", "reason": "source_file_missing", "source_path": str(path)}
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
        scan_record, warnings = _record_from_file(source_root, path, raw, payload)
        candidate = _build_candidate(scan_record, path, source_root, cluster_frequency=1)
        candidate["template_id"] = str(record.get("template_id"))
        canvases: list[dict[str, Any]] = []
        seen: set[tuple[int, int, str]] = set()
        for draft in _inner_drafts(payload):
            config = draft.get("canvas_config") or {}
            width = int(config.get("width") or 0)
            height = int(config.get("height") or 0)
            ratio = str(config.get("ratio") or "")
            if width <= 0 or height <= 0:
                continue
            key = (width, height, ratio)
            if key in seen:
                continue
            seen.add(key)
            canvases.append({"width": width, "height": height, "ratio": ratio})
        text_slot_count = len((candidate.get("slots") or {}).get("text") or [])
        recovery = {
            "status": "rescanned",
            "source_path": str(path),
            "source_hash": scan_record.get("exact_file_sha256"),
            "structure_hash": scan_record.get("normalized_structure_sha256"),
            "uses_nested_draft": bool(scan_record.get("uses_nested_draft")),
            "canvases": canvases,
            "text_slot_count": text_slot_count,
            "manual_slot_count": int(candidate.get("manual_slot_count") or 0),
            "manual_style_slot_count": int(candidate.get("manual_style_slot_count") or 0),
            "auto_fill_ready": bool(candidate.get("auto_fill_ready")),
            "warnings": warnings,
            "source_mutation_performed": False,
        }
        candidate["phase2_source_rescan"] = recovery
        return candidate, recovery
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {}, {"status": "failed", "reason": type(exc).__name__, "message": str(exc), "source_path": str(path)}


def media_binding_plan(record: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    slots = candidate.get("slots") or {}
    bindings: list[dict[str, Any]] = []
    missing = False
    expected_total = sum(int((record.get("slot_counts") or {}).get(kind) or 0) for kind in ("image", "video"))
    candidate_total = sum(
        len([row for row in slots.get(kind) or [] if isinstance(row, Mapping)])
        for kind in ("image", "video")
    )
    for kind in ("image", "video"):
        rows = [row for row in slots.get(kind) or [] if isinstance(row, Mapping)]
        expected = int((record.get("slot_counts") or {}).get(kind) or 0)
        if expected and len(rows) != expected:
            missing = True
        for index, slot in enumerate(rows):
            default = slot.get("default_resource") or {}
            bindings.append({
                "kind": kind,
                "slot_id": slot.get("slot_id") or f"{kind}_{index + 1:02d}",
                "required": bool(slot.get("required")),
                "placeholder_required": True,
                "placeholder": default,
                "source_basename": default.get("source_basename"),
                "bound_current_video_asset": False,
            })
    if not bindings and expected_total:
        missing = True
    if expected_total == 0 and candidate_total == 0:
        return {
            "status": "not_required",
            "bindings": [],
            "requires_current_video_assets": False,
            "placeholder_policy": "not_applicable",
            "reasons": [],
        }
    return {
        "status": "binding_pending" if not missing else "failed_unresolved",
        "bindings": bindings,
        "requires_current_video_assets": True,
        "placeholder_policy": "explicit_placeholder_or_user_asset_required",
        "reasons": ["media_slot_enumeration_incomplete"] if missing else [],
    }


def resource_plan(record: Mapping[str, Any], candidate: Mapping[str, Any], index: Mapping[str, list[Path]]) -> dict[str, Any]:
    reasons = record.get("production", {}).get("blocked_reasons") or []
    if "resource_missing" not in reasons:
        return {
            "status": "not_required",
            "phase1_policy": "not_applicable",
            "complete_proof": True,
            "dependency_decisions": [],
        }
    decisions, counts = dependency_decisions(candidate, local_index=index)
    return {
        "status": "inherited_phase1_policy_pending",
        "phase1_policy": "reuse_phase1_evidence_without_deleting_remote_dependencies",
        "phase1_scope": "not_exact_resource_missing_only",
        "complete_proof": bool(decisions) and all(item["resolution"] == "local_resource_resolvable" for item in decisions),
        "dependency_decisions": decisions,
        "decision_counts": counts,
        "remote_dependencies_preserved": True,
    }


def primary_group(reasons: list[str]) -> str:
    if "canvas_unknown" in reasons or "manual_mapping_required" in reasons or "text_track_contract_missing" in reasons:
        return "inner_draft_mapping"
    if "media_slot_present" in reasons:
        return "media_binding"
    if "canvas_not_9_16_1080x1920" in reasons:
        return "canvas_adaptation"
    if "resource_missing" in reasons:
        return "resource_reuse"
    return "other"


def build_phase2(
    *,
    v1_path: Path = V1_PATH,
    auto_path: Path = AUTO_PATH,
    phase1_manifest_path: Path = PHASE1_MANIFEST,
    local_roots: Iterable[Path] = (SOURCE_ROOT, CURRENT_CACHE),
    source_root: Path = SOURCE_ROOT,
    enforce_target_count: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    ledger = read_json(v1_path)
    auto = candidate_rows(read_json(auto_path))
    targets = phase2_targets(ledger)
    if enforce_target_count and len(targets) != 357:
        raise ValueError(f"第二阶段目标数应为357，实际为 {len(targets)}")
    phase1_rows = {
        str(row.get("template_id")): row
        for row in (read_json(phase1_manifest_path).get("records") if phase1_manifest_path.is_file() else [])
        if isinstance(row, Mapping) and row.get("template_id")
    }
    resource_index = local_file_index(local_roots)
    v3 = copy.deepcopy(ledger)
    v3["schema"] = "jianying-adapter.standardized-preset-catalog.v3-candidate"
    v3["generator"] = "adapt_preset_catalog_phase2.py"
    v3["phase2_scope"] = {
        "target_count": len(targets),
        "target_filter": "blocked minus version_*_hold, visual_rejected, source_package_missing, exact [resource_missing]",
        "source_ledger_is_read_only": True,
        "draft_written_or_registered": False,
    }
    v3_by_id = {str(row.get("template_id")): row for row in v3.get("records", []) if isinstance(row, Mapping)}
    rows: list[dict[str, Any]] = []
    rescanned_candidates: list[dict[str, Any]] = []
    rescan_counts: Counter[str] = Counter()
    for record in targets:
        template_id = str(record.get("template_id"))
        reasons = [str(reason) for reason in record.get("production", {}).get("blocked_reasons") or []]
        candidate = dict(auto.get(template_id, {}))
        source_rescan = {"status": "not_needed", "reason": "candidate_present"}
        if not candidate:
            candidate, source_rescan = rescan_source_candidate(record, source_root)
        if source_rescan.get("status") == "rescanned" and candidate:
            rescanned_candidates.append(copy.deepcopy(candidate))
        rescan_counts[str(source_rescan.get("status") or "unknown")] += 1
        canvas = canvas_plan(record, candidate)
        text = text_mapping_plan(record, candidate)
        non_text_visual = is_non_text_visual_candidate(record, candidate)
        if non_text_visual:
            text = {
                "status": "not_applicable_non_text_visual",
                "enumeration_source": text.get("enumeration_source"),
                "tracks": [],
                "reasons": [],
                "capacity_proven": True,
                "recovered_from_source": bool(text.get("recovered_from_source")),
                "pending_contracts": [],
            }
        media = media_binding_plan(record, candidate)
        resources = resource_plan(record, candidate, resource_index)
        failures = list(text["reasons"])
        if canvas["status"] == "failed_unresolved":
            failures.append(canvas["proof"]["reason"])
        if media["status"] == "failed_unresolved":
            failures.extend(media["reasons"])
        if "resource_missing" in reasons and not resources["complete_proof"]:
            # A known dependency graph can be adapted safely while resource
            # hydration remains pending.  Missing graph evidence is a hard
            # failure because it cannot support a truthful binding plan.
            if not resources["dependency_decisions"]:
                failures.append("resource_dependency_evidence_unavailable")
        failures = list(dict.fromkeys(failures))
        pending: list[str] = []
        if canvas["status"] == "adaptation_pending":
            pending.append("canvas_boundary_proof_pending")
        if media["status"] == "binding_pending":
            pending.append("current_video_media_binding_pending")
        if "resource_missing" in reasons and not resources["complete_proof"]:
            pending.append("resource_rehydration_pending")
        if text.get("status") == "enumerated_pending_capacity_measurement":
            pending.append("text_capacity_measurement_pending")
        pending.extend(str(value) for value in text.get("pending_contracts") or [])
        pending = list(dict.fromkeys(pending))
        if non_text_visual:
            repair_class = "reclassified"
            status = "catalog_reclassified_non_text_visual"
        elif failures:
            repair_class = "failed"
            status = "failed_unresolved"
        elif pending:
            repair_class = "partial"
            status = "adapter_ready_pending_current_video"
        else:
            repair_class = "complete"
            status = "production_ready"
        row = {
            "template_id": template_id,
            "source_hash": record.get("source", {}).get("source_hash"),
            "blocked_reasons_before": reasons,
            "primary_group": primary_group(reasons),
            "groups": {
                "canvas": canvas,
                "text_mapping": text,
                "media_binding": media,
                "resources": resources,
            },
            "source_rescan": source_rescan,
            "catalog_classification": "non_text_visual_asset" if non_text_visual else "text_or_motion_preset",
            "pending": pending,
            "failures": failures,
            "repair_class": repair_class,
            "phase2_status": status,
            "source_mutation_performed": False,
            "draft_written_or_registered": False,
        }
        rows.append(row)
        v3_record = v3_by_id[template_id]
        v3_record["phase2_adaptation"] = {
            "repair_class": repair_class,
            "status": status,
            "primary_group": row["primary_group"],
            "manifest_template_id": template_id,
            "remote_dependencies_preserved": True,
            "source_mutation_performed": False,
            "draft_written_or_registered": False,
        }
        v3_record["production"]["phase2_status"] = status
        v3_record["production"]["phase2_ready_for_jianying_8_8"] = status == "production_ready"
    counts = Counter(row["repair_class"] for row in rows)
    group_audit: dict[str, Any] = {}
    for group in sorted({row["primary_group"] for row in rows}):
        group_rows = [row for row in rows if row["primary_group"] == group]
        group_audit[group] = {
            "count": len(group_rows),
            "complete": sum(row["repair_class"] == "complete" for row in group_rows),
            "partial": sum(row["repair_class"] == "partial" for row in group_rows),
            "failed": sum(row["repair_class"] == "failed" for row in group_rows),
            "reclassified": sum(row["repair_class"] == "reclassified" for row in group_rows),
        }
    before_ready = sum(row.get("production", {}).get("status") == "ready" for row in ledger.get("records", []))
    audit = {
        "schema": "jianying-adapter.preset-engineering-adaptation-audit.v1",
        "scope": {
            "source_ledger": str(v1_path),
            "target_filter": "blocked minus version_*_hold, visual_rejected, source_package_missing, exact [resource_missing]",
            "target_count": len(rows),
            "target_count_expected": 357,
        },
        "counts": {
            "fully_repaired": counts["complete"],
            "partially_adapted": counts["partial"],
            "failed": counts["failed"],
            "reclassified_non_text": counts["reclassified"],
            "ready_total_before": before_ready,
            "ready_total_after_candidate": sum(
                row.get("production", {}).get("status") == "ready" for row in v3.get("records", [])
            ),
        },
        "groups": group_audit,
        "resource_policy": {
            "phase1_manifest": str(phase1_manifest_path),
            "phase1_manifest_available": bool(phase1_rows),
            "remote_dependencies_deleted": False,
            "resource_missing_never_promoted_to_ready": True,
        },
        "source_rescan": dict(sorted(rescan_counts.items())),
        "v1_untouched": True,
        "source_presets_untouched": True,
        "project_state_untouched": True,
        "memos_untouched": True,
        "draft_written_or_registered": False,
        "source_snapshot": {
            "v1_sha256": file_sha256(v1_path),
            "auto_sha256": file_sha256(auto_path),
            "phase1_manifest_sha256": file_sha256(phase1_manifest_path) if phase1_manifest_path.is_file() else None,
        },
    }
    complete = {"schema": "jianying-adapter.phase2-fully-repaired.v1", "records": [row for row in rows if row["repair_class"] == "complete"]}
    partial = {"schema": "jianying-adapter.phase2-partially-adapted.v1", "records": [row for row in rows if row["repair_class"] == "partial"]}
    failed = {"schema": "jianying-adapter.phase2-failed.v1", "records": [row for row in rows if row["repair_class"] == "failed"]}
    reclassified = {"schema": "jianying-adapter.phase2-reclassified-non-text.v1", "records": [row for row in rows if row["repair_class"] == "reclassified"]}
    return v3, audit, {"schema": "jianying-adapter.phase2-adaptation-manifest.v1", "records": rows}, {"complete": complete, "partial": partial, "failed": failed, "reclassified": reclassified, "rescanned_candidates": {"schema": "jianying-adapter.phase2-rescanned-source-candidates.v1", "candidates": rescanned_candidates}}


def recovery_markdown(audit: Mapping[str, Any]) -> str:
    counts = audit["counts"]
    return "\n".join([
        "# 预设工程适配第二阶段恢复说明",
        "",
        "本阶段只读取 standardized_preset_catalog_v1，并在隔离工位生成适配计划与 v3 候选；不覆盖 v1/v2、源预设、project_state 或 Memo，不写入/注册真实剪映草稿。",
        "",
        f"- 目标：{audit['scope']['target_count']} 条（期望357）。完全修复：{counts['fully_repaired']}；部分适配：{counts['partially_adapted']}；失败：{counts['failed']}；改归为非文字视觉资产：{counts['reclassified_non_text']}。",
        f"- ready 总数：{counts['ready_total_before']} → {counts['ready_total_after_candidate']}；未把资源缺失、媒体绑定或未证明画布边界提升为 production_ready。",
        "- 非目标 285 条仅 resource_missing 记录不在本阶段；version_*_hold、visual_rejected、source_package_missing 也未进入本阶段。",
        "- 非9:16记录仅生成整组 scale/translate 变体计划，保留内部比例和动画结构意图；没有内层渲染/边界证据时保持 adaptation_pending。",
        "- 图片/视频槽只生成逐槽绑定契约与占位要求，未绑定当前视频素材，因此不能伪装成纯文字 ready。",
        "- 资源缺失沿用第一阶段的保守策略，保留远程依赖；本工具不会删除路径或把导入成功当资源完整。",
        "",
        "恢复方式：保留本目录全部 JSON/Markdown；删除或移动本工位不会影响源文件。输入未变时可重复运行：",
        "`python jianying-adapter/scripts/adapt_preset_catalog_phase2.py`",
        "",
    ])


def main() -> None:
    v3, audit, manifest, lists = build_phase2()
    write_json(MANIFEST_PATH, manifest)
    write_json(AUDIT_PATH, audit)
    write_json(V3_PATH, v3)
    write_json(COMPLETE_PATH, lists["complete"])
    write_json(PARTIAL_PATH, lists["partial"])
    write_json(FAILED_PATH, lists["failed"])
    write_json(RECLASSIFIED_PATH, lists["reclassified"])
    write_json(RESCANNED_CANDIDATES_PATH, lists["rescanned_candidates"])
    RECOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECOVERY_PATH.write_text(recovery_markdown(audit), encoding="utf-8")
    print(json.dumps({"workbench": str(WORKBENCH), **audit["counts"], "target_count": audit["scope"]["target_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
