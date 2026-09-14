"""Build the non-live execution queue for the 357 phase-2 presets.

The queue separates generic preset verification from current-video binding.
It does not write or register a Jianying draft and does not promote anything
to production-ready.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


ADAPTER = Path(__file__).resolve().parents[1]
PROJECT = ADAPTER.parent
WORKBENCH = ADAPTER / "repair_workspaces/phase2_engineering_adaptation_20260903"
MANIFEST = WORKBENCH / "adaptation_manifest_phase2.json"
AUTO_CANDIDATES = ADAPTER / "preset_catalog/all_auto_candidates_v1.json"
RESCANNED_CANDIDATES = WORKBENCH / "rescanned_source_candidates_phase2.json"
OUTPUT_ROOT = PROJECT / "video_trials/字幕预设黑幕筛选_20260902/phase2_engineering_verification"
OUTPUT = OUTPUT_ROOT / "phase2_357_verification_queue.json"
BATCH_SIZE = 50
START_US = 1_000_000
GAP_US = 500_000
MAX_BATCH_DURATION_US = 180_000_000


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def candidate_map(*values: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        for row in value.get("candidates") or []:
            if isinstance(row, Mapping) and row.get("template_id"):
                result[str(row["template_id"])] = row
    return result


def duration_map(candidates: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    return {
        template_id: int((row.get("duration") or {}).get("microseconds") or 0)
        for template_id, row in candidates.items()
    }


def build_queue(
    manifest: Mapping[str, Any],
    duration_by_id: Mapping[str, int],
    *,
    batch_size: int = BATCH_SIZE,
    max_batch_duration_us: int = MAX_BATCH_DURATION_US,
) -> dict[str, Any]:
    records = [row for row in manifest.get("records") or [] if isinstance(row, Mapping)]
    ids = [str(row.get("template_id") or "") for row in records]
    if len(records) != 357 or len(set(ids)) != 357 or not all(ids):
        raise ValueError(f"phase2 scope must be 357 unique templates, got records={len(records)} unique={len(set(ids))}")
    items: list[dict[str, Any]] = []
    batch = 1
    batch_order = 0
    cursor_us = START_US
    for order, row in enumerate(records, 1):
        pending = [str(value) for value in row.get("pending") or []]
        template_id = ids[order - 1]
        duration_us = int(duration_by_id.get(template_id) or 0)
        if duration_us <= 0:
            raise ValueError(f"missing positive duration for {template_id}")
        if START_US + duration_us > max_batch_duration_us:
            raise ValueError(f"template exceeds batch duration: {template_id} duration={duration_us}")
        if batch_order >= batch_size or cursor_us + duration_us > max_batch_duration_us:
            batch += 1
            batch_order = 0
            cursor_us = START_US
        batch_order += 1
        start_us = cursor_us
        end_us = start_us + duration_us
        cursor_us = end_us + GAP_US
        frontend_goals: list[str] = ["native_animation_smoke"]
        if "resource_rehydration_pending" in pending:
            frontend_goals.append("resource_rehydration")
        if "canvas_boundary_proof_pending" in pending:
            frontend_goals.append("canvas_boundary_smoke")
        offline_contracts = [
            value
            for value in pending
            if value in {
                "text_capacity_measurement_pending",
                "styled_parts_binding_pending",
                "synchronized_group_binding_pending",
            }
        ]
        current_video_only = [
            value for value in pending if value == "current_video_media_binding_pending"
        ]
        items.append({
            "template_id": template_id,
            "order": order,
            "batch": batch,
            "batch_order": batch_order,
            "duration_us": duration_us,
            "start_us": start_us,
            "end_us": end_us,
            "primary_group": row.get("primary_group"),
            "catalog_classification": row.get("catalog_classification"),
            "repair_class": row.get("repair_class"),
            "phase2_status": row.get("phase2_status"),
            "pending": pending,
            "frontend_goals": frontend_goals,
            "offline_contracts": offline_contracts,
            "current_video_only": current_video_only,
            "source_rescan_status": (row.get("source_rescan") or {}).get("status"),
            "execution_status": "planned_not_live_written",
        })
    batch_count = max(item["batch"] for item in items)
    batch_sizes = [sum(item["batch"] == batch for item in items) for batch in range(1, batch_count + 1)]
    batch_end_us = [max(item["end_us"] for item in items if item["batch"] == batch) for batch in range(1, batch_count + 1)]
    pending_counts = Counter(value for item in items for value in item["pending"])
    class_counts = Counter(str(item["repair_class"]) for item in items)
    checks = {
        "scope_is_357_unique": len(items) == len(set(ids)) == 357,
        "batch_sizes_sum_to_scope": sum(batch_sizes) == 357,
        "all_batch_windows_within_180_seconds": all(value <= max_batch_duration_us for value in batch_end_us),
        "no_hard_failures": class_counts["failed"] == 0,
        "generic_and_current_video_work_separated": all(
            "current_video_media_binding_pending" not in item["offline_contracts"]
            for item in items
        ),
        "no_live_write_or_registration": True,
    }
    return {
        "schema": "huoke.phase2-engineering-verification-queue.v1",
        "status": "planning",
        "scope_count": len(items),
        "batch_size": batch_size,
        "planned_batch_count": batch_count,
        "batch_sizes": batch_sizes,
        "batch_end_us": batch_end_us,
        "repair_class_counts": dict(sorted(class_counts.items())),
        "pending_gate_counts": dict(sorted(pending_counts.items())),
        "checks": checks,
        "policy": {
            "frontend_black_screen_is_generic_preset_evidence_only": True,
            "current_video_media_binding_is_not_global_preset_repair": True,
            "production_ready_claimed": False,
            "live_write_or_registration_performed": False,
            "next_operation": "build_batch_01_isolated_black_screen_verification_candidate_before_single_writer",
            "candidate_assembler_scope": "required_later_for_real_video_production_candidate_not_generic_black_screen_probe",
        },
        "items": items,
    }


def main() -> None:
    candidates = candidate_map(read_json(AUTO_CANDIDATES), read_json(RESCANNED_CANDIDATES))
    queue = build_queue(read_json(MANIFEST), duration_map(candidates))
    if not all(queue["checks"].values()):
        raise RuntimeError("phase2 queue checks failed")
    write_json(OUTPUT, queue)
    print(json.dumps({
        "output": str(OUTPUT),
        "scope_count": queue["scope_count"],
        "planned_batch_count": queue["planned_batch_count"],
        "batch_sizes": queue["batch_sizes"],
        "repair_class_counts": queue["repair_class_counts"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
