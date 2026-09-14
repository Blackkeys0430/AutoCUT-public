"""Build a workspace-only Jianying base draft from a multi-source edit plan.

The input is deliberately small and explicit: source ranges are already the
rough-cut decisions, while caption times are already in the resulting
timeline.  This module only materializes that plan with the vendored
``pyJianYingDraft`` package and writes auditable sidecar JSON files.  It does
not start Jianying, export video, or touch the user's real draft directory.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = REPO_ROOT / "vendor" / "pyJianYingDraft-source"
LOCAL_DEPS = REPO_ROOT / "vendor" / "python-deps"
SRC_ROOT = REPO_ROOT / "src"
for _path in (LOCAL_DEPS, VENDOR_ROOT, SRC_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pyJianYingDraft import (  # noqa: E402
    ClipSettings,
    DraftFolder,
    FontType,
    TextSegment,
    TextShadow,
    TextStyle,
    TrackSpec,
    TrackType,
    VideoMaterial,
    VideoSegment,
    trange,
)

from jianying_adapter.cut_safety import safe_audio_fade  # noqa: E402


SCHEMA = "multiclip_edit_plan_v1"
EDL_SCHEMA = "multiclip_edl_v1"
TIME_MAP_SCHEMA = "multiclip_time_map_v1"
SUBTITLE_SCHEMA = "multiclip_subtitle_plan_v1"
MANIFEST_SCHEMA = "multiclip_base_manifest_v1"
KEYWORD_COLOR = "#FFD600"


class MulticlipPlanError(ValueError):
    """The edit plan is invalid or cannot be materialized safely."""


def _read_json(path: str | Path) -> dict[str, Any]:
    plan_path = Path(path).resolve()
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise MulticlipPlanError(f"无法读取 edit_plan: {plan_path}") from exc
    except json.JSONDecodeError as exc:
        raise MulticlipPlanError(f"edit_plan 不是有效 JSON: {plan_path}") from exc
    if not isinstance(value, dict):
        raise MulticlipPlanError("edit_plan 根对象必须是 JSON object")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MulticlipPlanError(f"{label} 必须是正整数")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MulticlipPlanError(f"{label} 必须是非负整数")
    return value


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise MulticlipPlanError(f"{label} 必须是正数")
    return float(value)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise MulticlipPlanError(f"{label} 必须是 boolean")
    return value


def _subtitle_y(value: Any, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not -1 <= value <= 1 or not math.isfinite(value)):
        raise MulticlipPlanError(f"{label} 必须是 [-1, 1] 内的有限数值")
    return float(value)


def _volume(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MulticlipPlanError(f"{label} 必须是非负有限数值")
    try:
        result = float(value)
    except OverflowError as exc:
        raise MulticlipPlanError(f"{label} 必须是非负有限数值") from exc
    if not math.isfinite(result) or result < 0:
        raise MulticlipPlanError(f"{label} 必须是非负有限数值")
    return result


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MulticlipPlanError(f"{label} 必须是 JSON object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise MulticlipPlanError(f"{label} 必须是 JSON array")
    return value


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MulticlipPlanError("draft_name 必须是非空字符串")
    name = value.strip()
    if name in {".", ".."} or Path(name).name != name:
        raise MulticlipPlanError("draft_name 只能是单层目录名")
    return name


def _keyword_ranges(text: str, keywords: Sequence[str]) -> list[tuple[int, int]]:
    """Return non-overlapping exact matches, including repeated occurrences."""

    ranges: list[tuple[int, int]] = []
    for keyword in keywords:
        if not isinstance(keyword, str) or not keyword:
            raise MulticlipPlanError("zh_keywords 只能包含非空字符串")
        cursor = 0
        while True:
            start = text.find(keyword, cursor)
            if start < 0:
                break
            end = start + len(keyword)
            if not any(start < old_end and old_start < end for old_start, old_end in ranges):
                ranges.append((start, end))
            cursor = end
    return sorted(ranges)


def _style_ranges(
    segment: TextSegment,
    text: str,
    keywords: Sequence[str],
    base: TextStyle,
    highlight: TextStyle,
    shadow: TextShadow,
) -> None:
    ranges = _keyword_ranges(text, keywords)
    boundaries = {0, len(text)}
    for start, end in ranges:
        boundaries.update((start, end))
    ordered = sorted(boundaries)
    for start, end in zip(ordered, ordered[1:]):
        if start == end:
            continue
        active = any(old_start <= start and end <= old_end for old_start, old_end in ranges)
        segment.add_style_range(
            start,
            end,
            style=highlight if active else base,
            font=FontType.宋体,
            shadow=shadow,
        )


def _layout(zh: str, en: str, *, english_enabled: bool,
            zh_transform_y: float | None = None) -> dict[str, Any]:
    """Lay both languages out as one compact subtitle block."""

    zh_lines = zh.count("\n") + 1
    en_lines = en.count("\n") + 1 if english_enabled else 0
    zh_y = -0.43 + 0.055 * (zh_lines - 1) if zh_transform_y is None else zh_transform_y
    # Text anchors sit at each text box's centre.  The old rule independently
    # pushed Chinese up and English down, double-counting multi-line height.
    # Keep a small centre-to-centre gap around one shared subtitle block.
    en_y = None
    if english_enabled:
        compact_gap = 0.17 + 0.02 * (zh_lines - 1) + 0.02 * (en_lines - 1)
        en_y = zh_y - compact_gap
    return {
        "zh_lines": zh_lines,
        "en_lines": en_lines,
        "zh_transform_y": round(zh_y, 6),
        "en_transform_y": round(en_y, 6) if en_y is not None else None,
        "zh_above_en": english_enabled,
        "layout_rule": "shared_compact_subtitle_block",
    }


def validate_edit_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the public input schema before any write."""

    if plan.get("schema") != SCHEMA:
        raise MulticlipPlanError(f"schema 必须是 {SCHEMA}")
    canvas = _object(plan.get("canvas"), "canvas")
    normalized_canvas = {
        "width": _positive_int(canvas.get("width"), "canvas.width"),
        "height": _positive_int(canvas.get("height"), "canvas.height"),
        "fps": _positive_int(canvas.get("fps"), "canvas.fps"),
    }
    draft_name = _validate_name(plan.get("draft_name"))
    subtitle_options = _object(plan.get("subtitle_options", {}), "subtitle_options")
    english_enabled = _boolean(
        subtitle_options.get("english_enabled", True),
        "subtitle_options.english_enabled",
    )
    normalized_subtitle_options = {"english_enabled": english_enabled}
    if "zh_transform_y" in subtitle_options:
        normalized_subtitle_options["zh_transform_y"] = _subtitle_y(
            subtitle_options["zh_transform_y"], "subtitle_options.zh_transform_y"
        )

    raw_segments = _list(plan.get("timeline_segments"), "timeline_segments")
    if not raw_segments:
        raise MulticlipPlanError("timeline_segments 不能为空")
    segments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_segments):
        item = _object(raw, f"timeline_segments[{index}]")
        segment_id = item.get("id")
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise MulticlipPlanError(f"timeline_segments[{index}].id 必须是非空字符串")
        segment_id = segment_id.strip()
        if segment_id in seen_ids:
            raise MulticlipPlanError(f"timeline segment id 重复: {segment_id}")
        seen_ids.add(segment_id)
        raw_path = item.get("source_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise MulticlipPlanError(f"timeline_segments[{index}].source_path 必须是非空字符串")
        source_path = Path(raw_path).expanduser()
        if not source_path.is_absolute():
            raise MulticlipPlanError(f"timeline_segments[{index}].source_path 必须是绝对路径")
        source_path = source_path.resolve()
        source_start_us = _nonnegative_int(
            item.get("source_start_us"), f"timeline_segments[{index}].source_start_us"
        )
        source_end_us = _positive_int(
            item.get("source_end_us"), f"timeline_segments[{index}].source_end_us"
        )
        if source_end_us <= source_start_us:
            raise MulticlipPlanError(f"timeline_segments[{index}] source_end_us 必须大于 source_start_us")
        segments.append(
            {
                "id": segment_id,
                "source_path": str(source_path),
                "source_start_us": source_start_us,
                "source_end_us": source_end_us,
                "speed": _positive_number(
                    item.get("speed", 1.0), f"timeline_segments[{index}].speed"
                ),
                "volume": _volume(
                    item.get("volume", 1.0), f"timeline_segments[{index}].volume"
                ),
            }
        )

    raw_captions = plan.get("captions", [])
    if raw_captions is None:
        raw_captions = []
    raw_captions = _list(raw_captions, "captions")
    captions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_captions):
        item = _object(raw, f"captions[{index}]")
        start_us = _nonnegative_int(item.get("start_us"), f"captions[{index}].start_us")
        duration_us = _positive_int(item.get("duration_us"), f"captions[{index}].duration_us")
        zh = item.get("zh")
        en = item.get("en")
        if not isinstance(zh, str) or not zh.strip():
            raise MulticlipPlanError(f"captions[{index}].zh 必须是非空字符串")
        if english_enabled and (not isinstance(en, str) or not en.strip()):
            raise MulticlipPlanError(f"captions[{index}].en 必须是非空字符串")
        keywords = item.get("zh_keywords", [])
        keywords = _list(keywords, f"captions[{index}].zh_keywords") if keywords is not None else []
        if any(not isinstance(keyword, str) or not keyword for keyword in keywords):
            raise MulticlipPlanError(f"captions[{index}].zh_keywords 只能包含非空字符串")
        captions.append(
            {
                "start_us": start_us,
                "duration_us": duration_us,
                "zh": zh,
                "en": en if isinstance(en, str) else "",
                "zh_keywords": list(keywords),
            }
        )
        if "zh_transform_y" in item:
            captions[-1]["zh_transform_y"] = _subtitle_y(
                item["zh_transform_y"], f"captions[{index}].zh_transform_y"
            )

    from jianying_adapter.semantic_evidence.content_gate import validate_retake_cuts
    cut_errors = validate_retake_cuts(plan.get("retake_cuts", []), segments)
    if cut_errors:
        raise MulticlipPlanError("; ".join(cut_errors))
    return {
        "schema": SCHEMA,
        "canvas": normalized_canvas,
        "draft_name": draft_name,
        "subtitle_options": normalized_subtitle_options,
        "timeline_segments": segments,
        "retake_cuts": plan.get("retake_cuts", []),
        "captions": captions,
    }


