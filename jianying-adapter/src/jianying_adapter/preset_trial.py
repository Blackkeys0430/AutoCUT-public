"""In-memory, guarded preset trial assembly for Jianying drafts.

This module stops at a Python ``dict``.  It does not start Jianying, write a
draft directory, or alter a preset source file.  The production boundary is
small on purpose:

* fill the selected registry candidate with ``apply_template_slots``;
* extract the candidate's inner draft;
* import only text/video/audio tracks;
* copy the transitive material dependencies of those tracks;
* remap track, segment, and material IDs before merging; and
* validate the resulting independent in-memory draft.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .preset_registry import apply_template_slots
from .validate import ValidationReport


# pyJianYingDraft exports native independent video effects as `effect`
# tracks.  Keep this in the structural validator's allow-list so a real
# EffectSegment is checked instead of rejected as an unknown track.
ALLOWED_TRACK_TYPES = frozenset({"text", "video", "audio", "effect"})
# These are the material collections which can put pixels on the canvas when
# referenced by a segment.  Do not treat ``canvases``/``effects`` and the
# other Jianying bookkeeping collections as media: they commonly have no
# local path by design.  A video material may have ``type=photo`` (this is how
# Jianying stores the transparent placeholder used by some presets).
_VISUAL_MATERIAL_GROUPS = frozenset({"videos", "images", "stickers"})
_MATERIAL_ID_KEYS = ("id", "material_id", "local_material_id", "origin_material_id")
_MATERIAL_REFERENCE_KEYS = frozenset(
    {
        "material_id",
        "material_ids",
        "material_refs",
        "text_material_id",
        "text_material_ids",
        "extra_material_refs",
    }
)
_EXTERNAL_RESOURCE_ID_KEYS = frozenset({"resource_id", "third_resource_id"})
_NESTED_REFERENCE_KEYS = frozenset(
    {"keyframe_id", "keyframe_ids", "common_keyframe_id", "common_keyframe_ids"}
)
_PATH_KEYS = frozenset(
    {
        "path",
        "media_path",
        "file_path",
        "filepath",
        "font_path",
        "font_url",
        "res_path",
        "production_path",
        "source_path",
        "intensifies_path",
    }
)
_REMOTE_PREFIXES = ("http://", "https://", "data:", "asset://", "builtin://")
_MAC_PREFIXES = (
    "/Users/",
    "/Volumes/",
    "/Applications/",
    "/private/var/",
)
_OFFICIAL_AUDIO_CACHE_NAME = re.compile(
    r"^[0-9a-fA-F]{32}\.(?:mp3|wav|aac|m4a|flac|ogg|wma)$",
    re.IGNORECASE,
)


IdFactory = Callable[[], str]


class TrialDraftError(ValueError):
    """Base error for a trial that cannot be safely assembled."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = dict(report or {})


class PresetExtractionError(TrialDraftError):
    """The supplied value is not an outer preset with an inner draft."""


class UnsupportedTrackError(TrialDraftError):
    """An imported preset contains a track outside the supported allow-list."""


class TrialDependencyError(TrialDraftError):
    """A required material or external path cannot be resolved explicitly."""


class TrialValidationError(TrialDraftError):
    """The merged in-memory draft failed structural validation."""

    def __init__(self, message: str, validation: ValidationReport):
        super().__init__(message, report=validation.to_dict())
        self.validation = validation


@dataclass(frozen=True)
class TrialBuildResult:
    """A successful in-memory trial and its auditable facts."""

    draft: dict[str, Any]
    report: dict[str, Any]
    id_map: dict[str, dict[str, str]]
    validation: ValidationReport
    imported_track_ids: tuple[str, ...]
    imported_material_ids: tuple[str, ...]

    @property
    def content(self) -> dict[str, Any]:
        """Compatibility alias matching the existing subtitle patch result."""

        return self.draft

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft": self.draft,
            "report": self.report,
            "id_map": self.id_map,
            "validation": self.validation.to_dict(),
            "imported_track_ids": list(self.imported_track_ids),
            "imported_material_ids": list(self.imported_material_ids),
        }


