from __future__ import annotations

from scripts import build_phase2_verification_queue as module


def _manifest() -> dict:
    records = []
    for index in range(357):
        pending = ["canvas_boundary_proof_pending"]
        if index < 253:
            pending.append("resource_rehydration_pending")
        if index < 121:
            pending.append("current_video_media_binding_pending")
        records.append({
            "template_id": f"T-{index:03d}",
            "primary_group": "canvas_adaptation",
            "catalog_classification": "text_or_motion_preset",
            "repair_class": "partial" if index < 355 else "reclassified",
            "phase2_status": "adapter_ready_pending_current_video",
            "pending": pending,
            "source_rescan": {"status": "not_needed"},
        })
    return {"records": records}


def test_queue_has_complete_deterministic_scope_and_batches() -> None:
    durations = {f"T-{index:03d}": 2_000_000 for index in range(357)}
    first = module.build_queue(_manifest(), durations)
    second = module.build_queue(_manifest(), durations)
    assert first == second
    assert first["scope_count"] == 357
    assert first["planned_batch_count"] == 8
    assert first["batch_sizes"] == [50, 50, 50, 50, 50, 50, 50, 7]
    assert all(first["checks"].values())


def test_queue_keeps_current_video_binding_out_of_generic_contracts() -> None:
    durations = {f"T-{index:03d}": 2_000_000 for index in range(357)}
    queue = module.build_queue(_manifest(), durations)
    first = queue["items"][0]
    assert "current_video_media_binding_pending" in first["current_video_only"]
    assert "current_video_media_binding_pending" not in first["offline_contracts"]
    assert queue["policy"]["production_ready_claimed"] is False


def test_queue_splits_before_duration_overflow() -> None:
    durations = {f"T-{index:03d}": 2_000_000 for index in range(357)}
    durations["T-000"] = 100_000_000
    durations["T-001"] = 100_000_000
    queue = module.build_queue(_manifest(), durations)
    assert queue["items"][0]["batch"] == 1
    assert queue["items"][1]["batch"] == 2
    assert queue["checks"]["all_batch_windows_within_180_seconds"] is True
