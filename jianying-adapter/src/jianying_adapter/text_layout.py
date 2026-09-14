"""Measured text layout gates for Jianying talking-head drafts.

The helpers intentionally operate on the final font, font size, segment scale
and transform.  Character counts are not layout evidence: a short Chinese
caption stays on one line whenever its measured glyphs fit the configured safe
width, while display text is rejected or moved before it can leave the canvas.
"""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from PIL import ImageFont


BBox = tuple[float, float, float, float]
DEFAULT_FALLBACK_FONT = Path(r"C:\Windows\Fonts\msyhbd.ttc")


@dataclass(frozen=True)
class MeasuredLine:
    text: str
    width_px: float


def bbox_intersection(
    first: Sequence[float],
    second: Sequence[float],
    *,
    minimum_gap_px: float = 0.0,
) -> dict[str, object]:
    """Measure overlap and optional breathing room between two glyph boxes."""
    a_left, a_top, a_right, a_bottom = (float(value) for value in first)
    b_left, b_top, b_right, b_bottom = (float(value) for value in second)
    gap = max(0.0, float(minimum_gap_px))
    overlap_width = min(a_right, b_right) - max(a_left, b_left)
    overlap_height = min(a_bottom, b_bottom) - max(a_top, b_top)
    horizontal_gap = max(b_left - a_right, a_left - b_right, 0.0)
    vertical_gap = max(b_top - a_bottom, a_top - b_bottom, 0.0)
    intersects = overlap_width > 0.0 and overlap_height > 0.0
    too_close = not intersects and horizontal_gap < gap and vertical_gap < gap
    return {
        "collides": intersects or too_close,
        "intersects": intersects,
        "overlap_width_px": round(max(0.0, overlap_width), 3),
        "overlap_height_px": round(max(0.0, overlap_height), 3),
        "horizontal_gap_px": round(horizontal_gap, 3),
        "vertical_gap_px": round(vertical_gap, 3),
        "minimum_gap_px": round(gap, 3),
    }


def _font(font_path: Path | str | None, pixel_size: float) -> tuple[ImageFont.FreeTypeFont, Path, str]:
    requested = Path(font_path) if font_path else None
    resolved = requested if requested and requested.is_file() else DEFAULT_FALLBACK_FONT
    if not resolved.is_file():
        raise FileNotFoundError(f"no measurable font available: {requested!s}; {resolved!s}")
    mode = "exact_font" if requested and requested.is_file() else "conservative_font_fallback"
    return ImageFont.truetype(str(resolved), max(1, round(pixel_size))), resolved, mode


def measure_text_width_px(
    text: str,
    *,
    font_path: Path | str | None,
    pixel_size: float,
    letter_spacing_px: float = 0.0,
    fallback_padding: float = 1.15,
) -> dict[str, object]:
    """Measure a line with the real font, padding conservative fallback results."""
    if "\n" in text or "\r" in text:
        raise ValueError("measure_text_width_px accepts one logical line")
    font, resolved, mode = _font(font_path, pixel_size)
    width = float(font.getlength(text)) + max(0, len(text) - 1) * letter_spacing_px
    if mode != "exact_font":
        width *= fallback_padding
    return {
        "text": text,
        "width_px": round(width, 3),
        "font_path": str(resolved),
        "measurement_mode": mode,
        "fallback_padding": fallback_padding if mode != "exact_font" else 1.0,
    }


def _best_two_line_break(
    text: str,
    *,
    font_path: Path | str | None,
    pixel_size: float,
    safe_width_px: float,
    letter_spacing_px: float,
    fallback_padding: float,
    preferred_breaks: Sequence[int] | None = None,
) -> tuple[str, str] | None:
    candidates: list[tuple[float, int, str, str]] = []
    punctuation = set("，。！？；：、,.!?;:")
    for index in preferred_breaks if preferred_breaks is not None else range(1, len(text)):
        left, right = text[:index], text[index:]
        lw = float(measure_text_width_px(left, font_path=font_path, pixel_size=pixel_size,
                                         letter_spacing_px=letter_spacing_px,
                                         fallback_padding=fallback_padding)["width_px"])
        rw = float(measure_text_width_px(right, font_path=font_path, pixel_size=pixel_size,
                                          letter_spacing_px=letter_spacing_px,
                                          fallback_padding=fallback_padding)["width_px"])
        if max(lw, rw) > safe_width_px:
            continue
        punctuation_penalty = 0 if left[-1] in punctuation else 1
        candidates.append((abs(lw - rw), punctuation_penalty, left, right))
    if not candidates:
        return None
    _, _, left, right = min(candidates, key=lambda item: (item[1], item[0]))
    return left, right


