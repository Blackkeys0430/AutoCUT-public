"""Small, deterministic bilingual subtitle layout helper."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CANVAS_SIZE = (1080, 1920)
FONT_PATH = Path(r"C:\Windows\Fonts\simsun.ttc")


@dataclass(frozen=True)
class SubtitleShadowSpec:
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    alpha: float = 0.9
    blur_ui: float = 15.0
    distance_ui: float = 5.0
    angle_degrees: float = -45.0


@dataclass(frozen=True)
class KeywordRange:
    start: int
    end: int
    color: str = "#FFD600"


@dataclass(frozen=True)
class SubtitleInput:
    zh_text: str
    en_text: str
    zh_keyword_ranges: tuple[KeywordRange, ...] = ()
    en_keyword_ranges: tuple[KeywordRange, ...] = ()


@dataclass(frozen=True)
class SubtitleLayoutConfig:
    canvas_width: int = CANVAS_SIZE[0]
    canvas_height: int = CANVAS_SIZE[1]
    font_path: Path = FONT_PATH
    font_index: int = 0
    zh_size_px: int = 78
    en_size_px: int = 51
    zh_bold: bool = True
    en_bold: bool = False
    keyword_size_scale: float = 1.12
    glyph_gap_px: float = 8.0
    line_gap_px: float = 8.0
    group_bottom_px: float = 1700.0
    bold_stroke_px: int = 1
    shadow: SubtitleShadowSpec = SubtitleShadowSpec()


BBox = tuple[float, float, float, float]


def _font(config: SubtitleLayoutConfig, size: float) -> ImageFont.FreeTypeFont:
    path = Path(config.font_path)
    if not path.is_file():
        raise FileNotFoundError(f"real SimSun font not found: {path}")
    return ImageFont.truetype(str(path), max(1, round(size)), index=config.font_index)


def _rgb(value: str) -> tuple[int, int, int]:
    if len(value) != 7 or not value.startswith("#"):
        raise ValueError(f"expected #RRGGBB color, got {value!r}")
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]


def _union(boxes: Sequence[BBox]) -> BBox:
    if not boxes:
        raise ValueError("at least one visible glyph is required")
    return (
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    )


def _shift(box: BBox, dy: float) -> list[float]:
    return [round(box[0], 4), round(box[1] + dy, 4), round(box[2], 4), round(box[3] + dy, 4)]


def _validate_text(text: str, name: str) -> None:
    if not text or text.endswith("\n") or "\r" in text or any(not line for line in text.split("\n")):
        raise ValueError(f"{name} must contain explicit non-empty lines only")


def _runs(text: str, ranges: Sequence[KeywordRange]) -> list[tuple[str, bool, str]]:
    boundaries = {0, len(text)}
    for item in ranges:
        if item.start < 0 or item.end <= item.start or item.end > len(text):
            raise ValueError(f"invalid keyword range [{item.start}, {item.end})")
        boundaries.update((item.start, item.end))
    ordered = sorted(boundaries)
    result: list[tuple[str, bool, str]] = []
    for start, end in zip(ordered, ordered[1:]):
        keyword = any(item.start <= start and end <= item.end for item in ranges)
        item = next((item for item in ranges if item.start <= start and end <= item.end), None)
        result.append((text[start:end], keyword, item.color if item else "#FFFFFF"))
    return result


def _language(text: str, language: str, ranges: Sequence[KeywordRange], config: SubtitleLayoutConfig) -> dict[str, Any]:
    base_size = config.zh_size_px if language == "zh" else config.en_size_px
    base_bold = config.zh_bold if language == "zh" else config.en_bold
    lines: list[dict[str, Any]] = []
    cursor = 0
    baseline = 0.0
    previous_bottom: float | None = None
    for text_line in text.split("\n"):
        line_end = cursor + len(text_line)
        crossing = [item for item in ranges if item.start < line_end and item.end > cursor and not (cursor <= item.start < item.end <= line_end)]
        if crossing:
            raise ValueError("keyword ranges cannot cross explicit line breaks")
        local_ranges = [KeywordRange(item.start - cursor, item.end - cursor, item.color)
                        for item in ranges if cursor <= item.start < item.end <= line_end]
        runs: list[dict[str, Any]] = []
        advance = 0.0
        for piece, keyword, color in _runs(text_line, local_ranges):
            size = base_size * (config.keyword_size_scale if keyword else 1.0)
            bold = keyword or base_bold
            font = _font(config, size)
            stroke = config.bold_stroke_px if bold else 0
            width = float(font.getlength(piece))
            bbox = tuple(float(value) for value in font.getbbox(piece, anchor="ls", stroke_width=stroke))
            runs.append({"text": piece, "keyword": keyword, "color": color if keyword else "#FFFFFF",
                         "size_px": round(size, 4), "bold": bold, "stroke_width_px": stroke,
                         "advance_px": round(width, 4), "relative_bbox": bbox, "x_offset": advance})
            advance += width
        x = config.canvas_width / 2 - advance / 2

        def position_runs() -> list[BBox]:
            boxes: list[BBox] = []
            for run in runs:
                relative = run["relative_bbox"]
                box = tuple(relative[index] + (x + run["x_offset"] if index in (0, 2) else baseline) for index in range(4))
                run["x_px"] = round(x + run["x_offset"], 4)
                run["baseline_y_px"] = round(baseline, 4)
                run["glyph_bbox"] = [round(value, 4) for value in box]
                boxes.append(box)
            return boxes

        glyph_box = _union(position_runs())
        if previous_bottom is not None:
            baseline += previous_bottom - glyph_box[1] + config.line_gap_px
            glyph_box = _union(position_runs())
        lines.append({"text": text_line, "runs": runs, "glyph_bbox": [round(value, 4) for value in glyph_box]})
        previous_bottom = glyph_box[3]
        cursor += len(text_line) + 1
    return {"language": language, "lines": lines, "glyph_bbox": [round(value, 4) for value in _union([tuple(line["glyph_bbox"]) for line in lines])]}


def measure_case(subtitle: SubtitleInput, config: SubtitleLayoutConfig | None = None) -> dict[str, Any]:
    """Measure explicit lines and place Chinese above English."""
    config = config or SubtitleLayoutConfig()
    _validate_text(subtitle.zh_text, "zh_text")
    _validate_text(subtitle.en_text, "en_text")
    zh = _language(subtitle.zh_text, "zh", subtitle.zh_keyword_ranges, config)
    en = _language(subtitle.en_text, "en", subtitle.en_keyword_ranges, config)
    zh_box = tuple(zh["glyph_bbox"])
    en_box = tuple(en["glyph_bbox"])
    en_shift = zh_box[3] - en_box[1] + config.glyph_gap_px
    en["glyph_bbox"] = _shift(en_box, en_shift)
    for line in en["lines"]:
        line["glyph_bbox"] = _shift(tuple(line["glyph_bbox"]), en_shift)
        for run in line["runs"]:
            run["baseline_y_px"] = round(run["baseline_y_px"] + en_shift, 4)
            run["glyph_bbox"] = _shift(tuple(run["glyph_bbox"]), en_shift)
    group = _union((tuple(zh["glyph_bbox"]), tuple(en["glyph_bbox"])))
    dy = config.group_bottom_px - group[3]
    for language in (zh, en):
        language["glyph_bbox"] = _shift(tuple(language["glyph_bbox"]), dy)
        for line in language["lines"]:
            line["glyph_bbox"] = _shift(tuple(line["glyph_bbox"]), dy)
            for run in line["runs"]:
                run["baseline_y_px"] = round(run["baseline_y_px"] + dy, 4)
                run["glyph_bbox"] = _shift(tuple(run["glyph_bbox"]), dy)
    group = _union((tuple(zh["glyph_bbox"]), tuple(en["glyph_bbox"])))
    return {"source": {"zh": subtitle.zh_text, "en": subtitle.en_text}, "zh": zh, "en": en,
            "glyph_gap_px": round(float(en["glyph_bbox"][1]) - float(zh["glyph_bbox"][3]), 4),
            "group_bottom_px": config.group_bottom_px, "group_bbox": [round(value, 4) for value in group]}


def render_preview(measured: dict[str, Any], output_path: Path, config: SubtitleLayoutConfig | None = None) -> None:
    """Render one plain preview image of a measured subtitle pair."""
    config = config or SubtitleLayoutConfig()
    image = Image.new("RGBA", (config.canvas_width, config.canvas_height), (24, 29, 38, 255))
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    alpha = max(0, min(255, round(config.shadow.alpha * 255)))
    angle = math.radians(config.shadow.angle_degrees)
    offset = (config.shadow.distance_ui * math.cos(angle), config.shadow.distance_ui * math.sin(angle))
    for language in ("zh", "en"):
        for line in measured[language]["lines"]:
            for run in line["runs"]:
                font = _font(config, run["size_px"])
                position = (run["x_px"], run["baseline_y_px"])
                shadow_draw.text((position[0] + offset[0], position[1] + offset[1]), run["text"], font=font, anchor="ls", fill=(0, 0, 0, alpha), stroke_width=run["stroke_width_px"], stroke_fill=(0, 0, 0, alpha))
    image = Image.alpha_composite(image, shadow.filter(ImageFilter.GaussianBlur(radius=config.shadow.blur_ui)))
    draw = ImageDraw.Draw(image)
    for language in ("zh", "en"):
        for line in measured[language]["lines"]:
            for run in line["runs"]:
                font = _font(config, run["size_px"])
                color = _rgb(run["color"])
                draw.text((run["x_px"], run["baseline_y_px"]), run["text"], font=font, anchor="ls", fill=(*color, 255), stroke_width=run["stroke_width_px"], stroke_fill=(*color, 255))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=False, compress_level=9)


__all__ = ["KeywordRange", "SubtitleInput", "SubtitleLayoutConfig", "measure_case", "render_preview"]
