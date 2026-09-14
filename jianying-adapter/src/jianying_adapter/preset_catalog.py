"""Read-only cataloguing for native Jianying preset drafts.

The files distributed as presets commonly contain a small outer draft whose
``materials.drafts[*].draft`` field contains the useful, nested draft.  This
module deliberately treats that nested draft as the primary input and never
writes to the preset tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA = "jianying-adapter.preset-catalog.v1"
DEFAULT_FILENAME = "draft_content.json"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[a-z]:[\\/]|\\\\|/)", re.IGNORECASE)

_OMIT_KEYS = {
    "name",
    "create_time",
    "update_time",
    "created_at",
    "updated_at",
    "created_time",
    "modified_time",
    "last_modified_time",
    "last_modified",
    "timestamp",
    "last_modified_platform",
    "platform",
    "new_version",
    "device_id",
    "machine_id",
    "account_id",
}
_INSTANCE_ID_KEYS = {
    "id",
    "uuid",
    "guid",
    "draft_id",
    "track_id",
    "segment_id",
    "raw_segment_id",
    "material_id",
    "local_id",
    "origin_material_id",
    "group_id",
    "request_id",
    "team_id",
    "combination_id",
}
_RESOURCE_ID_KEYS = {
    "effect_id",
    "resource_id",
    "resource_ids",
    "animation_id",
    "animation_ids",
    "font_id",
    "font_ids",
    "bubble_id",
    "bubble_ids",
    "template_id",
    "sticker_id",
    "filter_id",
    "transition_id",
}
_TEXT_KEYS = {
    "text",
    "content",
    "description",
    "desc",
    "caption",
    "lyrics",
    "source_text",
    "target_text",
}
_PATH_KEYS = {"path", "url", "source", "media_path", "draft_file_path", "cover_path"}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _duration_bucket(value: Any) -> str:
    try:
        # Jianying draft durations are normally microseconds.
        seconds = max(0.0, float(value)) / 1_000_000
    except (TypeError, ValueError):
        return "unknown"
    if seconds == 0:
        return "zero"
    if seconds < 1:
        return "lt_1s"
    if seconds < 3:
        return "1_3s"
    if seconds < 5:
        return "3_5s"
    if seconds < 10:
        return "5_10s"
    if seconds < 30:
        return "10_30s"
    return "30s_plus"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _StructureNormalizer:
    """Normalize instance identity while retaining the draft's behavior data."""

    def __init__(self) -> None:
        self._instance_ids: dict[str, str] = {}
        self._resource_uuids: dict[str, str] = {}

    def _remap(self, value: str, *, resource: bool = False) -> str:
        mapping = self._resource_uuids if resource else self._instance_ids
        prefix = "resource_uuid" if resource else "instance_uuid"
        if value not in mapping:
            mapping[value] = f"<{prefix}_{len(mapping) + 1}>"
        return mapping[value]

    @staticmethod
    def _is_text_key(key: str) -> bool:
        lowered = key.lower()
        return lowered in _TEXT_KEYS or lowered.endswith("_text") or lowered.endswith("_content")

    @staticmethod
    def _is_path_key(key: str) -> bool:
        lowered = key.lower()
        return lowered in _PATH_KEYS or lowered.endswith("_path") or lowered.endswith("_url")

    @staticmethod
    def _is_instance_id_key(key: str) -> bool:
        lowered = key.lower()
        return lowered in _INSTANCE_ID_KEYS or lowered.endswith("_ids") and lowered not in _RESOURCE_ID_KEYS

    @staticmethod
    def _relative_range(value: Any, text_length: int) -> Any:
        if not isinstance(value, list) or len(value) != 2:
            return _MISSING
        try:
            start, end = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return _MISSING
        if text_length <= 0:
            return [0.0, 0.0]
        start = min(1.0, max(0.0, start / text_length))
        end = min(1.0, max(0.0, end / text_length))
        return [round(start, 6), round(end, 6)]

    def normalize(self, value: Any, key: str = "", content_text_length: int | None = None) -> Any:
        lowered = key.lower()
        if isinstance(value, Mapping):
            normalized: dict[str, Any] = {}
            for name in sorted(value):
                name_text = str(name)
                name_lower = name_text.lower()
                if name_lower in _OMIT_KEYS or name_lower in {"draft_name", "project_name"}:
                    continue
                if content_text_length is not None and name_lower == "range":
                    child = self._relative_range(value[name], content_text_length)
                    if child is _MISSING:
                        child = self.normalize(value[name], name_text, content_text_length)
                else:
                    child = self.normalize(value[name], name_text, content_text_length)
                if child is not _MISSING:
                    normalized[name_text] = child
            return normalized
        if isinstance(value, list):
            return [self.normalize(child, key, content_text_length) for child in value]
        if isinstance(value, str):
            if lowered == "content":
                try:
                    parsed_content = json.loads(value)
                except json.JSONDecodeError:
                    parsed_content = _MISSING
                if isinstance(parsed_content, (Mapping, list)):
                    text_value = parsed_content.get("text") if isinstance(parsed_content, Mapping) else None
                    text_length = len(text_value) if isinstance(text_value, str) else 0
                    return self.normalize(parsed_content, "content_json", text_length)
            if self._is_text_key(key):
                return "<text>"
            if self._is_path_key(key) or _ABSOLUTE_PATH_RE.match(value):
                return "<path>"
            if self._is_instance_id_key(key):
                return self._remap(value)
            if lowered in _RESOURCE_ID_KEYS:
                return self._remap(value, resource=True) if _UUID_RE.match(value) else value
            if _UUID_RE.match(value):
                return self._remap(value)
            return value
        if value is None:
            return None
        if self._is_instance_id_key(key) or lowered in _RESOURCE_ID_KEYS:
            # Numeric IDs are instance IDs for generic fields, but resource IDs
            # remain behavior-bearing values in the latter case.  Remapping
            # numeric IDs too preserves their reference relationships.
            return self._remap(str(value)) if self._is_instance_id_key(key) else value
        return value


