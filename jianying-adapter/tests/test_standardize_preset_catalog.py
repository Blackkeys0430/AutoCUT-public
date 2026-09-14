from __future__ import annotations

import json
from pathlib import Path

from scripts import standardize_preset_catalog as module


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_inputs(tmp_path: Path) -> dict[str, Path]:
    source_root = tmp_path / "preset-root"
    source = source_root / "T-OLD" / "preset_draft" / "draft_content.json"
    preview = source_root / "T-OLD" / "preview.jpeg"
    source.parent.mkdir(parents=True)
    source.write_text("{}", encoding="utf-8")
    preview.write_bytes(b"preview")

    usage = tmp_path / "usage.json"
    write_json(usage, {"records": [{
        "template_id": "T-OLD",
        "display_name": "Old",
        "primary_category": "question_hook",
        "content_contract": {"total_text_slot_count": 3, "editable_text_slot_count": 2,
                              "conservative_max_chars_per_slot": [4, 6],
                              "fixed_decorative_text": ["？"]},
        "technical_gate": {"compatibility_tier": "exact_8_8", "resource_gate": "source_paths_complete",
                            "visual_validation": "pending"},
        "timing_contract": {"native_duration_seconds": 1.0},
        "source": {"source_path": "T-OLD/preset_draft/draft_content.json", "preview_path": "T-OLD/preview.jpeg"},
    }]})
    auto = tmp_path / "auto.json"
    write_json(auto, {"candidates": [{
        "template_id": "T-OLD", "display_name": "Old", "app_version": "8.8.0",
        "app_versions": ["8.8.0"], "jianying_8_8_compatibility": "le_8_8",
        "source_path": "T-OLD/preset_draft/draft_content.json", "preview_path": "T-OLD/preview.jpeg",
        "duration": {"seconds": 1.0}, "dependencies": {"missing_path_count": 0, "remote_resource_ids": []},
        "slots": {"text": [
            {"slot_id": "text_01", "default_text": "主标题", "required": True, "locators": []},
            {"slot_id": "text_02", "default_text": "补充", "required": True, "locators": []},
            {"slot_id": "text_03", "default_text": "？", "required": True, "decorative_locked": True, "locators": []},
        ], "audio": [], "image": [], "video": []},
    }]})
    queue = tmp_path / "queue.json"
    write_json(queue, {"items": [{"template_id": "T-OLD", "batch": 1, "queue_order": 1}]})
    index = tmp_path / "index.json"
    write_json(index, {"entries": [{"template_id": "T-OLD", "canvases": [{"width": 1080, "height": 1920, "ratio": "original"}],
                                     "slot_counts": {"text": 3, "audio": 0, "image": 0, "video": 0}}]})
    ledger = tmp_path / "ledger.json"
    write_json(ledger, {"structures": [
        {"existing_template_id": "T-OLD", "normalized_structure_sha256": "A" * 64,
         "canonical_source_path": "T-OLD/preset_draft/draft_content.json", "preview_path": "T-OLD/preview.jpeg",
         "primary_category": "question_hook"},
        {"normalized_structure_sha256": "B" * 64, "canonical_source_path": "T-MISSING/preset_draft/draft_content.json",
         "preview_path": "T-MISSING/preview.jpeg", "primary_category": "quote_conclusion",
         "ledger_status": "unindexed_manual_mapping", "resource_facts": {"manual_slot_count": 1}},
    ]})
    return {"usage_path": usage, "auto_path": auto, "queue_path": queue,
            "index_path": index, "ledger_path": ledger, "source_root": source_root}


def test_standardization_is_idempotent_and_assigns_roles(tmp_path: Path, monkeypatch) -> None:
    paths = make_inputs(tmp_path)
    monkeypatch.setattr(module, "SOURCE_ROOT", paths["source_root"])
    first, first_audit = module.build_catalog(**{key: paths[key] for key in (
        "usage_path", "auto_path", "queue_path", "index_path", "ledger_path")})
    second, second_audit = module.build_catalog(**{key: paths[key] for key in (
        "usage_path", "auto_path", "queue_path", "index_path", "ledger_path")})
    assert first == second
    assert first_audit == second_audit
    assert first["summary"]["record_count"] == 2
    old = next(row for row in first["records"] if row["template_id"] == "T-OLD")
    assert old["production"]["status"] == "ready"
    assert [row["role"] for row in old["text_tracks"]] == ["main_emphasis", "supporting", "fixed_symbol"]
    generated = next(row for row in first["records"] if row["template_id"].startswith("JY-STD-"))
    assert generated["production"]["status"] == "blocked"
    assert "manual_mapping_required" in generated["production"]["blocked_reasons"]


def test_source_audit_reports_duplicates_and_missing_ids() -> None:
    audit = module.source_audit(
        {"records": [{"template_id": "A"}, {"template_id": "A"}]},
        {"candidates": [{"template_id": "A"}, {"template_id": "B"}]},
        {"items": [{"template_id": "B"}, {"template_id": "C"}]},
    )
    assert audit["sources"]["preset_usage_registry_v1"]["duplicate_ids"] == ["A"]
    assert audit["missing_ids"]["auto_not_in_usage"] == ["B"]
    assert audit["missing_ids"]["queue_not_in_auto"] == ["C"]
