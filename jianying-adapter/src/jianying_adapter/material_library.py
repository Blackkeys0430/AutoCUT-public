"""Shared material discovery, freezing and CandidatePlan handoff.

The agent searches the web, views candidates and invokes image_gen. This module
does the deterministic work and never turns a site flag into an item license.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PureWindowsPath
from functools import lru_cache
from fractions import Fraction
from typing import Any, Mapping
from urllib.parse import urlparse

from PIL import Image

from .media_ledger import DEFAULT_POLICY, file_sha256, load_json, validate_media_ledger
from .media_crop import crop_rectangle, fit_region, material_crop

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "assets" / "material_library_config.json"
SCHEMA = "jianying_media_asset_ledger_v1"


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _path(value: Any, base: Path | None = None) -> Path:
    path = Path(str(value or ""))
    return (path if path.is_absolute() else (base or Path.cwd()) / path).resolve()


def _storage_root(config: Mapping[str, Any]) -> Path:
    root = _path(config["library_root"])
    if PureWindowsPath(str(root)).drive.casefold() == "c:":
        raise ValueError("素材库不可写入 C 盘")
    return root


def validate_request(request: Mapping[str, Any]) -> None:
    for field in ("id", "node_id", "visual_event_id", "purpose", "composition"):
        if not isinstance(request.get(field), str) or not request[field].strip():
            raise ValueError(f"material request requires {field}")
    if request.get("media_type") not in {"image", "video"}:
        raise ValueError("material request media_type must be image or video")
    if request.get('time_behavior') == 'continuous_action' and request['media_type'] != 'video':
        raise ValueError('连续动作需求需要真实视频源，静态图片运动不能替代操作过程')
    if request.get("truth_role") not in {"evidence", "illustration"}:
        raise ValueError("truth_role must be evidence or illustration")
    if not (request.get("query_zh") or request.get("query_en")):
        raise ValueError("material request requires semantic search queries")
    requirements = request.get("requirements") or {}
    for field in ("min_width", "min_height", "duration_us"):
        if field in requirements and (type(requirements[field]) is not int or requirements[field] <= 0):
            raise ValueError(f"requirements.{field} must be a positive integer")


@lru_cache(maxsize=1)
def _alpha_pixel_formats() -> frozenset[str]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_pixel_formats", "-of", "json"],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=30,
    )
    return frozenset(row["name"] for row in json.loads(result.stdout)["pixel_formats"]
                     if row.get("flags", {}).get("alpha"))


def _probe_video_alpha(path: Path, stream: Mapping[str, Any]) -> dict[str, Any]:
    declared = (stream.get("pix_fmt") in _alpha_pixel_formats()
                or str((stream.get("tags") or {}).get("alpha_mode", "0")) == "1")
    if not declared:
        return {"alpha": False, "visible": True, "alpha_channel": False,
                "alpha_evidence": {"scope": "stream_metadata", "decoded_frames": 0}}
    # FFmpeg's native VP8/VP9 decoders can expose only the color stream even
    # when the WebM container explicitly declares an alpha side stream.
    decoder = {"vp8": "libvpx", "vp9": "libvpx-vp9"}.get(stream.get("codec_name"))
    options = ["-c:v", decoder] if decoder else []
    filters = ("alphaextract,scale=in_range=full:out_range=full,format=yuv444p16le,"
               "signalstats,metadata=mode=print:key=lavfi.signalstats.YMIN:file=-,"
               "metadata=mode=print:key=lavfi.signalstats.YMAX:file=-")
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", *options, "-i", str(path),
         "-map", f"0:{stream['index']}", "-vf", filters, "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=300,
    )
    minima = [int(n) for n in re.findall(r"lavfi\.signalstats\.YMIN=(\d+)", result.stdout)]
    maxima = [int(n) for n in re.findall(r"lavfi\.signalstats\.YMAX=(\d+)", result.stdout)]
    if not minima or len(minima) != len(maxima):
        raise ValueError("视频声明透明通道，但未取得完整的解码透明度统计")
    low, high = min(minima), max(maxima)
    if not 0 <= low <= high <= 65535:
        raise ValueError("视频透明度统计超出16位完整范围")
    return {"alpha": low < 65535, "visible": high > 0, "alpha_channel": True,
            "alpha_evidence": {"scope": "all_decoded_frames", "decoded_frames": len(minima),
                               "minimum": low, "maximum": high, "maximum_value": 65535,
                               "decoder": decoder or "default", "method": "alphaextract_full_range_signalstats"}}


def _video_timing(path: Path, stream: Mapping[str, Any]) -> dict[str, Any]:
    start = Fraction(stream.get("start_time") or "0")
    duration = Fraction(stream.get("duration") or "0")
    evidence = "stream_duration"
    if duration <= 0:
        # Matroska format.duration may include a nonzero starting offset, or
        # belong to a longer audio stream. Use this video stream's last PTS.
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", str(stream["index"]), "-show_packets",
             "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", check=True, timeout=300,
        )
        packets = [p for p in json.loads(result.stdout).get("packets", []) if p.get("pts_time") is not None]
        if not packets:
            raise ValueError("视频流没有可用显示时间，不能借用容器或音频时长")
        last = max(packets, key=lambda p: Fraction(p["pts_time"]))
        last_duration = Fraction(last.get("duration_time") or "0")
        if last_duration <= 0:
            raise ValueError("视频流缺少末帧时长，不能从平均帧率或容器时间偏移猜测")
        if stream.get("start_time") is None:
            start = min(Fraction(p["pts_time"]) for p in packets)
        duration = Fraction(last["pts_time"]) + last_duration - start
        evidence = "video_packet_pts"
    if duration <= 0:
        raise ValueError("视频时长不可用")
    return {"duration_us": round(duration * 1_000_000), "start_time_us": round(start * 1_000_000),
            "duration_evidence": evidence}


def probe_media(path: Path, media_type: str) -> dict[str, Any]:
    if media_type == "image":
        with Image.open(path) as im:
            im.load()
            alpha = im.convert("RGBA").getchannel("A").getextrema()
            return {"width": im.width, "height": im.height, "alpha": alpha[0] < 255,
                    "visible": alpha[1] > 0, "duration_us": None}
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=30,
    )
    data = json.loads(result.stdout)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
                   and not s.get("disposition", {}).get("attached_pic")), None)
    if stream is None:
        raise ValueError("素材没有视频流")
    timing = _video_timing(path, stream)
    rotation = next((row["rotation"] for row in stream.get("side_data_list", []) if "rotation" in row), 0)
    return {"width": int(stream["width"]), "height": int(stream["height"]),
            **timing, "stream_index": stream["index"], "codec": stream.get("codec_name"),
            "container": data.get("format", {}).get("format_name"), "pixel_format": stream.get("pix_fmt"),
            "frame_rate": stream.get("avg_frame_rate"), "time_base": stream.get("time_base"),
            "sample_aspect_ratio": stream.get("sample_aspect_ratio"), "rotation_degrees": rotation,
            "color": {key: stream.get(key) for key in ("color_range", "color_space", "color_transfer", "color_primaries")},
            "audio_stream_count": sum(s.get("codec_type") == "audio" for s in data.get("streams", [])),
            **_probe_video_alpha(path, stream)}


def _check_requirements(request: Mapping[str, Any], probe: Mapping[str, Any]) -> None:
    requirements = request.get("requirements") or {}
    if not probe.get("visible", True):
        raise ValueError("素材完全透明，没有可见内容")
    for field in ("width", "height"):
        if probe[field] < requirements.get(f"min_{field}", 1):
            raise ValueError(f"素材 {field} 不足，不能以填写通过代替所需清晰度")
    if requirements.get("alpha_required") and not probe.get("alpha"):
        raise ValueError("需要真实透明通道，当前素材不透明")
    if request["media_type"] == "video" and probe["duration_us"] < requirements.get("duration_us", 1):
        raise ValueError("素材视频时长不足")


def _current_asset_probe(asset: Mapping[str, Any]) -> dict[str, Any]:
    # Old ledgers stored alpha=False for every video. Refresh that observation
    # on demand without rewriting the original ledger or its source/license.
    probe = asset["probe"]
    if asset["media_type"] == "video" and any(key not in probe for key in ("alpha_evidence", "duration_evidence")):
        return probe_media(Path(asset["local_path"]), "video")
    return dict(probe)


def _can_deliver_evidence(asset: Mapping[str, Any]) -> bool:
    kind = asset.get("source_kind")
    return kind not in {"generated", "authored"} and (kind != "derived" or asset.get("truth_role") == "evidence")


def search_materials(request: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    validate_request(request)
    terms = [part.casefold() for part in re.split(r"[\s,，/]+", " ".join(str(request.get(k) or "") for k in ("query_zh", "query_en"))) if part]
    matches, warnings = [], []
    for ledger_path in _storage_root(config).glob("*/ledger.json"):
        try:
            for asset in load_json(ledger_path)["assets"]:
                if asset["media_type"] != request["media_type"]:
                    continue
                haystack = " ".join(str(asset.get(k) or "") for k in ("semantic", "query_zh", "query_en", "tags")).casefold()
                score = sum(term in haystack for term in terms)
                if not score:
                    continue
                if request["truth_role"] == "evidence" and not _can_deliver_evidence(asset):
                    continue
                if not Path(asset["local_path"]).is_file():
                    continue
                probe = _current_asset_probe(asset)
                _check_requirements(request, probe)
                matches.append({"asset_id": asset["asset_id"], "ledger_path": str(ledger_path),
                                "local_path": asset["local_path"], "preview_path": asset.get("preview_path"),
                                "semantic": asset["semantic"], "probe": probe, "score": score,
                                "needs_current_visual_review": True})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"{ledger_path}: {exc}")
    sources = []
    # Keep the existing preferred providers available for each actual media
    # type. ClipNav currently labels Pexels/Pixabay only as video sites.
    providers = load_json(DEFAULT_POLICY).get("sources", [])
    records = [{"id": p["provider"], "name": p["provider"].title(), "url": f"https://{p['domains'][0]}/"}
               for p in sorted(providers, key=lambda p: p["priority"])
               if p.get("default_enabled") and request["media_type"] in p.get("media_types", [])]
    navigation = _path(config["navigation_data"])
    if navigation.is_file():
        wanted = {request["media_type"]}
        for site in load_json(navigation).get("sites", []):
            if site.get("category") not in wanted:
                continue
            records.append(site)
    else:
        warnings.append(f"素材导航不存在: {navigation}; 可继续本地检索和直接网页检索")
    seen_hosts = set()
    for site in records:
        host = (urlparse(site["url"]).hostname or "").removeprefix("www.")
        if not host or host in seen_hosts:
            continue
        seen_hosts.add(host)
        sources.append({"id": site["id"], "name": site["name"], "url": site["url"],
                        "web_queries": [f"site:{host} {request[key]}" for key in ("query_en", "query_zh") if request.get(key)],
                        "keyword_hints": site.get("keywords_en", []), "item_license_check_required": True})
    guide = _path(config["keyword_guide"])
    hints = [line for line in guide.read_text(encoding="utf-8-sig").splitlines()
             if line.startswith("|") and any(term in line.casefold() for term in terms)] if guide.is_file() else []
    return {"request_id": request["id"], "local_matches": sorted(matches, key=lambda m: -m["score"]),
            "source_candidates": sources, "keyword_guide": str(guide), "keyword_matches": hints[:12],
            "warnings": warnings, "web_search_executed": False,
            "next_action": "先查看本地候选；不足时使用 web_queries 实际搜索并查看具体素材。记录结果后再决定生成。"}


def generation_brief(request: Mapping[str, Any]) -> dict[str, Any]:
    validate_request(request)
    if request["truth_role"] != "illustration":
        raise ValueError("事实证据必须保留真实来源，不能用生成画面替代")
    if request["media_type"] != "image":
        raise ValueError("内置生成回退当前输出图片；需要动图时显式改用静态素材加现有关键帧，或取得视频服务授权")
    attempts = request.get("search_attempts") or []
    for stage in ("local", "web"):
        completed = [a for a in attempts if a.get("stage") == stage and a.get("query") and a.get("source") and a.get("reason") and a.get("outcome") in {"no_results", "rejected", "unavailable"}]
        if not completed:
            raise ValueError(f"生成前缺少实际 {stage} 检索结果或不适用原因")
        for attempt in completed:
            if attempt["outcome"] == "rejected" and not attempt.get("reviewed_candidates"):
                raise ValueError("素材效果不合适的判断须记录已查看的候选及原因")
            for candidate in attempt.get("reviewed_candidates", []):
                if not all(candidate.get(key) for key in ("source", "scope", "reason")):
                    raise ValueError("被拒候选须记录 source/scope/reason")
    req = request.get("requirements") or {}
    prompt = (f"为中文口播视频制作可独立使用的辅助图片。\n信息职责：{request['purpose']}\n"
              f"主体与内容：{request.get('subject') or request.get('query_zh') or request.get('query_en')}\n"
              f"构图与入画用途：{request['composition']}\n风格：{request.get('style') or '服从当前视频已确定的视觉设计'}\n"
              f"最低尺寸：{req.get('min_width', '按用途')} × {req.get('min_height', '按用途')}\n"
              f"约束：{request.get('constraints') or '主体清楚，避免无关装饰，不冒充真实事件或真实证据'}\n"
              f"文字：{request.get('text') or '不要额外添加文字，由剪辑字幕承载信息'}")
    if req.get("alpha_required"):
        prompt += "\n必须输出真实透明背景并保留 alpha；不要棋盘格假透明。"
    arguments: dict[str, Any] = {"prompt": prompt}
    if request.get("reference_images"):
        arguments["referenced_image_paths"] = request["reference_images"]
    return {"request_id": request["id"], "tool": "image_gen", "arguments": arguments,
            "generated": False, "next_action": "先查看所有参考图，再调用内置 image_gen；查看结果，定向修改不合适之处，完成后 material-register 入库。"}


def _review(review: Mapping[str, Any], request: Mapping[str, Any], source: Path, probe: Mapping[str, Any]) -> None:
    if review.get("decision") != "accept" or not all(review.get(k) for k in ("content", "composition", "quality", "reviewed_path")):
        raise ValueError("须查看实际素材，记录 accept、content、composition、quality 和 reviewed_path")
    if _path(review["reviewed_path"]) != source.resolve():
        raise ValueError("视觉检查对象与入选素材不是同一文件")
    if request["media_type"] == "video":
        if review.get("scope") not in {"video_segment", "video_frames"}:
            raise ValueError("视频检查须如实区分 video_segment 与 video_frames")
        time_range = review.get("source_timerange") or {}
        start, duration = time_range.get("start", -1), time_range.get("duration", 0)
        if type(start) is not int or type(duration) is not int or start < 0 or duration <= 0 or start + duration > probe["duration_us"]:
            raise ValueError("须记录实际查看的视频源区间，且不能超出素材")
    elif review.get("scope") != "image":
        raise ValueError("图片检查须记录 scope=image")


def process_material(request: Mapping[str, Any], processing: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Produce reviewable local intermediates; register/use remain the same path."""
    from .media_processing import render_processed_media, validate_processing_record, video_frames
    validate_request(request)
    if set(processing) - {"asset_id", "source_asset_id", "authorization_source", "parameters"}:
        raise ValueError("未知素材加工交接字段")
    for field in ("asset_id", "source_asset_id"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", str(processing.get(field) or "")):
            raise ValueError(f"{field} 只允许英文、数字、短横线和下划线")
    if not isinstance(processing.get("authorization_source"), str) or not processing["authorization_source"].strip():
        raise ValueError("须记录当前素材加工的真实授权来源")
    root = _storage_root(config)
    parent_path = root / processing["source_asset_id"] / "ledger.json"
    parent_ledger = load_json(parent_path)
    parents = [a for a in parent_ledger.get("assets", []) if a.get("asset_id") == processing["source_asset_id"]]
    if len(parents) != 1:
        raise ValueError("加工源必须唯一绑定已入库素材")
    report = validate_media_ledger({"schema": parent_ledger.get("schema"), "assets": parents}, policy=load_json(DEFAULT_POLICY))
    if not report.ok:
        raise ValueError(json.dumps(report.to_dict(), ensure_ascii=False))
    parent = parents[0]
    if parent["media_type"] != "video":
        raise ValueError("当前共享加工从视频提取帧或生成局部视频")
    if request["truth_role"] == "evidence" and parent.get("truth_role") != "evidence":
        raise ValueError("原始示意素材不能经加工升级为事实证据")
    parameters = processing.get("parameters") or {}
    from .media_processing import normalize_processing
    source_probe = _current_asset_probe(parent)
    parameters = normalize_processing(parameters, source_probe)
    expected_type = "image" if parameters["mode"] == "frame" else "video"
    if request["media_type"] != expected_type:
        raise ValueError("加工输出类型与当前画面需求不一致")
    seen = (parent.get("visual_review") or {}).get("source_timerange") or {}
    used = parameters["source_timerange"]
    if (used["start"] < seen.get("start", -1) or "duration" not in seen
            or used["start"] + used["duration"] > seen.get("start", 0) + seen["duration"]):
        raise ValueError("加工区间超出已查看的源视频区间")
    destination = root / ".processing" / processing["asset_id"]
    if destination.exists() or (root / processing["asset_id"]).exists():
        raise FileExistsError("加工素材ID已存在，请复用已有输出或选择新ID")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".work-", dir=destination.parent) as stage_name:
        stage = Path(stage_name)
        rendered = render_processed_media(Path(parent["local_path"]), parameters, source_probe, stage)
        output = Path(rendered.pop("path"))
        probe = probe_media(output, expected_type)
        _check_requirements(request, probe)
        if expected_type == "video":
            actual_frames = video_frames(output, probe)
            expected = rendered["frame_map"]
            if (len(actual_frames) != len(expected)
                    or any(abs(a["source_time_us"] - e["output_start_us"]) > 2 for a, e in zip(actual_frames, expected))):
                raise ValueError("派生视频实际帧数或显示时间与加工映射不一致")
        digest = file_sha256(output)
        derivation = {**rendered, "parent": {"asset_id": parent["asset_id"], "ledger_path": str(parent_path),
                                           "sha256": parent["sha256"]}, "output_sha256": digest}
        validate_processing_record(derivation, probe)
        origin_fields = ("provider", "license_status", "license_url", "license_name", "license_checked_at",
                         "source_page_url", "license_evidence", "usage_scope", "editorial_only",
                         "attribution_required", "attribution_text")
        selection = {key: copy.deepcopy(parent[key]) for key in origin_fields if key in parent}
        selection.update(asset_id=processing["asset_id"], source_kind="derived", local_path=str(destination / output.name),
                         authorization_source=processing["authorization_source"], derivation=derivation,
                         processing_record_path=str(destination / "processing.json"))
        result = {"selection": selection, "probe": probe, "media_type": expected_type,
                  "processing_record_path": str(destination / "processing.json"),
                  "next_action": "查看实际派生文件，补 selection.visual_review 后 material-register；沿原 material-frame/use 交接。未进行8.8或用户视觉验收。"}
        _write(stage / "request.json", request)
        _write(stage / "processing.json", derivation)
        _write(stage / "result.json", result)
        stage.rename(destination)
    return result


