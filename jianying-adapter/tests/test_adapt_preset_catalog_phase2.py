from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts import adapt_preset_catalog_phase2 as module


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def make_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    v1_path = tmp_path / "standardized_preset_catalog_v1.json"
    auto_path = tmp_path / "all_auto_candidates_v1.json"
    phase1_path = tmp_path / "phase1_manifest.json"
    base = {
        "source": {"source_hash": "a" * 64},
        "jianying": {"compatibility_tier": "legacy_le_8_8"},
        "visual_validation": "pending",
        "production": {"status": "blocked", "blocked_reasons": []},
    }
    partial = copy.deepcopy(base)
    partial.update({
        "template_id": "PARTIAL-01",
        "canvas": [{"width": 1920, "height": 1080, "ratio": "16:9"}],
        "slot_counts": {"text": 1, "audio": 0, "image": 0, "video": 0},
        "text_tracks": [{"track_index": 1, "slot_id": "text_01", "role": "main_emphasis",
                         "editable": True, "max_chars": 8, "default_text": "标题"}],
        "production": {"status": "blocked", "blocked_reasons": ["canvas_not_9_16_1080x1920", "resource_missing"]},
    })
    unknown = copy.deepcopy(base)
    unknown.update({
        "template_id": "UNKNOWN-01",
        "canvas": [],
        "slot_counts": {"text": 0, "audio": 0, "image": 0, "video": 0},
        "text_tracks": [],
        "production": {"status": "blocked", "blocked_reasons": ["canvas_unknown", "text_track_contract_missing", "manual_mapping_required"]},
    })
    excluded = []
    for index, reasons in enumerate((
        ["resource_missing"],
        ["version_gt_8_8_hold"],
        ["visual_rejected"],
        ["source_package_missing"],
    ), 1):
        row = copy.deepcopy(base)
        row.update({"template_id": f"EXCLUDED-{index:02d}", "production": {"status": "blocked", "blocked_reasons": reasons}})
        excluded.append(row)
    write_json(v1_path, {"records": [partial, unknown, *excluded]})
    write_json(auto_path, {"candidates": [{
        "template_id": "PARTIAL-01",
        "slots": {"text": [{"slot_id": "text_01", "default_text": "标题", "required": True}], "image": [], "video": []},
        "dependencies": {
            "path_dependencies": [{"kind": "resource", "original_path": "/old/missing.bin"}],
            "remote_resource_ids": [{"kind": "resource", "remote_resource_id": "remote-1"}],
        },
    }]})
    write_json(phase1_path, {"records": []})
    return v1_path, auto_path, phase1_path


def test_phase2_exact_scope_and_grouped_statuses(tmp_path: Path) -> None:
    v1_path, auto_path, phase1_path = make_inputs(tmp_path)
    v3, audit, manifest, lists = module.build_phase2(
        v1_path=v1_path,
        auto_path=auto_path,
        phase1_manifest_path=phase1_path,
        local_roots=(tmp_path / "no-local-resources",),
        enforce_target_count=False,
    )
    assert audit["scope"]["target_count"] == 2
    assert audit["counts"]["partially_adapted"] == 1
    assert audit["counts"]["failed"] == 1
    rows = {row["template_id"]: row for row in manifest["records"]}
    assert rows["PARTIAL-01"]["phase2_status"] == "adapter_ready_pending_current_video"
    assert rows["PARTIAL-01"]["groups"]["canvas"]["status"] == "adaptation_pending"
    assert rows["PARTIAL-01"]["groups"]["media_binding"]["status"] == "not_required"
    assert "current_video_media_binding_pending" not in rows["PARTIAL-01"]["pending"]
    assert rows["PARTIAL-01"]["groups"]["resources"]["remote_dependencies_preserved"] is True
    assert rows["UNKNOWN-01"]["phase2_status"] == "failed_unresolved"
    assert rows["UNKNOWN-01"]["groups"]["text_mapping"]["status"] == "failed_unresolved"
    assert len(lists["partial"]["records"]) == 1
    assert len(lists["failed"]["records"]) == 1
    partial_v3 = next(row for row in v3["records"] if row["template_id"] == "PARTIAL-01")
    assert partial_v3["production"]["status"] == "blocked"
    assert partial_v3["production"]["phase2_status"] == "adapter_ready_pending_current_video"


