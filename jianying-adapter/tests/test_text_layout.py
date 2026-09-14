from __future__ import annotations

import pytest

from jianying_adapter.text_layout import (
    bbox_intersection,
    reflow_caption,
    shift_bbox_into_safe_area,
    transformed_text_bbox,
)


def test_bbox_intersection_reports_real_overlap() -> None:
    report = bbox_intersection(
        (54, 138.165, 1020, 391.165),
        (61.6, 275.079, 1018.4, 401.579),
    )
    assert report["collides"] is True
    assert report["overlap_height_px"] == 116.086


def test_bbox_intersection_can_reserve_breathing_room() -> None:
    report = bbox_intersection(
        (0, 0, 100, 100),
        (105, 0, 205, 100),
        minimum_gap_px=8,
    )
    assert report["intersects"] is False
    assert report["collides"] is True


def test_short_caption_with_planner_break_returns_to_one_line() -> None:
    result = reflow_caption("记住三个\n理财原则", font_path=None, pixel_size=56, safe_width_px=820)
    assert result["text"] == "记住三个理财原则"
    assert result["line_count"] == 1
    assert result["reason"] == "measured_single_line_fit"


def test_long_caption_wraps_only_after_measurement() -> None:
    result = reflow_caption("这是一句明显超过安全宽度需要换行的长字幕",
                            font_path=None, pixel_size=56, safe_width_px=700)
    assert result["line_count"] == 2
    assert all(line["width_px"] <= 700 for line in result["lines"])


def test_preferred_break_preserves_word_and_does_not_force_short_text_to_wrap():
    text = "真的能舒服自己和身边的人"
    arguments = dict(font_path=None, pixel_size=81, preferred_breaks=[7])
    wrapped = reflow_caption(text, safe_width_px=907, **arguments)
    assert wrapped["text"] == "真的能舒服自己\n和身边的人"
    assert wrapped["break_selection"] == "preferred_breaks"
    single = reflow_caption(text, safe_width_px=1200, **arguments)
    assert single["text"] == text
    assert single["break_selection"] == "none"


def test_unusable_preferred_break_does_not_fall_back_to_splitting_a_word():
    with pytest.raises(ValueError, match="cannot fit"):
        reflow_caption("真的能舒服自己和身边的人", font_path=None, pixel_size=81,
                       safe_width_px=600, preferred_breaks=[7])


@pytest.mark.parametrize("breaks", [[True], [0], [12], [], "7"])
def test_invalid_preferred_break_offsets_are_rejected(breaks):
    with pytest.raises(ValueError, match="preferred_breaks"):
        reflow_caption("真的能舒服自己和身边的人", font_path=None, pixel_size=81,
                       safe_width_px=907, preferred_breaks=breaks)


def test_edge_display_text_is_detected_and_shifted_inside() -> None:
    measured = transformed_text_bbox("理财三原则", font_path=None, pixel_size=128,
                                     canvas_width=1080, canvas_height=1920,
                                     transform_x=-0.69, transform_y=0.5)
    before = measured["bbox"]
    assert before[0] < 54
    fixed = shift_bbox_into_safe_area(before, canvas_width=1080, canvas_height=1920,
                                      margin_x_px=54, margin_y_px=70)
    assert fixed["ok"] is True
    assert fixed["bbox"][0] >= 54
    assert fixed["bbox"][2] <= 1026