def register_material(request: Mapping[str, Any], selection: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    validate_request(request)
    asset = copy.deepcopy(dict(selection))
    asset_id = str(asset.get("asset_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", asset_id):
        raise ValueError("asset_id 只允许英文、数字、短横线和下划线")
    source = _path(asset.get("local_path"))
    if not source.is_file():
        raise ValueError("须先取得真实本地文件，不能把网页地址当素材")
    kind = asset.get("source_kind", "download")
    if kind == "generated":
        generation_brief(request)
    if kind in {"generated", "authored"} and request["truth_role"] != "illustration":
        raise ValueError("制作画面不能冒充事实证据")
    probe = probe_media(source, request["media_type"])
    _check_requirements(request, probe)
    _review(asset.get("visual_review") or {}, request, source, probe)
    asset.update(semantic_node_id=request["node_id"], semantic=request["purpose"], truth_role=request["truth_role"],
                 query_zh=request.get("query_zh", ""), query_en=request.get("query_en", ""), media_type=request["media_type"],
                 sha256=file_sha256(source), local_path=str(source), probe=probe, original_path=str(source))
    report = validate_media_ledger({"schema": SCHEMA, "assets": [asset]}, policy=load_json(DEFAULT_POLICY))
    if not report.ok:
        raise ValueError(json.dumps(report.to_dict(), ensure_ascii=False))
    root = _storage_root(config)
    destination = root / asset_id
    if destination.exists():
        raise ValueError(f"素材 ID 已存在，复用或使用新 ID，不覆盖: {asset_id}")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".import-", dir=root) as stage_name:
        stage = Path(stage_name)
        filename = "media" + source.suffix.lower()
        shutil.copy2(source, stage / filename)
        if file_sha256(stage / filename) != asset["sha256"]:
            raise ValueError("入库复制后内容发生变化")
        if request["media_type"] == "image":
            preview_name = filename
        else:
            preview_name = "preview.jpg"
            start = asset["visual_review"]["source_timerange"]["start"] / 1_000_000
            subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(stage / filename),
                            "-frames:v", "1", "-vf", "scale=640:-2", str(stage / preview_name)],
                           check=True, capture_output=True, timeout=30)
            if not (stage / preview_name).is_file():
                raise ValueError("素材预览生成失败")
        asset.update(local_path=str(destination / filename), preview_path=str(destination / preview_name))
        if kind == "derived":
            _write(stage / "processing.json", asset["derivation"])
            asset["processing_record_path"] = str(destination / "processing.json")
        asset["visual_review"]["reviewed_sha256"] = asset["sha256"]
        _write(stage / "request.json", request)
        _write(stage / "ledger.json", {"schema": SCHEMA, "assets": [asset]})
        stage.rename(destination)
    return {"asset_id": asset_id, "ledger_path": str(destination / "ledger.json"),
            "local_path": asset["local_path"], "preview_path": asset["preview_path"], "probe": probe}


