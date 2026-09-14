from __future__ import annotations

from pathlib import Path

from PIL import Image

from jianying_adapter.preset_preflight import preflight_selection


def registry() -> dict:
    return {
        "records": [
            {
                "template_id": "T-01",
                "primary_category": "quote_conclusion",
                "content_contract": {
                    "editable_text_slot_count": 1,
                    "conservative_max_chars_per_slot": [6],
                },
                "timing_contract": {"recommended_minimum_node_seconds": 2.0},
                "technical_gate": {"production_gate": "guarded_production_eligible"},
            }
        ]
    }


def evidence(tmp_path: Path, *, intersects: bool = False) -> dict:
    phases = {}
    for index, phase in enumerate(("entry", "stable", "exit"), 1):
        snapshot = tmp_path / f"{phase}.png"
        Image.new("RGB", (32, 32), (index * 30, 20, 40)).save(snapshot)
        phases[phase] = {
            "snapshot": str(snapshot),
            "canvas_bounds": "passed",
            "caption_collision": "clear" if phase == "stable" else "not_applicable",
            "evidence_mode": "current_video_frontend",
            "timeline_time_us": {
                "entry": 1_050_000,
                "stable": 2_000_000,
                "exit": 3_250_000,
            }[phase],
        }
    return {
        "canvas": {"width": 1080, "height": 1920},
        "resources": "passed",
        "render_phases": phases,
        "subject_overlap": {"intersects_key_features": intersects},
        "current_video_ready": True,
    }


def selection(tmp_path: Path) -> dict:
    return {
        "template_id": "T-01",
        "category": "quote_conclusion",
        "slots": ["重要结论"],
        "node_id": "node_01",
        "start_us": 1_000_000,
        "end_us": 3_300_000,
        "node_duration_us": 2_300_000,
        "layout_context": {"subject_overlap_policy": "avoid_key_features"},
        "current_video_evidence": evidence(tmp_path),
    }


def test_selected_preset_becomes_ready_only_with_current_video_evidence(tmp_path: Path) -> None:
    report = preflight_selection(selection(tmp_path), registry(), target_canvas=(1080, 1920))
    assert report.current_video_ready
    assert all(report.checks.values())


