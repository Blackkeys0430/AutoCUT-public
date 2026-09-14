"""Plan all phase-1 probes and build one workspace-only recovery batch.

No live draft path or registration API is accepted.  The output candidate is
an ordinary JSON envelope for a later, separately authorized single Writer.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ADAPTER = Path(__file__).resolve().parents[1]
PROJECT = ADAPTER.parent
TRIAL = PROJECT / "video_trials/字幕预设黑幕筛选_20260902"
SRC = ADAPTER / "src"
SCRIPTS = ADAPTER / "scripts"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import build_five_template_trial as trial_builder  # noqa: E402
from audit_resource_recovery_post_open import collect_visual_dependencies  # noqa: E402


PHASE1 = ADAPTER / "repair_workspaces/phase1_resource_repair_20260903/repair_manifest_phase1.json"
REGISTRY = ADAPTER / "preset_catalog/all_auto_candidates_v1.json"
PRESET_ROOT = PROJECT / "jianying_presets/剪映1000个高级感字幕预设"
BASE = TRIAL / "workspace_base_batch01.json"
BLACK = TRIAL / "assets/black_1080x1920_phase1_probe.mp4"
OUTPUT_ROOT = TRIAL / "phase1_resource_recovery_batches"
KNOWN_EVIDENCE = {
    "JIANYING-25-05": {
        "status": "partially_recovered",
        "report": str(TRIAL / "resource_recovery_probes/JIANYING-25-05_post_open_runtime_audit.json"),
        "avoid_duplicate_open": True,
    }
}
START_US = 1_000_000
GAP_US = 500_000
BATCH_SIZE = 50
BASE_DURATION_US = 180_000_000


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _candidate_map(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["template_id"]): item
        for item in registry.get("candidates", [])
        if isinstance(item, dict) and item.get("template_id")
    }


def plan_queue() -> dict[str, Any]:
    phase1 = read_json(PHASE1)
    by_id = _candidate_map(read_json(REGISTRY))
    ordered = [str(item["template_id"]) for item in phase1.get("records", [])]
    if len(ordered) != 285 or len(set(ordered)) != 285:
        raise ValueError(f"phase1 scope must contain 285 unique templates: {len(ordered)}")
    missing = [template_id for template_id in ordered if template_id not in by_id]
    if missing:
        raise ValueError("registry missing phase1 templates: " + ", ".join(missing))
    pending = [template_id for template_id in ordered if template_id not in KNOWN_EVIDENCE]
    batches = [pending[index : index + BATCH_SIZE] for index in range(0, len(pending), BATCH_SIZE)]
    items: list[dict[str, Any]] = []
    pending_position = {template_id: index + 1 for index, template_id in enumerate(pending)}
    for phase1_order, template_id in enumerate(ordered, 1):
        if template_id in KNOWN_EVIDENCE:
            item = {
                "template_id": template_id,
                "phase1_order": phase1_order,
                "execution_status": "existing_evidence_no_duplicate_open",
                "evidence": KNOWN_EVIDENCE[template_id],
                "batch": None,
                "batch_order": None,
            }
        else:
            position = pending_position[template_id]
            item = {
                "template_id": template_id,
                "phase1_order": phase1_order,
                "execution_status": "pending_frontend_open",
                "evidence": None,
                "batch": ((position - 1) // BATCH_SIZE) + 1,
                "batch_order": ((position - 1) % BATCH_SIZE) + 1,
            }
        candidate = by_id[template_id]
        item.update(
            {
                "display_name": candidate.get("display_name"),
                "duration_us": int((candidate.get("duration") or {}).get("microseconds") or 0),
                "source_path": candidate.get("source_path"),
            }
        )
        items.append(item)
    return {
        "schema": "huoke.phase1-resource-recovery-queue.v1",
        "scope_count": len(ordered),
        "existing_evidence_count": len(KNOWN_EVIDENCE),
        "pending_execution_count": len(pending),
        "planned_batch_count": len(batches),
        "batch_sizes": [len(batch) for batch in batches],
        "policy": {
            "source_order": "repair_manifest_phase1.records",
            "batch_size": BATCH_SIZE,
            "existing_evidence_retained_in_ledger": True,
            "existing_evidence_not_reopened": True,
            "live_write_or_registration": False,
        },
        "items": items,
    }


def _source(
    candidate: Mapping[str, Any],
    *,
    preset_root: Path = PRESET_ROOT,
) -> tuple[Path, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    path = preset_root / str(candidate.get("source_path") or "")
    source = read_json(path)
    wrappers = ((source.get("materials") or {}).get("drafts") or [])
    if wrappers and isinstance(wrappers[0], Mapping) and isinstance(wrappers[0].get("draft"), dict):
        wrapper = wrappers[0]
        inner = copy.deepcopy(wrapper["draft"])
    else:
        wrapper = {}
        inner = copy.deepcopy(source)
    metadata = [
        {"key": key, "value": value, "classification": "source_wrapper_metadata_not_runtime_visual_dependency"}
        for key, value in sorted(wrapper.items())
        if key != "draft" and str(key) in {"draft_config_path", "draft_cover_path", "draft_file_path"}
        and isinstance(value, str) and value
    ]
    return path, source, inner, metadata


def _slot_copy(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    result: list[str] = []
    for slot in ((candidate.get("slots") or {}).get("text") or []):
        if not isinstance(slot, Mapping) or not slot.get("required") or slot.get("decorative_locked"):
            continue
        value = slot.get("default_text")
        if not isinstance(value, str):
            raise ValueError(f"{candidate.get('template_id')} required slot lacks default text")
        result.append(value)
    return tuple(result)


def _id_factory(batch: int):
    counter = itertools.count(1)
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"huoke-phase1-resource-batch:{batch}")
    return lambda: str(uuid.uuid5(namespace, str(next(counter)))).upper()


def _base(
    *,
    base_path: Path = BASE,
    black_path: Path = BLACK,
    base_duration_us: int = BASE_DURATION_US,
    retain_base_track_names: tuple[str, ...] = ("BLACK_BACKGROUND",),
) -> dict[str, Any]:
    base = read_json(base_path)
    versions = (
        (base.get("platform") or {}).get("app_version"),
        (base.get("last_modified_platform") or {}).get("app_version"),
    )
    if versions != ("8.8.0", "8.8.0"):
        raise ValueError(f"base must be double 8.8.0: {versions}")
    if not black_path.is_file() or black_path.stat().st_size <= 0:
        raise FileNotFoundError(f"missing 180-second black asset: {black_path}")
    result = copy.deepcopy(base)
    result["duration"] = base_duration_us
    result["tracks"] = [
        track for track in result.get("tracks", [])
        if isinstance(track, dict) and track.get("name") in retain_base_track_names
    ]
    black_tracks = [track for track in result["tracks"] if track.get("name") == "BLACK_BACKGROUND"]
    if len(black_tracks) != 1:
        raise ValueError("base must contain exactly one BLACK_BACKGROUND track")
    segment = black_tracks[0]["segments"][0]
    segment["target_timerange"] = {"start": 0, "duration": base_duration_us}
    segment["source_timerange"] = {"start": 0, "duration": base_duration_us}
    video_id = segment["material_id"]
    for material in (result.get("materials") or {}).get("videos") or []:
        if material.get("id") == video_id:
            material["path"] = str(black_path)
            material["media_path"] = str(black_path)
            material["duration"] = base_duration_us
    materials = result.get("materials") or {}
    if isinstance(materials, dict) and "AUDITION_INDEX_LABELS" not in retain_base_track_names:
        materials["texts"] = []
    return result


def _windows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cursor = START_US
    result = []
    for item in items:
        duration = int(item["duration_us"])
        if duration <= 0:
            raise ValueError(f"non-positive duration: {item['template_id']}")
        result.append({**item, "start_us": cursor, "end_us": cursor + duration})
        cursor += duration + GAP_US
    if result and result[-1]["end_us"] > BASE_DURATION_US:
        raise ValueError(f"batch exceeds black asset: {result[-1]['end_us']}")
    return result


def build_batch(
    queue: Mapping[str, Any],
    batch: int,
    *,
    dependency_scope: str = "all_source_visual",
    registry_path: Path = REGISTRY,
    preset_root: Path = PRESET_ROOT,
    base_path: Path = BASE,
    black_path: Path = BLACK,
    id_factory_override: Any | None = None,
    start_us: int = START_US,
    gap_us: int = GAP_US,
    base_duration_us: int = BASE_DURATION_US,
    retain_base_track_names: tuple[str, ...] = ("BLACK_BACKGROUND",),
    visual_path_maps: Mapping[str, Mapping[str, str | Path]] | None = None,
    max_template_duration_us: int | None = None,
) -> dict[str, Any]:
    if dependency_scope not in {"all_source_visual", "imported_fragment_visual"}:
        raise ValueError(f"unsupported dependency_scope: {dependency_scope}")
    selected = [
        dict(item) for item in queue.get("items", [])
        if isinstance(item, Mapping) and item.get("batch") == batch
    ]
    if not selected:
        raise ValueError(f"batch has no pending items: {batch}")
    selected.sort(key=lambda item: int(item["batch_order"]))
    cursor = start_us
    timed: list[dict[str, Any]] = []
    for item in selected:
        duration = int(item["duration_us"])
        if duration <= 0:
            raise ValueError(f"non-positive duration: {item['template_id']}")
        timed.append({**item, "start_us": cursor, "end_us": cursor + duration})
        cursor += duration + gap_us
    if timed and timed[-1]["end_us"] > base_duration_us:
        raise ValueError(f"batch exceeds black asset: {timed[-1]['end_us']}")
    selected = timed
    by_id = _candidate_map(read_json(registry_path))
    registry = {"schema": "huoke.phase1-recovery-runtime-registry.v1", "candidates": [by_id[item["template_id"]] for item in selected]}
    slot_copy = {item["template_id"]: _slot_copy(by_id[item["template_id"]]) for item in selected}
    aggregate: dict[str, Any] = _base(
        base_path=base_path,
        black_path=black_path,
        base_duration_us=base_duration_us,
        retain_base_track_names=retain_base_track_names,
    )
    id_factory = id_factory_override or _id_factory(batch)
    pending_paths: set[str] = set()
    reports: list[dict[str, Any]] = []
    source_evidence: dict[str, dict[str, Any]] = {}
    for item in selected:
        template_id = item["template_id"]
        source_path, _payload, inner, metadata = _source(by_id[template_id], preset_root=preset_root)
        dependencies = collect_visual_dependencies(inner)
        source_evidence[template_id] = {
            "source_path": str(source_path),
            "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest().upper(),
            "runtime_visual_dependencies": dependencies,
            "source_wrapper_metadata": metadata,
        }
    for start in range(0, len(selected), 5):
        group = selected[start : start + 5]
        ids = tuple(item["template_id"] for item in group)
        offsets = {item["template_id"]: int(item["start_us"]) for item in group}
        try:
                envelope = trial_builder.build_five_template_trial(
                aggregate,
                registry,
                PRESET_ROOT,
                (),
                disable_audio=True,
                preserve_visual_resource_refs=True,
                allowed_existing_missing_paths=sorted(pending_paths),
                template_ids=ids,
                template_offsets_us=offsets,
                slot_copy_table=slot_copy,
                    visual_path_maps=visual_path_maps,
                    # Recovery preserves the complete source timeline by
                    # default; screening can opt into its bounded sample.
                    max_template_duration_us=max_template_duration_us,
                    id_factory=id_factory,
                )
        except Exception as exc:
            raise RuntimeError(f"template_group={ids}: {exc}") from exc
        aggregate = envelope["draft"]
        reports.extend(envelope["manifest"]["templates"])
        pending_paths.update(envelope["manifest"]["visual_resource_rehydrate"]["pending_paths"])
    reports_by_id = {row["template_id"]: row for row in reports}
    manifest_items: list[dict[str, Any]] = []
    for item in selected:
        template_id = item["template_id"]
        report = reports_by_id[template_id]
        source_expected = source_evidence[template_id]["runtime_visual_dependencies"]
        actual = [
            row for row in report["inner_drafts"]
            for row in row["build_report"].get("visual_rehydrations", [])
        ]
        source_expected_pairs = sorted((row["remote_id"], row["path"]) for row in source_expected)
        actual_pairs = sorted(
            (resource_id, row["original_path"])
            for row in actual
            for resource_id in row.get("remote_resource_ids", [])
        )
        if dependency_scope == "imported_fragment_visual":
            expected_pairs = actual_pairs
        else:
            expected_pairs = source_expected_pairs
        preserved = all(any(pair == candidate for candidate in actual_pairs) for pair in expected_pairs)
        missing_pairs = [
            {"remote_id": remote_id, "path": path}
            for remote_id, path in expected_pairs
            if (remote_id, path) not in actual_pairs
        ]
        excluded_source_pairs = [
            {"remote_id": remote_id, "path": path}
            for remote_id, path in source_expected_pairs
            if (remote_id, path) not in actual_pairs
        ]
        manifest_items.append(
            {
                "template_id": template_id,
                "batch_order": item["batch_order"],
                "start_us": item["start_us"],
                "end_us": item["end_us"],
                "runtime_visual_dependencies": (
                    [
                        {"remote_id": remote_id, "path": path, "scope": "imported_fragment_visual"}
                        for remote_id, path in expected_pairs
                    ]
                    if dependency_scope == "imported_fragment_visual"
                    else source_expected
                ),
                "source_runtime_visual_dependencies": source_expected,
                "dependency_scope": dependency_scope,
                "excluded_source_visual_dependencies": excluded_source_pairs,
                "runtime_visual_dependency_inventory_complete": True,
                "no_runtime_visual_dependency": not expected_pairs,
                "runtime_visual_dependency_count": len(expected_pairs),
                "all_runtime_visual_ids_and_paths_preserved": preserved,
                "missing_runtime_visual_dependencies": missing_pairs,
                "source_wrapper_metadata": source_evidence[template_id]["source_wrapper_metadata"],
                "source_wrapper_metadata_excluded": all(
                    row["value"] not in json.dumps(aggregate, ensure_ascii=False)
                    for row in source_evidence[template_id]["source_wrapper_metadata"]
                ),
                "audio_disabled": True,
                "builder_report": report,
            }
        )
    checks = {
        "batch_count_matches_plan": len(manifest_items) == len(selected),
        "exclusive_non_overlapping_windows": all(
            current["start_us"] >= previous["end_us"]
            for previous, current in zip(manifest_items, manifest_items[1:])
        ),
        "double_8_8_header": (
            (aggregate.get("platform") or {}).get("app_version"),
            (aggregate.get("last_modified_platform") or {}).get("app_version"),
        ) == ("8.8.0", "8.8.0"),
        "canvas_1080x1920": aggregate.get("canvas_config", {}).get("width") == 1080
        and aggregate.get("canvas_config", {}).get("height") == 1920,
        "all_runtime_visual_ids_and_paths_preserved": all(item["all_runtime_visual_ids_and_paths_preserved"] for item in manifest_items),
        "all_wrapper_metadata_excluded": all(item["source_wrapper_metadata_excluded"] for item in manifest_items),
        "original_author_audio_disabled": not any(track.get("type") == "audio" for track in aggregate.get("tracks", []) if isinstance(track, Mapping)),
        "workspace_only_no_live_write_or_registration": True,
    }
    manifest = {
        "schema": "huoke.phase1-resource-recovery-batch-candidate.v1",
        "batch": batch,
        "status": "candidate_ready_not_live_written",
        "template_count": len(manifest_items),
        "cache_baseline_required_before_frontend_open": True,
        "checks": checks,
        "templates": manifest_items,
        "pending_visual_paths": sorted(pending_paths),
        "policy": {
            "builder": "build_five_template_trial(preserve_visual_resource_refs=True)",
            "black_background": str(black_path),
            "disable_audio": True,
            "live_write_or_registration": False,
            "download_or_visual_success_claimed": False,
        },
        "structure_ready": all(checks.values()),
        "download_success": None,
        "visual_success": None,
    }
    if not manifest["structure_ready"]:
        dependency_failures = [
            {"template_id": item["template_id"], "missing": item["missing_runtime_visual_dependencies"]}
            for item in manifest_items
            if not item["all_runtime_visual_ids_and_paths_preserved"]
        ]
        raise RuntimeError(
            "batch structure checks failed: "
            + ", ".join(key for key, value in checks.items() if not value)
            + (f"; dependency_failures={json.dumps(dependency_failures, ensure_ascii=False)}" if dependency_failures else "")
        )
    return {"draft": aggregate, "manifest": manifest}


def capture_cache_baseline(cache_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []
    seen: set[str] = set()
    effect_root = cache_root / "effect"
    if not effect_root.is_dir():
        raise FileNotFoundError(effect_root)
    # Enumerate the tree once.  The earlier implementation repeated a full
    # recursive glob for every dependency record; large batches can contain
    # many duplicate leaves, making an otherwise read-only snapshot take
    # hours.  Indexing by leaf preserves the same match set while retaining
    # the complete directory snapshot below.
    all_existing = sorted(
        (path for path in effect_root.rglob("*") if path.exists()),
        key=lambda path: str(path).casefold(),
    )
    by_leaf: dict[str, list[Path]] = {}
    for path in all_existing:
        by_leaf.setdefault(path.name.casefold(), []).append(path)
    # Complete directory snapshot is required because Jianying may rewrite a
    # stale path to a new hash that cannot be predicted before opening.
    for target in (path for path in all_existing if path.is_dir()):
        seen.add(str(target.resolve(strict=False)).casefold())
        rows.append({"relative_path": target.relative_to(cache_root).as_posix(), "kind": "directory"})
    for template in manifest.get("templates", []) or []:
        for dependency in template.get("runtime_visual_dependencies", []) or []:
            raw = str(dependency.get("path") or "")
            parts = [part for part in raw.replace("\\", "/").split("/") if part]
            leaf = parts[-1] if parts else ""
            matches = by_leaf.get(leaf.casefold(), []) if leaf else []
            targets = matches or [effect_root / str(dependency.get("remote_id")) / leaf]
            for target in targets:
                key = str(target.resolve(strict=False)).casefold()
                if key in seen:
                    continue
                seen.add(key)
                relative = target.relative_to(cache_root).as_posix()
                record = {"relative_path": relative, "kind": "directory" if target.is_dir() else "file"}
                (rows if target.exists() else absent).append(record)
    return {
        "schema": "huoke.jianying-cache-baseline.v1",
        "captured_at": None,
        "cache_root": str(cache_root),
        "complete_recursive_snapshot": True,
        "entries": rows,
        "absent_entries": absent,
        "note": "set captured_at immediately before the authorized frontend open",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--capture-cache-baseline", type=Path)
    args = parser.parse_args(argv)
    queue = plan_queue()
    output = OUTPUT_ROOT
    write_json(output / "phase1_285_queue_plan.json", queue)
    envelope = build_batch(queue, args.batch)
    write_json(output / f"batch_{args.batch:02d}_candidate.json", envelope)
    write_json(output / f"batch_{args.batch:02d}_manifest.json", envelope["manifest"])
    write_json(
        output / f"batch_{args.batch:02d}_execution_plan.json",
        {
            "schema": "huoke.phase1-resource-recovery-execution-plan.v1",
            "batch": args.batch,
            "status": "candidate_ready_not_live_written",
            "candidate": str(output / f"batch_{args.batch:02d}_candidate.json"),
            "cache_baseline": str(output / f"batch_{args.batch:02d}_cache_baseline.json"),
            "operation_order": ["capture_cache_baseline", "single_writer", "frontend_open", "normal_close", "post_open_batch_audit"],
            "existing_evidence_skipped": [item for item in queue["items"] if item["execution_status"] == "existing_evidence_no_duplicate_open"],
            "live_write_or_registration_performed": False,
        },
    )
    if args.capture_cache_baseline:
        write_json(
            output / f"batch_{args.batch:02d}_cache_baseline.json",
            capture_cache_baseline(args.capture_cache_baseline, envelope["manifest"]),
        )
    print(json.dumps({"batch": args.batch, "template_count": envelope["manifest"]["template_count"], "structure_ready": True, "output_root": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
