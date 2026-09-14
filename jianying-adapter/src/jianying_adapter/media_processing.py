"""Frame-based local media processing for the existing material-library entry.

This produces intermediate files, never a Jianying draft or a visual approval.
Source/permission checks and registration remain in material_library/media_ledger.
"""
from __future__ import annotations

import math
import subprocess
import json
from bisect import bisect_right
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping


VIDEO_FORMATS = {
    "h264_mp4": (".mp4", ["-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p"]),
    "qtrle_mov": (".mov", ["-c:v", "qtrle", "-pix_fmt", "argb"]),
    "prores4444_mov": (".mov", ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le", "-alpha_bits", "16"]),
}


def _round(value: Fraction) -> int:
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def video_frames(path: Path, probe: Mapping[str, Any]) -> list[dict[str, int]]:
    """Read actual display timestamps; frame indexes also survive nonzero/VFR PTS."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", str(probe["stream_index"]),
         "-show_frames", "-show_entries", "frame=best_effort_timestamp_time,duration_time",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=300,
    )
    frames = []
    for index, row in enumerate(json.loads(result.stdout).get("frames", [])):
        if "best_effort_timestamp_time" not in row:
            raise ValueError("加工源帧缺少实际显示时间，不能按平均帧率猜测")
        pts = _round(Fraction(row["best_effort_timestamp_time"]) * 1_000_000) - probe["start_time_us"]
        if frames and pts <= frames[-1]["source_time_us"]:
            raise ValueError("加工源帧的显示时间必须递增")
        frames.append({"source_frame_index": index, "source_time_us": pts})
    if not frames:
        raise ValueError("没有可解码的视频帧")
    return frames


def normalize_processing(spec: Mapping[str, Any], source_probe: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"mode", "source_timerange", "frame", "reverse", "speed", "frame_step", "saturation",
               "output_fps", "output_format"}
    if set(spec) - allowed:
        raise ValueError(f"未知素材加工参数: {sorted(set(spec) - allowed)}")
    mode = spec.get("mode")
    if mode not in {"frame", "video"}:
        raise ValueError("加工 mode 必须为 frame 或 video")
    timerange = spec.get("source_timerange")
    if not isinstance(timerange, Mapping) or set(timerange) != {"start", "duration"}:
        raise ValueError("加工须明确 source_timerange.start/duration（微秒）")
    start, duration = timerange["start"], timerange["duration"]
    if (type(start) is not int or type(duration) is not int or start < 0 or duration <= 0
            or start + duration > source_probe["duration_us"]):
        raise ValueError("加工源区间必须在真实视频时长内")
    result = {"mode": mode, "source_timerange": dict(timerange)}
    if mode == "frame":
        if set(spec) - {"mode", "source_timerange", "frame"}:
            raise ValueError("源帧提取不接受视频变速、倒放或色彩参数")
        frame = spec.get("frame", "first")
        if frame not in {"first", "last"}:
            raise ValueError("frame 必须为所选源区间内的 first 或 last")
        return {**result, "frame": frame}
    if "frame" in spec:
        raise ValueError("视频加工不能指定定格 frame")
    reverse = spec.get("reverse", False)
    step = spec.get("frame_step", 1)
    speed, saturation = spec.get("speed", 1.0), spec.get("saturation", 1.0)
    if type(reverse) is not bool or type(step) is not int or step < 1:
        raise ValueError("reverse 须为布尔值，frame_step 须为正整数")
    if type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0:
        raise ValueError("speed 须为有限正数")
    if type(saturation) not in (int, float) or not math.isfinite(saturation) or not 0 <= saturation <= 2:
        raise ValueError("saturation 须在0—2之间")
    try:
        fps = Fraction(str(spec["output_fps"]))
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("视频加工须明确 output_fps，如30或30000/1001") from exc
    if not 0 < fps <= 120:
        raise ValueError("当前加工输出帧率须大于0且不超过120")
    output_format = spec.get("output_format", "qtrle_mov" if source_probe.get("alpha") else "h264_mp4")
    if output_format not in VIDEO_FORMATS:
        raise ValueError("未知视频输出组合")
    if source_probe.get("alpha") and output_format == "h264_mp4":
        raise ValueError("当前H.264输出不保留透明度，请选择透明MOV输出")
    return {**result, "reverse": reverse, "speed": float(speed), "frame_step": step,
            "saturation": float(saturation), "output_fps": str(fps), "output_format": output_format}


def render_processed_media(source: Path, spec: Mapping[str, Any], source_probe: Mapping[str, Any],
                           directory: Path) -> dict[str, Any]:
    parameters = normalize_processing(spec, source_probe)
    all_frames = video_frames(source, source_probe)
    timerange = parameters["source_timerange"]
    start, end = timerange["start"], timerange["start"] + timerange["duration"]
    selected = [frame for frame in all_frames if start <= frame["source_time_us"] < end]
    if not selected:
        raise ValueError("所选半开源区间内没有实际视频帧")
    decoder = (source_probe.get("alpha_evidence") or {}).get("decoder", "default")
    input_options = ["-c:v", decoder] if decoder != "default" else []
    if parameters["mode"] == "frame":
        chosen = selected[0 if parameters["frame"] == "first" else -1]
        filters = [f"trim=start_frame={chosen['source_frame_index']}:end_frame={chosen['source_frame_index'] + 1}",
                   "setpts=PTS-STARTPTS", "format=rgba"]
        filename, encoding = "media.png", ["-frames:v", "1"]
        mapping = [{**chosen, "output_start_us": 0, "output_duration_us": None}]
        duration_us = None
    else:
        filters = [f"trim=start_frame={selected[0]['source_frame_index']}:end_frame={selected[-1]['source_frame_index'] + 1}",
                   "settb=expr=1/1000000", "setpts=PTS-STARTPTS"]
        step = parameters["frame_step"]
        if step > 1:
            filters.append(f"select=not(mod(n\\,{step}))")
        selected = selected[::step]
        content_frames = list(reversed(selected)) if parameters["reverse"] else selected
        if parameters["reverse"]:
            filters.append("reverse")
        speed = Fraction(str(parameters["speed"]))
        filters.append(f"setpts=(PTS-STARTPTS)*{speed.denominator}/{speed.numerator}")
        if parameters["saturation"] != 1:
            filters.append(f"hue=s={parameters['saturation']}")
        fps = Fraction(parameters["output_fps"])
        count = max(1, _round(Fraction(timerange["duration"], 1_000_000) * fps / speed))
        duration_us = _round(Fraction(count * 1_000_000, 1) / fps)
        filters.extend([f"fps={fps}:start_time=0:round=near:eof_action=pass",
                        f"tpad=stop_mode=clone:stop_duration={duration_us / 1_000_000:.6f}"])
        suffix, codec = VIDEO_FORMATS[parameters["output_format"]]
        filename = "media" + suffix
        encoding = [*codec, "-frames:v", str(count), "-video_track_timescale", "1000000",
                    "-movie_timescale", "1000000"]
        # fps rounds input presentation times to output ticks, then holds the
        # last frame at/before each tick. Reverse changes content, not these PTS.
        ticks = [_round(Fraction(int(Fraction(f["source_time_us"] - selected[0]["source_time_us"]) / speed), 1_000_000) * fps)
                 for f in selected]
        mapping = []
        for index in range(count):
            chosen = content_frames[max(0, bisect_right(ticks, index) - 1)]
            begin = _round(Fraction(index * 1_000_000, 1) / fps)
            finish = _round(Fraction((index + 1) * 1_000_000, 1) / fps)
            mapping.append({**chosen, "output_start_us": begin, "output_duration_us": finish - begin})
    path = directory / filename
    command = ["ffmpeg", "-v", "error", "-nostdin", "-n", *input_options, "-i", str(source),
               "-map", f"0:{source_probe['stream_index']}", "-vf", ",".join(filters), "-an", *encoding, str(path)]
    subprocess.run(command, capture_output=True, check=True, timeout=600)
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                             encoding="utf-8", check=True, timeout=30).stdout.splitlines()[0]
    return {"path": str(path), "parameters": parameters, "frame_map": mapping,
            "expected_duration_us": duration_us, "ffmpeg_version": version,
            "filtergraph": ",".join(filters), "audio_policy": "discard",
            "source_probe": dict(source_probe)}


def validate_processing_record(record: Mapping[str, Any], output_probe: Mapping[str, Any]) -> None:
    """Validate the stored mapping, using the same supported parameter contract."""
    if not isinstance(record, Mapping):
        raise ValueError("派生素材缺少加工记录")
    source_probe = record.get("source_probe") or {}
    parameters = normalize_processing(record.get("parameters") or {}, source_probe)
    if parameters != record.get("parameters") or not record.get("ffmpeg_version") or not record.get("filtergraph"):
        raise ValueError("加工参数、实际版本或滤镜记录不完整")
    if record.get("audio_policy") != "discard":
        raise ValueError("当前局部加工不输出音轨，声音须按原计划单独绑定")
    mapping = record.get("frame_map")
    if not isinstance(mapping, list) or not mapping:
        raise ValueError("派生素材缺少实际源帧到输出的映射")
    timerange = parameters["source_timerange"]
    end, previous = timerange["start"] + timerange["duration"], 0
    for frame in mapping:
        if (not isinstance(frame, Mapping) or type(frame.get("source_frame_index")) is not int
                or frame["source_frame_index"] < 0 or type(frame.get("source_time_us")) is not int
                or not timerange["start"] <= frame["source_time_us"] < end
                or frame.get("output_start_us") != previous):
            raise ValueError("加工帧映射超出源区间或输出区间不连续")
        if parameters["mode"] == "video":
            duration = frame.get("output_duration_us")
            if type(duration) is not int or duration <= 0:
                raise ValueError("加工输出帧时长必须为正整数")
            previous += duration
    if parameters["mode"] == "frame":
        if len(mapping) != 1 or mapping[0].get("output_duration_us") is not None or output_probe.get("duration_us") is not None:
            raise ValueError("源帧提取必须对应单张图片")
    elif (abs(previous - output_probe["duration_us"]) > 2
          or output_probe.get("audio_stream_count") != 0):
        raise ValueError("派生视频实际时长或音轨与加工记录不一致")