def check_asset_binding(spec: Mapping[str, Any], plan_path: Path, draft: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    binding = spec.get("media_asset")
    if binding is None:
        return None  # Existing plans and original A-roll remain readable.
    if not isinstance(binding, Mapping):
        raise ValueError("media_asset must be an object")
    ledger_path = _path(binding.get("ledger_path"), plan_path.parent)
    request_path = _path(binding.get("request_path"), plan_path.parent)
    request = load_json(request_path)
    validate_request(request)
    if binding.get('request_context') is not None and binding['request_context'] != request_context(request):
        raise ValueError('素材用途、内容或构图要求已变化，须针对当前需求重新 material-use')
    assets = [a for a in load_json(ledger_path).get("assets", []) if a.get("asset_id") == binding.get("asset_id")]
    if len(assets) != 1:
        raise ValueError("media_asset 必须唯一绑定台账中的真实素材")
    asset = assets[0]
    report = validate_media_ledger({"schema": SCHEMA, "assets": assets}, policy=load_json(DEFAULT_POLICY))
    if not report.ok:
        raise ValueError(json.dumps(report.to_dict(), ensure_ascii=False))
    asset = {**asset, "probe": _current_asset_probe(asset)}
    source = _path(spec.get("source_path"), plan_path.parent)
    if source != _path(asset["local_path"]) or spec.get("node_id") != request["node_id"] or request["media_type"] != asset["media_type"]:
        raise ValueError("操作、需求和台账的节点/素材路径/类型不一致")
    if request["truth_role"] == "evidence" and not _can_deliver_evidence(asset):
        raise ValueError("生成或派生示意素材不可复用为事实证据")
    _check_requirements(request, asset["probe"])
    review = binding.get("visual_review") or {}
    _review(review, request, source, asset["probe"])
    _validate_use_framing(spec, review, request, asset['probe'])
    if asset["media_type"] == "video":
        used = spec.get("source_timerange") or (spec.get("segment") or {}).get("source_timerange") or {}
        seen = review["source_timerange"]
        if used.get("start", -1) < seen["start"] or used.get("duration", 0) <= 0 or used.get("start", 0) + used.get("duration", 0) > seen["start"] + seen["duration"]:
            raise ValueError("实际使用的源区间超出已查看的可用区间")
    if draft is not None:
        ids = {str(m.get("id")): m for m in (draft.get("materials") or {}).get("videos", [])}
        tracks = [t for t in draft.get("tracks", []) if t.get("name") == spec.get("track_name")]
        segments = [s for t in tracks for s in t.get("segments", [])]
        if not segments or any(_path(ids.get(str(s.get("material_id")), {}).get("path"), plan_path.parent) != source for s in segments):
            raise ValueError("装配或 Writer 回读的实际素材与台账不一致")
        expected_range = spec.get("source_timerange") or (spec.get("segment") or {}).get("source_timerange")
        if len(segments) != 1 or segments[0].get("source_timerange") != expected_range:
            raise ValueError("装配或 Writer 回读的实际源区间与素材使用计划不一致")
        actual = segments[0]
        if material_crop(ids[str(actual['material_id'])]) != crop_rectangle(spec.get('source_crop', [0, 0, 1, 1])):
            raise ValueError('装配或 Writer 回读的实际 source_crop 与已查看取景不一致')
        def contains(actual_value, expected):
            return all(key in actual_value and (contains(actual_value[key], value) if isinstance(value, Mapping)
                       and isinstance(actual_value[key], Mapping) else actual_value[key] == value) for key, value in expected.items())
        if not contains(actual.get('clip') or {}, spec.get('clip') or {}):
            raise ValueError('装配或 Writer 回读的实际 clip 与素材构图不一致')
    return {"asset_id": asset["asset_id"], "request_id": request["id"], "request_path": str(request_path),
            "ledger_path": str(ledger_path), "visual_event_id": request["visual_event_id"],
            'visual_requirement_id': request.get('visual_requirement_id')}


def request_context(request):
    """Keep the actual creative demand; search history can grow independently."""
    return {key: copy.deepcopy(request.get(key)) for key in (
        'id', 'node_id', 'visual_event_id', 'visual_requirement_id', 'purpose', 'subject', 'observable',
        'composition', 'style', 'constraints', 'text', 'reference_images', 'truth_role', 'media_type', 'time_behavior', 'requirements')}


def _validate_use_framing(spec, review, request, probe):
    crop = crop_rectangle(spec.get('source_crop', [0, 0, 1, 1]))
    framed_probe = {**probe, 'width': probe['width']*(crop[2]-crop[0]),
                    'height': probe['height']*(crop[3]-crop[1])}
    _check_requirements(request, framed_probe)
    framing = (spec.get('media_asset') or {}).get('framing')
    if crop != [0, 0, 1, 1] or framing is not None:
        if review.get('source_crop') != crop or review.get('clip') != spec.get('clip'):
            raise ValueError('裁切使用须记录实际查看的 source_crop 和 clip，不能只查看原素材全图')
    if framing is not None:
        solved = fit_region([probe['width'], probe['height']], framing['canvas_size'], framing['target_rect'],
                            source_crop=crop, content_region=framing.get('content_region'))
        if solved['clip'] != spec.get('clip'):
            raise ValueError('素材 clip 与当前 framing 取景求解不一致')


def plan_material_requests(plan: Mapping[str, Any], plan_path: Path) -> dict[str, Any]:
    """Read declared demand before operations exist; do not invent search results."""
    errors, requests, seen = [], [], set()
    for event in plan.get('visual_events', []):
        support = event.get('supporting_visual') or {}
        paths = support.get('request_paths', [])
        if not isinstance(paths, list) or any(not isinstance(p, str) or not p.strip() for p in paths):
            errors.append(f"material request {event.get('id')}: request_paths 必须为路径列表")
            continue
        needs = event.get('visual_requirements') or []
        paths = list(dict.fromkeys(paths + [need['request_path'] for need in needs
            if isinstance(need, Mapping) and need.get('delivery') == 'media' and isinstance(need.get('request_path'), str)]))
        if paths and support.get('status') == 'not_needed':
            errors.append(f"material request {event.get('id')}: 已提出画面需求，不能以 not_needed 隐藏未完成素材")
        for value in paths:
            try:
                path = _path(value, plan_path.parent)
                if path in seen:
                    raise ValueError('同一需求路径重复绑定；不同视觉事件应使用各自的当前用途需求')
                seen.add(path)
                request = load_json(path)
                validate_request(request)
                if request['visual_event_id'] != event.get('id'):
                    raise ValueError('需求的 visual_event_id 与当前事件不一致')
                if needs:
                    matched = [need for need in needs if need.get('id') == request.get('visual_requirement_id')]
                    if len(matched) != 1 or matched[0].get('delivery') != 'media':
                        raise ValueError('素材需求必须绑定当前需要媒体呈现的 visual_requirement_id')
                    need = matched[0]
                    if any(request.get(key) != need.get(key) for key in ('subject', 'observable', 'time_behavior')):
                        raise ValueError('素材需求与当前 visual_requirement 的对象、观察目标或时间行为不一致')
                linked = [op['id'] for op in plan.get('operations', [])
                          if isinstance(op.get('media_asset'), Mapping) and op.get('id')
                          and op['id'] in support.get('operation_ids', [])
                          and _path(op['media_asset'].get('request_path'), plan_path.parent) == path]
                requests.append({'request_path': str(path), 'request_id': request['id'],
                                 'visual_event_id': event['id'], 'purpose': request['purpose'],
                                 'visual_requirement_id': request.get('visual_requirement_id'),
                                 'subject': request.get('subject'), 'observable': request.get('observable'),
                                 'truth_role': request['truth_role'], 'media_type': request['media_type'],
                                 'query_zh': request.get('query_zh'), 'query_en': request.get('query_en'),
                                 'operation_ids': linked, 'status': 'bound' if linked else 'needs_asset',
                                 'next_action': '核对当前绑定的实际素材及构图' if linked else 'material-search 后实搜实看；合适后 register/use，生成按当前授权和真实来源要求处理'})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"material request {event.get('id')}: {exc}")
    return {'ok': not errors, 'errors': errors, 'requests': requests,
            'pending_count': sum(row['status'] == 'needs_asset' for row in requests),
            'web_search_executed': False, 'generation_executed': False}


