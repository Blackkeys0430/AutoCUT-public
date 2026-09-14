"""Verify a live Phase-2 engineering probe before Jianying is opened.

This verifier deliberately checks only draft identity, compatibility and the
offline probe contract.  It does not promote generic black-screen evidence to
production/current-video visual evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "jianying-adapter" / "vendor" / "pyJianYingDraft-source"
DEPS = ROOT / "jianying-adapter" / "vendor" / "python-deps"
sys.path[:0] = [str(DEPS), str(VENDOR)]

from pyJianYingDraft import DraftCryptoConfig, JianyingDraftCryptoCodec
from pyJianYingDraft.draft_codec import load_json_object_with_codec
import psutil


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def without_native_indexes(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_native_indexes(child)
            for key, child in value.items()
            if key not in {"render_index", "track_render_index"}
        }
    if isinstance(value, list):
        return [without_native_indexes(child) for child in value]
    return value


def inventory(content: dict[str, Any]) -> dict[str, Any]:
    tracks = [row for row in content.get("tracks") or [] if isinstance(row, dict)]
    track_type_counts: dict[str, int] = {}
    segment_type_counts: dict[str, int] = {}
    segments: list[dict[str, Any]] = []
    for track in tracks:
        track_type = str(track.get("type") or "unknown")
        track_type_counts[track_type] = track_type_counts.get(track_type, 0) + 1
        current_segments = [
            row for row in track.get("segments") or [] if isinstance(row, dict)
        ]
        segments.extend(current_segments)
        segment_type_counts[track_type] = (
            segment_type_counts.get(track_type, 0) + len(current_segments)
        )

    materials = content.get("materials") or {}
    text_materials = [
        row for row in materials.get("texts") or [] if isinstance(row, dict)
    ]
    text_template_materials = [
        row for row in materials.get("text_templates") or [] if isinstance(row, dict)
    ]
    animation_materials = [
        row
        for row in materials.get("material_animations") or []
        if isinstance(row, dict)
    ]
    all_material_ids = {
        str(row.get("id"))
        for rows in materials.values()
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, dict) and row.get("id")
    }
    text_material_ids = {
        str(row.get("id")) for row in text_materials if row.get("id")
    } | {
        str(row.get("id"))
        for row in text_template_materials
        if row.get("id")
    }
    text_segments = [
        row
        for track in tracks
        if track.get("type") == "text"
        for row in track.get("segments") or []
        if isinstance(row, dict)
    ]
    unresolved_text_material_ids = sorted(
        {
            str(row.get("material_id") or "")
            for row in text_segments
            if str(row.get("material_id") or "") not in text_material_ids
        }
    )
    unresolved_text_extra_refs = sorted(
        {
            str(ref)
            for row in text_segments
            for ref in row.get("extra_material_refs") or []
            if str(ref) not in all_material_ids
        }
    )
    return {
        "duration_us": content.get("duration"),
        "track_count": len(tracks),
        "track_type_counts": dict(sorted(track_type_counts.items())),
        "segment_count": len(segments),
        "segment_type_counts": dict(sorted(segment_type_counts.items())),
        "text_material_count": len(text_materials),
        "text_template_material_count": len(text_template_materials),
        "material_animation_count": len(animation_materials),
        "text_segment_count": len(text_segments),
        "unresolved_text_material_ids": unresolved_text_material_ids,
        "unresolved_text_extra_refs": unresolved_text_extra_refs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--logical-root", type=Path, required=True)
    parser.add_argument("--physical-root", type=Path, required=True)
    parser.add_argument("--compatibility-reference-path", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    logical = args.logical_root / args.name
    physical = args.physical_root / args.name
    codec = JianyingDraftCryptoCodec(
        DraftCryptoConfig(jy_install_dir=args.install_dir, backup=False)
    )
    decoded, _ = load_json_object_with_codec(
        logical / "draft_content.json", content_codec=codec
    )
    candidate = read_object(args.candidate)
    if isinstance(candidate.get("draft"), dict):
        candidate = candidate["draft"]
    candidate["id"] = decoded.get("id")
    for key in ("version", "new_version", "platform", "last_modified_platform"):
        candidate[key] = decoded.get(key)

    live_inventory = inventory(decoded)
    candidate_inventory = inventory(candidate)

    mirror_ok = True
    mirror = logical / "template-2.tmp"
    if mirror.is_file():
        mirror_value, _ = load_json_object_with_codec(mirror, content_codec=codec)
        mirror_ok = mirror_value == decoded

    root_meta = read_object(args.logical_root / "root_meta_info.json")
    matches = [
        item
        for item in root_meta.get("all_draft_store", [])
        if isinstance(item, dict)
        and (
            item.get("draft_id") == decoded.get("id")
            or item.get("draft_name") == args.name
        )
    ]
    manifest = read_object(args.manifest)
    offline_checks = manifest.get("checks") or {}
    exact_executable = (args.install_dir / "JianyingPro.exe").resolve()
    running_processes: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = str(process.info.get("name") or "")
            if name.casefold() != "jianyingpro.exe":
                continue
            executable = str(process.info.get("exe") or "")
            running_processes.append({
                "pid": int(process.info["pid"]),
                "name": name,
                "executable": executable,
                "exact_8_8": bool(executable) and Path(executable).resolve() == exact_executable,
            })
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    global_locked_files = sorted(str(path) for path in args.logical_root.rglob(".locked") if path.is_file())
    reference_versions: tuple[str | None, str | None] | None = None
    if args.compatibility_reference_path is not None:
        reference, _ = load_json_object_with_codec(
            args.compatibility_reference_path, content_codec=codec
        )
        reference_versions = (
            (reference.get("platform") or {}).get("app_version"),
            (reference.get("last_modified_platform") or {}).get("app_version"),
        )
    checks = {
        "all_jianyingpro_processes_zero": not running_processes,
        "exact_jianyingpro_8_8_processes_zero": not any(row["exact_8_8"] for row in running_processes),
        "all_draft_locked_files_zero": not global_locked_files,
        "compatibility_reference_double_8_8": reference_versions == ("8.8.0", "8.8.0"),
        "platform_version_8_8": (decoded.get("platform") or {}).get("app_version")
        == "8.8.0",
        "last_modified_version_8_8": (
            decoded.get("last_modified_platform") or {}
        ).get("app_version")
        == "8.8.0",
        "c_e_same_physical_draft": logical.is_dir()
        and physical.is_dir()
        and os.path.samefile(logical, physical),
        "candidate_roundtrip_equal_except_native_indexes": without_native_indexes(
            decoded
        )
        == without_native_indexes(candidate),
        "duration_at_most_180_seconds": isinstance(decoded.get("duration"), int)
        and 0 < decoded["duration"] <= 180_000_000,
        "track_inventory_matches_candidate": (
            live_inventory["track_count"],
            live_inventory["track_type_counts"],
            live_inventory["segment_count"],
            live_inventory["segment_type_counts"],
        )
        == (
            candidate_inventory["track_count"],
            candidate_inventory["track_type_counts"],
            candidate_inventory["segment_count"],
            candidate_inventory["segment_type_counts"],
        ),
        "text_inventory_matches_candidate": (
            live_inventory["text_material_count"],
            live_inventory["text_template_material_count"],
            live_inventory["text_segment_count"],
        )
        == (
            candidate_inventory["text_material_count"],
            candidate_inventory["text_template_material_count"],
            candidate_inventory["text_segment_count"],
        ),
        "animation_inventory_matches_candidate": live_inventory[
            "material_animation_count"
        ]
        == candidate_inventory["material_animation_count"],
        "all_text_material_and_extra_refs_resolve": not live_inventory[
            "unresolved_text_material_ids"
        ]
        and not live_inventory["unresolved_text_extra_refs"],
        "content_mirrors_deep_equal": mirror_ok,
        "no_locked_file": not any(logical.rglob(".locked")),
        "single_c_junction_registration": len(matches) == 1
        and str(matches[0].get("draft_fold_path") or "").lower().startswith("c:"),
        "offline_structure_ready": manifest.get("structure_ready") is True,
        "offline_checks_passed": bool(offline_checks)
        and all(value is True for value in offline_checks.values()),
        "generic_probe_not_promoted_to_current_video": (
            manifest.get("policy", {}).get("purpose")
            in {
                "generic_preset_resource_and_native_animation_smoke_only",
                "user_visual_screening_of_audited_fully_recovered_presets",
            }
            and manifest.get("policy", {}).get("current_video_media_bound") is False
            and manifest.get("policy", {}).get("production_ready_claimed") is False
        ),
    }
    result = {
        "schema": "huoke.phase2-engineering-live-verification.v1",
        "ok": all(checks.values()),
        "status": "written_unopened_pending_frontend_playback",
        "draft_name": args.name,
        "logical_draft": str(logical),
        "physical_draft": str(physical),
        "draft_id": decoded.get("id"),
        "duration_us": decoded.get("duration"),
        "track_count": len(decoded.get("tracks") or []),
        "inventory": live_inventory,
        "candidate_inventory": candidate_inventory,
        "root_meta_matches": len(matches),
        "writer_environment": {
            "all_jianyingpro_process_count": len(running_processes),
            "exact_jianyingpro_8_8_process_count": sum(1 for row in running_processes if row["exact_8_8"]),
            "running_processes": running_processes,
            "all_draft_locked_file_count": len(global_locked_files),
            "locked_files": global_locked_files,
            "logical_root": str(args.logical_root),
            "physical_root": str(args.physical_root),
            "root_samefile": os.path.samefile(args.logical_root, args.physical_root),
            "compatibility_reference_path": str(args.compatibility_reference_path) if args.compatibility_reference_path else None,
            "compatibility_reference_versions": list(reference_versions) if reference_versions else None,
        },
        "checks": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