def test_unplanned_key_feature_overlap_is_rejected(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["current_video_evidence"] = evidence(tmp_path, intersects=True)
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert not report.current_video_ready
    assert any("避让" in error for error in report.errors)


def test_intentional_overlap_can_pass_with_context_and_authorization(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["layout_context"] = {
        "subject_overlap_policy": "intentional_overlap",
        "design_basis": "人物已被明确设为模糊背景，文字承担主信息",
        "authorized_by": "user_current_request",
    }
    value["current_video_evidence"] = evidence(tmp_path, intersects=True)
    assert preflight_selection(value, registry(), target_canvas=(1080, 1920)).current_video_ready


def test_intentional_overlap_without_basis_is_rejected(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["layout_context"] = {"subject_overlap_policy": "intentional_overlap"}
    value["current_video_evidence"] = evidence(tmp_path, intersects=True)
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert not report.current_video_ready
    assert any("设计依据" in error for error in report.errors)


def test_actual_track_mapping_overrides_incomplete_registry_slots(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["slots"] = ["甲", "乙", "丙"]
    value["actual_text_track_map"] = {"track_01": "甲", "track_02": "乙", "track_03": "丙"}
    value["actual_slot_capacities"] = [2, 2, 2]
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert report.current_video_ready
    assert report.evidence["slot_lengths"] == [1, 1, 1]


def test_real_track_inventory_includes_fixed_decorative_text(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["slots"] = ["开店", "怎么选项目"]
    value["actual_text_tracks"] = [
        {"track_name": "JY_PRESET_T-01_01", "text": "开店", "max_chars": 4, "role": "editable"},
        {"track_name": "JY_PRESET_T-01_02", "text": "？", "max_chars": 1, "role": "fixed_decorative"},
        {"track_name": "JY_PRESET_T-01_03", "text": "怎么选项目", "max_chars": 6, "role": "editable"},
    ]
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert report.current_video_ready
    assert report.evidence["slot_lengths"] == [2, 1, 5]


def test_real_track_inventory_rejects_missing_or_duplicate_names(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["actual_text_tracks"] = [
        {"track_name": "same", "text": "重要结论", "max_chars": 6, "role": "editable"},
        {"track_name": "same", "text": "。", "max_chars": 1, "role": "fixed_decorative"},
    ]
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert not report.current_video_ready
    assert any("必须唯一" in error for error in report.errors)


def test_real_track_inventory_requires_main_emphasis_role(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["actual_text_tracks"] = [
        {"track_name": "support", "text": "重要结论", "max_chars": 6, "role": "supporting"},
    ]
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert not report.current_video_ready
    assert any("main_emphasis" in error for error in report.errors)


def test_multisegment_track_slots_follow_track_then_segment_order(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["slots"] = ["夹起的造型", "夹起的", "造型"]
    value["actual_text_tracks"] = [
        {"track_name": "first", "texts": ["夹起的造型", "夹起的"],
         "segment_count": 2, "max_chars": 5, "role": "supporting"},
        {"track_name": "second", "text": "造型", "segment_count": 1,
         "max_chars": 2, "role": "main_emphasis"},
    ]
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert report.current_video_ready
    assert report.evidence["slot_lengths"] == [5, 3, 2]
    value["slots"][:2] = reversed(value["slots"][:2])
    assert any("editable 文字顺序" in error for error in
               preflight_selection(value, registry(), target_canvas=(1080, 1920)).errors)


def test_multisegment_track_rejects_missing_text_and_checks_each_capacity(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["slots"] = ["短", "完整的重点句"]
    track = {"track_name": "first", "texts": list(value["slots"]),
             "segment_count": 2, "max_chars": 2, "role": "main_emphasis"}
    value["actual_text_tracks"] = [track]
    assert any("超过真实轨容量" in error for error in
               preflight_selection(value, registry(), target_canvas=(1080, 1920)).errors)
    for texts in (["短"], [], ["短", 12]):
        track["texts"] = texts
        assert any("segment_count 等长" in error for error in
                   preflight_selection(value, registry(), target_canvas=(1080, 1920)).errors)
    track.update(texts=["短", "完整的重点句"], text="短")
    assert any("不能同时声明" in error for error in
               preflight_selection(value, registry(), target_canvas=(1080, 1920)).errors)


def test_current_video_evidence_is_bound_to_project_node_and_roughcut(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["current_video_evidence"].update(
        {
            "project_id": "PROJECT-1",
            "roughcut_sha256": "A" * 64,
            "template_id": "T-01",
            "final_texts": ["重要结论"],
            "position": {"x": 0.0, "y": 0.53},
            "group_scale": 1.0,
        }
    )
    expected = {
        "project_id": "PROJECT-1",
        "roughcut_sha256": "A" * 64,
        "project_root": tmp_path,
        "final_texts": ["重要结论"],
        "final_position": {"x": 0.0, "y": 0.53},
        "final_group_scale": 1.0,
    }
    report = preflight_selection(
        value,
        registry(),
        target_canvas=(1080, 1920),
        expected_binding=expected,
    )
    assert report.current_video_ready, report.errors

    value["current_video_evidence"]["position"]["x"] = 0.25
    report = preflight_selection(
        value,
        registry(),
        target_canvas=(1080, 1920),
        expected_binding=expected,
    )
    assert not report.current_video_ready
    assert any("final_position" in error for error in report.errors)
    value["current_video_evidence"]["position"]["x"] = 0.0

    value["current_video_evidence"]["project_id"] = "OTHER-PROJECT"
    value["current_video_evidence"]["render_phases"]["stable"]["snapshot"] = str(
        tmp_path.parent / "other-project" / "stable.png"
    )
    report = preflight_selection(
        value,
        registry(),
        target_canvas=(1080, 1920),
        expected_binding=expected,
    )
    assert not report.current_video_ready
    assert any("当前项目" in error for error in report.errors)


def test_current_video_evidence_requires_distinct_phase_snapshots(tmp_path: Path) -> None:
    value = selection(tmp_path)
    binding = {
        "project_id": "PROJECT-1",
        "roughcut_sha256": "A" * 64,
        "project_root": tmp_path,
        "final_texts": ["重要结论"],
        "final_position": {"x": 0.0, "y": 0.53},
        "final_group_scale": 1.0,
    }
    value["current_video_evidence"].update(
        {
            "project_id": "PROJECT-1",
            "roughcut_sha256": "A" * 64,
            "template_id": "T-01",
            "final_texts": ["重要结论"],
            "position": {"x": 0.0, "y": 0.53},
            "group_scale": 1.0,
        }
    )
    stable = value["current_video_evidence"]["render_phases"]["stable"]["snapshot"]
    value["current_video_evidence"]["render_phases"]["exit"]["snapshot"] = stable
    report = preflight_selection(
        value,
        registry(),
        target_canvas=(1080, 1920),
        expected_binding=binding,
    )
    assert not report.current_video_ready
    assert any("不同文件" in error for error in report.errors)


def test_declared_not_ready_and_offline_evidence_are_rejected(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["current_video_evidence"]["current_video_ready"] = False
    value["current_video_evidence"]["render_phases"]["stable"]["evidence_mode"] = (
        "offline_structural_only"
    )
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert not report.current_video_ready
    assert any("必须显式为 true" in error for error in report.errors)
    assert any("真实当前视频证据" in error for error in report.errors)


def test_copied_phase_content_is_rejected_even_with_different_paths(tmp_path: Path) -> None:
    value = selection(tmp_path)
    stable = Path(value["current_video_evidence"]["render_phases"]["stable"]["snapshot"])
    exit_path = Path(value["current_video_evidence"]["render_phases"]["exit"]["snapshot"])
    exit_path.write_bytes(stable.read_bytes())
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert not report.current_video_ready
    assert any("复制同一画面" in error for error in report.errors)


def test_reencoded_same_pixels_are_rejected(tmp_path: Path) -> None:
    value = selection(tmp_path)
    stable = Path(value["current_video_evidence"]["render_phases"]["stable"]["snapshot"])
    exit_path = Path(value["current_video_evidence"]["render_phases"]["exit"]["snapshot"])
    with Image.open(stable) as image:
        image.save(exit_path, optimize=True)
    assert stable.read_bytes() != exit_path.read_bytes()
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert not report.current_video_ready
    assert any("复制同一画面" in error for error in report.errors)


def test_phase_times_must_be_ordered_inside_current_node(tmp_path: Path) -> None:
    value = selection(tmp_path)
    value["current_video_evidence"]["render_phases"]["entry"]["timeline_time_us"] = 3_200_000
    value["current_video_evidence"]["render_phases"]["stable"]["timeline_time_us"] = 2_000_000
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert not report.current_video_ready
    assert any("严格递增" in error for error in report.errors)


def test_fake_image_payload_is_rejected(tmp_path: Path) -> None:
    value = selection(tmp_path)
    entry = Path(value["current_video_evidence"]["render_phases"]["entry"]["snapshot"])
    entry.write_bytes(b"png")
    report = preflight_selection(value, registry(), target_canvas=(1080, 1920))
    assert not report.current_video_ready
    assert any("不是可验证图片" in error for error in report.errors)