def reflow_caption(
    text: str,
    *,
    font_path: Path | str | None,
    pixel_size: float,
    safe_width_px: float,
    letter_spacing_px: float = 0.0,
    fallback_padding: float = 1.15,
    preferred_breaks: Sequence[int] | None = None,
) -> dict[str, object]:
    """Remove planner line breaks, then add one only when measured width requires it."""
    logical_text = "".join(text.replace("\r", "").split("\n"))
    if preferred_breaks is not None:
        if (not isinstance(preferred_breaks, (list, tuple)) or not preferred_breaks
                or any(type(index) is not int or not 0 < index < len(logical_text)
                       for index in preferred_breaks)):
            raise ValueError("preferred_breaks must contain interior character offsets in logical text")
    single = measure_text_width_px(
        logical_text,
        font_path=font_path,
        pixel_size=pixel_size,
        letter_spacing_px=letter_spacing_px,
        fallback_padding=fallback_padding,
    )
    if float(single["width_px"]) <= safe_width_px:
        lines = (logical_text,)
        reason = "measured_single_line_fit"
    else:
        split = _best_two_line_break(
            logical_text,
            font_path=font_path,
            pixel_size=pixel_size,
            safe_width_px=safe_width_px,
            letter_spacing_px=letter_spacing_px,
            fallback_padding=fallback_padding,
            preferred_breaks=preferred_breaks,
        )
        if split is None:
            raise ValueError(f"caption cannot fit in at most two measured lines: {logical_text}")
        lines = split
        reason = "measured_width_requires_two_lines"
    measured_lines = [
        MeasuredLine(
            line,
            float(measure_text_width_px(line, font_path=font_path, pixel_size=pixel_size,
                                        letter_spacing_px=letter_spacing_px,
                                        fallback_padding=fallback_padding)["width_px"]),
        )
        for line in lines
    ]
    return {
        "source_text": text,
        "logical_text": logical_text,
        "text": "\n".join(lines),
        "line_count": len(lines),
        "reason": reason,
        "break_selection": "none" if len(lines) == 1 else (
            "preferred_breaks" if preferred_breaks is not None else "automatic"),
        "safe_width_px": round(safe_width_px, 3),
        "lines": [{"text": line.text, "width_px": round(line.width_px, 3)} for line in measured_lines],
        "font_path": single["font_path"],
        "measurement_mode": single["measurement_mode"],
        "fallback_padding": single["fallback_padding"],
    }


def transformed_text_bbox(
    text: str,
    *,
    font_path: Path | str | None,
    pixel_size: float,
    canvas_width: int,
    canvas_height: int,
    transform_x: float,
    transform_y: float,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    rotation_degrees: float = 0.0,
    letter_spacing_px: float = 0.0,
    fallback_padding: float = 1.15,
) -> dict[str, object]:
    """Return a conservative canvas-space glyph box for a centered Jianying text clip."""
    logical_lines = text.replace("\r", "").split("\n")
    widths: list[float] = []
    font, resolved, mode = _font(font_path, pixel_size)
    for line in logical_lines:
        width = float(font.getlength(line)) + max(0, len(line) - 1) * letter_spacing_px
        widths.append(width)
    line_height = float(font.getbbox("国Ag", anchor="lt")[3])
    width = max(widths, default=0.0)
    height = line_height * max(1, len(logical_lines))
    if mode != "exact_font":
        width *= fallback_padding
        height *= fallback_padding
    width *= abs(scale_x)
    height *= abs(scale_y)
    radians = math.radians(rotation_degrees)
    rotated_width = abs(width * math.cos(radians)) + abs(height * math.sin(radians))
    rotated_height = abs(width * math.sin(radians)) + abs(height * math.cos(radians))
    center_x = canvas_width * (0.5 + transform_x / 2.0)
    center_y = canvas_height * (0.5 - transform_y / 2.0)
    bbox = (
        center_x - rotated_width / 2.0,
        center_y - rotated_height / 2.0,
        center_x + rotated_width / 2.0,
        center_y + rotated_height / 2.0,
    )
    return {
        "bbox": [round(value, 3) for value in bbox],
        "font_path": str(resolved),
        "measurement_mode": mode,
        "fallback_padding": fallback_padding if mode != "exact_font" else 1.0,
        "pixel_size": round(pixel_size, 3),
    }


def union_bbox(boxes: Iterable[Sequence[float]]) -> BBox:
    values = [tuple(float(value) for value in box) for box in boxes]
    if not values:
        raise ValueError("at least one bbox is required")
    return (
        min(box[0] for box in values),
        min(box[1] for box in values),
        max(box[2] for box in values),
        max(box[3] for box in values),
    )


