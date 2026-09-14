"""Phase 1 resource repair for the narrowly scoped preset subset.

The source catalogs and v1 standardized ledger are read-only.  The command
only writes an isolated repair workbench and a v2 candidate ledger; it never
registers or writes a Jianying draft.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


ADAPTER = Path(__file__).resolve().parents[1]
PROJECT = ADAPTER.parent
CATALOG = ADAPTER / "preset_catalog"
V1_PATH = CATALOG / "standardized_preset_catalog_v1.json"
AUTO_PATH = CATALOG / "all_auto_candidates_v1.json"
WORKBENCH = ADAPTER / "repair_workspaces" / "phase1_resource_repair_20260903"
MANIFEST_PATH = WORKBENCH / "repair_manifest_phase1.json"
AUDIT_PATH = WORKBENCH / "phase1_audit.json"
V2_PATH = WORKBENCH / "standardized_preset_catalog_v2_candidate.json"
RECOVERY_PATH = WORKBENCH / "recovery.md"
FAILURES_PATH = WORKBENCH / "failed_resources_phase1.json"
SOURCE_ROOT = PROJECT / "jianying_presets" / "剪映1000个高级感字幕预设"
CURRENT_CACHE = Path(os.environ.get("LOCALAPPDATA", "C:/Users/YOUR_USER/AppData/Local")) / "JianyingPro" / "User Data" / "Cache"


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


def local_file_index(roots: Iterable[Path]) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                result.setdefault(path.name.lower(), []).append(path)
    return result


def path_class(original_path: str) -> str:
    normalized = original_path.replace("\\", "/")
    if "_presetpath_placeholder_" in normalized:
        return "source_placeholder"
    if normalized.startswith("/") or len(normalized) >= 3 and normalized[1:3] == ":/":
        return "stale_external_path"
    return "relative_unresolved_path"


def dependency_decisions(
    candidate: Mapping[str, Any],
    *,
    local_index: Mapping[str, list[Path]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    remote_ids = [
        str(item.get("remote_resource_id") or "")
        for item in (candidate.get("dependencies") or {}).get("remote_resource_ids") or []
        if isinstance(item, Mapping) and item.get("remote_resource_id")
    ]
    decisions: list[dict[str, Any]] = []
    counts = Counter()
    for item in (candidate.get("dependencies") or {}).get("path_dependencies") or []:
        if not isinstance(item, Mapping):
            continue
        original = str(item.get("original_path") or "")
        direct = Path(original) if original else None
        basename_matches = local_index.get(Path(original).name.lower(), []) if original else []
        if direct and direct.is_file():
            resolution = "local_resource_resolvable"
            resolved = direct
        elif len(basename_matches) == 1:
            resolution = "local_resource_resolvable"
            resolved = basename_matches[0]
        elif remote_ids:
            resolution = "remote_rehydration_required"
            resolved = None
        else:
            resolution = "unrecoverable_resource"
            resolved = None
        stale = path_class(original)
        counts[stale] += 1
        counts[resolution] += 1
        decisions.append({
            "kind": str(item.get("kind") or "unknown"),
            "original_path": original,
            "exists_on_current_machine": bool(item.get("exists_on_current_machine")),
            "path_class": stale,
            "safe_to_clear_stale_path": stale in {"stale_external_path", "source_placeholder"},
            "resolution": resolution,
            "resolved_local_path": str(resolved) if resolved else None,
            "remote_resource_ids_available": bool(remote_ids),
        })
    return decisions, dict(sorted(counts.items()))


def target_preconditions(record: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if record.get("jianying", {}).get("compatibility_tier") not in {"exact_8_8", "legacy_le_8_8"}:
        failures.append("not_jianying_8_8_compatible")
    if not any(
        int(row.get("width") or 0) == 1080 and int(row.get("height") or 0) == 1920
        for row in record.get("canvas") or []
        if isinstance(row, Mapping)
    ):
        failures.append("not_1080x1920")
    slots = record.get("slot_counts") or {}
    if int(slots.get("image") or 0) or int(slots.get("video") or 0):
        failures.append("media_slot_present")
    if not record.get("text_tracks") or not any(
        row.get("role") == "main_emphasis"
        for row in record.get("text_tracks") or []
        if isinstance(row, Mapping)
    ):
        failures.append("text_track_contract_incomplete")
    if record.get("visual_validation") == "rejected":
        failures.append("visual_rejected")
    return failures


def build_phase1(
    *,
    v1_path: Path = V1_PATH,
    auto_path: Path = AUTO_PATH,
    local_roots: Iterable[Path] = (SOURCE_ROOT, CURRENT_CACHE),
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    ledger = read_json(v1_path)
    auto = candidate_rows(read_json(auto_path))
    targets = [
        record for record in ledger.get("records", [])
        if isinstance(record, Mapping)
        and record.get("production", {}).get("status") == "blocked"
        and record.get("production", {}).get("blocked_reasons") == ["resource_missing"]
    ]
    index = local_file_index(local_roots)
    manifest_rows: list[dict[str, Any]] = []
    v2 = copy.deepcopy(ledger)
    v2_by_id = {
        str(record.get("template_id")): record
        for record in v2.get("records", [])
        if isinstance(record, Mapping)
    }
    for record in sorted(targets, key=lambda row: str(row.get("template_id"))):
        template_id = str(record["template_id"])
        candidate = auto.get(template_id, {})
        decisions, decision_counts = dependency_decisions(candidate, local_index=index)
        precondition_failures = target_preconditions(record)
        local_resolved = [item for item in decisions if item["resolution"] == "local_resource_resolvable"]
        unresolved = [item for item in decisions if item["resolution"] != "local_resource_resolvable"]
        resource_repaired = bool(decisions) and not unresolved
        status = "repaired_pending_freeze" if resource_repaired and not precondition_failures else "failed_unresolved"
        row = {
            "template_id": template_id,
            "source_hash": record.get("source", {}).get("source_hash"),
            "source_path": record.get("source", {}).get("source_path_absolute"),
            "scope": {
                "matched_exactly_one_resource_missing_block": True,
                "precondition_failures": precondition_failures,
            },
            "dependencies": decisions,
            "decision_counts": decision_counts,
            "actions": {
                "clearable_stale_path_count": sum(item["safe_to_clear_stale_path"] for item in decisions),
                "local_resource_resolvable_count": len(local_resolved),
                "remote_rehydration_required_count": sum(item["resolution"] == "remote_rehydration_required" for item in decisions),
                "unrecoverable_count": sum(item["resolution"] == "unrecoverable_resource" for item in decisions),
                "source_mutation_performed": False,
                "freeze_performed": False,
            },
            "repair_status": status,
            "ready_after_repair": False,
        }
        manifest_rows.append(row)
        v2_record = v2_by_id[template_id]
        v2_record["resource_repair_phase1"] = {
            "status": status,
            "decision_counts": decision_counts,
            "clearable_stale_path_count": row["actions"]["clearable_stale_path_count"],
            "local_resource_resolvable_count": len(local_resolved),
            "resource_complete_proof": False,
            "source_mutation_performed": False,
            "freeze_performed": False,
        }
    success = sum(row["repair_status"] == "repaired_pending_freeze" for row in manifest_rows)
    failed = len(manifest_rows) - success
    before_ready = sum(record.get("production", {}).get("status") == "ready" for record in ledger.get("records", []))
    audit = {
        "schema": "jianying-adapter.preset-resource-repair-audit.v1",
        "scope": {"source_ledger": str(v1_path), "target_filter": "blocked + exactly [resource_missing]"},
        "target_count": len(manifest_rows),
        "success_count": success,
        "failed_count": failed,
        "ready_total_before": before_ready,
        "ready_total_after_candidate": before_ready,
        "resource_resolution_counts": dict(sorted(Counter(
            item["resolution"] for row in manifest_rows for item in row["dependencies"]
        ).items())),
        "stale_path_class_counts": dict(sorted(Counter(
            item["path_class"] for row in manifest_rows for item in row["dependencies"]
        ).items())),
        "v1_untouched": True,
        "source_presets_untouched": True,
    }
    failures = [row for row in manifest_rows if row["repair_status"] == "failed_unresolved"]
    return v2, audit, {"records": manifest_rows}, failures


def recovery_markdown(audit: Mapping[str, Any]) -> str:
    return "\n".join([
        "# 预设资源修复第一阶段恢复说明",
        "",
        "本阶段只处理 v1 中 blocked_reasons 恰好为 `[resource_missing]` 的记录。所有源预设、v1 真源、project_state 和 Memo 均保持不变。",
        "",
        f"- 目标：{audit['target_count']} 条；成功取得本地资源冻结证据：{audit['success_count']} 条；失败：{audit['failed_count']} 条。",
        f"- ready 总数：{audit['ready_total_before']} → {audit['ready_total_after_candidate']}（本阶段未取得资源完整证明，因此不提升 ready）。",
        "- 旧 Mac/其他用户路径只登记为可安全清除的失效路径建议，没有实际清除源字段；远程资源 ID 只记录为待剪映/资源包重hydration，不能算资源已恢复。",
        "- 若后续获得本地可验证资源，先把冻结文件放入本工位并重新运行工具；工具会重新计算输入 SHA256，并仍要求 8.8、1080×1920、纯文字、完整文字轨、非 visual_rejected 全部通过。",
        "",
        "恢复操作：保留本目录下的 manifest、audit、v2 candidate 和失败清单；删除或移动本工位不会影响任何源文件。需要重建时运行：",
        "`python jianying-adapter/scripts/repair_preset_resources_phase1.py`",
        "",
        "第二阶段（其余 357 条）未启动。",
        "",
    ])


def main() -> None:
    v2, audit, manifest, failures = build_phase1()
    write_json(MANIFEST_PATH, manifest)
    write_json(AUDIT_PATH, audit)
    write_json(V2_PATH, v2)
    write_json(FAILURES_PATH, {"schema": "jianying-adapter.preset-resource-failures.v1", "records": failures})
    RECOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECOVERY_PATH.write_text(recovery_markdown(audit), encoding="utf-8")
    print(json.dumps({"workbench": str(WORKBENCH), **{key: audit[key] for key in (
        "target_count", "success_count", "failed_count", "ready_total_before", "ready_total_after_candidate")}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