class _Missing:
    pass


_MISSING = _Missing()


def _normalized_inner_structure(drafts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    normalizer = _StructureNormalizer()
    return {"drafts": [normalizer.normalize(draft) for draft in drafts]}


def _nested_drafts(payload: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
    """Return usable inner drafts and whether an inner wrapper was found."""

    materials = payload.get("materials")
    candidates: list[Mapping[str, Any]] = []
    found_wrapper = False
    if isinstance(materials, Mapping):
        drafts = materials.get("drafts")
        if isinstance(drafts, list):
            found_wrapper = bool(drafts)
            for item in drafts:
                if isinstance(item, Mapping) and isinstance(item.get("draft"), Mapping):
                    candidates.append(item["draft"])
    return candidates, found_wrapper


def _material_index(draft: Mapping[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    materials = draft.get("materials")
    if not isinstance(materials, Mapping):
        return result
    for group, values in materials.items():
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, Mapping):
                continue
            for key in ("id", "material_id", "local_id", "origin_material_id"):
                token = _string(item.get(key))
                if token:
                    result[token].append(str(group))
    return result


def _nonempty_material_groups(draft: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    materials = draft.get("materials")
    if not isinstance(materials, Mapping):
        return {}
    result: dict[str, list[Mapping[str, Any]]] = {}
    for group, values in materials.items():
        if isinstance(values, list):
            objects = [item for item in values if isinstance(item, Mapping)]
            if objects:
                result[str(group)] = objects
    return result


def _infer_segment_material_type(segment: Mapping[str, Any], index: Mapping[str, list[str]]) -> str:
    for key in ("material_type", "type", "segment_type"):
        value = _string(segment.get(key))
        if value:
            return value
    material_id = _string(segment.get("material_id"))
    groups = index.get(material_id, [])
    if groups:
        return sorted(set(groups))[0]
    return "unknown"


def _feature_counter(draft: Mapping[str, Any], groups: Mapping[str, list[Mapping[str, Any]]]) -> Counter[str]:
    features: Counter[str] = Counter()
    group_aliases = {
        "texts": "text",
        "text_templates": "text",
        "videos": "video",
        "images": "image",
        "audios": "audio",
        "audio_effects": "audio_effect",
        "audio_fades": "audio_fade",
        "material_animations": "animation",
        "transitions": "transition",
        "effects": "effect",
        "video_effects": "effect",
        "plugin_effects": "effect",
        "stickers": "sticker",
        "bubbles": "bubble",
        "fonts": "font",
        "canvases": "canvas",
        "common_mask": "mask",
        "mask": "mask",
        "filters": "filter",
        "speeds": "speed",
    }
    for group, values in groups.items():
        alias = group_aliases.get(group)
        if alias:
            features[alias] += len(values)
    for track in _as_list(draft.get("tracks")):
        if not isinstance(track, Mapping):
            continue
        track_type = _string(track.get("type")).lower()
        if track_type in {"text", "caption", "subtitle", "sticker", "audio", "video", "image"}:
            features[track_type if track_type != "caption" else "text"] += 1
        segments = track.get("segments")
        for segment in _as_list(segments):
            if not isinstance(segment, Mapping):
                continue
            if isinstance(segment.get("caption_info"), Mapping):
                features["text"] += 1
            nonempty_keys = {
                str(key).lower()
                for key, value in segment.items()
                if value not in (None, "", [], {})
            }
            if any("animation" in key for key in nonempty_keys) or any(
                key in nonempty_keys for key in {"keyframe_refs", "common_keyframes"}
            ):
                features["animation"] += 1
            if any("effect" in key for key in nonempty_keys):
                features["effect"] += 1
            if any("bubble" in key for key in nonempty_keys):
                features["bubble"] += 1
    return features


def _structure_profile(drafts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    draft_list = list(drafts)
    track_shapes: Counter[tuple[str, int, tuple[str, ...]]] = Counter()
    material_type_counts: Counter[str] = Counter()
    feature_counts: Counter[str] = Counter()
    dependency_groups: Counter[str] = Counter()
    duration_buckets: Counter[str] = Counter()
    segment_duration_buckets: Counter[str] = Counter()
    total_segments = 0
    missing_tracks = 0
    missing_materials = 0

    for draft in draft_list:
        if not isinstance(draft.get("tracks"), list):
            missing_tracks += 1
        if not isinstance(draft.get("materials"), Mapping):
            missing_materials += 1
        groups = _nonempty_material_groups(draft)
        index = _material_index(draft)
        feature_counts.update(_feature_counter(draft, groups))
        for group, values in groups.items():
            dependency_groups[group] += len(values)
        duration_buckets[_duration_bucket(draft.get("duration"))] += 1

        for track in _as_list(draft.get("tracks")):
            if not isinstance(track, Mapping):
                continue
            track_type = _string(track.get("type")) or "unknown"
            segments = [item for item in _as_list(track.get("segments")) if isinstance(item, Mapping)]
            segment_types = tuple(sorted(_infer_segment_material_type(item, index) for item in segments))
            track_shapes[(track_type, len(segments), segment_types)] += 1
            total_segments += len(segments)
            for segment in segments:
                timerange = segment.get("target_timerange")
                duration = timerange.get("duration") if isinstance(timerange, Mapping) else None
                segment_duration_buckets[_duration_bucket(duration)] += 1
                material_type_counts[_infer_segment_material_type(segment, index)] += 1

    track_records = []
    for (track_type, segment_count, segment_types), count in sorted(track_shapes.items()):
        track_records.append({
            "type": track_type,
            "count": count,
            "segment_count": segment_count,
            "segment_material_types": list(segment_types),
        })
    return {
        "inner_draft_count": len(draft_list),
        "track_shapes": track_records,
        "segment_material_type_counts": dict(sorted(material_type_counts.items())),
        "dependency_groups": dict(sorted(dependency_groups.items())),
        "feature_counts": dict(sorted(feature_counts.items())),
        "duration_buckets": dict(sorted(duration_buckets.items())),
        "segment_duration_buckets": dict(sorted(segment_duration_buckets.items())),
        "total_segments": total_segments,
        "missing_tracks": missing_tracks,
        "missing_materials": missing_materials,
    }


def _path_text(relative_path: str) -> str:
    return relative_path.replace("\\", "/").lower()


def _version_facts(drafts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    app_versions: set[str] = set()
    new_versions: set[str] = set()
    for draft in drafts:
        platform = draft.get("platform")
        if isinstance(platform, Mapping):
            value = _string(platform.get("app_version"))
            if value:
                app_versions.add(value)
        value = _string(draft.get("new_version"))
        if value:
            new_versions.add(value)

    buckets: set[str] = set()
    for value in app_versions:
        match = _VERSION_RE.search(value)
        if not match:
            buckets.add("unknown")
            continue
        major, minor, patch = (int(part or 0) for part in match.groups())
        buckets.add("le_8_8" if (major, minor, patch) <= (8, 8, 0) else "gt_8_8")
    if not buckets:
        compatibility = "unknown"
    elif len(buckets) > 1:
        compatibility = "mixed"
    else:
        compatibility = next(iter(buckets))
    return {
        "app_version": sorted(app_versions),
        "new_version": sorted(new_versions),
        "app_version_buckets": sorted(buckets),
        "jianying_8_8_compatibility": compatibility,
    }


def _classify(relative_path: str, profile: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    path = _path_text(relative_path)
    features = profile.get("feature_counts", {})
    groups = profile.get("dependency_groups", {})
    labels: set[str] = set()
    reasons: list[str] = []

    def add(label: str, reason: str) -> None:
        labels.add(label)
        reasons.append(reason)

    if features.get("text", 0) or any(token in path for token in ("字幕", "文字", "文本", "subtitle", "caption", "font")):
        add("subtitle/text", "发现文字轨道、文字材料或字幕/文字路径提示")
    if any(token in path for token in ("开场", "片头", "片尾", "结尾", "标题", "intro", "outro", "title")):
        add("intro_outro/title", "路径名称包含开场、片头/片尾或标题提示")
    if features.get("animation", 0) or any(token in path for token in ("动画", "动效", "滑动", "弹跳", "逐字", "显影", "animation", "motion")):
        add("motion_animation", "发现动画/关键帧结构或动效路径提示")
    if features.get("transition", 0) or "转场" in path or "transition" in path:
        add("transition", "发现 transition 材料或转场路径提示")
    if features.get("audio", 0) or features.get("audio_effect", 0) or any(token in path for token in ("音效", "音乐", "音频", "bgm", "sfx", "audio")):
        add("sfx_audio", "发现音频材料/音频效果或音效路径提示")
    visual_materials = features.get("image", 0) + features.get("sticker", 0)
    if visual_materials or any(token in path for token in ("b-roll", "broll", "配图", "图片", "视觉", "素材", "image", "sticker")):
        add("broll_visual", "发现图片/贴纸材料或 B-roll/视觉路径提示")

    content_families = labels.intersection({"subtitle/text", "intro_outro/title", "sfx_audio", "broll_visual"})
    if len(content_families) >= 2:
        add("mixed", "同时包含两个或以上可独立复用的模板功能族")
    if not labels:
        add("unknown", "没有足够的轨道、材料或路径事实用于分类")
    return sorted(labels), reasons


def _record_from_file(root: Path, path: Path, raw: bytes, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("JSON 顶层不是对象")
    inners, wrapper_found = _nested_drafts(payload)
    warnings: list[dict[str, str]] = []
    if inners:
        drafts = inners
    else:
        drafts = [payload]
        if wrapper_found:
            warnings.append({"kind": "missing_inner_draft", "message": "materials.drafts 存在但没有可用的 drafts[*].draft，已回退外层"})
    profile = _structure_profile(drafts)
    version_facts = _version_facts(drafts)
    normalized_structure = _normalized_inner_structure(drafts)
    relative = path.relative_to(root).as_posix()
    categories, reasons = _classify(relative, profile)
    normalized_hash = _sha256_bytes(_compact_json(normalized_structure).encode("utf-8"))
    record = {
        "relative_path": relative,
        "display_name": path.parent.parent.name if path.parent.name == "preset_draft" else path.parent.name,
        "exact_file_sha256": _sha256_bytes(raw),
        "normalized_structure_sha256": normalized_hash,
        "uses_nested_draft": bool(inners),
        "features": version_facts,
        "categories": categories,
        "reasons": reasons,
        "structural_profile": profile,
    }
    return record, warnings


def _group_records(records: list[Mapping[str, Any]], key: str, only_duplicates: bool = False) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for record in records:
        groups[str(record[key])].append(str(record["relative_path"]))
    result = []
    for digest, paths in sorted(groups.items()):
        paths.sort()
        if only_duplicates and len(paths) < 2:
            continue
        result.append({"sha256": digest, "count": len(paths), "paths": paths})
    return result


def scan_preset_root(root: str | Path, filename: str = DEFAULT_FILENAME) -> dict[str, Any]:
    """Scan ``root`` without changing it and return a deterministic catalog."""

    root_path = Path(root).expanduser().resolve()
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if not root_path.exists():
        return {
            "schema": SCHEMA,
            "summary": {"root": str(root_path), "files_discovered": 0, "records": 0, "errors": 1, "nested_records": 0},
            "records": [],
            "exact_duplicate_groups": [],
            "structural_clusters": [],
            "category_counts": {},
            "errors": [{"relative_path": ".", "kind": "missing_root", "message": "扫描根目录不存在"}],
        }

    files = sorted(root_path.rglob(filename), key=lambda item: item.relative_to(root_path).as_posix())
    for path in files:
        relative = path.relative_to(root_path).as_posix()
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8-sig"))
            record, warnings = _record_from_file(root_path, path, raw, payload)
            records.append(record)
            errors.extend({"relative_path": relative, **warning} for warning in warnings)
            profile = record["structural_profile"]
            if profile["missing_tracks"]:
                errors.append({"relative_path": relative, "kind": "missing_field", "message": "缺少 tracks，已按空轨道容错"})
            if profile["missing_materials"]:
                errors.append({"relative_path": relative, "kind": "missing_field", "message": "缺少 materials，已按空材料容错"})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            errors.append({"relative_path": relative, "kind": type(exc).__name__, "message": str(exc) or "无法解析"})

    records.sort(key=lambda item: item["relative_path"])
    category_counts: Counter[str] = Counter()
    nested_count = 0
    for record in records:
        category_counts.update(record["categories"])
        nested_count += int(record["uses_nested_draft"])
    errors.sort(key=lambda item: (item["relative_path"], item["kind"], item["message"]))
    return {
        "schema": SCHEMA,
        "summary": {
            "root": str(root_path),
            "files_discovered": len(files),
            "records": len(records),
            "errors": len(errors),
            "nested_records": nested_count,
        },
        "records": records,
        "exact_duplicate_groups": _group_records(records, "exact_file_sha256", only_duplicates=True),
        "structural_clusters": _group_records(records, "normalized_structure_sha256"),
        "category_counts": dict(sorted(category_counts.items())),
        "errors": errors,
    }


def write_catalog(catalog: Mapping[str, Any], output: str | Path) -> None:
    """Write a catalog file; this is separate from scanning and never touches presets."""

    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读扫描剪映 preset_draft/draft_content.json")
    parser.add_argument("root", type=Path, help="预设根目录")
    parser.add_argument("--output", "-o", type=Path, help="可选：把目录 JSON 写入指定路径")
    args = parser.parse_args(argv)
    catalog = scan_preset_root(args.root)
    rendered = json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        write_catalog(catalog, args.output)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke usage
    raise SystemExit(main())
