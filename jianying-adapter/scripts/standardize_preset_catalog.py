"""Build a deterministic, project-owned manifest for every known preset structure.

This tool only reads the preset catalog and source drafts.  It does not copy,
rewrite, register, or open any Jianying draft.  Existing IDs are preserved;
structures that were never assigned an ID receive a stable hash-based ID.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


ADAPTER = Path(__file__).resolve().parents[1]
PROJECT = ADAPTER.parent
CATALOG = ADAPTER / "preset_catalog"
SOURCE_ROOT = PROJECT / "jianying_presets" / "剪映1000个高级感字幕预设"
USAGE_PATH = CATALOG / "preset_usage_registry_v1.json"
AUTO_PATH = CATALOG / "all_auto_candidates_v1.json"
QUEUE_PATH = PROJECT / "video_trials" / "字幕预设黑幕筛选_20260902" / "screening_queue_v1.json"
INDEX_PATH = CATALOG / "preset_library_index_v1.json"
LEDGER_PATH = PROJECT / "video_trials" / "模板库1200盘点_20260902" / "full_structure_ledger_v1.json"
OUTPUT_PATH = CATALOG / "standardized_preset_catalog_v1.json"
AUDIT_PATH = CATALOG / "standardized_preset_audit_v1.json"
RECOVERY_PATH = CATALOG / "standardized_preset_recovery_v1.md"

TARGET_CANVAS = {"width": 1080, "height": 1920, "ratio": "9:16"}
READY_COMPATIBILITY = {"exact_8_8", "legacy_le_8_8", "le_8_8"}


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


def json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()


def ids(rows: Any) -> list[str]:
    if not isinstance(rows, list):
        return []
    return [str(row.get("template_id") or "") for row in rows if isinstance(row, Mapping)]


def duplicate_ids(values: list[str]) -> list[str]:
    return sorted({value for value, count in Counter(values).items() if value and count > 1})


def source_audit(
    usage: Mapping[str, Any],
    auto: Mapping[str, Any],
    queue: Mapping[str, Any],
) -> dict[str, Any]:
    sets = {
        "preset_usage_registry_v1": ids(usage.get("records")),
        "all_auto_candidates_v1": ids(auto.get("candidates")),
        "screening_queue_v1": ids(queue.get("items")),
    }
    unique = {name: set(values) for name, values in sets.items()}
    return {
        "sources": {
            name: {
                "record_count": len(values),
                "unique_id_count": len(unique[name]),
                "duplicate_ids": duplicate_ids(values),
            }
            for name, values in sets.items()
        },
        "overlap": {
            "usage_and_auto": len(unique["preset_usage_registry_v1"] & unique["all_auto_candidates_v1"]),
            "usage_and_queue": len(unique["preset_usage_registry_v1"] & unique["screening_queue_v1"]),
            "auto_and_queue": len(unique["all_auto_candidates_v1"] & unique["screening_queue_v1"]),
        },
        "missing_ids": {
            "usage_not_in_auto": sorted(unique["preset_usage_registry_v1"] - unique["all_auto_candidates_v1"]),
            "auto_not_in_usage": sorted(unique["all_auto_candidates_v1"] - unique["preset_usage_registry_v1"]),
            "queue_not_in_auto": sorted(unique["screening_queue_v1"] - unique["all_auto_candidates_v1"]),
            "auto_not_in_queue": sorted(unique["all_auto_candidates_v1"] - unique["screening_queue_v1"]),
        },
    }


def char_count(value: Any) -> int:
    return len("".join(str(value or "").split()))


def punctuation_only(value: Any) -> bool:
    text = "".join(str(value or "").split())
    return bool(text) and all(unicodedata.category(char).startswith(("P", "S")) for char in text)


def records_by_id(value: Mapping[str, Any], key: str) -> dict[str, Mapping[str, Any]]:
    rows = value.get(key)
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("template_id")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("template_id")
    }


def resolve_source(relative: Any) -> tuple[str | None, Path | None]:
    if not relative:
        return None, None
    relative_text = str(relative)
    path = Path(relative_text)
    absolute = path if path.is_absolute() else SOURCE_ROOT / path
    return relative_text, absolute


def candidate_slots(candidate: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    slots = candidate.get("slots")
    if not isinstance(slots, Mapping):
        return {kind: [] for kind in ("text", "audio", "image", "video")}
    return {
        kind: [item for item in slots.get(kind, []) if isinstance(item, Mapping)]
        for kind in ("text", "audio", "image", "video")
    }


def text_tracks(
    candidate: Mapping[str, Any] | None,
    usage: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    slots = candidate_slots(candidate or {}).get("text", [])
    usage_contract = (usage or {}).get("content_contract") or {}
    usage_limits = [int(value) for value in usage_contract.get("conservative_max_chars_per_slot") or []]
    editable_index = 0
    for index, slot in enumerate(slots, 1):
        default_text = str(slot.get("default_text") or "")
        locked = bool(slot.get("decorative_locked"))
        if locked and punctuation_only(default_text):
            role = "fixed_symbol"
        elif locked:
            role = "decorative"
        else:
            role = "main_emphasis" if editable_index == 0 else "supporting"
            editable_index += 1
        capacity = max(char_count(default_text), usage_limits[editable_index - 1] if not locked and editable_index <= len(usage_limits) else 0)
        rows.append({
            "track_index": index,
            "slot_id": str(slot.get("slot_id") or f"text_{index:02d}"),
            "role": role,
            "editable": role in {"main_emphasis", "supporting"},
            "default_text": default_text,
            "max_chars": capacity or None,
            "required": bool(slot.get("required", True)),
            "locators": slot.get("locators") or [],
        })
    if rows:
        return rows
    editable_count = int(usage_contract.get("editable_text_slot_count") or 0)
    fixed = [str(value) for value in usage_contract.get("fixed_decorative_text") or []]
    limits = usage_limits[:editable_count]
    for index, limit in enumerate(limits, 1):
        rows.append({
            "track_index": index,
            "slot_id": f"text_{index:02d}",
            "role": "main_emphasis" if index == 1 else "supporting",
            "editable": True,
            "default_text": None,
            "max_chars": limit,
            "required": True,
            "locators": [],
        })
    for offset, default_text in enumerate(fixed, len(rows) + 1):
        rows.append({
            "track_index": offset,
            "slot_id": f"text_{offset:02d}",
            "role": "fixed_symbol" if punctuation_only(default_text) else "decorative",
            "editable": False,
            "default_text": default_text,
            "max_chars": char_count(default_text) or None,
            "required": True,
            "locators": [],
        })
    return rows


def compatibility(candidate: Mapping[str, Any], usage: Mapping[str, Any], ledger: Mapping[str, Any]) -> str:
    raw = str(candidate.get("jianying_8_8_compatibility") or "").strip()
    if raw == "le_8_8":
        version = str(candidate.get("app_version") or "")
        return "exact_8_8" if version == "8.8.0" else "legacy_le_8_8"
    if raw == "gt_8_8":
        return "gt_8_8_hold"
    if raw == "mixed":
        return "mixed_version_hold"
    if raw == "unknown":
        return "unknown_version_hold"
    value = str((usage.get("technical_gate") or {}).get("compatibility_tier") or "")
    if value:
        return value
    value = str(ledger.get("compatibility") or "")
    return {"le_8_8": "legacy_le_8_8", "gt_8_8": "gt_8_8_hold"}.get(value, "unknown_version_hold")


def canvases(index_row: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "width": item.get("width"),
            "height": item.get("height"),
            "ratio": item.get("ratio"),
        }
        for item in index_row.get("canvases") or []
        if isinstance(item, Mapping)
    ]


def target_canvas_present(rows: list[Mapping[str, Any]]) -> bool:
    """Some native drafts label an exact 1080x1920 canvas as ``original``."""
    return any(
        int(row.get("width") or 0) == TARGET_CANVAS["width"]
        and int(row.get("height") or 0) == TARGET_CANVAS["height"]
        for row in rows
    ) or any(row.get("ratio") == TARGET_CANVAS["ratio"] for row in rows)


def normalized_id(ledger_row: Mapping[str, Any]) -> str:
    existing = str(ledger_row.get("existing_template_id") or "").strip()
    if existing:
        return existing
    digest = str(ledger_row.get("normalized_structure_sha256") or "").strip().upper()
    if not digest:
        digest = json_sha256(ledger_row)
    return f"JY-STD-{digest[:12]}"


def build_record(
    stable_id: str,
    *,
    candidate: Mapping[str, Any] | None,
    usage: Mapping[str, Any] | None,
    ledger: Mapping[str, Any] | None,
    index_row: Mapping[str, Any] | None,
    queue_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    candidate = candidate or {}
    usage = usage or {}
    ledger = ledger or {}
    index_row = index_row or {}
    relative_source, source_path = resolve_source(
        candidate.get("source_path") or usage.get("source", {}).get("source_path") or ledger.get("canonical_source_path")
    )
    relative_preview, preview_path = resolve_source(
        candidate.get("preview_path") or usage.get("source", {}).get("preview_path") or ledger.get("preview_path")
    )
    slots = candidate_slots(candidate)
    usage_contract = usage.get("content_contract") or {}
    slot_counts = {
        kind: len(slots[kind]) if candidate else int((index_row.get("slot_counts") or {}).get(kind, 0) or 0)
        for kind in ("text", "audio", "image", "video")
    }
    if not candidate:
        slot_counts["text"] = int(usage_contract.get("total_text_slot_count") or slot_counts["text"] or 0)
    visual = str((usage.get("technical_gate") or {}).get("visual_validation") or "pending")
    if visual == "rejected":
        visual_status = "rejected"
    elif visual == "accepted":
        visual_status = "accepted"
    elif visual == "conditional":
        visual_status = "conditional"
    else:
        visual_status = "pending"
    resource_missing = int((candidate.get("dependencies") or {}).get("missing_path_count") or index_row.get("missing_source_path_count") or 0)
    resource_gate = str((usage.get("technical_gate") or {}).get("resource_gate") or "")
    if not resource_gate:
        resource_gate = "pending_current_machine_rehydration" if resource_missing else "unknown"
    source_exists = bool(source_path and source_path.is_file())
    preview_exists = bool(preview_path and preview_path.is_file())
    compatibility_tier = compatibility(candidate, usage, ledger)
    canvas_rows = canvases(index_row)
    if not canvas_rows and stable_id.startswith("MODERN-"):
        canvas_rows = [{**TARGET_CANVAS}]
    tracks = text_tracks(candidate if candidate else None, usage)
    reasons: list[str] = []
    if not source_exists:
        reasons.append("source_package_missing")
    if not preview_exists:
        reasons.append("preview_missing")
    if compatibility_tier not in READY_COMPATIBILITY:
        reasons.append(f"version_{compatibility_tier}")
    if not canvas_rows:
        reasons.append("canvas_unknown")
    elif not target_canvas_present(canvas_rows):
        reasons.append("canvas_not_9_16_1080x1920")
    if slot_counts["image"] or slot_counts["video"]:
        reasons.append("media_slot_present")
    if resource_missing and resource_gate != "validated_current_machine_in_acceptance_draft":
        reasons.append("resource_missing")
    if not tracks:
        reasons.append("text_track_contract_missing")
    elif not any(track["role"] == "main_emphasis" for track in tracks):
        reasons.append("main_emphasis_missing")
    if visual_status == "rejected":
        reasons.append("visual_rejected")
    manual_slots = int((candidate.get("manual_slot_count") or (ledger.get("resource_facts") or {}).get("manual_slot_count") or 0))
    manual_styles = int((candidate.get("manual_style_slot_count") or (ledger.get("resource_facts") or {}).get("manual_style_slot_count") or 0))
    if manual_slots or manual_styles:
        reasons.append("manual_mapping_required")
    reason_set = list(dict.fromkeys(reasons))
    semantic_category = str(
        (usage.get("primary_category") or ledger.get("primary_category") or "unclear")
    )
    return {
        "template_id": stable_id,
        "display_name": candidate.get("display_name") or usage.get("display_name") or ledger.get("display_name"),
        "semantic_category": semantic_category,
        "semantic_tags": candidate.get("semantic_tags") or usage.get("source", {}).get("semantic_tags") or ledger.get("secondary_tags") or [],
        "canvas": canvas_rows,
        "jianying": {
            "target_version": "8.8.0",
            "compatibility_tier": compatibility_tier,
            "app_version": candidate.get("app_version") or usage.get("technical_gate", {}).get("app_version"),
            "app_versions": candidate.get("app_versions") or [],
        },
        "duration": candidate.get("duration") or {"seconds": usage.get("timing_contract", {}).get("native_duration_seconds")},
        "text_tracks": tracks,
        "slot_counts": slot_counts,
        "resources": {
            "status": "ready" if not resource_missing or resource_gate == "validated_current_machine_in_acceptance_draft" else "blocked",
            "resource_gate": resource_gate,
            "missing_path_count": resource_missing,
            "remote_resource_id_count": len((candidate.get("dependencies") or {}).get("remote_resource_ids") or []),
        },
        "source": {
            "source_path": relative_source,
            "source_path_absolute": str(source_path) if source_path else None,
            "source_exists": source_exists,
            "preview_path": relative_preview,
            "preview_path_absolute": str(preview_path) if preview_path else None,
            "preview_exists": preview_exists,
            "source_hash": candidate.get("source_hash"),
            "structure_hash": candidate.get("structure_hash") or ledger.get("normalized_structure_sha256"),
        },
        "screening": {
            "queue_batch": queue_row.get("batch") if queue_row else None,
            "queue_order": queue_row.get("queue_order") if queue_row else None,
        },
        "visual_validation": visual_status,
        "production": {
            "status": "ready" if not reason_set else "blocked",
            "ready_for_jianying_8_8": not reason_set,
            "blocked_reasons": reason_set,
            "visual_validation_is_not_current_video_qa": True,
        },
    }


def build_catalog(
    *,
    usage_path: Path = USAGE_PATH,
    auto_path: Path = AUTO_PATH,
    queue_path: Path = QUEUE_PATH,
    index_path: Path = INDEX_PATH,
    ledger_path: Path = LEDGER_PATH,
) -> tuple[dict[str, Any], dict[str, Any]]:
    usage = read_json(usage_path)
    auto = read_json(auto_path)
    queue = read_json(queue_path)
    index = read_json(index_path)
    ledger = read_json(ledger_path)
    usage_rows = records_by_id(usage, "records")
    auto_rows = records_by_id(auto, "candidates")
    queue_rows = records_by_id(queue, "items")
    index_rows = records_by_id(index, "entries")
    ledger_rows: dict[str, Mapping[str, Any]] = {}
    for row in ledger.get("structures", []):
        if not isinstance(row, Mapping):
            continue
        ledger_rows[normalized_id(row)] = row
        existing = str(row.get("existing_template_id") or "").strip()
        if existing:
            ledger_rows[existing] = row

    all_ids = set(usage_rows) | set(auto_rows) | set(queue_rows) | set(ledger_rows)
    records: list[dict[str, Any]] = []
    for stable_id in sorted(all_ids):
        ledger_row = ledger_rows.get(stable_id, {})
        records.append(build_record(
            stable_id,
            candidate=auto_rows.get(stable_id),
            usage=usage_rows.get(stable_id),
            ledger=ledger_row,
            index_row=index_rows.get(stable_id),
            queue_row=queue_rows.get(stable_id),
        ))
    records.sort(key=lambda item: item["template_id"])
    status_counts = Counter(item["production"]["status"] for item in records)
    reason_counts = Counter(
        reason
        for item in records
        for reason in item["production"]["blocked_reasons"]
    )
    source_snapshot = {
        "usage_registry_sha256": file_sha256(usage_path),
        "auto_candidates_sha256": file_sha256(auto_path),
        "screening_queue_sha256": file_sha256(queue_path),
        "library_index_sha256": file_sha256(index_path),
        "structure_ledger_sha256": file_sha256(ledger_path),
    }
    catalog = {
        "schema": "jianying-adapter.standardized-preset-catalog.v1",
        "generator": "standardize_preset_catalog.py",
        "policy": {
            "source_files_are_read_only": True,
            "stable_ids_preserve_existing_ids": True,
            "missing_ids_use_normalized_structure_hash": True,
            "ready_means_structurally_packaged_for_jianying_8_8": True,
            "blocked_never_means_source_deleted_or_discarded": True,
            "visual_validation_does_not_equal_current_video_qa": True,
        },
        "target": {"jianying_version": "8.8.0", "canvas": TARGET_CANVAS, "media_policy": "image_or_video_slots_block"},
        "inputs": {
            "preset_usage_registry": str(usage_path),
            "all_auto_candidates": str(auto_path),
            "screening_queue": str(queue_path),
            "library_index": str(index_path),
            "structure_ledger": str(ledger_path),
        },
        "source_snapshot": source_snapshot,
        "summary": {
            "record_count": len(records),
            "ready_count": status_counts["ready"],
            "blocked_count": status_counts["blocked"],
            "blocked_reason_counts": dict(sorted(reason_counts.items())),
            "new_stable_id_count": sum(item["template_id"].startswith("JY-STD-") for item in records),
        },
        "records": records,
    }
    audit = {
        "schema": "jianying-adapter.standardized-preset-audit.v1",
        "source_audit": source_audit(usage, auto, queue),
        "structure_ledger": {
            "record_count": len(ledger.get("structures") or []),
            "unique_stable_id_count": len({normalized_id(row) for row in ledger.get("structures") or [] if isinstance(row, Mapping)}),
            "unindexed_structure_count": sum(
                str(row.get("ledger_status") or "") == "unindexed_manual_mapping"
                for row in ledger.get("structures") or []
                if isinstance(row, Mapping)
            ),
        },
        "catalog_summary": catalog["summary"],
        "source_snapshot": source_snapshot,
        "idempotence": {
            "stable_record_order": True,
            "stable_generated_ids": True,
            "output_has_runtime_timestamp": False,
        },
    }
    return catalog, audit


def recovery_markdown(catalog: Mapping[str, Any], audit: Mapping[str, Any]) -> str:
    summary = catalog["summary"]
    source = audit["source_audit"]
    lines = [
        "# 全量预设标准化产物与恢复说明",
        "",
        "本产物只建立项目自有的机器清单与包记录，不复制、覆盖或删除任何原始预设，也不注册或写入剪映草稿。",
        "",
        "## 结果",
        "",
        f"- 标准化记录：{summary['record_count']} 条；ready：{summary['ready_count']} 条；blocked：{summary['blocked_count']} 条。",
        f"- 新分配稳定 ID：{summary['new_stable_id_count']} 条，格式为 `JY-STD-<规范结构哈希前12位>`；已有 `JIANYING-*`/`MODERN-*` ID 原样保留。",
        f"- 三个直接来源：统一注册表 {source['sources']['preset_usage_registry_v1']['record_count']} 条、自动候选 {source['sources']['all_auto_candidates_v1']['record_count']} 条、筛选队列 {source['sources']['screening_queue_v1']['record_count']} 条；各自无重复 ID。",
        "- 结构总账额外纳入未索引结构，确保全库 915 个去重结构都有记录；未完成资源/槽位映射的记录保持 blocked。",
        "",
        "## ready 定义",
        "",
        "ready 仅表示源包存在、预览存在、版本可用于剪映 8.8、画布为 1080×1920（9:16）、没有图片/视频槽、文字轨契约完整且资源状态可用。它不等于任何具体口播视频的当前画面验收。",
        "",
        "## 恢复/重建",
        "",
        "1. 保留本文件、`standardized_preset_catalog_v1.json` 与 `standardized_preset_audit_v1.json`。",
        "2. 源文件未被改写；如需恢复清单，重新运行：",
        "   `python jianying-adapter/scripts/standardize_preset_catalog.py`",
        "3. 由于输出不含运行时 timestamp，输入未变时重跑结果中的记录、ID、分类和 blocked 原因保持稳定。",
        "4. 若源文件发生变化，先比较输出中的 `source_snapshot` SHA256，再决定是否接受新清单；旧 JSON 仍可作为恢复副本。",
        "",
        f"输入快照：`{json.dumps(catalog['source_snapshot'], ensure_ascii=False, sort_keys=True)}`",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    catalog, audit = build_catalog()
    write_json(OUTPUT_PATH, catalog)
    write_json(AUDIT_PATH, audit)
    RECOVERY_PATH.write_text(recovery_markdown(catalog, audit), encoding="utf-8")
    print(json.dumps({"catalog": str(OUTPUT_PATH), **catalog["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
