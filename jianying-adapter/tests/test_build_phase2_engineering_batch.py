from __future__ import annotations

from scripts import build_phase2_engineering_batch as module


def _candidate(template_id: str, duration: int = 2_000_000) -> dict:
    return {
        "template_id": template_id,
        "display_name": template_id,
        "source_path": f"{template_id}/preset_draft/draft_content.json",
        "duration": {"microseconds": duration},
        "slots": {"image": [], "video": []},
    }


def test_merged_registry_prefers_rescanned_candidate_and_preserves_queue_order() -> None:
    ids = [f"T-{index:03d}" for index in range(357)]
    auto = {"candidates": [_candidate(template_id) for template_id in ids]}
    rescanned = {"candidates": [{**_candidate("T-010"), "display_name": "rescanned"}]}
    queue = {"items": [{"template_id": template_id} for template_id in reversed(ids)]}
    registry = module.merged_candidate_registry(auto, rescanned, queue)
    assert registry["scope_count"] == 357
    assert registry["candidates"][0]["template_id"] == ids[-1]
    selected = next(row for row in registry["candidates"] if row["template_id"] == "T-010")
    assert selected["display_name"] == "rescanned"


def test_probe_registry_disables_required_media_without_binding_current_video() -> None:
    ids = [f"T-{index:03d}" for index in range(357)]
    rows = [_candidate(template_id) for template_id in ids]
    rows[0]["slots"]["video"] = [{"slot_id": "video_01", "required": True}]
    registry = module.merged_candidate_registry(
        {"candidates": rows},
        {"candidates": []},
        {"items": [{"template_id": template_id} for template_id in ids]},
    )
    slot = registry["candidates"][0]["slots"]["video"][0]
    assert slot["required"] is False
    assert slot["phase2_probe_original_required"] is True
    assert registry["current_video_media_bound"] is False


def test_probe_registry_preserves_manual_style_text_without_rewriting_slot() -> None:
    ids = [f"T-{index:03d}" for index in range(357)]
    rows = [_candidate(template_id) for template_id in ids]
    rows[0]["slots"]["text"] = [{
        "slot_id": "text_01",
        "required": True,
        "default_text": "保留原文",
        "requires_manual_style_mapping": True,
    }]
    registry = module.merged_candidate_registry(
        {"candidates": rows},
        {"candidates": []},
        {"items": [{"template_id": template_id} for template_id in ids]},
    )
    slot = registry["candidates"][0]["slots"]["text"][0]
    assert slot["required"] is False
    assert slot["phase2_probe_original_required"] is True
    assert slot["phase2_probe_preserve_source_text_and_style"] is True
    assert registry["probe_styled_text_preserve_count"] == 1


def test_probe_registry_preserves_ambiguous_manual_slot_without_rewriting() -> None:
    ids = [f"T-{index:03d}" for index in range(357)]
    rows = [_candidate(template_id) for template_id in ids]
    rows[0]["slots"]["text"] = [{
        "slot_id": "text_01",
        "required": True,
        "default_text": "同步原文",
        "requires_manual_slot_mapping": True,
    }]
    registry = module.merged_candidate_registry(
        {"candidates": rows},
        {"candidates": []},
        {"items": [{"template_id": template_id} for template_id in ids]},
    )
    slot = registry["candidates"][0]["slots"]["text"][0]
    assert slot["required"] is False
    assert slot["phase2_probe_original_required"] is True
    assert slot["phase2_probe_preserve_source_text_and_mapping"] is True
    assert registry["probe_manual_slot_preserve_count"] == 1


def test_bridge_uses_explicit_queue_batch_and_candidate_duration() -> None:
    ids = [f"T-{index:03d}" for index in range(357)]
    registry = {"candidates": [_candidate(template_id) for template_id in ids]}
    queue = {"items": [{"template_id": template_id, "batch": 1, "batch_order": index + 1} for index, template_id in enumerate(ids)]}
    bridge = module.phase1_compatible_queue(queue, registry)
    assert bridge["items"][0]["duration_us"] == 2_000_000
    assert bridge["items"][0]["batch"] == 1