@dataclass(frozen=True)
class _MaterialEntry:
    group: str
    index: int
    item: dict[str, Any]

    @property
    def key(self) -> tuple[str, int]:
        return self.group, self.index


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _identifier(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _load_json_value(value: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    path = Path(value)
    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise PresetExtractionError(f"JSON 根对象不是 object: {path}")
    return loaded


def extract_inner_drafts(payload: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Extract every ``materials.drafts[*].draft`` from an outer preset.

    A direct inner draft is accepted as a convenience for synthetic tests and
    for callers that already extracted the wrapper.  The returned values are
    deep copies, so later slot filling cannot mutate the source object.
    """

    if not isinstance(payload, Mapping):
        raise PresetExtractionError("preset payload 必须是 object")
    materials = payload.get("materials")
    if isinstance(materials, Mapping):
        drafts = [
            item["draft"]
            for item in _as_list(materials.get("drafts"))
            if isinstance(item, Mapping) and isinstance(item.get("draft"), Mapping)
        ]
        if drafts:
            return tuple(copy.deepcopy(dict(item)) for item in drafts)
    if isinstance(materials, Mapping) and isinstance(payload.get("tracks"), list):
        return (copy.deepcopy(dict(payload)),)
    raise PresetExtractionError("未找到 materials.drafts[*].draft inner draft")


def extract_inner_draft(payload: Mapping[str, Any], draft_index: int = 0) -> dict[str, Any]:
    """Extract one inner draft by wrapper index."""

    drafts = extract_inner_drafts(payload)
    try:
        return copy.deepcopy(drafts[draft_index])
    except (IndexError, TypeError) as exc:
        raise PresetExtractionError(f"inner draft index 无效: {draft_index}") from exc


def _find_candidate(registry: Mapping[str, Any], candidate_id: str) -> Mapping[str, Any]:
    candidates = registry.get("candidates")
    if not isinstance(candidates, list):
        raise TrialDraftError("registry 缺少 candidates 数组")
    matches = [
        item
        for item in candidates
        if isinstance(item, Mapping) and str(item.get("template_id", "")) == str(candidate_id)
    ]
    if len(matches) != 1:
        raise TrialDraftError(f"candidate_id 必须唯一匹配一个模板: {candidate_id!r}")
    return matches[0]


def _resolve_preset_source(
    candidate: Mapping[str, Any],
    *,
    outer_preset: Mapping[str, Any] | str | Path | None,
    preset_root: str | Path | None,
) -> dict[str, Any]:
    if outer_preset is not None:
        return _load_json_value(outer_preset)
    source = _identifier(candidate.get("source_path"))
    if not source:
        raise PresetExtractionError("candidate 缺少 source_path，且未提供 outer_preset")
    source_path = Path(source)
    if not source_path.is_absolute():
        if preset_root is None:
            raise PresetExtractionError("candidate source_path 是相对路径，必须提供 preset_root")
        source_path = Path(preset_root) / source_path
    if not source_path.is_file():
        raise PresetExtractionError(f"找不到 candidate source preset: {source_path}")
    return _load_json_value(source_path)


def _material_index(inner_draft: Mapping[str, Any]) -> dict[str, _MaterialEntry]:
    materials = inner_draft.get("materials")
    if not isinstance(materials, Mapping):
        raise TrialDependencyError("inner draft 缺少 materials object")
    index: dict[str, _MaterialEntry] = {}
    ambiguous_aliases: set[str] = set()
    for group, raw_items in materials.items():
        if group == "drafts" or not isinstance(raw_items, list):
            continue
        for item_index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            entry = _MaterialEntry(str(group), item_index, item)
            for key in _MATERIAL_ID_KEYS:
                value = _identifier(item.get(key))
                if not value or value in ambiguous_aliases:
                    continue
                previous = index.get(value)
                if previous is not None and previous.key != entry.key:
                    if key == "id":
                        raise TrialDependencyError(
                            f"material ID 在不同素材对象中重复: {value!r}"
                        )
                    # Non-primary aliases such as ``material_id`` can be a
                    # shared remote catalogue identity.  Remove an ambiguous
                    # alias instead of binding it to the wrong local object;
                    # unique root ``id`` values remain available to segments.
                    index.pop(value, None)
                    ambiguous_aliases.add(value)
                    continue
                index[value] = entry
    return index


def _reference_values(key: str, value: Any) -> list[str]:
    if key not in _MATERIAL_REFERENCE_KEYS:
        return []
    values = value if isinstance(value, list) else [value]
    return [_identifier(item) for item in values if _identifier(item)]


def _visual_material_entries(
    materials: Mapping[str, Any],
) -> dict[str, list[tuple[str, int, Mapping[str, Any]]]]:
    """Index visual material aliases, retaining ambiguous shared aliases.

    ``material_id`` is often a shared online catalogue identity, while ``id``
    is the local object identity.  Keeping all owners here means a shared
    online alias cannot accidentally hide one owner with an empty path.
    """

    result: dict[str, list[tuple[str, int, Mapping[str, Any]]]] = {}
    for group, raw_items in materials.items():
        if str(group) not in _VISUAL_MATERIAL_GROUPS or not isinstance(raw_items, list):
            continue
        for index, item in enumerate(raw_items):
            if not isinstance(item, Mapping):
                continue
            owner = (str(group), index, item)
            for key in _MATERIAL_ID_KEYS:
                identifier = _identifier(item.get(key))
                if identifier:
                    result.setdefault(identifier, []).append(owner)
    return result


def _visual_material_missing_kind(item: Mapping[str, Any]) -> str:
    """Return a stable explanation for a referenced visual with no local media."""

    name = _identifier(item.get("material_name"))
    item_type = _identifier(item.get("type")).casefold()
    source_platform = _identifier(item.get("source_platform"))
    if item_type == "video" and ("复合片段" in name or "compound" in name.casefold()):
        return "empty_path_compound_clip"
    if item_type == "photo" and name == "透明":
        return "empty_path_transparent_photo"
    if source_platform not in {"", "0"} or any(
        _identifier(item.get(key)) not in {"", "0"}
        for key in ("resource_id", "third_resource_id", "remote_url")
    ):
        return "empty_path_online_media"
    return "empty_path_visual_material"


def _validate_referenced_visual_materials(
    report: ValidationReport,
    *,
    materials: Mapping[str, Any],
    referenced_segments: Sequence[tuple[str, str, Mapping[str, Any]]],
    material_scope: set[str] | None,
    exists_fn: Callable[[Path], bool],
    path_base: Path | None,
    allowed_missing_paths: set[str],
) -> None:
    """Fail closed on visual materials used by a segment but lacking media.

    The generic path walker intentionally skips empty strings.  That is safe
    for optional fields, but unsafe for a material which is actually placed
    on a video track: Jianying renders it as ``Media Not Found``.  This gate
    therefore starts from segment references and inspects the owning material
    object directly.
    """

    index = _visual_material_entries(materials)
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for segment_location, reference, segment in referenced_segments:
        for group, item_index, item in index.get(reference, []):
            item_ids = {
                _identifier(item.get(key))
                for key in _MATERIAL_ID_KEYS
                if _identifier(item.get(key))
            }
            if material_scope is not None and not item_ids.intersection(material_scope):
                continue
            marker = (group, item_index, segment_location)
            if marker in seen:
                continue
            seen.add(marker)
            path_values: list[tuple[str, str]] = []
            for key in ("path", "media_path", "file_path", "filepath", "source_path"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    path_values.append((key, value))
            usable_paths: list[tuple[str, str]] = []
            for key, value in path_values:
                if _is_remote_path(value):
                    continue
                if any(_path_target_exists(candidate) and exists_fn(candidate) for candidate in _path_candidates(value, path_base)):
                    usable_paths.append((key, value))
            if usable_paths:
                facts.append(
                    {
                        "group": group,
                        "material_index": item_index,
                        "material_id": _identifier(item.get("id")) or reference,
                        "material_name": _identifier(item.get("material_name")),
                        "type": _identifier(item.get("type")),
                        "segment_location": segment_location,
                        "segment_id": _identifier(segment.get("id")),
                        "target_timerange": copy.deepcopy(segment.get("target_timerange")),
                        "status": "resolved",
                        "resolved_paths": [value for _key, value in usable_paths],
                    }
                )
                continue
            kind = _visual_material_missing_kind(item)
            allowed_rehydrate = any(
                str(Path(value)).replace("/", "\\").casefold() in allowed_missing_paths
                for _key, value in path_values
            )
            fact = {
                "group": group,
                "material_index": item_index,
                "material_id": _identifier(item.get("id")) or reference,
                "material_name": _identifier(item.get("material_name")),
                "type": _identifier(item.get("type")),
                "missing_kind": kind,
                "segment_location": segment_location,
                "segment_id": _identifier(segment.get("id")),
                "target_timerange": copy.deepcopy(segment.get("target_timerange")),
                "path_values": [{"key": key, "value": value} for key, value in path_values],
                "status": "pending_rehydrate" if allowed_rehydrate else "pending",
            }
            facts.append(fact)
            if not allowed_rehydrate:
                report.add(
                    "visual_material_pending",
                    f"segment 引用的视觉素材缺少可用本地媒体 ({kind}): "
                    f"{fact['material_name'] or fact['material_id']}",
                    segment_location,
                )
    report.facts["referenced_visual_materials"] = facts
    report.facts["pending_visual_materials"] = [
        fact for fact in facts if fact.get("status") == "pending"
    ]


def _walk_material_references(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _reference_values(str(key), item)
            if str(key) not in _MATERIAL_REFERENCE_KEYS:
                yield from _walk_material_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_material_references(item)


def _track_reference_values(segment: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    material_id = _identifier(segment.get("material_id"))
    if material_id:
        values.append(material_id)
    values.extend(_reference_values("extra_material_refs", segment.get("extra_material_refs", [])))
    return values


def _collect_material_entries(
    inner_draft: Mapping[str, Any], tracks: Sequence[Mapping[str, Any]]
) -> list[_MaterialEntry]:
    """Collect material dependencies transitively from selected tracks."""

    index = _material_index(inner_draft)
    pending: deque[str] = deque()
    for track in tracks:
        for segment in _as_list(track.get("segments")):
            if not isinstance(segment, Mapping):
                raise TrialDependencyError("track segment 不是 object")
            values = _track_reference_values(segment)
            if not values and segment:
                raise TrialDependencyError("segment 缺少 material_id")
            pending.extend(values)

    selected: dict[tuple[str, int], _MaterialEntry] = {}
    while pending:
        reference = pending.popleft()
        entry = index.get(reference)
        if entry is None:
            raise TrialDependencyError(f"发现悬空 material 引用: {reference!r}")
        if entry.key in selected:
            continue
        selected[entry.key] = entry
        # Root identity aliases describe this material; they are not
        # transitive references to another local material.  Nested
        # ``material_id`` fields remain dependency-bearing.
        dependency_payload = {
            key: value for key, value in entry.item.items() if key not in _MATERIAL_ID_KEYS
        }
        for dependency in _walk_material_references(dependency_payload):
            if dependency not in index:
                raise TrialDependencyError(
                    f"素材 {entry.group}[{entry.index}] 的传递依赖不存在: {dependency!r}"
                )
            pending.append(dependency)
    return list(selected.values())


def collect_material_dependencies(
    inner_draft: Mapping[str, Any], tracks: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Public, JSON-friendly dependency collection helper."""

    entries = _collect_material_entries(inner_draft, tracks)
    return tuple(
        {
            "group": entry.group,
            "index": entry.index,
            "ids": [
                _identifier(entry.item.get(key))
                for key in _MATERIAL_ID_KEYS
                if _identifier(entry.item.get(key))
            ],
        }
        for entry in entries
    )


def _path_kind(key: str, group: str | None, track_type: str | None) -> str:
    if key in {"font_path", "font_url"}:
        return "font"
    if track_type == "audio" or group == "audios":
        return "audio"
    if key in {"res_path", "production_path", "intensifies_path"}:
        return "resource"
    return "media"


def _is_path_key(key: str) -> bool:
    return key in _PATH_KEYS or key.endswith("_path")


def _is_remote_path(value: str) -> bool:
    return value.lower().startswith(_REMOTE_PREFIXES)


def _is_mac_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return normalized.startswith(_MAC_PREFIXES) or "/Library/Containers/" in normalized


def _path_candidates(raw: str, path_base: Path | None) -> tuple[Path, ...]:
    try:
        path = Path(raw)
    except (TypeError, ValueError):
        return ()
    if path.is_absolute() or path_base is None:
        return (path,)
    return (path_base / path, path)


def _path_target_exists(value: Path) -> bool:
    """Accept a non-empty file or a Jianying resource-package directory."""

    if value.is_file():
        return value.stat().st_size > 0
    if value.is_dir():
        return any(path.is_file() and path.stat().st_size > 0 for path in value.rglob("*"))
    return False


def _mapped_path(raw: str, path_map: Mapping[str, str | Path]) -> str | None:
    variants = (raw, raw.replace("\\", "/"), raw.replace("/", "\\"))
    for variant in variants:
        if variant in path_map:
            replacement = path_map[variant]
            if not isinstance(replacement, (str, Path)):
                raise TrialDependencyError(f"path_map 的替换值必须是路径: {variant!r}")
            return str(replacement)
    return None


def official_audio_cache_target(
    material: Mapping[str, Any],
    raw_path: str,
    audio_cache_root: Path | None,
) -> tuple[Path, str] | None:
    """Return the current-machine cache target for one Jianying cloud SFX.

    Jianying sound materials keep a local UUID in ``id`` but use ``effect_id``
    as their stable cloud identity.  Their cached media filename is already a
    content-like 32-hex name.  Reusing both facts lets Jianying resolve the
    exact sound without guessing a semantically similar local file.
    """

    if audio_cache_root is None:
        return None
    if _identifier(material.get("type")).lower() != "sound":
        return None
    if str(material.get("source_platform", "")).strip() != "1":
        return None
    effect_id = _identifier(material.get("effect_id"))
    if not effect_id.isdigit():
        return None
    basename = Path(raw_path.replace("\\", "/")).name
    if not _OFFICIAL_AUDIO_CACHE_NAME.fullmatch(basename):
        return None
    return audio_cache_root / basename, effect_id


def _resolve_path_dependencies(
    value: Any,
    *,
    path_map: Mapping[str, str | Path],
    path_base: Path | None,
    audio_cache_root: Path | None = None,
    allow_audio_rehydrate: bool = False,
    allow_visual_rehydrate: bool = False,
    group: str | None = None,
    track_type: str | None = None,
    prefix: str = "$",
) -> list[dict[str, Any]]:
    """Resolve paths in a copied fragment and return explicit dependency facts."""

    records: list[dict[str, Any]] = []

    def visual_remote_ids(node: Any) -> list[str]:
        found: set[str] = set()
        if isinstance(node, Mapping):
            for key, child in node.items():
                if str(key) in {
                    "resource_id",
                    "third_resource_id",
                    "effect_id",
                    "font_resource_id",
                }:
                    identifier = _identifier(child)
                    if identifier and identifier != "0":
                        found.add(identifier)
                found.update(visual_remote_ids(child))
        elif isinstance(node, list):
            for child in node:
                found.update(visual_remote_ids(child))
        return sorted(found)

    visual_ids = visual_remote_ids(value) if group != "audios" else []

    def visit(node: Any, location: str) -> None:
        if isinstance(node, dict):
            for key, child in list(node.items()):
                child_location = f"{location}.{key}"
                if _is_path_key(str(key)) and isinstance(child, str) and child:
                    raw = child
                    if _is_remote_path(raw):
                        continue
                    replacement = _mapped_path(raw, path_map)
                    official_audio = None
                    if (
                        replacement is None
                        and group == "audios"
                        and str(key) == "path"
                        and isinstance(value, Mapping)
                    ):
                        official_audio = official_audio_cache_target(
                            value,
                            raw,
                            audio_cache_root,
                        )
                    if official_audio is not None:
                        target, effect_id = official_audio
                        effective = str(target)
                        exists = target.is_file()
                        pending_rehydrate = not exists
                        record = {
                            "kind": "audio",
                            "location": child_location,
                            "original_path": raw,
                            "resolved_path": effective,
                            "mapped": True,
                            "exists_on_current_machine": exists,
                            "resolution": "jianying_official_audio_cache",
                            "effect_id": effect_id,
                            "pending_rehydrate": pending_rehydrate,
                        }
                        records.append(record)
                        if pending_rehydrate and not allow_audio_rehydrate:
                            raise TrialDependencyError(
                                "剪映官方音效尚未缓存；需显式允许首次打开时下载，"
                                "或通过 path_map 指定本地音效文件: "
                                f"{raw}",
                                report={"path_dependencies": records},
                            )
                        node[key] = effective
                        continue
                    mapped = replacement is not None
                    effective = replacement if replacement is not None else raw
                    exists = any(_path_target_exists(candidate) for candidate in _path_candidates(effective, path_base))
                    kind = _path_kind(str(key), group, track_type)
                    record = {
                        "kind": kind,
                        "location": child_location,
                        "original_path": raw,
                        "resolved_path": effective,
                        "mapped": mapped,
                        "exists_on_current_machine": exists,
                    }
                    if mapped and group != "audios" and visual_ids:
                        record.update(
                            {
                                "resolution": "explicit_visual_path_map",
                                "remote_resource_ids": visual_ids,
                                "pending_rehydrate": False,
                            }
                        )
                    if (
                        not mapped
                        and not exists
                        and allow_visual_rehydrate
                        and group != "audios"
                        and visual_ids
                    ):
                        record.update(
                            {
                                "resolution": "jianying_visual_resource_pending_rehydrate",
                                "remote_resource_ids": visual_ids,
                                "pending_rehydrate": True,
                            }
                        )
                        records.append(record)
                        continue
                    records.append(record)
                    if mapped:
                        if not exists:
                            raise TrialDependencyError(
                                f"path_map 指向的文件不存在: {effective}",
                                report={"path_dependencies": records},
                            )
                        node[key] = effective
                    elif _is_mac_path(raw) or not exists:
                        raise TrialDependencyError(
                            f"外部路径未显式解析，拒绝写入: {raw}",
                            report={"path_dependencies": records},
                        )
                    continue
                visit(child, child_location)
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{location}[{index}]")

    visit(value, prefix)
    return records


def _collect_all_strings(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            found.update(_collect_all_strings(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_collect_all_strings(item))
    elif isinstance(value, str) and value:
        found.add(value)
    return found


def _default_id() -> str:
    return str(uuid.uuid4()).upper()


def _allocate_id(
    old: str,
    mapping: dict[str, str],
    used: set[str],
    id_factory: IdFactory,
) -> None:
    if old in mapping:
        return
    generated = str(id_factory()).strip()
    if not generated or generated in used:
        raise TrialDraftError(f"ID 工厂生成空值或碰撞: {generated!r}")
    mapping[old] = generated
    used.add(generated)


def _external_resource_identity(value: Mapping[str, Any]) -> str:
    identifier = _identifier(value.get("id"))
    if identifier and any(
        _identifier(value.get(key)) == identifier for key in _EXTERNAL_RESOURCE_ID_KEYS
    ):
        return identifier
    # Native text animations can carry a short numeric effect id alongside a
    # different long resource_id. Both identify the catalog asset; neither is
    # a draft-local UUID. Keyframes and material/segment roots remain local.
    if (
        identifier.isdigit() and int(identifier) > 0
        and value.get("material_type") == "sticker"
        and value.get("type") in {"in", "out", "loop", "group"}
        and _identifier(value.get("resource_id")).isdigit()
        and int(_identifier(value.get("resource_id"))) > 0
    ):
        return identifier
    return ""


def _register_nested_material_ids(
    value: Any,
    mapping: dict[str, str],
    used: set[str],
    id_factory: IdFactory,
    *,
    root: bool = True,
) -> None:
    if isinstance(value, dict):
        external_identity = "" if root else _external_resource_identity(value)
        for key, child in value.items():
            if key == "id" and not (root and not _identifier(child)):
                identifier = _identifier(child)
                if identifier and identifier != external_identity:
                    _allocate_id(identifier, mapping, used, id_factory)
            _register_nested_material_ids(child, mapping, used, id_factory, root=False)
    elif isinstance(value, list):
        for child in value:
            _register_nested_material_ids(child, mapping, used, id_factory, root=False)


def _remap_value(
    value: Any,
    *,
    context: str,
    material_map: Mapping[str, str],
    track_map: Mapping[str, str],
    segment_map: Mapping[str, str],
    nested_map: Mapping[str, str],
    root: bool = True,
) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        external_identity = "" if root else _external_resource_identity(value)
        for key, child in value.items():
            key_text = str(key)
            if key_text == "id":
                # Only the fragment roots have a typed identity.  IDs below a
                # root (for example common_keyframes/keyframe_list entries)
                # are local nested identities and must not be looked up in the
                # track/segment/material maps.  Keeping this distinction is
                # important when a nested ID happens to equal a track ID.
                selected_map = (
                    {
                        "track": track_map,
                        "segment": segment_map,
                        "material": material_map,
                    }.get(context, material_map)
                    if root
                    else nested_map
                )
                identifier = _identifier(child)
                result[key] = (
                    child
                    if identifier and identifier == external_identity
                    else selected_map.get(identifier, child) if identifier else child
                )
            elif key_text in _EXTERNAL_RESOURCE_ID_KEYS:
                result[key] = child
            elif key_text in _NESTED_REFERENCE_KEYS:
                result[key] = _remap_nested_reference(child, nested_map)
            elif key_text in _MATERIAL_REFERENCE_KEYS:
                result[key] = _remap_material_reference(child, material_map)
            elif key_text == "track_id":
                identifier = _identifier(child)
                result[key] = track_map.get(identifier, child) if identifier else child
            elif key_text == "segment_id":
                identifier = _identifier(child)
                result[key] = segment_map.get(identifier, child) if identifier else child
            elif key_text.endswith("_id") and isinstance(child, str):
                result[key] = material_map.get(
                    child,
                    track_map.get(
                        child,
                        segment_map.get(child, nested_map.get(child, child)),
                    ),
                )
            else:
                result[key] = _remap_value(
                    child,
                    context=context,
                    material_map=material_map,
                    track_map=track_map,
                    segment_map=segment_map,
                    nested_map=nested_map,
                    root=False,
                )
        return result
    if isinstance(value, list):
        return [
            _remap_value(
                child,
                context=context,
                material_map=material_map,
                track_map=track_map,
                segment_map=segment_map,
                nested_map=nested_map,
                root=root,
            )
            for child in value
        ]
    return value


def _remap_material_reference(value: Any, material_map: Mapping[str, str]) -> Any:
    if isinstance(value, list):
        return [material_map.get(_identifier(item), item) for item in value]
    identifier = _identifier(value)
    return material_map.get(identifier, value) if identifier else value


def _remap_nested_reference(value: Any, nested_map: Mapping[str, str]) -> Any:
    if isinstance(value, list):
        return [nested_map.get(_identifier(item), item) for item in value]
    identifier = _identifier(value)
    return nested_map.get(identifier, value) if identifier else value


def _remap_track(
    track: Mapping[str, Any],
    *,
    material_map: Mapping[str, str],
    track_map: Mapping[str, str],
    segment_map: Mapping[str, str],
    nested_map: Mapping[str, str],
    offset: int,
) -> dict[str, Any]:
    source = copy.deepcopy(dict(track))
    source_segments = source.pop("segments", [])
    result = _remap_value(
        source,
        context="track",
        material_map=material_map,
        track_map=track_map,
        segment_map=segment_map,
        nested_map=nested_map,
    )
    segments: list[dict[str, Any]] = []
    for segment in _as_list(source_segments):
        if not isinstance(segment, dict):
            raise TrialValidationError("导入 segment 不是 object", ValidationReport())
        segment = _remap_value(
            segment,
            context="segment",
            material_map=material_map,
            track_map=track_map,
            segment_map=segment_map,
            nested_map=nested_map,
        )
        timerange = segment.get("target_timerange")
        if not isinstance(timerange, dict):
            raise TrialValidationError("导入 segment 缺少 target_timerange", ValidationReport())
        start = timerange.get("start")
        duration = timerange.get("duration")
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            raise TrialValidationError("target_timerange.start 不是数字", ValidationReport())
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise TrialValidationError("target_timerange.duration 不是数字", ValidationReport())
        shifted_start = int(start) + offset
        if shifted_start < 0:
            raise TrialValidationError("offset 使 target_timerange.start 小于 0", ValidationReport())
        timerange["start"] = shifted_start
        timerange["duration"] = int(duration)
        segments.append(segment)
    result["segments"] = segments
    return result


def _id_occurrences(
    value: Any,
    prefix: str = "$",
    *,
    role: str = "draft",
) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    if isinstance(value, dict):
        external_identity = _external_resource_identity(value) if role == "nested" else ""
        for key, child in value.items():
            location = f"{prefix}.{key}"
            if key == "id":
                identifier = _identifier(child)
                if identifier and identifier != external_identity:
                    found.setdefault(identifier, []).append(location)
            if key == "materials" and isinstance(child, Mapping):
                child_role = "materials"
            elif role == "materials" and isinstance(child, list):
                child_role = "material_group"
            elif key == "tracks" and isinstance(child, list):
                child_role = "tracks"
            elif key == "segments" and isinstance(child, list):
                child_role = "segments"
            else:
                child_role = "nested"
            for identifier, locations in _id_occurrences(
                child,
                location,
                role=child_role,
            ).items():
                found.setdefault(identifier, []).extend(locations)
    elif isinstance(value, list):
        child_role = {
            "material_group": "material",
            "tracks": "track",
            "segments": "segment",
        }.get(role, "nested")
        for index, child in enumerate(value):
            for identifier, locations in _id_occurrences(
                child,
                f"{prefix}[{index}]",
                role=child_role,
            ).items():
                found.setdefault(identifier, []).extend(locations)
    return found


def _material_aliases(materials: Mapping[str, Any]) -> tuple[set[str], dict[str, list[tuple[tuple[str, int], str]]]]:
    aliases: set[str] = set()
    locations: dict[str, list[tuple[tuple[str, int], str]]] = {}
    for group, raw_items in materials.items():
        if not isinstance(raw_items, list):
            continue
        for index, item in enumerate(raw_items):
            if not isinstance(item, Mapping):
                continue
            for key in _MATERIAL_ID_KEYS:
                identifier = _identifier(item.get(key))
                if identifier:
                    aliases.add(identifier)
                    locations.setdefault(identifier, []).append(
                        ((str(group), index), f"$.materials.{group}[{index}].{key}")
                    )
    return aliases, locations


def _iter_path_values(value: Any, prefix: str = "$") -> Iterable[tuple[str, str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            location = f"{prefix}.{key}"
            if _is_path_key(str(key)) and isinstance(child, str) and child:
                yield location, str(key), child
            elif not _is_path_key(str(key)):
                yield from _iter_path_values(child, location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_path_values(child, f"{prefix}[{index}]")


def _validate_timerange(
    report: ValidationReport,
    segment: Mapping[str, Any],
    location: str,
    draft_duration: int | None,
) -> None:
    timerange = segment.get("target_timerange")
    if not isinstance(timerange, Mapping):
        report.add("timerange_missing", "segment 缺少 target_timerange", location)
        return
    start = timerange.get("start")
    duration = timerange.get("duration")
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        report.add("timerange_invalid", "target_timerange.start 不是数字", f"{location}.target_timerange.start")
        return
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        report.add("timerange_invalid", "target_timerange.duration 不是数字", f"{location}.target_timerange.duration")
        return
    if start < 0 or duration <= 0:
        report.add("timerange_invalid", "target_timerange 必须满足 start>=0 且 duration>0", location)
        return
    if draft_duration is not None and start + duration > draft_duration:
        report.add("timerange_outside_draft", "segment 超出 draft duration", location)


def validate_trial_draft(
    draft: Mapping[str, Any],
    *,
    imported_track_ids: Iterable[str] | None = None,
    imported_material_ids: Iterable[str] | None = None,
    path_base: str | Path | None = None,
    path_exists: Callable[[Path], bool] | None = None,
    allowed_missing_paths: Iterable[str | Path] | None = None,
) -> ValidationReport:
    """Validate IDs, references, timeranges, track types, and path residue.

    When scope IDs are supplied, unsupported-track and path checks are limited
    to the newly imported fragment.  This lets an existing customer draft keep
    its unrelated native tracks while still making the trial write auditable.
    Without scope IDs, the whole supplied dictionary is checked.
    """

    report = ValidationReport()
    if not isinstance(draft, Mapping):
        report.add("draft_invalid", "trial draft 必须是 object", "$")
        return report
    tracks = draft.get("tracks")
    materials = draft.get("materials")
    if not isinstance(tracks, list):
        report.add("tracks_invalid", "draft.tracks 必须是数组", "$.tracks")
        tracks = []
    if not isinstance(materials, Mapping):
        report.add("materials_invalid", "draft.materials 必须是 object", "$.materials")
        materials = {}

    occurrences = _id_occurrences(draft)
    duplicate_ids = {identifier: locations for identifier, locations in occurrences.items() if len(locations) > 1}
    for identifier, locations in sorted(duplicate_ids.items()):
        report.add("duplicate_id", f"ID 重复: {identifier}", ", ".join(locations))

    aliases, alias_locations = _material_aliases(materials)
    for identifier, locations in sorted(alias_locations.items()):
        owners = {owner for owner, _location in locations}
        if len(owners) > 1:
            location_values = [location for _owner, location in locations]
            if all(location.endswith(".material_id") for location in location_values):
                # Shared catalogue identity, not duplicate local ownership.
                continue
            report.add(
                "duplicate_material_id",
                f"material ID/别名重复: {identifier}",
                ", ".join(location_values),
            )

    track_scope = None if imported_track_ids is None else {str(item) for item in imported_track_ids}
    material_scope = None if imported_material_ids is None else {str(item) for item in imported_material_ids}
    draft_duration: int | None = None
    raw_duration = draft.get("duration")
    if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool):
        if raw_duration < 0:
            report.add("duration_invalid", "draft.duration 不能小于 0", "$.duration")
        else:
            draft_duration = int(raw_duration)

    referenced_materials: set[str] = set()
    # Keep the original segment location alongside references so the visual
    # media gate can report the exact offending template/time window.
    referenced_segments: list[tuple[str, str, Mapping[str, Any]]] = []
    imported_track_count = 0
    for track_index, track in enumerate(tracks):
        location = f"$.tracks[{track_index}]"
        if not isinstance(track, Mapping):
            report.add("track_invalid", "track 不是 object", location)
            continue
        track_id = _identifier(track.get("id"))
        in_scope = track_scope is None or track_id in track_scope
        if in_scope:
            imported_track_count += 1
            track_type = _identifier(track.get("type")).lower()
            if track_type not in ALLOWED_TRACK_TYPES:
                report.add("unsupported_track_type", f"不支持的轨道类型: {track_type!r}", f"{location}.type")
        segments = track.get("segments")
        if not isinstance(segments, list):
            report.add("segments_invalid", "track.segments 必须是数组", f"{location}.segments")
            continue
        for segment_index, segment in enumerate(segments):
            segment_location = f"{location}.segments[{segment_index}]"
            if not isinstance(segment, Mapping):
                report.add("segment_invalid", "segment 不是 object", segment_location)
                continue
            _validate_timerange(report, segment, segment_location, draft_duration)
            material_id = _identifier(segment.get("material_id"))
            if not material_id:
                report.add("material_reference_missing", "segment 缺少 material_id", segment_location)
            elif material_id not in aliases:
                report.add("material_reference_dangling", f"material_id 无对应素材: {material_id}", segment_location)
            else:
                referenced_materials.add(material_id)
                referenced_segments.append((segment_location, material_id, segment))
            extra = segment.get("extra_material_refs", [])
            if extra is not None and not isinstance(extra, list):
                report.add("extra_refs_invalid", "extra_material_refs 必须是数组", segment_location)
            for ref in _reference_values("extra_material_refs", extra):
                if ref not in aliases:
                    report.add("material_reference_dangling", f"extra_material_ref 无对应素材: {ref}", segment_location)
                else:
                    referenced_materials.add(ref)
                    referenced_segments.append((segment_location, ref, segment))

    # Validate known material-reference fields in material definitions too.
    for group, raw_items in materials.items():
        if not isinstance(raw_items, list):
            continue
        for item_index, item in enumerate(raw_items):
            if not isinstance(item, Mapping):
                continue
            location = f"$.materials.{group}[{item_index}]"
            if material_scope is not None:
                item_ids = {_identifier(item.get(key)) for key in _MATERIAL_ID_KEYS}
                item_ids.discard("")
                if not item_ids.intersection(material_scope):
                    continue
            for ref in _walk_material_references(item):
                if ref not in aliases:
                    report.add("material_reference_dangling", f"传递 material 引用无对应素材: {ref}", location)
                else:
                    referenced_materials.add(ref)

    exists_fn = path_exists or _path_target_exists
    base = Path(path_base) if path_base is not None else None
    allowed_missing = {
        str(Path(value)).replace("/", "\\").casefold()
        for value in (allowed_missing_paths or ())
    }
    path_facts: list[dict[str, Any]] = []
    for location, key, raw in _iter_path_values(draft):
        if _is_remote_path(raw):
            continue
        if material_scope is not None:
            # Path checks for scoped validation are performed on the selected
            # material objects below; unrelated base paths are not part of the
            # trial write.
            if not any(location.startswith(f"$.materials.{group}[") for group in materials):
                continue
            marker = location.split(".", 3)
            if len(marker) < 3:
                continue
            group_and_index = marker[2]
            try:
                group, index_text = group_and_index.split("[", 1)
                index = int(index_text.rstrip("]"))
                item = materials[group][index]
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            item_ids = {_identifier(item.get(item_key)) for item_key in _MATERIAL_ID_KEYS}
            item_ids.discard("")
            if not item_ids.intersection(material_scope):
                continue
        candidates = _path_candidates(raw, base)
        exists = any(exists_fn(candidate) for candidate in candidates)
        pending_rehydrate = (
            not exists
            and str(Path(raw)).replace("/", "\\").casefold() in allowed_missing
        )
        forbidden = (_is_mac_path(raw) or not exists) and not pending_rehydrate
        path_facts.append(
            {
                "location": location,
                "key": key,
                "path": raw,
                "exists": exists,
                "pending_rehydrate": pending_rehydrate,
            }
        )
        if forbidden:
            report.add("forbidden_path", f"存在未解析或不存在的路径: {raw}", location)

    _validate_referenced_visual_materials(
        report,
        materials=materials,
        referenced_segments=referenced_segments,
        material_scope=material_scope,
        exists_fn=exists_fn,
        path_base=base,
        allowed_missing_paths=allowed_missing,
    )

    report.facts.update(
        {
            "id_count": len(occurrences),
            "duplicate_ids": duplicate_ids,
            "material_alias_count": len(aliases),
            "referenced_material_count": len(referenced_materials),
            "imported_track_count": imported_track_count,
            "path_dependencies": path_facts,
            "referenced_segment_count": len(referenced_segments),
        }
    )
    return report


def normalize_native_zero_offsets(inner: dict[str, Any]) -> list[str]:
    """Expand omitted zero offsets in a copied, native 8.8 preset.

    Jianying 8.8 omits zero-valued protobuf fields when saving. A missing
    timerange start, animation start, or inline keyframe time is therefore
    zero in that format. Explicit null/invalid values and missing durations
    remain errors; other draft versions keep their existing contract.
    """
    if any(not isinstance(inner.get(key), Mapping) or inner[key].get("app_version") != "8.8.0"
           for key in ("platform", "last_modified_platform")):
        return []
    expanded: list[str] = []

    def zero(item: Any, key: str, location: str) -> None:
        if isinstance(item, dict) and key not in item:
            item[key] = 0
            expanded.append(f"{location}.{key}")

    for ti, track in enumerate(inner.get("tracks") or []):
        if not isinstance(track, dict):
            continue
        for si, segment in enumerate(track.get("segments") or []):
            if not isinstance(segment, dict):
                continue
            location = f"tracks[{ti}].segments[{si}]"
            for key in ("target_timerange", "source_timerange"):
                timerange = segment.get(key)
                if (isinstance(timerange, dict)
                        and type(timerange.get("duration")) is int and timerange["duration"] > 0):
                    zero(timerange, "start", f"{location}.{key}")
            for gi, group in enumerate(segment.get("common_keyframes") or []):
                if not isinstance(group, dict):
                    continue
                for ki, point in enumerate(group.get("keyframe_list") or []):
                    zero(point, "time_offset", f"{location}.common_keyframes[{gi}].keyframe_list[{ki}]")
    for mi, container in enumerate((inner.get("materials") or {}).get("material_animations") or []):
        if not isinstance(container, dict):
            continue
        for ai, animation in enumerate(container.get("animations") or []):
            if (isinstance(animation, dict)
                    and type(animation.get("duration")) is int and animation["duration"] > 0):
                zero(animation, "start", f"materials.material_animations[{mi}].animations[{ai}]")
    return expanded


def build_trial_draft(
    base_draft: Mapping[str, Any],
    registry: Mapping[str, Any],
    candidate_id: str,
    slot_values: Mapping[str, Any],
    *,
    outer_preset: Mapping[str, Any] | str | Path | None = None,
    preset_root: str | Path | None = None,
    draft_index: int = 0,
    offset: int = 0,
    path_map: Mapping[str, str | Path] | None = None,
    path_base: str | Path | None = None,
    audio_cache_root: str | Path | None = None,
    allow_audio_rehydrate: bool = False,
    allow_visual_rehydrate: bool = False,
    disable_audio: bool = False,
    unsupported_track_policy: str = "reject",
    id_factory: IdFactory = _default_id,
) -> TrialBuildResult:
    """Build one safe, in-memory preset trial.

    ``outer_preset`` may be a loaded outer JSON object or a path.  If omitted,
    the candidate's registry ``source_path`` is resolved below ``preset_root``.
    No output path is accepted by this function; the returned ``draft`` is the
    only write product.
    """

    if not isinstance(base_draft, Mapping):
        raise TrialDraftError("base_draft 必须是 object")
    if not isinstance(slot_values, Mapping):
        raise TrialDraftError("slot_values 必须是 object")
    if unsupported_track_policy not in {"reject", "skip"}:
        raise TrialDraftError("unsupported_track_policy 只能是 reject 或 skip")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TrialDraftError("offset 必须是整数微秒")
    if not isinstance(disable_audio, bool):
        raise TrialDraftError("disable_audio 必须明确是 bool")
    if not isinstance(allow_audio_rehydrate, bool):
        raise TrialDraftError("allow_audio_rehydrate 必须明确是 bool")
    if not isinstance(allow_visual_rehydrate, bool):
        raise TrialDraftError("allow_visual_rehydrate 必须明确是 bool")
    if allow_audio_rehydrate and audio_cache_root is None:
        raise TrialDraftError("允许音效自动下载时必须提供 audio_cache_root")
    audio_cache_root_value = (
        Path(audio_cache_root).expanduser()
        if audio_cache_root is not None
        else None
    )
    candidate = _find_candidate(registry, candidate_id)
    source_payload = _resolve_preset_source(
        candidate,
        outer_preset=outer_preset,
        preset_root=preset_root,
    )

    # This is the existing registry-controlled slot path.  It deep-copies the
    # source and rejects missing required slots before any fragment is built.
    filled_payload = apply_template_slots(source_payload, slot_values, candidate=candidate)
    inner = extract_inner_draft(filled_payload, draft_index)
    normalize_native_zero_offsets(inner)
    source_tracks = inner.get("tracks")
    if not isinstance(source_tracks, list):
        raise TrialDraftError("inner draft.tracks 必须是数组")

    selected_tracks: list[dict[str, Any]] = []
    skipped_tracks: list[dict[str, Any]] = []
    for index, raw_track in enumerate(source_tracks):
        if not isinstance(raw_track, dict):
            raise TrialDraftError(f"inner draft.tracks[{index}] 不是 object")
        track_type = _identifier(raw_track.get("type")).lower()
        track_id = _identifier(raw_track.get("id")) or f"track[{index}]"
        if track_type not in ALLOWED_TRACK_TYPES:
            if unsupported_track_policy == "skip":
                skipped_tracks.append({"track_id": track_id, "type": track_type, "reason": "unsupported_track_type"})
                continue
            raise UnsupportedTrackError(f"发现不支持的导入轨道类型: {track_type!r}")
        if track_type == "audio" and disable_audio:
            skipped_tracks.append({"track_id": track_id, "type": track_type, "reason": "audio_disabled"})
            continue
        selected_tracks.append(copy.deepcopy(raw_track))

    entries = _collect_material_entries(inner, selected_tracks)
    path_dependencies: list[dict[str, Any]] = []
    effective_path_map = dict(path_map or {})
    resolved_materials: list[tuple[_MaterialEntry, dict[str, Any]]] = []
    for entry in entries:
        material = copy.deepcopy(entry.item)
        path_dependencies.extend(
            _resolve_path_dependencies(
                material,
                path_map=effective_path_map,
                path_base=Path(path_base) if path_base is not None else None,
                audio_cache_root=audio_cache_root_value,
                allow_audio_rehydrate=allow_audio_rehydrate,
                allow_visual_rehydrate=allow_visual_rehydrate,
                group=entry.group,
                prefix=f"$.materials.{entry.group}[{entry.index}]",
            )
        )
        resolved_materials.append((entry, material))
    resolved_tracks: list[dict[str, Any]] = []
    for index, track in enumerate(selected_tracks):
        track_copy = copy.deepcopy(track)
        path_dependencies.extend(
            _resolve_path_dependencies(
                track_copy,
                path_map=effective_path_map,
                path_base=Path(path_base) if path_base is not None else None,
                audio_cache_root=audio_cache_root_value,
                allow_audio_rehydrate=allow_audio_rehydrate,
                allow_visual_rehydrate=allow_visual_rehydrate,
                track_type=_identifier(track.get("type")).lower(),
                prefix=f"$.tracks[{index}]",
            )
        )
        resolved_tracks.append(track_copy)

    used_ids = _collect_all_strings(base_draft)
    material_map: dict[str, str] = {}
    track_map: dict[str, str] = {}
    segment_map: dict[str, str] = {}
    nested_map: dict[str, str] = {}
    for _entry, material in resolved_materials:
        for key in _MATERIAL_ID_KEYS:
            identifier = _identifier(material.get(key))
            if identifier:
                _allocate_id(identifier, material_map, used_ids, id_factory)
        # Material root IDs are typed material identities.  Everything below
        # the root (including animation/keyframe objects) gets its own local
        # namespace so it cannot collide with a track or segment ID.
        nested_material = copy.deepcopy(material)
        nested_material.pop("id", None)
        _register_nested_material_ids(nested_material, nested_map, used_ids, id_factory)
    for track in resolved_tracks:
        track_id = _identifier(track.get("id"))
        if track_id:
            _allocate_id(track_id, track_map, used_ids, id_factory)
        nested_track = copy.deepcopy(track)
        nested_track.pop("id", None)
        source_segments = nested_track.pop("segments", [])
        _register_nested_material_ids(nested_track, nested_map, used_ids, id_factory)
        for segment in _as_list(source_segments):
            if not isinstance(segment, Mapping):
                raise TrialDraftError("导入 segment 不是 object")
            segment_id = _identifier(segment.get("id"))
            if segment_id:
                _allocate_id(segment_id, segment_map, used_ids, id_factory)
            nested_segment = copy.deepcopy(dict(segment))
            nested_segment.pop("id", None)
            _register_nested_material_ids(nested_segment, nested_map, used_ids, id_factory)

    remapped_materials: list[tuple[str, dict[str, Any]]] = []
    for entry, material in resolved_materials:
        remapped_materials.append(
            (
                entry.group,
                _remap_value(
                    material,
                    context="material",
                    material_map=material_map,
                    track_map=track_map,
                    segment_map=segment_map,
                    nested_map=nested_map,
                ),
            )
        )
    remapped_tracks = [
        _remap_track(
            track,
            material_map=material_map,
            track_map=track_map,
            segment_map=segment_map,
            nested_map=nested_map,
            offset=offset,
        )
        for track in resolved_tracks
    ]

    result = copy.deepcopy(dict(base_draft))
    result_materials = result.setdefault("materials", {})
    if not isinstance(result_materials, dict):
        raise TrialDraftError("base_draft.materials 必须是 object")
    for group, material in remapped_materials:
        existing = result_materials.setdefault(group, [])
        if not isinstance(existing, list):
            raise TrialDraftError(f"base materials.{group} 不是数组")
        existing.append(material)
    result_tracks = result.setdefault("tracks", [])
    if not isinstance(result_tracks, list):
        raise TrialDraftError("base_draft.tracks 必须是数组")
    result_tracks.extend(remapped_tracks)

    imported_track_ids = tuple(_identifier(track.get("id")) for track in remapped_tracks if _identifier(track.get("id")))
    imported_material_ids = tuple(sorted(set(material_map.values())))
    imported_end = 0
    for track in remapped_tracks:
        for segment in _as_list(track.get("segments")):
            timerange = segment.get("target_timerange") if isinstance(segment, Mapping) else None
            if isinstance(timerange, Mapping):
                start = timerange.get("start")
                duration = timerange.get("duration")
                if isinstance(start, (int, float)) and isinstance(duration, (int, float)):
                    imported_end = max(imported_end, int(start) + int(duration))
    existing_duration = result.get("duration")
    if isinstance(existing_duration, (int, float)) and not isinstance(existing_duration, bool):
        result["duration"] = max(int(existing_duration), imported_end)
    elif imported_end:
        result["duration"] = imported_end

    pending_audio_paths = {
        item["resolved_path"]
        for item in path_dependencies
        if item.get("kind") == "audio" and item.get("pending_rehydrate")
    }
    pending_visual_paths = {
        item["resolved_path"]
        for item in path_dependencies
        if item.get("resolution") == "jianying_visual_resource_pending_rehydrate"
    }
    validation = validate_trial_draft(
        result,
        imported_track_ids=imported_track_ids,
        imported_material_ids=imported_material_ids,
        path_base=path_base,
        allowed_missing_paths=pending_audio_paths | pending_visual_paths,
    )
    if not validation.ok:
        raise TrialValidationError("合并后的 trial draft 未通过结构校验", validation)

    report = {
        "candidate_id": str(candidate_id),
        "offset_us": offset,
        "disable_audio": disable_audio,
        "unsupported_track_policy": unsupported_track_policy,
        "imported_track_count": len(remapped_tracks),
        "imported_material_count": len(remapped_materials),
        "skipped_tracks": skipped_tracks,
        "path_dependencies": path_dependencies,
        "audio_rehydrations": [
            item
            for item in path_dependencies
            if item.get("resolution") == "jianying_official_audio_cache"
        ],
        "visual_rehydrations": [
            item
            for item in path_dependencies
            if item.get("resolution")
            in {"jianying_visual_resource_pending_rehydrate", "explicit_visual_path_map"}
        ],
        "validation": validation.to_dict(),
    }
    return TrialBuildResult(
        draft=result,
        report=report,
        id_map={
            "materials": material_map,
            "tracks": track_map,
            "segments": segment_map,
            "nested": nested_map,
        },
        validation=validation,
        imported_track_ids=imported_track_ids,
        imported_material_ids=imported_material_ids,
    )


__all__ = [
    "ALLOWED_TRACK_TYPES",
    "PresetExtractionError",
    "TrialBuildResult",
    "TrialDependencyError",
    "TrialDraftError",
    "TrialValidationError",
    "UnsupportedTrackError",
    "build_trial_draft",
    "collect_material_dependencies",
    "extract_inner_draft",
    "official_audio_cache_target",
    "extract_inner_drafts",
    "validate_trial_draft",
]