def _output_paths(output_root: Path, draft_name: str) -> dict[str, Path]:
    root = output_root.resolve()
    draft = root / draft_name
    paths = {
        "draft": draft,
        "edl": root / "edl.json",
        "time_map": root / "time_map.json",
        "subtitle_plan": root / "subtitle_plan.json",
        "manifest": root / "manifest.json",
    }
    for path in paths.values():
        if path != root and root not in path.parents and path != draft:
            raise MulticlipPlanError(f"输出路径越出 output-root: {path}")
    return paths


def _assert_sources_and_ranges(segments: Sequence[Mapping[str, Any]]) -> dict[str, VideoMaterial]:
    materials: dict[str, VideoMaterial] = {}
    for item in segments:
        source_path = Path(str(item["source_path"]))
        if not source_path.is_file():
            raise FileNotFoundError(f"找不到源文件: {source_path}")
        key = str(source_path)
        material = materials.get(key)
        if material is None:
            material = VideoMaterial(key)
            materials[key] = material
        if int(item["source_end_us"]) > material.duration:
            raise MulticlipPlanError(
                f"{source_path} 的 source_end_us 超出素材时长 {material.duration}us"
            )
    return materials


def _validate_caption_ranges(captions: Sequence[Mapping[str, Any]], duration_us: int) -> None:
    for index, caption in enumerate(captions):
        start = int(caption["start_us"])
        end = start + int(caption["duration_us"])
        if end > duration_us:
            raise MulticlipPlanError(f"captions[{index}] 超出粗剪后总时长 {duration_us}us")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(
    plan: Mapping[str, Any] | str | Path,
    output_root: str | Path,
    compatibility_reference: str | Path | None = None,
) -> dict[str, Any]:
    """Materialize one validated plan into a new workspace draft."""

    raw_plan = _read_json(plan) if isinstance(plan, (str, Path)) else dict(plan)
    normalized = validate_edit_plan(raw_plan)
    root = Path(output_root).resolve()
    paths = _output_paths(root, normalized["draft_name"])
    root.mkdir(parents=True, exist_ok=True)
    if paths["draft"].exists():
        raise FileExistsError(f"拒绝覆盖已存在草稿: {paths['draft']}")
    for label in ("edl", "time_map", "subtitle_plan", "manifest"):
        if paths[label].exists():
            raise FileExistsError(f"拒绝覆盖已有输出: {paths[label]}")

    materials = _assert_sources_and_ranges(normalized["timeline_segments"])
    total_duration_us = sum(
        round(
            (int(item["source_end_us"]) - int(item["source_start_us"]))
            / float(item["speed"])
        )
        for item in normalized["timeline_segments"]
    )
    _validate_caption_ranges(normalized["captions"], total_duration_us)

    folder = DraftFolder(str(root), user_data_path=str(root / "workspace_user_data"))
    canvas = normalized["canvas"]
    script = folder.create_draft(
        normalized["draft_name"], canvas["width"], canvas["height"], canvas["fps"]
    )
    video_track = script.append_track(TrackSpec(TrackType.video, "JY_ROUGH_CUT_VIDEO"))
    zh_track = script.append_track(TrackSpec(TrackType.text, "JY_ZH_SUBTITLES"))
    english_enabled = normalized["subtitle_options"]["english_enabled"]
    en_track = (
        script.append_track(TrackSpec(TrackType.text, "JY_EN_SUBTITLES"))
        if english_enabled
        else None
    )

    target_cursor_us = 0
    time_map: list[dict[str, Any]] = []
    audio_fade_segments: list[dict[str, Any]] = []
    for item in normalized["timeline_segments"]:
        source_start_us = int(item["source_start_us"])
        source_end_us = int(item["source_end_us"])
        source_duration_us = source_end_us - source_start_us
        speed = float(item["speed"])
        duration_us = round(source_duration_us / speed)
        target_start_us = target_cursor_us
        target_end_us = target_start_us + duration_us
        source_path = str(item["source_path"])
        video_segment = VideoSegment(
            materials[source_path],
            trange(target_start_us, duration_us),
            source_timerange=trange(source_start_us, source_duration_us),
            speed=speed,
            volume=item["volume"],
        )
        fade_in_us, fade_out_us = safe_audio_fade(duration_us)
        if (fade_in_us or fade_out_us) and hasattr(video_segment, "add_fade"):
            video_segment.add_fade(fade_in_us, fade_out_us)
        script.add_segment(video_segment, track=video_track)
        time_map.append(
            {
                "segment_id": item["id"],
                "source_path": source_path,
                "source_start_us": source_start_us,
                "source_end_us": source_end_us,
                "speed": speed,
                "volume": item["volume"],
                "target_start_us": target_start_us,
                "target_end_us": target_end_us,
                "audio_fade_in_us": fade_in_us,
                "audio_fade_out_us": fade_out_us,
            }
        )
        audio_fade_segments.append(
            {
                "segment_id": item["id"],
                "in_us": fade_in_us,
                "out_us": fade_out_us,
                "requested_each_side_us": 30_000,
                "clamped": fade_in_us != 30_000 or fade_out_us != 30_000,
            }
        )
        target_cursor_us = target_end_us

    shadow = TextShadow(alpha=0.9, diffuse=15.0, distance=5.0, angle=-45.0)
    zh_style = TextStyle(size=7.5, bold=True, align=1, auto_wrapping=False, max_line_width=0.82)
    zh_highlight = TextStyle(
        size=7.5 * 1.12,
        bold=True,
        color=(1.0, 0.843, 0.0),
        align=1,
        auto_wrapping=False,
        max_line_width=0.82,
    )
    en_style = TextStyle(size=4.5, bold=False, align=1, auto_wrapping=False, max_line_width=0.86)
    subtitle_plan: list[dict[str, Any]] = []
    for caption in normalized["captions"]:
        start_us = int(caption["start_us"])
        duration_us = int(caption["duration_us"])
        layout = _layout(caption["zh"], caption["en"], english_enabled=english_enabled,
                         zh_transform_y=caption.get("zh_transform_y", normalized["subtitle_options"].get("zh_transform_y")))
        zh_segment = TextSegment(
            caption["zh"],
            trange(start_us, duration_us),
            font=FontType.宋体,
            style=zh_style,
            shadow=shadow,
            clip_settings=ClipSettings(transform_y=layout["zh_transform_y"]),
        )
        _style_ranges(zh_segment, caption["zh"], caption["zh_keywords"], zh_style, zh_highlight, shadow)
        script.add_segment(zh_segment, track=zh_track)
        if english_enabled and en_track is not None:
            en_segment = TextSegment(
                caption["en"],
                trange(start_us, duration_us),
                font=FontType.宋体,
                style=en_style,
                shadow=shadow,
                clip_settings=ClipSettings(transform_y=layout["en_transform_y"]),
            )
            script.add_segment(en_segment, track=en_track)
        subtitle_plan.append(
            {
                "start_us": start_us,
                "duration_us": duration_us,
                "zh": caption["zh"],
                "en": caption["en"],
                "zh_keywords": caption["zh_keywords"],
                "keyword_color": KEYWORD_COLOR,
                **layout,
            }
        )

    script.save()
    content_path = paths["draft"] / "draft_content.json"
    if not content_path.is_file():
        raise RuntimeError(f"pyJianYingDraft 未生成 draft_content.json: {content_path}")
    content = json.loads(content_path.read_text(encoding="utf-8-sig"))
    if compatibility_reference is not None:
        reference = _read_json(compatibility_reference)
        if isinstance(reference.get("draft"), Mapping):
            reference = dict(reference["draft"])
        for field in ("version", "new_version", "platform", "last_modified_platform"):
            if field not in reference:
                raise MulticlipPlanError(f"兼容参考缺少字段: {field}")
            content[field] = reference[field]
        _write_json(content_path, content)
    if int(content.get("duration", -1)) != total_duration_us:
        raise RuntimeError("草稿总时长与连续视频段总时长不一致")
    if content.get("canvas_config", {}).get("width") != canvas["width"] or content.get("canvas_config", {}).get("height") != canvas["height"]:
        raise RuntimeError("草稿画布与 edit_plan 不一致")

    edl = {
        "schema": EDL_SCHEMA,
        "draft_name": normalized["draft_name"],
        "canvas": canvas,
        "segments": [
            {
                **item,
                "target_start_us": mapped["target_start_us"],
                "target_end_us": mapped["target_end_us"],
            }
            for item, mapped in zip(normalized["timeline_segments"], time_map)
        ],
        "duration_us": total_duration_us,
    }
    subtitle_payload = {
        "schema": SUBTITLE_SCHEMA,
        "canvas": canvas,
        "tracks": {
            "zh": "JY_ZH_SUBTITLES",
            "en": "JY_EN_SUBTITLES" if english_enabled else None,
        },
        "style": {
            "font": "宋体",
            "shadow": {"alpha": 0.9, "diffuse": 15.0, "distance": 5.0, "angle": -45.0},
            "zh": {"size": 7.5, "bold": True, "position": "above"},
            "en": {"size": 4.5, "bold": False, "position": "below"},
            "keyword_color": KEYWORD_COLOR,
            "keyword_size_scale": 1.12,
        },
        "cards": subtitle_plan,
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "draft_name": normalized["draft_name"],
        "output_root": str(root),
        "draft": str(paths["draft"]),
        "files": {
            "edl": str(paths["edl"]),
            "time_map": str(paths["time_map"]),
            "subtitle_plan": str(paths["subtitle_plan"]),
        },
        "canvas": canvas,
        "duration_us": total_duration_us,
        "timeline_segment_count": len(time_map),
        "caption_count": len(subtitle_plan),
        "english_subtitles": english_enabled,
        "audio": (
            "original_video_audio_preserved"
            if all(item["volume"] == 1.0 for item in normalized["timeline_segments"])
            else "original_video_audio_with_segment_volume"
        ),
        "audio_fade": {
            "enabled": True,
            "requested_each_side_us": 30_000,
            "short_segment_rule": "each side is clamped to at most half of the segment duration",
            "segments": audio_fade_segments,
        },
        "compatibility_reference": (
            str(Path(compatibility_reference).resolve())
            if compatibility_reference is not None
            else None
        ),
        "started_jianying": False,
        "exported": False,
    }
    _write_json(paths["edl"], edl)
    _write_json(paths["time_map"], {"schema": TIME_MAP_SCHEMA, "segments": time_map})
    _write_json(paths["subtitle_plan"], subtitle_payload)
    _write_json(paths["manifest"], manifest)
    return {"draft": str(paths["draft"]), "duration_us": total_duration_us, "manifest": str(paths["manifest"])}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 multiclip_edit_plan_v1 生成工作区剪映基础草稿")
    parser.add_argument("--plan", type=Path, required=True, help="edit_plan JSON 路径")
    parser.add_argument("--output-root", type=Path, required=True, help="工作区输出根目录")
    parser.add_argument(
        "--compatibility-reference",
        type=Path,
        help="可被目标剪映版本打开的原生 draft_content.json，用于复制兼容头",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(
        json.dumps(
            build(args.plan, args.output_root, args.compatibility_reference),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
