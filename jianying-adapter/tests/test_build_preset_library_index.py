from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_preset_library_index.py"
E88_001_VISUAL = (
    Path(__file__).resolve().parents[2]
    / "video_trials/模板库779入库_v1/batches/E88-001/visual_validation.json"
)
SPEC = importlib.util.spec_from_file_location("build_preset_library_index", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_compatibility_tier_prefers_exact_8_8() -> None:
    assert MODULE.compatibility_tier(
        {"jianying_8_8_compatibility": "le_8_8", "app_version": "8.8.0"}
    ) == "exact_8_8"
    assert MODULE.compatibility_tier(
        {"jianying_8_8_compatibility": "le_8_8", "app_version": "8.1.1"}
    ) == "legacy_le_8_8"


def test_build_batches_keeps_holds_out_of_ready_queue() -> None:
    entries = [
        {
            "template_id": "A",
            "visual_validation": "pending",
            "compatibility_tier": "exact_8_8",
            "missing_source_path_count": 1,
            "selection_score": 10,
            "display_name": "A",
        },
        {
            "template_id": "B",
            "visual_validation": "pending",
            "compatibility_tier": "gt_8_8_hold",
            "missing_source_path_count": 1,
            "selection_score": 10,
            "display_name": "B",
        },
        {
            "template_id": "C",
            "visual_validation": "accepted",
            "compatibility_tier": "exact_8_8",
            "missing_source_path_count": 0,
            "selection_score": 10,
            "display_name": "C",
        },
    ]
    batches = MODULE.build_batches(entries)
    assert batches[0]["template_ids"] == ["A"]
    assert batches[0]["status"] == "ready_for_audition"
    assert batches[1]["template_ids"] == ["B"]
    assert batches[1]["status"] == "compatibility_hold"


def test_full_library_count_and_acceptance_partition() -> None:
    library = MODULE.build_library()
    summary = library["summary"]
    assert summary["source_candidate_count"] == 779
    assert summary["accepted_overlap_with_source"] == 10
    assert summary["accepted_external_count"] == 2
    assert summary["unique_library_count"] == 781
    assert summary["visual_accepted_count"] == 23
    assert summary["known_visual_rejected_count"] == 83
    assert summary["pending_visual_count"] == 675
    assert (
        summary["visual_accepted_count"]
        + summary["known_visual_rejected_count"]
        + summary["pending_visual_count"]
        == summary["unique_library_count"]
    )


def test_batches_cover_every_pending_template_once() -> None:
    library = MODULE.build_library()
    pending_ids = {
        entry["template_id"]
        for entry in library["entries"]
        if entry["library_status"] == "cataloged_pending_visual"
    }
    batched_ids = [
        template_id
        for batch in library["validation_batches"]
        for template_id in batch["template_ids"]
    ]
    assert len(batched_ids) == len(set(batched_ids))
    assert pending_ids <= set(batched_ids)
    assert set(batched_ids) - pending_ids == set(
        MODULE.load_json(MODULE.VISUAL_DECISIONS)["accepted"]
    )
    assert all(len(batch["template_ids"]) <= MODULE.BATCH_SIZE for batch in library["validation_batches"])


def test_generated_summary_routes_usage_to_current_policy_not_legacy_batches() -> None:
    library = MODULE.build_library()
    summary = MODULE.markdown_summary(library)
    assert '不是当前验收队列' in summary
    assert 'preset_usage_registry_v1.json' in summary
    assert '不能被生产自动选择' not in summary
    assert library['policy']['validation_batches_role'] == 'legacy_grouping_only_not_current_review_queue'


def test_first_issued_batch_keeps_identity_and_records_visual_result() -> None:
    library = MODULE.build_library()
    first = library["validation_batches"][0]
    assert first["batch_id"] == "E88-001"
    assert first["status"] == "visual_qa_complete"
    evidence = MODULE.load_json(E88_001_VISUAL)
    assert set(first["template_ids"]) == (
        set(evidence["accepted_template_ids"])
        | set(evidence["conditional_template_ids"])
    )
