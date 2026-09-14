from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "jianying-adapter"
CATALOG_DIR = ADAPTER / "preset_catalog"
SOURCE_CATALOG = CATALOG_DIR / "all_auto_candidates_v1.json"
ACCEPTED_CATALOG = CATALOG_DIR / "subtitle_round2_shortlist_v1.json"
VISUAL_DECISIONS = CATALOG_DIR / "preset_library_visual_decisions_v1.json"
OUTPUT_JSON = CATALOG_DIR / "preset_library_index_v1.json"
OUTPUT_MD = CATALOG_DIR / "preset_library_summary_v1.md"
BATCH_SIZE = 12


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def compatibility_tier(candidate: Mapping[str, Any]) -> str:
    compatibility = str(candidate.get("jianying_8_8_compatibility") or "unknown")
    version = candidate.get("app_version")
    if compatibility == "le_8_8" and version == "8.8.0":
        return "exact_8_8"
    if compatibility == "le_8_8":
        return "legacy_le_8_8"
    if compatibility == "mixed":
        return "mixed_version_hold"
    if compatibility == "gt_8_8":
        return "gt_8_8_hold"
    return "unknown_version_hold"


def required_slots(candidate: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    slots = candidate.get("slots", {}).get(kind, [])
    return [
        slot
        for slot in slots
        if isinstance(slot, Mapping)
        and slot.get("required")
        and not slot.get("decorative_locked")
    ]


def slot_lengths(candidate: Mapping[str, Any]) -> list[int]:
    return [
        len(str(slot.get("default_text") or "").strip())
        for slot in required_slots(candidate, "text")
    ]


def canvas_inventory(source_path: Path) -> list[dict[str, Any]]:
    if not source_path.exists():
        return []
    try:
        payload = load_json(source_path)
    except (OSError, json.JSONDecodeError):
        return []

    drafts: list[Mapping[str, Any]] = []
    materials = payload.get("materials", {})
    if isinstance(materials, Mapping):
        for item in materials.get("drafts", []):
            if not isinstance(item, Mapping):
                continue
            inner = item.get("draft")
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except json.JSONDecodeError:
                    continue
            if isinstance(inner, Mapping):
                drafts.append(inner)
    if not drafts:
        drafts = [payload]

    canvases: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for draft in drafts:
        config = draft.get("canvas_config", {})
        if not isinstance(config, Mapping):
            continue
        row = {
            "width": config.get("width"),
            "height": config.get("height"),
            "ratio": config.get("ratio"),
        }
        key = (row["width"], row["height"], row["ratio"])
        if key in seen:
            continue
        seen.add(key)
        canvases.append(row)
    return canvases


def resource_gate(candidate: Mapping[str, Any], accepted: bool) -> str:
    if accepted:
        return "validated_current_machine_in_acceptance_draft"
    missing = int(candidate.get("dependencies", {}).get("missing_path_count") or 0)
    if missing:
        return "pending_current_machine_rehydration"
    return "source_paths_complete"


def known_visual_rejection(candidate: Mapping[str, Any]) -> str | None:
    name = str(candidate.get("display_name") or "")
    if name.startswith("李-"):
        return "用户已确认李系列基础动画视觉过旧，不再重复验收"
    return None


def build_entry(
    candidate: Mapping[str, Any],
    preset_root: Path,
    accepted_ids: set[str],
) -> dict[str, Any]:
    template_id = str(candidate.get("template_id"))
    source_relative = candidate.get("source_path")
    preview_relative = candidate.get("preview_path")
    source_path = preset_root / source_relative if source_relative else None
    preview_path = preset_root / preview_relative if preview_relative else None
    accepted = template_id in accepted_ids
    rejection_reason = None if accepted else known_visual_rejection(candidate)
    if accepted:
        library_status = "visual_accepted"
        visual_validation = "accepted"
    elif rejection_reason:
        library_status = "known_visual_rejected"
        visual_validation = "rejected"
    else:
        library_status = "cataloged_pending_visual"
        visual_validation = "pending"
    lengths = slot_lengths(candidate)
    slot_counts = {
        kind: len(required_slots(candidate, kind))
        for kind in ("text", "audio", "image", "video")
    }
    return {
        "template_id": template_id,
        "display_name": candidate.get("display_name"),
        "library_status": library_status,
        "visual_validation": visual_validation,
        "rejection_reason": rejection_reason,
        "guarded_use_eligible": accepted,
        "auto_select_eligible": False,
        "compatibility_tier": compatibility_tier(candidate),
        "app_version": candidate.get("app_version"),
        "app_versions": candidate.get("app_versions", []),
        "semantic_tags": candidate.get("semantic_tags", []),
        "duration_seconds": candidate.get("duration", {}).get("seconds"),
        "slot_counts": slot_counts,
        "observed_default_text_lengths": lengths,
        "text_capacity_status": "pending_per_slot_measurement",
        "source_path": source_relative,
        "source_exists": bool(source_path and source_path.exists()),
        "preview_path": preview_relative,
        "preview_exists": bool(preview_path and preview_path.exists()),
        "canvases": canvas_inventory(source_path) if source_path else [],
        "missing_source_path_count": int(
            candidate.get("dependencies", {}).get("missing_path_count") or 0
        ),
        "remote_resource_id_count": len(
            candidate.get("dependencies", {}).get("remote_resource_ids", [])
        ),
        "resource_gate": resource_gate(candidate, accepted),
        "placement_mode": (
            "per_video_adaptive_translate_xy"
            if accepted
            else "not_applicable_known_rejected"
            if rejection_reason
            else "pending_visual_validation"
        ),
        "selection_score": candidate.get("selection_score", {}).get("total"),
    }


def accepted_rows(shortlist: Mapping[str, Any]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    ids = set(shortlist.get("user_validation", {}).get("accepted_template_ids", []))
    rows: dict[str, dict[str, Any]] = {}
    for group in shortlist.get("groups", []):
        role = group.get("role")
        for candidate in group.get("candidates", []):
            template_id = str(candidate.get("template_id"))
            rows[template_id] = {**candidate, "semantic_role": role}
    return ids, rows


def external_accepted_entry(template_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "template_id": template_id,
        "display_name": row.get("name"),
        "library_status": "visual_accepted",
        "visual_validation": "accepted",
        "rejection_reason": None,
        "guarded_use_eligible": True,
        "auto_select_eligible": False,
        "compatibility_tier": "exact_8_8_external_validated",
        "app_version": "8.8.0",
        "app_versions": ["8.8.0"],
        "semantic_tags": [row.get("semantic_role")],
        "duration_seconds": row.get("duration_seconds"),
        "slot_counts": {
            "text": row.get("text_slots", 0),
            "audio": row.get("audio_slots", 0),
            "image": 0,
            "video": 0,
        },
        "observed_default_text_lengths": [],
        "text_capacity_status": "pending_per_slot_measurement",
        "source_path": None,
        "source_exists": True,
        "preview_path": None,
        "preview_exists": True,
        "canvases": [{"width": 1080, "height": 1920, "ratio": "9:16"}],
        "missing_source_path_count": 0,
        "remote_resource_id_count": 0,
        "resource_gate": "validated_current_machine_in_acceptance_draft",
        "placement_mode": "per_video_adaptive_translate_xy",
        "selection_score": None,
        "source_registry": "subtitle_round2_shortlist_v1.json",
    }


def apply_visual_decisions(entries: list[dict[str, Any]]) -> None:
    if not VISUAL_DECISIONS.exists():
        return
    decisions = load_json(VISUAL_DECISIONS)
    accepted = {str(value) for value in decisions.get("accepted", [])}
    conditional = {
        str(template_id): str(reason)
        for template_id, reason in decisions.get("conditional", {}).items()
    }
    overlap = accepted & set(conditional)
    if overlap:
        raise ValueError(f"visual decision overlap: {sorted(overlap)}")

    entry_by_id = {str(entry["template_id"]): entry for entry in entries}
    unknown = (accepted | set(conditional)) - set(entry_by_id)
    if unknown:
        raise ValueError(f"visual decisions reference unknown templates: {sorted(unknown)}")

    decision_source = str(VISUAL_DECISIONS.relative_to(ADAPTER)).replace("\\", "/")
    for template_id in accepted:
        entry = entry_by_id[template_id]
        entry.update(
            {
                "library_status": "visual_accepted",
                "visual_validation": "accepted",
                "rejection_reason": None,
                "guarded_use_eligible": True,
                "auto_select_eligible": False,
                "resource_gate": "validated_current_machine_in_acceptance_draft",
                "placement_mode": "per_video_adaptive_translate_xy",
                "visual_decision_source": decision_source,
            }
        )

    for template_id, reason in conditional.items():
        entry = entry_by_id[template_id]
        entry.update(
            {
                "library_status": "cataloged_pending_visual",
                "visual_validation": "conditional",
                "rejection_reason": None,
                "guarded_use_eligible": False,
                "auto_select_eligible": False,
                "resource_gate": "validated_current_machine_in_acceptance_draft",
                "placement_mode": "requires_per_video_collision_reposition",
                "conditional_reason": reason,
                "visual_decision_source": decision_source,
            }
        )


def pending_sort_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    tier_order = {
        "exact_8_8": 0,
        "legacy_le_8_8": 1,
        "mixed_version_hold": 2,
        "gt_8_8_hold": 3,
        "unknown_version_hold": 4,
    }
    score = entry.get("selection_score")
    return (
        tier_order.get(str(entry.get("compatibility_tier")), 99),
        int(entry.get("missing_source_path_count") or 0),
        -(float(score) if isinstance(score, (int, float)) else -9999.0),
        str(entry.get("display_name") or ""),
        str(entry.get("template_id") or ""),
    )


def chunked(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def build_batches(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending = [entry for entry in entries if entry["visual_validation"] == "pending"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in sorted(pending, key=pending_sort_key):
        grouped[str(entry["compatibility_tier"])].append(entry)

    prefix = {
        "exact_8_8": "E88",
        "legacy_le_8_8": "LE88",
        "mixed_version_hold": "MIX",
        "gt_8_8_hold": "GT88",
        "unknown_version_hold": "UNK",
    }
    batches: list[dict[str, Any]] = []
    for tier in ("exact_8_8", "legacy_le_8_8", "mixed_version_hold", "gt_8_8_hold", "unknown_version_hold"):
        for batch_number, batch in enumerate(chunked(grouped[tier], BATCH_SIZE), start=1):
            batches.append(
                {
                    "batch_id": f"{prefix[tier]}-{batch_number:03d}",
                    "compatibility_tier": tier,
                    "status": "ready_for_audition" if tier in {"exact_8_8", "legacy_le_8_8"} else "compatibility_hold",
                    "template_ids": [entry["template_id"] for entry in batch],
                }
            )
    return batches


def preserve_existing_batch_manifest(
    entries: list[dict[str, Any]],
    generated_batches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep issued batch IDs stable while a validation campaign is in progress."""
    if not OUTPUT_JSON.exists():
        return generated_batches
    try:
        previous = load_json(OUTPUT_JSON).get("validation_batches", [])
    except (OSError, json.JSONDecodeError):
        return generated_batches
    if not isinstance(previous, list) or not previous:
        return generated_batches

    entry_by_id = {str(entry["template_id"]): entry for entry in entries}
    issued_ids = [
        str(template_id)
        for batch in previous
        if isinstance(batch, Mapping)
        for template_id in batch.get("template_ids", [])
    ]
    if len(issued_ids) != len(set(issued_ids)) or not set(issued_ids) <= set(entry_by_id):
        return generated_batches

    batches: list[dict[str, Any]] = []
    for old_batch in previous:
        batch = dict(old_batch)
        template_ids = [str(value) for value in batch.get("template_ids", [])]
        validations = [entry_by_id[value]["visual_validation"] for value in template_ids]
        tier = str(batch.get("compatibility_tier"))
        if validations and all(value != "pending" for value in validations):
            batch["status"] = "visual_qa_complete"
        elif tier in {"exact_8_8", "legacy_le_8_8"}:
            batch["status"] = "ready_for_audition"
        else:
            batch["status"] = "compatibility_hold"
        batches.append(batch)
    return batches


def markdown_summary(library: Mapping[str, Any]) -> str:
    summary = library["summary"]
    tiers = summary["pending_by_compatibility_tier"]
    lines = [
        "# 剪映预设全量入库摘要 v1",
        "",
        "这份索引登记全部779个去重结构代表，并合并2个不在原779中的现代已验收预设。入库不等于视觉通过。",
        "",
        f"- 原候选登记：{summary['source_candidate_count']} 条",
        f"- 统一库唯一ID：{summary['unique_library_count']} 条",
        f"- 用户视觉已通过：{summary['visual_accepted_count']} 条",
        f"- 已确认视觉淘汰：{summary['known_visual_rejected_count']} 条",
        f"- 尚未逐款视觉验收：{summary['pending_visual_count']} 条，按具体视频选用后检查",
        f"- 保留的旧筛选分组：{summary['batch_count']} 组，每组最多 {BATCH_SIZE} 条（兼容索引，不是当前验收队列）",
        "",
        "## 未逐款验收条目的兼容性分布",
        "",
        f"- 剪映8.8原生：{tiers.get('exact_8_8', 0)} 条",
        f"- 低于或等于8.8的旧版：{tiers.get('legacy_le_8_8', 0)} 条",
        f"- 混合版本暂缓：{tiers.get('mixed_version_hold', 0)} 条",
        f"- 高于8.8暂缓：{tiers.get('gt_8_8_hold', 0)} 条",
        f"- 版本未知暂缓：{tiers.get('unknown_version_hold', 0)} 条",
        "",
        "## 使用门禁",
        "",
        "- visual_accepted 只表示用户已看过原生动态与位置适配；文字容量仍需逐槽检查。",
        "- cataloged_pending_visual 不是自动候选禁用标记；分类与视觉通过分别记录，按本片用途检查入选项。",
        "- 当前选择和使用要求统一见[使用库 policy](preset_usage_registry_v1.json)及[使用说明](preset_usage_guide_v1.md)。不安排整库逐款主观验收。",
        "- known_visual_rejected 保留用于历史追踪和去重，不再进入动态验收队列。",
        "- 所有旧来源路径都按当前机器重新恢复或重链；源包旧路径存在不等于当前机器资源完整。",
        "- 已通过样例仍须按当前主体、文案、时长和构图适配；样例通过不等于当前视频通过。",
        "",
    ]
    return "\n".join(lines)


def build_library() -> dict[str, Any]:
    source = load_json(SOURCE_CATALOG)
    shortlist = load_json(ACCEPTED_CATALOG)
    accepted_ids, accepted_metadata = accepted_rows(shortlist)
    preset_root = Path(source["preset_root"])

    entries = [
        build_entry(candidate, preset_root, accepted_ids)
        for candidate in source.get("candidates", [])
    ]
    source_ids = {entry["template_id"] for entry in entries}
    for template_id in sorted(accepted_ids - source_ids):
        entries.append(external_accepted_entry(template_id, accepted_metadata[template_id]))
    entries.sort(key=lambda entry: str(entry["template_id"]))

    apply_visual_decisions(entries)

    batches = preserve_existing_batch_manifest(entries, build_batches(entries))
    pending = [
        entry for entry in entries
        if entry["library_status"] == "cataloged_pending_visual"
    ]
    tier_counts = Counter(str(entry["compatibility_tier"]) for entry in pending)
    status_counts = Counter(str(entry["library_status"]) for entry in entries)
    library = {
        "schema": "jianying-adapter.preset-library-index.v1",
        "source_catalog": str(SOURCE_CATALOG.relative_to(ADAPTER)).replace("\\", "/"),
        "accepted_catalog": str(ACCEPTED_CATALOG.relative_to(ADAPTER)).replace("\\", "/"),
        "visual_decisions": str(VISUAL_DECISIONS.relative_to(ADAPTER)).replace("\\", "/"),
        "policy": {
            "ingestion_does_not_equal_visual_acceptance": True,
            "usage_policy_source": "preset_usage_registry_v1.json#policy",
            "validation_batches_role": "legacy_grouping_only_not_current_review_queue",
            "batch_size": BATCH_SIZE,
            "production_auto_select_requires_visual_semantic_capacity_and_resource_gates": True,
            "placement_for_accepted_presets": "per_video_adaptive_translate_xy",
        },
        "summary": {
            "source_candidate_count": len(source.get("candidates", [])),
            "accepted_overlap_with_source": len(accepted_ids & source_ids),
            "accepted_external_count": len(accepted_ids - source_ids),
            "unique_library_count": len(entries),
            "visual_accepted_count": status_counts["visual_accepted"],
            "known_visual_rejected_count": status_counts["known_visual_rejected"],
            "pending_visual_count": status_counts["cataloged_pending_visual"],
            "pending_by_compatibility_tier": dict(sorted(tier_counts.items())),
            "batch_count": len(batches),
        },
        "entries": entries,
        "validation_batches": batches,
    }
    return library


def main() -> None:
    library = build_library()
    OUTPUT_JSON.write_text(json.dumps(library, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_MD.write_text(markdown_summary(library), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT_JSON), **library["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