def validate_plan_materials(plan: Mapping[str, Any], plan_path: Path, draft: Mapping[str, Any] | None = None) -> dict[str, Any]:
    demands = plan_material_requests(plan, plan_path)
    errors, checked = list(demands['errors']), []
    for spec in plan.get("operations", []):
        if not isinstance(spec, Mapping) or "media_asset" not in spec:
            continue
        try:
            if spec.get("kind") != "add_broll":
                raise ValueError("media_asset 当前只接入 add_broll")
            framing = (spec.get('media_asset') or {}).get('framing')
            target = plan.get('target') or {}
            if framing and target and framing.get('canvas_size') != [target.get('width'), target.get('height')]:
                raise ValueError('素材 framing 画布与当前 CandidatePlan.target 不一致')
            result = check_asset_binding(spec, plan_path, draft)
            events = [e for e in plan.get("visual_events", []) if e.get("id") == result["visual_event_id"]]
            if len(events) != 1 or spec.get("id") not in (events[0].get("supporting_visual") or {}).get("operation_ids", []):
                raise ValueError("素材需求必须绑定当前 visual_event 的辅助画面操作")
            checked.append(result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"material {spec.get('id')}: {exc}")
    checked_requests = {row['request_path'] for row in checked}
    for row in demands['requests']:
        if row['request_path'] not in checked_requests:
            errors.append(f"material request {row['request_id']}: 已提出的画面需求尚未连接合格的实际素材；不得省略或用其他素材代交")
    return {"ok": not errors, "errors": errors, "checked_assets": checked, 'requests': demands['requests']}


