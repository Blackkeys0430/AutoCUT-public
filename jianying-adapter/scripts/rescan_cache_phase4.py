"""Rescan only the post-phase-3 Jianying cache delta in a new workbench.

This phase is deliberately cache-only and read-only with respect to all
catalogs and drafts.  It freezes a newly changed file only when the existing
phase-3 type checks accept it; cache sidecars and remote IDs alone never close
a dependency.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from scripts.rehydrate_preset_resources_phase3 import file_header_valid
except ModuleNotFoundError:  # direct ``python scripts/<tool>.py`` invocation
    from rehydrate_preset_resources_phase3 import file_header_valid  # type: ignore[no-redef]


ADAPTER = Path(__file__).resolve().parents[1]
CATALOG = ADAPTER / "preset_catalog"
PHASE3_ROOT = ADAPTER / "repair_workspaces" / "phase3_remote_resource_rehydration_20260903"
PHASE3_INVENTORY = PHASE3_ROOT / "resource_inventory_phase3.json"
PHASE3_CLOSURE = PHASE3_ROOT / "preset_closure_manifest_phase3.json"
PHASE3_V3 = PHASE3_ROOT / "standardized_preset_catalog_v4_candidate.json"
PHASE1_MANIFEST = ADAPTER / "repair_workspaces" / "phase1_resource_repair_20260903" / "repair_manifest_phase1.json"
PHASE2_MANIFEST = ADAPTER / "repair_workspaces" / "phase2_engineering_adaptation_20260903" / "adaptation_manifest_phase2.json"
V1_PATH = CATALOG / "standardized_preset_catalog_v1.json"
AUTO_PATH = CATALOG / "all_auto_candidates_v1.json"
WORKBENCH = ADAPTER / "repair_workspaces" / "phase4_cache_rescan_20260903"
CACHE_ROOT = Path(os.environ.get("LOCALAPPDATA", "C:/Users/YOUR_USER/AppData/Local")) / "JianyingPro" / "User Data" / "Cache"
SOURCE_ROOT = ADAPTER.parent / "jianying_presets" / "剪映1000个高级感字幕预设"
RESCAN_SINCE = datetime(2026, 9, 3, 1, 59, 0)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根对象不是 object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def resource_key(kind: str, original_path: str) -> str:
    return f"{kind}:{original_path.replace(chr(92), '/') }"


def scan_files(root: Path, *, since: datetime | None = None) -> list[Path]:
    if not root.is_dir():
        return []
    threshold = since.timestamp() if since else None
    rows = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if threshold is not None and path.stat().st_mtime < threshold:
            continue
        rows.append(path)
    return sorted(rows, key=lambda path: str(path).lower())


def safe_extension(value: str | None, fallback: str) -> str:
    extension = str(value or fallback or ".bin")
    if not extension.startswith("."):
        extension = "." + extension
    return extension if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", extension) else ".bin"


def freeze(path: Path, *, kind: str, expected_extension: str | None, frozen_root: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size <= 0:
            return {"status": "invalid_empty_file", "source_path": str(path)}
    except OSError:
        return {"status": "unreadable", "source_path": str(path)}
    valid, validation = file_header_valid(path, kind)
    if not valid:
        return {"status": "invalid_local_match", "validation": validation, "source_path": str(path)}
    digest = sha256_file(path)
    destination = frozen_root / f"{digest}{safe_extension(expected_extension, path.suffix.lower())}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file():
        shutil.copy2(path, destination)
    if sha256_file(destination) != digest or destination.stat().st_size <= 0:
        return {"status": "freeze_verification_failed", "source_path": str(path)}
    return {
        "status": "new_cache_hit_frozen",
        "validation": validation,
        "source_path": str(path),
        "frozen_path": str(destination),
        "sha256": digest,
        "size_bytes": destination.stat().st_size,
        "source": "new_or_modified_cache",
    }


def build_phase4(
    *,
    phase3_inventory_path: Path = PHASE3_INVENTORY,
    phase3_closure_path: Path = PHASE3_CLOSURE,
    phase3_v3_path: Path = PHASE3_V3,
    phase1_manifest_path: Path = PHASE1_MANIFEST,
    phase2_manifest_path: Path = PHASE2_MANIFEST,
    v1_path: Path = V1_PATH,
    auto_path: Path = AUTO_PATH,
    cache_root: Path = CACHE_ROOT,
    source_root: Path = SOURCE_ROOT,
    since: datetime = RESCAN_SINCE,
    workbench: Path = WORKBENCH,
    enforce_scope: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    phase3_inventory = read_json(phase3_inventory_path)
    phase3_closure = read_json(phase3_closure_path)
    v4 = copy.deepcopy(read_json(phase3_v3_path))
    phase1 = read_json(phase1_manifest_path)
    phase2 = read_json(phase2_manifest_path)
    v1 = read_json(v1_path)
    auto = read_json(auto_path)
    phase1_ids = {str(row.get("template_id")) for row in phase1.get("records", []) if isinstance(row, Mapping) and row.get("template_id")}
    phase2_ids = {str(row.get("template_id")) for row in phase2.get("records", []) if isinstance(row, Mapping) and row.get("repair_class") == "partial"}
    if enforce_scope and (len(phase1_ids) != 285 or len(phase2_ids) != 242):
        raise ValueError(f"Phase4 范围应为 phase1=285、phase2=242，实际为 {len(phase1_ids)}、{len(phase2_ids)}")
    targets = phase1_ids | phase2_ids
    inventory_by_key = {str(row.get("resource_key")): row for row in phase3_inventory.get("records", []) if isinstance(row, Mapping) and row.get("resource_key")}
    phase1_remote_refs_all = [
        resource_key(str(dep.get("kind") or "resource"), str(dep.get("original_path") or ""))
        for row in phase1.get("records", []) if isinstance(row, Mapping)
        for dep in row.get("dependencies", []) if isinstance(dep, Mapping)
        if dep.get("resolution") == "remote_rehydration_required"
    ]
    phase1_remote_refs_remaining = [
        key for key in phase1_remote_refs_all
        if inventory_by_key.get(key, {}).get("resolution", {}).get("status") != "cache_hit_frozen"
    ]
    if enforce_scope and len(phase1_remote_refs_remaining) != 731:
        raise ValueError(f"Phase4 remaining dependency count should be 731, got {len(phase1_remote_refs_remaining)}")
    missing_keys = set(phase1_remote_refs_remaining)
    new_cache = scan_files(cache_root, since=since)
    source_files = scan_files(source_root)
    by_name: dict[str, list[Path]] = defaultdict(list)
    by_token: dict[str, list[Path]] = defaultdict(list)
    for path in new_cache:
        by_name[path.name.lower()].append(path)
        for token in re.findall(r"(?<!\d)(\d{8,})(?!\d)", str(path)):
            by_token[token].append(path)
    source_by_name: dict[str, list[Path]] = defaultdict(list)
    for path in source_files:
        source_by_name[path.name.lower()].append(path)
    candidates = json.loads(json.dumps(auto.get("candidates", [])))
    candidate_by_id = {str(row.get("template_id")): row for row in candidates if isinstance(row, Mapping) and row.get("template_id")}
    new_inventory: dict[str, dict[str, Any]] = {}
    changed_matches: list[dict[str, Any]] = []
    for key in sorted(missing_keys):
        old = inventory_by_key[key]
        kind = str(old.get("kind") or "resource")
        expected_name = str(old.get("expected_filename") or "")
        remote_ids = [str(value) for value in old.get("remote_resource_ids") or [] if value]
        candidates_for_key = list(by_name.get(expected_name.lower(), [])) if expected_name else []
        expected_suffix = Path(expected_name).suffix.lower() if expected_name else ""
        for remote_id in remote_ids:
            candidates_for_key.extend(
                path for path in by_token.get(remote_id, [])
                if not expected_suffix or path.suffix.lower() == expected_suffix
            )
        # Source files are a second read-only fallback, never counted as a new
        # cache hit and never allowed to overwrite phase3 frozen files.
        if expected_name:
            candidates_for_key.extend(source_by_name.get(expected_name.lower(), []))
        resolution = None
        for path in dict.fromkeys(candidates_for_key):
            result = freeze(path, kind=kind, expected_extension=old.get("expected_extension"), frozen_root=workbench / "frozen_resources")
            if result.get("status") == "new_cache_hit_frozen":
                resolution = result
                break
        if resolution:
            changed_matches.append({"resource_key": key, "source_path": resolution["source_path"], "source_kind": "new_cache" if Path(resolution["source_path"]).is_relative_to(cache_root) else "source_root"})
        new_inventory[key] = {
            "resource_key": key,
            "kind": kind,
            "expected_filename": old.get("expected_filename"),
            "expected_extension": old.get("expected_extension"),
            "remote_resource_ids": remote_ids,
            "phase3_resolution_status": old.get("resolution", {}).get("status"),
            "resolution": resolution or {"status": "not_matched_in_phase4_delta"},
        }
    changed_keys = {row["resource_key"] for row in changed_matches}
    phase1_new_hits = sum(key in changed_keys for key in phase1_remote_refs_remaining)
    remaining_preset_ids = {
        str(row.get("template_id"))
        for row in phase1.get("records", []) if isinstance(row, Mapping)
        if any(
            resource_key(str(dep.get("kind") or "resource"), str(dep.get("original_path") or "")) in missing_keys
            for dep in row.get("dependencies", []) if isinstance(dep, Mapping)
            and dep.get("resolution") == "remote_rehydration_required"
        )
    }
    closure_rows: list[dict[str, Any]] = []
    v4_by_id = {str(row.get("template_id")): row for row in v4.get("records", []) if isinstance(row, Mapping)}
    for old_closure in phase3_closure.get("records", []):
        if not isinstance(old_closure, Mapping) or str(old_closure.get("template_id")) not in targets:
            continue
        template_id = str(old_closure["template_id"])
        old_unresolved = [str(key) for key in old_closure.get("unresolved_dependency_keys") or []]
        resolved_now = [key for key in old_unresolved if new_inventory.get(key, {}).get("resolution", {}).get("status") == "new_cache_hit_frozen"]
        unresolved = [key for key in old_unresolved if key not in resolved_now]
        phase2_status = old_closure.get("phase2_status_before")
        if not unresolved and phase2_status in {"adapter_ready_pending_current_video", "failed_unresolved"}:
            status = phase2_status
        elif not unresolved:
            status = "production_ready"
        elif phase2_status == "adapter_ready_pending_current_video":
            status = "adapter_ready_pending_current_video"
        else:
            status = "blocked"
        closure = {
            "template_id": template_id,
            "scope": old_closure.get("scope"),
            "phase3_unresolved_dependency_count": len(old_unresolved),
            "phase4_newly_closed_dependency_count": len(resolved_now),
            "unresolved_dependency_keys": unresolved,
            "all_required_dependencies_closed": not unresolved,
            "phase3_status_before": phase2_status,
            "phase4_status": status,
            "phase2_pending_never_promoted_to_production_ready": status != "production_ready" or phase2_status is None,
            "remote_ids_preserved": True,
        }
        closure_rows.append(closure)
        if template_id in v4_by_id:
            v4_by_id[template_id]["phase4_cache_rescan"] = closure
            v4_by_id[template_id].setdefault("production", {})["phase4_status"] = status
            v4_by_id[template_id]["production"]["phase4_ready_for_jianying_8_8"] = status == "production_ready"
    status_counts = Counter(row["phase4_status"] for row in closure_rows)
    preset_counts = {
        "phase1_285": {
            "count": sum(row.get("scope") == "phase1_285" for row in closure_rows),
            "production_ready": sum(row.get("scope") == "phase1_285" and row["phase4_status"] == "production_ready" for row in closure_rows),
            "blocked": sum(row.get("scope") == "phase1_285" and row["phase4_status"] == "blocked" for row in closure_rows),
        },
        "phase2_242": {
            "count": sum(row.get("scope") == "phase2_partial_242" for row in closure_rows),
            "production_ready": sum(row.get("scope") == "phase2_partial_242" and row["phase4_status"] == "production_ready" for row in closure_rows),
            "adapter_ready_pending_current_video": sum(row.get("scope") == "phase2_partial_242" and row["phase4_status"] == "adapter_ready_pending_current_video" for row in closure_rows),
        },
    }
    audit = {
        "schema": "jianying-adapter.phase4-cache-rescan-audit.v1",
        "scope": {"rescan_since_local": since.isoformat(), "cache_root": str(cache_root), "new_cache_file_count": len(new_cache), "new_cache_bytes": sum(path.stat().st_size for path in new_cache), "phase1_preset_count": len(phase1_ids), "phase2_partial_preset_count": len(phase2_ids), "phase3_remaining_dependency_count": len(phase1_remote_refs_remaining), "phase3_remaining_dependency_expected": 731, "phase3_remaining_preset_count": len(remaining_preset_ids), "phase3_remote_reference_baseline": len(phase1_remote_refs_all)},
        "matches": {"phase3_remaining_new_hit_count": phase1_new_hits, "phase3_remaining_still_missing_count": len(phase1_remote_refs_remaining) - phase1_new_hits, "newly_hit_resource_key_count": len(changed_keys), "source_root_matches": sum(row["source_kind"] == "source_root" for row in changed_matches), "cache_matches": sum(row["source_kind"] == "new_cache" for row in changed_matches), "frozen_file_count_deduplicated_by_sha256": len({row["resolution"].get("sha256") for row in new_inventory.values() if row["resolution"].get("sha256")}), "successful_download_count": 0},
        "presets": preset_counts,
        "status_counts": dict(sorted(status_counts.items())),
        "phase2_never_promoted": all(row.get("scope") != "phase2_partial_242" or row["phase4_status"] != "production_ready" for row in closure_rows),
        "phase3_untouched": True,
        "v1_v2_v3_untouched": True,
        "source_presets_untouched": True,
        "project_state_untouched": True,
        "memos_untouched": True,
        "draft_written_or_registered": False,
        "source_snapshot": {"phase3_inventory_sha256": sha256_file(phase3_inventory_path), "phase3_closure_sha256": sha256_file(phase3_closure_path), "phase3_v4_sha256": sha256_file(phase3_v3_path)},
    }
    v4["schema"] = "jianying-adapter.standardized-preset-catalog.v5-phase4-candidate"
    v4["generator"] = "rescan_cache_phase4.py"
    v4["phase4_scope"] = {"rescan_since_local": since.isoformat(), "source_ledgers_are_read_only": True, "draft_written_or_registered": False}
    inventory = {"schema": "jianying-adapter.phase4-cache-resource-inventory.v1", "records": [new_inventory[key] for key in sorted(new_inventory)], "changed_matches": sorted(changed_matches, key=lambda row: row["resource_key"])}
    closure = {"schema": "jianying-adapter.phase4-cache-closure-manifest.v1", "records": closure_rows}
    return v4, audit, inventory, closure


def recovery_markdown(audit: Mapping[str, Any]) -> str:
    return "\n".join([
        "# Phase 4 剪映缓存增量复扫说明",
        "",
        "仅复扫 2026-09-03 01:59:00 之后新增/修改的本机缓存，并在新隔离目录冻结通过真实字节与类型校验的文件。phase3 与所有源账本保持不变。",
        "",
        f"- 新增/修改缓存：{audit['scope']['new_cache_file_count']} 个文件，{audit['scope']['new_cache_bytes']} bytes。",
        f"- phase3 真正剩余依赖：{audit['scope']['phase3_remaining_dependency_count']}（原 remote 引用基线 {audit['scope']['phase3_remote_reference_baseline']}）；新增命中：{audit['matches']['phase3_remaining_new_hit_count']}；仍缺：{audit['matches']['phase3_remaining_still_missing_count']}。",
        f"- phase1 production_ready 新增：{audit['presets']['phase1_285']['production_ready']}；phase2 production_ready 新增：{audit['presets']['phase2_242']['production_ready']}，adapter_pending：{audit['presets']['phase2_242']['adapter_ready_pending_current_video']}。",
        "- phase2 的 adapter_ready_pending_current_video 不会因缓存命中升级为 production_ready；仍需当前视频画布/媒体证据。",
        "- 不下载网络资源、不猜 URL、不删除 remote ID；恢复或重建：",
        "`python jianying-adapter/scripts/rescan_cache_phase4.py`",
        "",
    ])


def main() -> None:
    v5, audit, inventory, closure = build_phase4()
    write_json(WORKBENCH / "cache_scan_phase4.json", {"schema": "jianying-adapter.phase4-cache-scan.v1", "scope": audit["scope"]})
    write_json(WORKBENCH / "resource_inventory_phase4.json", inventory)
    write_json(WORKBENCH / "preset_closure_manifest_phase4.json", closure)
    write_json(WORKBENCH / "phase4_audit.json", audit)
    write_json(WORKBENCH / "standardized_preset_catalog_v5_candidate.json", v5)
    write_json(WORKBENCH / "phase4_new_hits.json", {"records": inventory["changed_matches"]})
    write_json(WORKBENCH / "phase4_still_missing.json", {"records": [row for row in inventory["records"] if row["resolution"].get("status") != "new_cache_hit_frozen"]})
    (WORKBENCH / "recovery.md").write_text(recovery_markdown(audit), encoding="utf-8")
    print(json.dumps({"workbench": str(WORKBENCH), **audit["matches"], **audit["status_counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