def test_phase2_is_deterministic_on_consecutive_runs(tmp_path: Path) -> None:
    v1_path, auto_path, phase1_path = make_inputs(tmp_path)
    kwargs = {
        "v1_path": v1_path,
        "auto_path": auto_path,
        "phase1_manifest_path": phase1_path,
        "local_roots": (tmp_path / "no-local-resources",),
        "enforce_target_count": False,
    }
    assert module.build_phase2(**kwargs) == module.build_phase2(**kwargs)


def test_phase2_recovers_missing_contract_from_nested_source_draft(tmp_path: Path) -> None:
    source = tmp_path / "温43-金句" / "preset_draft" / "draft_content.json"
    source.parent.mkdir(parents=True)
    text_id = "22222222-2222-2222-2222-222222222222"
    inner = {
        "id": "inner-draft",
        "duration": 3_000_000,
        "canvas_config": {"width": 1080, "height": 1920, "ratio": "original"},
        "platform": {"app_version": "8.8.0"},
        "materials": {
            "texts": [{
                "id": text_id,
                "text": "重点文案",
                "content": json.dumps({"text": "重点文案", "styles": [{"range": [0, 4], "size": 32}]}, ensure_ascii=False),
            }],
            "videos": [],
            "images": [],
            "audios": [],
        },
        "tracks": [{
            "id": "text-track",
            "type": "text",
            "segments": [{
                "id": "text-segment",
                "material_id": text_id,
                "target_timerange": {"start": 0, "duration": 3_000_000},
            }],
        }],
    }
    source.write_text(json.dumps({
        "id": "outer-draft",
        "materials": {"drafts": [{"id": "wrapper", "draft": inner}]},
        "tracks": [{"type": "video", "segments": []}],
    }, ensure_ascii=False), encoding="utf-8")
    v1_path = tmp_path / "v1.json"
    auto_path = tmp_path / "auto.json"
    phase1_path = tmp_path / "phase1.json"
    write_json(v1_path, {"records": [{
        "template_id": "RECOVER-01",
        "source": {"source_path": str(source.relative_to(tmp_path)), "source_exists": True},
        "canvas": [],
        "slot_counts": {"text": 0, "audio": 0, "image": 0, "video": 0},
        "text_tracks": [],
        "production": {
            "status": "blocked",
            "blocked_reasons": ["canvas_unknown", "text_track_contract_missing", "manual_mapping_required"],
        },
    }]})
    write_json(auto_path, {"candidates": []})
    write_json(phase1_path, {"records": []})

    _, audit, manifest, _ = module.build_phase2(
        v1_path=v1_path,
        auto_path=auto_path,
        phase1_manifest_path=phase1_path,
        local_roots=(tmp_path,),
        source_root=tmp_path,
        enforce_target_count=False,
    )

    row = manifest["records"][0]
    assert row["source_rescan"]["status"] == "rescanned"
    assert row["groups"]["canvas"]["status"] == "no_canvas_change"
    assert row["groups"]["text_mapping"]["status"] == "enumerated_pending_capacity_measurement"
    assert row["groups"]["text_mapping"]["tracks"][0]["role"] == "main_emphasis"
    assert row["failures"] == []
    assert row["repair_class"] == "partial"
    assert audit["source_rescan"] == {"rescanned": 1}


def test_unknown_capacity_is_pending_measurement_not_hard_failure() -> None:
    record = {
        "text_tracks": [{
            "track_index": 1,
            "slot_id": "text_01",
            "role": "main_emphasis",
            "editable": True,
            "max_chars": None,
            "default_text": "标题",
        }],
        "production": {"blocked_reasons": []},
    }
    candidate = {
        "slots": {"text": [{"slot_id": "text_01", "default_text": "标题", "locators": [{"inner_draft_index": 0}]}]},
    }
    plan = module.text_mapping_plan(record, candidate)
    assert plan["status"] == "enumerated_pending_capacity_measurement"
    assert plan["reasons"] == []
    assert plan["capacity_proven"] is False


def test_background_without_text_is_reclassified_as_visual_asset() -> None:
    record = {"display_name": "127.背景", "text_tracks": []}
    candidate = {"display_name": "127.背景", "slots": {"text": [], "video": [], "image": []}}
    assert module.is_non_text_visual_candidate(record, candidate) is True