def attach_material_handoff(plan: Mapping[str, Any], plan_path: Path, handoff: Mapping[str, Any]) -> dict[str, Any]:
    """Attach one checked asset to the existing plan without dropping other demand.

    Pure in memory; the CLI saves a new workspace plan. No Writer/QA flags change.
    """
    result = copy.deepcopy(dict(plan))
    events = [e for e in result.get('visual_events', []) if e.get('id') == handoff['visual_event_id']]
    if len(events) != 1:
        raise ValueError('material-use --plan 需要唯一匹配的 visual_event')
    event, operation, broll = events[0], copy.deepcopy(handoff['operation']), copy.deepcopy(handoff['broll'])
    request_path = _path(handoff['asset_request_path'], plan_path.parent)
    request = load_json(request_path)
    binding = check_asset_binding(operation, plan_path)
    if (binding['visual_event_id'] != event['id'] or _path(binding['request_path']) != request_path
            or request['visual_event_id'] != event['id']):
        raise ValueError('交接、需求与素材操作的事件或需求路径不一致')
    if not (event['start_us'] <= operation['start_us'] < operation['end_us'] <= event['end_us']):
        raise ValueError('素材目标区间必须位于当前视觉事件内')
    support = event.setdefault('supporting_visual', {})
    if support.get('status') == 'not_needed':
        raise ValueError('当前事件声明不需要辅助画面；先按实际创作判断调整该事件，不能由交接命令暗改设计')
    operations = result.setdefault('operations', [])
    existing = [op for op in operations if op.get('id') == operation['id']]
    if existing and (len(existing) != 1 or existing[0] != operation):
        raise ValueError('同名素材操作已存在且内容不同，请使用新操作 ID 或先明确修改计划')
    if not existing:
        if any(op.get('kind') == 'add_broll' and op.get('track_name') == operation['track_name'] for op in operations):
            raise ValueError('素材轨道已被其他操作使用，请指定独立 track_name')
        operations.append(operation)
    brolls = result.setdefault('brolls', [])
    existing_broll = [item for item in brolls if item.get('track_name') == broll['track_name']]
    if existing_broll and (len(existing_broll) != 1 or existing_broll[0] != broll):
        raise ValueError('同名 B-roll 声明不同，不自动覆盖')
    if not existing_broll:
        brolls.append(broll)
    paths = support.setdefault('request_paths', [])
    if request_path not in {_path(p, plan_path.parent) for p in paths}:
        paths.append(str(request_path))
    support.setdefault('purpose', request['purpose'])
    refs = support.setdefault('operation_ids', [])
    if operation['id'] not in refs:
        refs.append(operation['id'])
    techniques = event.setdefault('techniques', [])
    media = next((t for t in techniques if t.get('kind') == 'media'), None)
    if media is None:
        media = {'kind': 'media', 'operation_ids': []}
        techniques.append(media)
    if operation['id'] not in media.setdefault('operation_ids', []):
        media['operation_ids'].append(operation['id'])
    if event.get('visual_requirements'):
        needs = [need for need in event['visual_requirements'] if need.get('id') == request.get('visual_requirement_id')]
        if len(needs) != 1 or needs[0].get('delivery') != 'media':
            raise ValueError('material-use 不能用其他画面需求代交当前对象')
        if operation['id'] not in needs[0].setdefault('operation_ids', []):
            needs[0]['operation_ids'].append(operation['id'])
    support['status'] = 'needs_asset'
    demands = plan_material_requests(result, plan_path)
    if not demands['ok']:
        raise ValueError('; '.join(demands['errors']))
    if all(row['status'] == 'bound' for row in demands['requests'] if row['visual_event_id'] == event['id']):
        support['status'] = 'ready'
    else:
        support.setdefault('asset_search', {
            'queries': [str(row.get('query_zh') or row.get('query_en')) for row in demands['requests']
                        if row['visual_event_id'] == event['id'] and row['status'] == 'needs_asset'],
            'next_action': '运行 material-search 并实际查看当前尚未绑定的需求，完成后继续 material-use'})
    # Design decisions are authored by the agent. Contradictory omit decisions
    # remain visible to the existing validator; a handoff never fabricates them.
    return result


