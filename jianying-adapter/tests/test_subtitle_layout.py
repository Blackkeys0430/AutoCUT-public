from __future__ import annotations

import hashlib
from pathlib import Path

from jianying_adapter.subtitle_layout import KeywordRange, SubtitleInput, measure_case, render_preview


def _box(value: list[float]) -> tuple[float, float, float, float]:
    return tuple(float(item) for item in value)


def test_1_plus_1_2_plus_1_and_2_plus_3_keep_chinese_above_english() -> None:
    cases = (SubtitleInput("中文", "English"), SubtitleInput("中文第一行\n中文第二行", "English"),
             SubtitleInput("中文第一行\n中文第二行", "English line one\nEnglish line two\nEnglish line three"))
    for case in cases:
        report = measure_case(case)
        assert _box(report["zh"]["glyph_bbox"])[3] + 8 == _box(report["en"]["glyph_bbox"])[1]
        assert report["glyph_gap_px"] == 8


def test_more_lines_grow_upward_while_group_bottom_stays_fixed() -> None:
    one_one = measure_case(SubtitleInput("中文", "English"))
    two_three = measure_case(SubtitleInput("中文第一行\n中文第二行", "English line one\nEnglish line two\nEnglish line three"))
    assert two_three["group_bottom_px"] == one_one["group_bottom_px"]
    assert _box(two_three["group_bbox"])[1] < _box(one_one["group_bbox"])[1]


def test_keyword_is_larger_and_yellow() -> None:
    report = measure_case(SubtitleInput("关键词在这里", "keyword", (KeywordRange(0, 3),)))
    runs = report["zh"]["lines"][0]["runs"]
    keyword = next(run for run in runs if run["keyword"])
    plain = next(run for run in runs if not run["keyword"])
    assert keyword["color"] == "#FFD600"
    assert keyword["size_px"] > plain["size_px"]


def test_same_input_renders_identical_png_bytes(tmp_path: Path) -> None:
    measured = measure_case(SubtitleInput("中文", "English"))
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    render_preview(measured, first)
    render_preview(measured, second)
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