def measure_reading_glyphs(segment: Mapping, material: Mapping, *, text_unit_to_px: float = 5.0,
                           materials: Mapping | None = None) -> dict:
    """Measure informative style runs, without line count, shadows or intro overshoot.

    The longest held scale interval is the reading state. A continuously scaling
    clip uses its minimum instead of claiming its largest frame is readable.
    Native animation interiors are excluded where their in/out ranges are known;
    native effects themselves remain a separate audiovisual review responsibility.
    """
    payload = json.loads(material['content'])
    text = payload.get('text', '')
    styles = payload.get('styles', [])
    informative = {i for i, char in enumerate(text) if char.isalnum()}
    if not informative:
        return {'informative': False, 'minimum_height_px': 0.0, 'maximum_height_px': 0.0}
    if not styles:
        raise ValueError('reading text has no font styles')
    covered, heights = set(), []
    for style in styles:
        span = style.get('range', [0, len(text)])
        if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                or not 0 <= span[0] < span[1] <= len(text)):
            raise ValueError('reading text has invalid style range')
        indexes = informative & set(range(*span))
        if not indexes:
            continue
        size = style.get('size', material.get('font_size'))
        if type(size) not in (int, float) or not math.isfinite(size) or size <= 0:
            raise ValueError('reading text has invalid font size')
        path = (style.get('font') or {}).get('path') or material.get('font_path')
        font, resolved, mode = _font(path, size * text_unit_to_px)
        if mode != 'exact_font':
            raise ValueError('reading text requires its bound local font: ' + str(path))
        # A small qualifier in another run cannot borrow a headline's height.
        glyphs = ''.join(text[i] for i in sorted(indexes))
        box = font.getbbox(glyphs)
        heights.append(float(box[3] - box[1]))
        covered.update(indexes)
    if covered != informative or not heights or min(heights) <= 0:
        raise ValueError('reading text has uncovered or unmeasurable glyphs')
    duration = segment['target_timerange']['duration']
    start, end = 0, duration
    native_unmeasured = False
    for ref in segment.get('extra_material_refs', []):
        for animation in (materials or {}).get(ref, {}).get('animations', []):
            native_unmeasured = True
            offset, length = animation.get('start', 0), animation.get('duration', 0)
            if type(offset) is int and type(length) is int and length > 0:
                if animation.get('type') == 'in': start = max(start, offset + length)
                if animation.get('type') == 'out': end = min(end, offset)
    if end <= start:
        raise ValueError('reading text has no stable interval between native in/out animations')
    base = (segment.get('clip') or {}).get('scale', {}).get('y', 1.0)
    frames, groups = [], 0
    for group in segment.get('common_keyframes', []):
        if group.get('property_type') != 'KFTypeScaleY':
            continue
        groups += 1
        for frame in group.get('keyframe_list', []):
            values, time = frame.get('values', []), frame.get('time_offset')
            if (frame.get('curveType', 'Line') != 'Line' or frame.get('graphID')
                    or type(time) not in (int, float) or not math.isfinite(time)
                    or len(values) != 1 or type(values[0]) not in (int, float)
                    or not math.isfinite(values[0]) or values[0] <= 0):
                raise ValueError('reading scale requires positive linear keyframes')
            frames.append((time, values[0]))
    frames.sort()
    if (groups > 1 or len({t for t, _ in frames}) != len(frames)
            or type(base) not in (int, float) or not math.isfinite(base) or base <= 0):
        raise ValueError('reading scale is invalid')
    def scale_at(time):
        if not frames: return base
        if time <= frames[0][0]: return frames[0][1]
        for (a, left), (b, right) in zip(frames, frames[1:]):
            if time <= b: return left + (right - left) * (time - a) / (b - a)
        return frames[-1][1]
    times = sorted({start, end, *(t for t, _ in frames if start < t < end)})
    holds = [(b-a, scale_at(a), a, b) for a, b in zip(times, times[1:])
             if abs(scale_at(a)-scale_at(b)) < 1e-8]
    if holds:
        _, scale, left, right = max(holds, key=lambda h: (h[0], -h[1]))
        method = 'longest_stable_scale_interval'
    else:
        scale, left, right = min(scale_at(t) for t in times), start, end
        method = 'minimum_continuous_reading_scale'
    return {'informative': True, 'minimum_height_px': min(heights) * scale,
            'maximum_height_px': max(heights) * scale, 'stable_scale_y': scale,
            'reading_range_us': [left, right], 'method': method,
            'native_animation_unmeasured': native_unmeasured}


def shift_bbox_into_safe_area(
    bbox: Sequence[float],
    *,
    canvas_width: int,
    canvas_height: int,
    margin_x_px: float,
    margin_y_px: float,
) -> dict[str, object]:
    """Compute the smallest translation that keeps a box inside the safe canvas."""
    left, top, right, bottom = (float(value) for value in bbox)
    safe_left, safe_right = margin_x_px, canvas_width - margin_x_px
    safe_top, safe_bottom = margin_y_px, canvas_height - margin_y_px
    if right - left > safe_right - safe_left or bottom - top > safe_bottom - safe_top:
        return {"ok": False, "reason": "bbox_larger_than_safe_area", "shift_px": [0.0, 0.0]}
    dx = max(safe_left - left, min(0.0, safe_right - right))
    dy = max(safe_top - top, min(0.0, safe_bottom - bottom))
    shifted = [left + dx, top + dy, right + dx, bottom + dy]
    return {
        "ok": True,
        "reason": "already_inside" if dx == 0 and dy == 0 else "shifted_inside",
        "shift_px": [round(dx, 3), round(dy, 3)],
        "bbox": [round(value, 3) for value in shifted],
    }


__all__ = [
    "bbox_intersection",
    "measure_text_width_px",
    "reflow_caption",
    "shift_bbox_into_safe_area",
    "transformed_text_bbox",
    "union_bbox",
]