def material_handoff(request_path: Path, usage: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    request = load_json(request_path)
    validate_request(request)
    asset_id = str(usage["asset_id"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", asset_id):
        raise ValueError("invalid asset_id")
    ledger = _storage_root(config) / asset_id / "ledger.json"
    asset = load_json(ledger)["assets"][0]
    asset = {**asset, "probe": _current_asset_probe(asset)}
    start, end = usage["start_us"], usage["end_us"]
    if type(start) is not int or type(end) is not int or start < 0 or end <= start or not (usage.get('clip') or usage.get('framing')):
        raise ValueError("handoff requires positive target range and actual clip layout")
    framing = usage.get('framing')
    crop = crop_rectangle(usage.get('source_crop', [0, 0, 1, 1]))
    clip = usage.get('clip')
    if framing is not None:
        solved = fit_region([asset['probe']['width'], asset['probe']['height']], framing['canvas_size'],
                            framing['target_rect'], source_crop=crop, content_region=framing.get('content_region'))
        if clip is not None and clip != solved['clip']:
            raise ValueError('同时给出 clip 和 framing 时必须一致')
        clip = solved['clip']
    operation = {"id": usage.get("operation_id") or f"media_{request['id']}", "kind": "add_broll",
                 "node_id": request["node_id"], "track_name": usage.get("track_name") or f"JY_BROLL_{request['node_id']}",
                 "source_path": asset["local_path"], "start_us": start, "end_us": end,
                 "source_timerange": {"start": usage.get("source_start_us", 0), "duration": end - start},
                 "clip": clip, "source_crop": crop, "segment": {"volume": 0.0},
                 "media_asset": {"asset_id": asset_id, "ledger_path": str(ledger), "request_path": str(request_path.resolve()),
                                 'request_context': request_context(request),
                                 "visual_review": usage["visual_review"]}}
    if framing is not None:
        operation['media_asset']['framing'] = copy.deepcopy(framing)
    check_asset_binding(operation, request_path)
    broll = {k: copy.deepcopy(operation[k]) for k in ("node_id", "track_name", "source_path", "start_us", "end_us", "clip", 'source_crop', 'source_timerange')}
    broll["segment_count"] = 1
    return {"operation": operation, "broll": broll, "visual_event_id": request["visual_event_id"],
            "supporting_visual_operation_id": operation["id"], "asset_request_path": str(request_path.resolve())}


def material_frame(request, usage, config):
    """Expose the native framing calculation before current-use review/attachment."""
    validate_request(request)
    identifier = str(usage.get('asset_id', ''))
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', identifier):
        raise ValueError('invalid asset_id')
    ledger = load_json(_storage_root(config) / identifier / 'ledger.json')
    assets = [asset for asset in ledger.get('assets', []) if asset.get('asset_id') == identifier]
    if len(assets) != 1:
        raise ValueError('素材必须唯一绑定台账')
    asset = {**assets[0], 'probe': _current_asset_probe(assets[0])}
    framing = usage.get('framing') or {}
    if asset['media_type'] != request['media_type']:
        raise ValueError('取景素材类型与当前需求不一致')
    solved = fit_region([asset['probe']['width'], asset['probe']['height']], framing.get('canvas_size'),
        framing.get('target_rect'), source_crop=usage.get('source_crop'), content_region=framing.get('content_region'))
    return {'asset_id': identifier, 'source_path': asset['local_path'], **solved,
            'framing': copy.deepcopy(framing), 'native_visual_verified': False,
            'next_action': '按当前源区间和裁切查看主体，用既有预览核对布局；将已查看的 source_crop/clip 写入当前用途 visual_review，再 material-use'}
