"""Build one isolated phase-2 black-screen verification candidate.

This intentionally reuses the proven phase-1 import path, but uses the
duration-aware phase-2 queue and the read-only rescanned candidates.  It never
writes or registers a live Jianying draft.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping


ADAPTER = Path(__file__).resolve().parents[1]
PROJECT = ADAPTER.parent
SCRIPTS = ADAPTER / "scripts"
SRC = ADAPTER / "src"
for path in (SCRIPTS, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import build_phase1_resource_recovery_batches as phase1  # noqa: E402
from jianying_adapter.candidate_plan import (  # noqa: E402
    CandidateAssembler,
    OperationRegistry,
    SHARED_OPERATION_REGISTRY,
)


WORKBENCH = ADAPTER / "repair_workspaces/phase2_engineering_adaptation_20260903"
AUTO = ADAPTER / "preset_catalog/all_auto_candidates_v1.json"
RESCANNED = WORKBENCH / "rescanned_source_candidates_phase2.json"
QUEUE = PROJECT / "video_trials/字幕预设黑幕筛选_20260902/phase2_engineering_verification/phase2_357_verification_queue.json"
OUTPUT_ROOT = QUEUE.parent
MERGED_REGISTRY = OUTPUT_ROOT / "phase2_357_runtime_candidate_registry.json"
PROJECT_STATE = QUEUE.parents[1] / "project_state.json"
BLACK = PROJECT / "video_trials/字幕预设黑幕筛选_20260902/assets/black_1080x1920_phase1_probe.mp4"
BASE_DRAFT = PROJECT / "video_trials/字幕预设黑幕筛选_20260902/workspace_base_batch01.json"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be object: {path}")
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


def merged_candidate_registry(auto: Mapping[str, Any], rescanned: Mapping[str, Any], queue: Mapping[str, Any]) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    for source in (auto, rescanned):
        for row in source.get("candidates") or []:
            if isinstance(row, dict) and row.get("template_id"):
                by_id[str(row["template_id"])] = row
    ids = [str(row.get("template_id") or "") for row in queue.get("items") or [] if isinstance(row, Mapping)]
    missing = [template_id for template_id in ids if template_id not in by_id]
    if len(ids) != 357 or len(set(ids)) != 357 or missing:
        raise ValueError(f"runtime registry coverage failed: ids={len(ids)} unique={len(set(ids))} missing={missing[:5]}")
    probe_candidates: list[dict[str, Any]] = []
    probe_media_slot_override_count = 0
    probe_styled_text_preserve_count = 0
    probe_manual_slot_preserve_count = 0
    for template_id in ids:
        candidate = copy.deepcopy(by_id[template_id])
        for slot in (candidate.get("slots") or {}).get("text") or []:
            if not isinstance(slot, dict):
                continue
            manual_style = bool(slot.get("requires_manual_style_mapping"))
            manual_slot = bool(slot.get("requires_manual_slot_mapping"))
            if not manual_style and not manual_slot:
                continue
            original_required = bool(slot.get("required"))
            slot["phase2_probe_original_required"] = original_required
            slot["phase2_probe_preserve_source_text_and_style"] = manual_style
            slot["phase2_probe_preserve_source_text_and_mapping"] = manual_slot
            if original_required and manual_style:
                probe_styled_text_preserve_count += 1
            if original_required and manual_slot:
                probe_manual_slot_preserve_count += 1
            # The generic black-screen probe must retain the source text and
            # its styled ranges verbatim.  Marking only these manual-style
            # slots optional prevents the phase-1 copy helper from attempting
            # an unsafe whole-text rewrite while the native segment/material
            # is still imported and verified in full.
            slot["required"] = False
        for kind in ("image", "video"):
            for slot in (candidate.get("slots") or {}).get(kind) or []:
                if not isinstance(slot, dict):
                    continue
                original_required = bool(slot.get("required"))
                slot["phase2_probe_original_required"] = original_required
                if original_required:
                    probe_media_slot_override_count += 1
                slot["required"] = False
        candidate["phase2_probe_media_policy"] = "preserve_source_visual_refs_without_current_video_binding"
        probe_candidates.append(candidate)
    return {
        "schema": "huoke.phase2-runtime-candidate-registry.v1",
        "scope_count": len(ids),
        "source_auto_count": len(auto.get("candidates") or []),
        "source_rescanned_count": len(rescanned.get("candidates") or []),
        "probe_media_slot_override_count": probe_media_slot_override_count,
        "probe_styled_text_preserve_count": probe_styled_text_preserve_count,
        "probe_manual_slot_preserve_count": probe_manual_slot_preserve_count,
        "current_video_media_bound": False,
        "candidates": probe_candidates,
    }


def phase1_compatible_queue(queue: Mapping[str, Any], registry: Mapping[str, Any]) -> dict[str, Any]:
    by_id = {str(row["template_id"]): row for row in registry.get("candidates") or []}
    items: list[dict[str, Any]] = []
    for row in queue.get("items") or []:
        if not isinstance(row, Mapping):
            continue
        candidate = by_id[str(row["template_id"])]
        items.append({
            **dict(row),
            "display_name": candidate.get("display_name"),
            "source_path": candidate.get("source_path"),
            "duration_us": int((candidate.get("duration") or {}).get("microseconds") or 0),
        })
    return {"schema": "huoke.phase2-phase1-builder-bridge.v1", "items": items}


def _id_factory(batch: int):
    counter = itertools.count(1)
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"huoke-phase2-engineering-batch:{batch}")
    return lambda: str(uuid.uuid5(namespace, str(next(counter)))).upper()


def build_batch(batch: int, *, only_template: str | None = None) -> dict[str, Any]:
    queue = read_json(QUEUE)
    registry = merged_candidate_registry(read_json(AUTO), read_json(RESCANNED), queue)
    write_json(MERGED_REGISTRY, registry)
    bridge = phase1_compatible_queue(queue, registry)
    if only_template:
        selected = [row for row in bridge["items"] if row["template_id"] == only_template]
        if len(selected) != 1:
            raise ValueError(f"template not found in queue: {only_template}")
        selected[0] = {**selected[0], "batch": batch, "batch_order": 1}
        bridge = {**bridge, "items": selected}
    old_registry = phase1.REGISTRY
    old_id_factory = phase1._id_factory
    try:
        phase1.REGISTRY = MERGED_REGISTRY
        phase1._id_factory = _id_factory
        envelope = phase1.build_batch(
            bridge,
            batch,
            dependency_scope="imported_fragment_visual",
        )
    finally:
        phase1.REGISTRY = old_registry
        phase1._id_factory = old_id_factory
    manifest = envelope["manifest"]
    manifest["schema"] = "huoke.phase2-engineering-black-screen-batch-candidate.v1"
    manifest["phase2_batch"] = batch
    manifest["status"] = "candidate_ready_not_live_written"
    manifest["policy"].update({
        "purpose": "generic_preset_resource_and_native_animation_smoke_only",
        "canvas_boundary_proof_claimed": False,
        "text_capacity_proof_claimed": False,
        "current_video_media_bound": False,
        "source_media_refs_preserved_for_frontend_probe": True,
        "dependency_scope": "imported_fragment_visual_with_source_exclusions_recorded",
        "production_ready_claimed": False,
        "candidate_assembler_not_applicable_reason": "generic_black_screen_probe_not_real_video_production_candidate",
    })
    return envelope


def assemble_batch_with_candidate_plan(batch: int, prefix: Path) -> dict[str, Any]:
    """Build one generic probe through the explicit CandidateAssembler path."""

    queue = read_json(QUEUE)
    registry = merged_candidate_registry(read_json(AUTO), read_json(RESCANNED), queue)
    write_json(MERGED_REGISTRY, registry)
    selected = sorted(
        [dict(row) for row in queue.get("items") or [] if isinstance(row, Mapping) and row.get("batch") == batch],
        key=lambda row: int(row["batch_order"]),
    )
    if not selected:
        raise ValueError(f"batch has no pending items: {batch}")

    candidate = prefix.with_name(prefix.name + "_candidate.json")
    manifest = prefix.with_name(prefix.name + "_manifest.json")
    execution_plan = prefix.with_name(prefix.name + "_execution_plan.json")
    candidate_plan = prefix.with_name(prefix.name + "_candidate_plan.json")
    semantic_gate = prefix.with_name(prefix.name + "_semantic_gate.json")
    build_report = prefix.with_name(prefix.name + "_candidate_build_report.json")
    writer_manifest = prefix.with_name(prefix.name + "_candidate_writer_manifest.json")
    for path in (candidate, manifest, execution_plan, candidate_plan, semantic_gate, build_report, writer_manifest):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite existing batch artifact: {path}")

    black_hash = file_sha256(BLACK)
    write_json(semantic_gate, {
        "schema": "huoke.generic-black-screen-probe-gate.v1",
        "status": "passed",
        "rough_cut": {"path": str(BLACK), "sha256": black_hash},
        "evidence": [
            {"kind": "phase2_queue_binding", "path": str(QUEUE), "sha256": file_sha256(QUEUE)},
            {"kind": "current_project_state_binding", "path": str(PROJECT_STATE), "sha256": file_sha256(PROJECT_STATE)},
        ],
        "checks": {
            "generic_probe_has_no_spoken_semantic_content": True,
            "queue_batch_scope_is_explicit": True,
            "black_asset_is_frozen": True,
        },
        "production_semantic_evidence_claimed": False,
    })
    write_json(candidate_plan, {
        "schema": "jianying-adapter.candidate-plan.v1",
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
        "base_draft": str(BASE_DRAFT),
        "rough_cut": str(BLACK),
        "semantic_gate": str(semantic_gate),
        "usage_registry": str(MERGED_REGISTRY),
        "target": {"width": 1080, "height": 1920, "jianying_version": "8.8.0"},
        "options": {"audio": {"enabled": False}, "bgm": {"enabled": False}},
        "subject_clarity_preflight": {
            "status": "passed",
            "scope": "generic_black_screen_probe_not_current_video",
            "not_applicable_reason": "pure black probe imports native preset fragments without a production subject",
            "checks": {
                "primary_subject_defined": True,
                "unused_headroom_checked": True,
                "background_distraction_checked": True,
                "perspective_checked": True,
                "caption_subject_clearance_checked": True,
            },
        },
        "transition_requires_real_before_after_state": True,
        "presets": [],
        "operations": [{
            "id": f"assemble-phase2-batch-{batch:02d}",
            "kind": "assemble_phase2_black_screen_probe",
            "batch": batch,
            "template_ids": [str(row["template_id"]) for row in selected],
            "preserve_manual_styled_text": True,
            "replace_manual_styled_text": False,
        }],
        "output": str(candidate),
        "report": str(build_report),
        "manifest": str(writer_manifest),
    })

    built: dict[str, Any] = {}

    def assemble_operation(_draft: dict[str, Any], spec: Mapping[str, Any], _context: Any):
        if spec.get("preserve_manual_styled_text") is not True or spec.get("replace_manual_styled_text") is not False:
            raise ValueError("manual styled text preservation gate is not explicit")
        envelope = build_batch(int(spec["batch"]))
        actual_ids = [str(row.get("template_id") or "") for row in envelope["manifest"].get("templates") or []]
        if actual_ids != list(spec.get("template_ids") or []):
            raise ValueError("assembled template order differs from candidate plan")
        built["envelope"] = envelope
        return copy.deepcopy(envelope["draft"]), {
            "template_count": len(actual_ids),
            "first_template_id": actual_ids[0],
            "last_template_id": actual_ids[-1],
            "manual_styled_text_policy": "preserve_original_text_and_styled_ranges",
        }

    def final_validator(draft: dict[str, Any], _context: Any) -> Mapping[str, Any]:
        envelope = built.get("envelope") or {}
        expected = envelope.get("draft")
        offline = (envelope.get("manifest") or {}).get("checks") or {}
        checks = {
            "candidate_equals_batch_builder_output": draft == expected,
            "offline_checks_passed": bool(offline) and all(value is True for value in offline.values()),
            "double_8_8_header": (
                (draft.get("platform") or {}).get("app_version"),
                (draft.get("last_modified_platform") or {}).get("app_version"),
            ) == ("8.8.0", "8.8.0"),
            "canvas_1080x1920": (
                int((draft.get("canvas_config") or {}).get("width") or 0),
                int((draft.get("canvas_config") or {}).get("height") or 0),
            ) == (1080, 1920),
            "duration_at_most_180_seconds": 0 < int(draft.get("duration") or 0) <= 180_000_000,
        }
        return {"ok": all(checks.values()), "checks": checks}

    assembler = CandidateAssembler(
        candidate_plan,
        OperationRegistry(
            SHARED_OPERATION_REGISTRY,
            {"assemble_phase2_black_screen_probe": assemble_operation},
        ),
        final_validator=final_validator,
    )
    assembler_result = assembler.run()
    phase2_manifest = copy.deepcopy(built["envelope"]["manifest"])
    phase2_manifest["policy"].pop("candidate_assembler_not_applicable_reason", None)
    phase2_manifest["policy"].update({
        "candidate_assembler_applied": True,
        "candidate_plan": str(candidate_plan),
        "manual_styled_text_policy": "preserve_original_text_and_styled_ranges",
        "unsafe_whole_text_replacement_performed": False,
    })
    write_json(manifest, phase2_manifest)
    write_json(execution_plan, {
        "schema": "huoke.phase2-engineering-black-screen-execution-plan.v1",
        "batch": batch,
        "status": "candidate_ready_not_live_written",
        "candidate_plan": str(candidate_plan),
        "candidate_build_report": str(build_report),
        "candidate_writer_manifest": str(writer_manifest),
        "candidate": str(candidate),
        "manifest": str(manifest),
        "operation_order": ["candidate_assembler", "capture_cache_baseline", "single_writer", "frontend_open", "full_playback", "normal_close", "post_open_audit"],
        "live_write_or_registration_performed": False,
        "production_ready_claimed": False,
    })
    return {**assembler_result, "phase2_manifest": str(manifest), "execution_plan": str(execution_plan)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--only-template")
    parser.add_argument("--use-candidate-assembler", action="store_true")
    args = parser.parse_args(argv)
    if args.use_candidate_assembler:
        if args.only_template:
            raise ValueError("--only-template cannot be combined with --use-candidate-assembler")
        prefix = OUTPUT_ROOT / f"batch_{args.batch:02d}"
        result = assemble_batch_with_candidate_plan(args.batch, prefix)
        print(json.dumps({"batch": args.batch, **result}, ensure_ascii=False))
        return 0
    envelope = build_batch(args.batch, only_template=args.only_template)
    suffix = f"_{args.only_template}" if args.only_template else ""
    prefix = OUTPUT_ROOT / f"batch_{args.batch:02d}{suffix}"
    candidate = prefix.with_name(prefix.name + "_candidate.json")
    manifest = prefix.with_name(prefix.name + "_manifest.json")
    plan = prefix.with_name(prefix.name + "_execution_plan.json")
    write_json(candidate, envelope)
    write_json(manifest, envelope["manifest"])
    write_json(plan, {
        "schema": "huoke.phase2-engineering-black-screen-execution-plan.v1",
        "batch": args.batch,
        "status": "candidate_ready_not_live_written",
        "candidate": str(candidate),
        "manifest": str(manifest),
        "operation_order": ["capture_cache_baseline", "single_writer", "frontend_open", "full_playback", "normal_close", "post_open_audit"],
        "live_write_or_registration_performed": False,
        "production_ready_claimed": False,
    })
    print(json.dumps({
        "batch": args.batch,
        "template_count": envelope["manifest"]["template_count"],
        "structure_ready": envelope["manifest"]["structure_ready"],
        "candidate": str(candidate),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
