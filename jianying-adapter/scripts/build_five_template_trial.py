"""Build a workspace-only blind trial with one to five Jianying preset templates.

The script is deliberately an orchestration layer around
``jianying_adapter.preset_trial.build_trial_draft``.  It never starts
Jianying, never registers a draft, and never writes AppData.  Its only output
is a JSON envelope containing the merged draft and an auditable manifest.

The current blind sample is T14: a previously unprocessed Mandarin talking
head.  The slot copy below is taken from the supplied T14 bilingual SRT, not
from a generic test sentence.  The old T14 frame names are rejected so an
accidental old-material substitution cannot enter this trial.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "video_trials" / "batch_talking_head_pilot" / "sources"
SRC_DIR = PROJECT_ROOT / "jianying-adapter" / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from jianying_adapter.preset_trial import (  # noqa: E402
    TrialBuildResult,
    TrialDependencyError,
    TrialDraftError,
    build_trial_draft,
    extract_inner_drafts,
    normalize_native_zero_offsets,
    official_audio_cache_target,
    validate_trial_draft,
    _path_target_exists,
    _external_resource_identity,
)


FIVE_TEMPLATE_IDS: tuple[str, ...] = (
    "JIANYING-25-08",
    "JIANYING-25-01",
    "JIANYING-25-16",
    "JIANYING-25-23",
    "JIANYING-25-06",
)
GAP_US = 1_000_000
DEFAULT_TRIAL_START_US = 3_500_000
# A screening card is a visual sample, not a playback of the preset's full
# source timeline.  A few Jianying compound clips advertise tens of seconds;
# allowing that duration to drive placement makes one bad source push all later
# samples out of their intended windows.
MAX_TEMPLATE_DURATION_US = 3_000_000
ALLOWED_MEDIA_KINDS = frozenset({"video", "image"})
MATERIAL_ID_KEYS = ("id", "material_id", "local_material_id", "origin_material_id")
MATERIAL_REFERENCE_KEYS = frozenset(
    {
        "material_id",
        "material_ids",
        "material_refs",
        "text_material_id",
        "text_material_ids",
        "extra_material_refs",
    }
)
EXTERNAL_RESOURCE_ID_KEYS = frozenset({"resource_id", "third_resource_id"})
PATH_KEYS = frozenset(
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
PRIMARY_MEDIA_PATH_KEYS = frozenset({"path", "media_path", "file_path", "filepath", "source_path"})
REMOTE_PREFIXES = ("http://", "https://", "data:", "asset://", "builtin://")

# These names were explicitly identified as frames from the old/incorrect
# material.  They are never accepted as --media for this blind test.
FORBIDDEN_OLD_T14_MEDIA = frozenset(
    {
        "t14_frame_08.jpg",
        "t14_frame_18.jpg",
        "t14_frame_28.jpg",
        "t14_frame_38.jpg",
    }
)

# Actual words/phrases from T14_Shuyang_Mandarin_blind_source.zh-Hans.srt.
# They are assigned by slot position only after reading the candidate's
# actual registry slot definitions.  No slot IDs are assumed here.
T14_SLOT_COPY: dict[str, tuple[str, ...]] = {
    "JIANYING-25-08": (
        "我叫 Li Shuang，我從北京來",
        "20（20s)年以前來到日本",
    ),
    "JIANYING-25-01": (
        "我從北京來",
        "來到日本",
        "兩個兒子",
        "國際交流",
    ),
    "JIANYING-25-16": ("國際交流",),
    "JIANYING-25-23": (
        "他們在上海讀小學",
        "現在呢，也在日本讀中學",
    ),
    "JIANYING-25-06": (
        "北京",
        "日本",
        "先生",
        "兩個兒子",
        "小學",
        "英文",
        "中學",
        "日文",
        "國際交流",
    ),
    "JIANYING-25-13": (
        "来自北京的李爽",
        "在日本生活二十年",
        "一段普通话自我介绍",
    ),
    "JIANYING-25-14": ("中文和日文",),
    "JIANYING-25-15": ("二十年前", "来到日本"),
    "JIANYING-25-17": ("去日本，也去中国",),
    "JIANYING-25-19": ("二十年前来到日本",),
    "JIANYING-25-25": ("希望你们加油",),
}
T14_SEMANTIC_SLOT_COPY: dict[str, tuple[str, ...]] = {
    **T14_SLOT_COPY,
    "JIANYING-25-15": ("二十年前", "来到日本"),
    "JIANYING-25-23": ("上海读小学", "也会说英文"),
    "JIANYING-25-16": ("中文和日文",),
}
T14_FALLBACK_COPY: tuple[str, ...] = (
    "我也很希望你們有機會來日本",
    "也來中國旅遊學中文和日文",
    "這個真的是很開心的事情",
    "就這樣。再見",
)


class FiveTemplateTrialError(ValueError):
    """The template trial cannot be assembled safely."""


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


def _read_json(value: Mapping[str, Any] | str | Path, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    path = Path(value)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise FiveTemplateTrialError(f"无法读取 {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FiveTemplateTrialError(f"{label} 不是有效 JSON: {path}") from exc
    if not isinstance(loaded, dict):
        raise FiveTemplateTrialError(f"{label} 根对象必须是 JSON object: {path}")
    return loaded


def _candidate_map(
    registry: Mapping[str, Any],
    template_ids: Sequence[str] = FIVE_TEMPLATE_IDS,
) -> dict[str, Mapping[str, Any]]:
    raw = registry.get("candidates")
    if not isinstance(raw, list):
        raise FiveTemplateTrialError("registry 缺少 candidates 数组")
    result: dict[str, Mapping[str, Any]] = {}
    for candidate in raw:
        if not isinstance(candidate, Mapping):
            continue
        template_id = _identifier(candidate.get("template_id"))
        if template_id:
            result[template_id] = candidate
    missing = [template_id for template_id in template_ids if template_id not in result]
    if missing:
        raise FiveTemplateTrialError("registry 缺少所选模板: " + ", ".join(missing))
    return result


def _candidate_slots(candidate: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    slots = candidate.get("slots")
    if not isinstance(slots, Mapping):
        raise FiveTemplateTrialError(f"{candidate.get('template_id')} 缺少 slots")
    raw = slots.get(kind, [])
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _locators(slot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = slot.get("locators")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    locator = slot.get("locator")
    return [locator] if isinstance(locator, Mapping) else []


def _segment_refs(slot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    refs: list[Mapping[str, Any]] = []
    for locator in _locators(slot):
        raw = locator.get("segment_refs")
        if isinstance(raw, list):
            refs.extend(item for item in raw if isinstance(item, Mapping))
    return refs


def _required_media_slots(candidate: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    required: list[tuple[str, Mapping[str, Any]]] = []
    for kind in sorted(ALLOWED_MEDIA_KINDS):
        for slot in _candidate_slots(candidate, kind):
            if slot.get("required") and _segment_refs(slot):
                required.append((kind, slot))
    return required


def _needs_explicit_media(candidate: Mapping[str, Any]) -> bool:
    return bool(_required_media_slots(candidate))


def _inner_refs(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    materials = payload.get("materials")
    if isinstance(materials, Mapping):
        drafts = [
            item["draft"]
            for item in _as_list(materials.get("drafts"))
            if isinstance(item, Mapping) and isinstance(item.get("draft"), Mapping)
        ]
        if drafts:
            return [item for item in drafts if isinstance(item, dict)]
    return [payload] if isinstance(payload, Mapping) else []


def _candidate_source(candidate: Mapping[str, Any], preset_root: Path) -> tuple[Path, dict[str, Any]]:
    source = _identifier(candidate.get("source_path"))
    if not source:
        raise FiveTemplateTrialError(f"{candidate.get('template_id')} 缺少 source_path")
    source_path = Path(source)
    if not source_path.is_absolute():
        source_path = preset_root / source_path
    if not source_path.is_file():
        raise FiveTemplateTrialError(f"找不到模板源文件: {source_path}")
    return source_path, _read_json(source_path, "模板源")


def _material_aliases(inner: Mapping[str, Any]) -> set[str]:
    materials = inner.get("materials")
    aliases: set[str] = set()
    if not isinstance(materials, Mapping):
        return aliases
    for group, raw_items in materials.items():
        if group == "drafts" or not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            for key in MATERIAL_ID_KEYS:
                identifier = _identifier(item.get(key))
                if identifier:
                    aliases.add(identifier)
    return aliases


def _is_path_key(key: str) -> bool:
    return key in PATH_KEYS or key.endswith("_path")


def _is_remote_path(value: str) -> bool:
    return value.lower().startswith(REMOTE_PREFIXES)


def _path_exists(value: str, path_base: Path) -> bool:
    try:
        path = Path(value)
    except (TypeError, ValueError):
        return False
    candidates = [path]
    if not path.is_absolute():
        candidates.append(path_base / path)
    # Native animation paths point to resource-package directories, not files.
    # Use the importer's predicate so sanitization cannot erase valid packages.
    return any(_path_target_exists(item) for item in candidates)


def _mapped_path(raw: str, path_map: Mapping[str, str | Path]) -> str | None:
    variants = (raw, raw.replace("\\", "/"), raw.replace("/", "\\"))
    for variant in variants:
        if variant in path_map:
            return str(path_map[variant])
    return None


def _direct_media_paths(item: Mapping[str, Any]) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    for key, value in item.items():
        if key in PRIMARY_MEDIA_PATH_KEYS and isinstance(value, str) and value:
            paths.append((str(key), value))
    return paths


def _material_at(
    inner_drafts: Sequence[Mapping[str, Any]], locator: Mapping[str, Any]
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    try:
        draft = inner_drafts[int(locator["inner_draft_index"])]
        materials = draft.get("materials")
        group = str(locator["materials_group"])
        index = int(locator["materials_index"])
        items = materials[group] if isinstance(materials, Mapping) else None
        item = items[index] if isinstance(items, list) else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return (draft, item) if isinstance(draft, Mapping) and isinstance(item, Mapping) else None


def _build_media_path_map(
    candidate: Mapping[str, Any],
    source_payload: Mapping[str, Any],
    media_paths: Sequence[Path],
) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Map explicit media files to visual slots without assuming a preset asset."""

    if _needs_explicit_media(candidate) and not media_paths:
        raise FiveTemplateTrialError(
            f"{candidate.get('template_id')} 含 required 素材槽，必须显式提供 --media；不使用参考成片"
        )
    inner_drafts = _inner_refs(source_payload)
    path_map: dict[str, str] = {}
    slot_values: dict[str, str] = {}
    mappings: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    next_media_index = 0

    for kind in ("video", "image"):
        for slot in _candidate_slots(candidate, kind):
            if not slot.get("required") or not _segment_refs(slot):
                continue
            slot_id = _identifier(slot.get("slot_id"))
            for locator in _locators(slot):
                resolved = _material_at(inner_drafts, locator)
                if resolved is None:
                    raise FiveTemplateTrialError(f"{candidate.get('template_id')} {slot_id} 的 locator 无效")
                _draft, item = resolved
                raw_paths = _direct_media_paths(item)
                if not raw_paths:
                    skipped.append(
                        {
                            "kind": "media_slot",
                            "slot_id": slot_id,
                            "material_id": _identifier(locator.get("material_id")),
                            "reason": "preset_material_has_no_direct_media_path",
                        }
                    )
                    mappings.append(
                        {
                            "slot_id": slot_id,
                            "kind": kind,
                            "material_id": _identifier(locator.get("material_id")),
                            "status": "no_direct_path",
                            "media_path": None,
                        }
                    )
                    continue

                chosen: str | None = None
                source_paths: list[str] = []
                for _key, raw_path in raw_paths:
                    source_paths.append(raw_path)
                    existing = _mapped_path(raw_path, path_map)
                    if existing is None:
                        if not media_paths:
                            raise FiveTemplateTrialError(
                                f"{candidate.get('template_id')} 的素材槽没有可用 --media"
                            )
                        chosen = str(media_paths[next_media_index % len(media_paths)])
                        next_media_index += 1
                        path_map[raw_path] = chosen
                    else:
                        chosen = existing
                assert chosen is not None
                slot_values[slot_id] = chosen
                mappings.append(
                    {
                        "slot_id": slot_id,
                        "kind": kind,
                        "material_id": _identifier(locator.get("material_id")),
                        "source_paths": source_paths,
                        "media_path": chosen,
                        "status": "mapped",
                    }
                )
    return path_map, slot_values, mappings, skipped


def _path_text(path: tuple[Any, ...]) -> str:
    result = "$"
    for part in path:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def _filter_extra_refs(
    node: Any,
    *,
    known_material_ids: set[str],
    location: tuple[Any, ...],
    skipped: list[dict[str, Any]],
) -> None:
    if isinstance(node, dict):
        for key, child in list(node.items()):
            child_location = location + (key,)
            if key == "extra_material_refs" and isinstance(child, list):
                retained: list[Any] = []
                for index, reference in enumerate(child):
                    identifier = _identifier(reference)
                    if identifier and identifier not in known_material_ids:
                        skipped.append(
                            {
                                "kind": "extra_material_ref",
                                "reference": identifier,
                                "location": _path_text(child_location + (index,)),
                                "reason": "dangling_optional_reference_removed_for_trial",
                            }
                        )
                        continue
                    retained.append(reference)
                node[key] = retained
                continue
            _filter_extra_refs(
                child,
                known_material_ids=known_material_ids,
                location=child_location,
                skipped=skipped,
            )
    elif isinstance(node, list):
        for index, child in enumerate(node):
            _filter_extra_refs(
                child,
                known_material_ids=known_material_ids,
                location=location + (index,),
                skipped=skipped,
            )


def _deduplicate_material_ids(
    inner: dict[str, Any],
    skipped: list[dict[str, Any]],
) -> None:
    """Remove byte-identical duplicate root material records in the copy.

    Jianying exports can contain the same effect record twice while a segment
    references the same material ID twice.  The low-level builder correctly
    rejects two different material objects owning one ID, so retaining one
    identical record is the only semantics-preserving repair.  Non-identical
    collisions are refused rather than guessed.
    """

    materials = inner.get("materials")
    if not isinstance(materials, Mapping):
        return
    seen: dict[str, tuple[str, int, Mapping[str, Any]]] = {}
    removals: dict[str, set[int]] = {}
    recorded: set[tuple[str, int, str]] = set()
    for group, raw_items in materials.items():
        if group == "drafts" or not isinstance(raw_items, list):
            continue
        for item_index, item in enumerate(raw_items):
            if not isinstance(item, Mapping):
                continue
            owner = (str(group), item_index)
            collision: tuple[str, int, Mapping[str, Any]] | None = None
            # Root material ownership is defined by ``id``.  ``material_id``
            # is often a shared remote catalogue identity (two clips may use
            # the same online asset) and must not be treated as an internal
            # JSON ownership collision.
            for key in ("id",):
                identifier = _identifier(item.get(key))
                if not identifier:
                    continue
                previous = seen.get(identifier)
                if previous is not None and previous[:2] != owner:
                    collision = previous
                    break
                seen[identifier] = (owner[0], owner[1], item)
            if collision is None:
                continue
            previous_group, previous_index, previous_item = collision
            if dict(previous_item) != dict(item):
                raise FiveTemplateTrialError(
                    "模板源存在不可安全合并的重复 material ID: "
                    f"{_identifier(item.get('id')) or _identifier(item.get('material_id'))}; "
                    f"owners=materials.{previous_group}[{previous_index}],materials.{group}[{item_index}]"
                )
            removals.setdefault(str(group), set()).add(item_index)
            marker = (str(group), item_index, _identifier(item.get("id")))
            if marker not in recorded:
                skipped.append(
                    {
                        "kind": "duplicate_material",
                        "group": str(group),
                        "index": item_index,
                        "kept_at": f"materials.{previous_group}[{previous_index}]",
                        "material_id": marker[2],
                        "reason": "identical_duplicate_material_removed_for_trial",
                    }
                )
                recorded.add(marker)
    for group, indexes in removals.items():
        raw_items = materials.get(group)
        if isinstance(raw_items, list):
            materials[group] = [item for index, item in enumerate(raw_items) if index not in indexes]


def _sanitize_paths(
    node: Any,
    *,
    group: str,
    location: tuple[Any, ...],
    path_map: Mapping[str, str | Path],
    path_base: Path,
    audio_cache_root: Path | None,
    cleared: list[dict[str, Any]],
    preserved: list[dict[str, Any]],
    preserve_visual_resource_refs: bool,
    preserve_context: bool = False,
) -> None:
    def has_remote_visual_identity(value: Any) -> bool:
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                if str(child_key) in {
                    "resource_id",
                    "third_resource_id",
                    "effect_id",
                    "font_resource_id",
                } and _identifier(child_value) not in {"", "0"}:
                    return True
                if has_remote_visual_identity(child_value):
                    return True
        elif isinstance(value, list):
            return any(has_remote_visual_identity(child) for child in value)
        return False

    preserve_current_visual_paths = (
        preserve_visual_resource_refs
        and group != "audios"
        and (preserve_context or has_remote_visual_identity(node))
    )
    if isinstance(node, dict):
        for key, child in list(node.items()):
            child_location = location + (key,)
            if _is_path_key(str(key)) and isinstance(child, str) and child:
                if _is_remote_path(child) or _mapped_path(child, path_map) is not None:
                    continue
                if group == "audios" and str(key) == "path":
                    official_audio = official_audio_cache_target(
                        node,
                        child,
                        audio_cache_root,
                    )
                    if official_audio is not None:
                        node[key] = str(official_audio[0])
                        continue
                    # Keep unrecognised audio dependencies intact.  The low-level
                    # importer must reject a missing file explicitly instead of
                    # silently clearing the path and producing a mute "success".
                    if not _path_exists(child, path_base):
                        continue
                if _path_exists(child, path_base):
                    continue
                if preserve_current_visual_paths:
                    preserved.append(
                        {
                            "kind": "external_path",
                            "group": group,
                            "original_path": child,
                            "location": _path_text(child_location),
                            "reason": "visual_resource_reference_preserved_for_probe",
                        }
                    )
                    continue
                node[key] = ""
                cleared.append(
                    {
                        "kind": "external_path",
                        "group": group,
                        "original_path": child,
                        "location": _path_text(child_location),
                        "reason": "missing_preset_dependency_cleared_in_copy",
                    }
                )
                continue
            if key == "content" and group == "texts" and isinstance(child, str):
                try:
                    embedded = json.loads(child)
                except (TypeError, json.JSONDecodeError):
                    embedded = None
                if isinstance(embedded, (dict, list)):
                    before = json.dumps(embedded, ensure_ascii=False, separators=(",", ":"))
                    _sanitize_paths(
                        embedded,
                        group=group,
                        location=child_location + ("<json>",),
                        path_map=path_map,
                        path_base=path_base,
                        audio_cache_root=audio_cache_root,
                        cleared=cleared,
                        preserved=preserved,
                        preserve_visual_resource_refs=preserve_visual_resource_refs,
                        preserve_context=preserve_current_visual_paths,
                    )
                    after = json.dumps(embedded, ensure_ascii=False, separators=(",", ":"))
                    if after != before:
                        node[key] = after
                continue
            _sanitize_paths(
                child,
                group=group,
                location=child_location,
                path_map=path_map,
                path_base=path_base,
                audio_cache_root=audio_cache_root,
                cleared=cleared,
                preserved=preserved,
                preserve_visual_resource_refs=preserve_visual_resource_refs,
                preserve_context=preserve_current_visual_paths,
            )
    elif isinstance(node, list):
        for index, child in enumerate(node):
            _sanitize_paths(
                child,
                group=group,
                location=location + (index,),
                path_map=path_map,
                path_base=path_base,
                audio_cache_root=audio_cache_root,
                cleared=cleared,
                preserved=preserved,
                preserve_visual_resource_refs=preserve_visual_resource_refs,
                preserve_context=preserve_current_visual_paths,
            )


def _id_locations(node: Any, path: tuple[Any, ...] = ()) -> Iterable[tuple[dict[str, Any], tuple[Any, ...], str]]:
    if isinstance(node, dict):
        for key, child in node.items():
            if key == "id":
                identifier = _identifier(child)
                if identifier:
                    yield node, path, identifier
            yield from _id_locations(child, path + (key,))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _id_locations(child, path + (index,))


def _is_root_id_path(path: tuple[Any, ...]) -> bool:
    if not path:
        return True
    if len(path) == 3 and path[0] == "materials" and isinstance(path[2], int):
        return True
    if len(path) == 2 and path[0] == "tracks" and isinstance(path[1], int):
        return True
    if (
        len(path) == 4
        and path[0] == "tracks"
        and isinstance(path[1], int)
        and path[2] == "segments"
        and isinstance(path[3], int)
    ):
        return True
    return False


def _referenced_identifiers(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, child in node.items():
            # ``resource_id``/``third_resource_id`` identify an external
            # Jianying resource.  They are not references to another JSON
            # object's ``id``; repeated nested animation IDs matching either
            # field are the same external identity and remain unchanged.
            if key in MATERIAL_REFERENCE_KEYS or key in {"track_id", "segment_id"}:
                values = child if isinstance(child, list) else [child]
                found.update(identifier for value in values if (identifier := _identifier(value)))
            found.update(_referenced_identifiers(child))
    elif isinstance(node, list):
        for child in node:
            found.update(_referenced_identifiers(child))
    return found


def _repair_nested_duplicate_ids(inner: dict[str, Any], repaired: list[dict[str, Any]]) -> None:
    occurrences: dict[str, list[tuple[dict[str, Any], tuple[Any, ...]]]] = {}
    for node, path, identifier in _id_locations(inner):
        occurrences.setdefault(identifier, []).append((node, path))
    referenced = _referenced_identifiers(inner)
    used = set(occurrences)
    for identifier, items in occurrences.items():
        if len(items) <= 1:
            continue
        for ordinal, (node, path) in enumerate(items[1:], 1):
            if (
                _is_root_id_path(path)
                or identifier in referenced
                or identifier == _external_resource_identity(node)
            ):
                continue
            replacement = f"{identifier}__trial_{ordinal:02d}"
            suffix = ordinal
            while replacement in used:
                suffix += 1
                replacement = f"{identifier}__trial_{suffix:02d}"
            node["id"] = replacement
            used.add(replacement)
            repaired.append(
                {
                    "original_id": identifier,
                    "replacement_id": replacement,
                    "location": _path_text(path + ("id",)),
                    "reason": "duplicate_nested_id_namespaced_in_trial_copy",
                }
            )


def _sanitize_source_payload(
    source_payload: Mapping[str, Any],
    *,
    path_map: Mapping[str, str | Path],
    path_base: Path,
    audio_cache_root: Path | None = None,
    preserve_visual_resource_refs: bool = False,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Prepare a copied preset while keeping slot locators and visual styles."""

    result = copy.deepcopy(dict(source_payload))
    skipped: list[dict[str, Any]] = []
    cleared_paths: list[dict[str, Any]] = []
    preserved_paths: list[dict[str, Any]] = []
    repaired_ids: list[dict[str, Any]] = []
    deduplicated_materials: list[dict[str, Any]] = []
    native_zero_offsets: list[dict[str, Any]] = []
    for inner_index, inner in enumerate(_inner_refs(result)):
        if not isinstance(inner, dict):
            continue
        native_zero_offsets.extend(
            {"inner_draft_index": inner_index, "location": location}
            for location in normalize_native_zero_offsets(inner)
        )
        _deduplicate_material_ids(inner, deduplicated_materials)
        known_ids = _material_aliases(inner)
        _filter_extra_refs(
            inner,
            known_material_ids=known_ids,
            location=("materials", "drafts", inner_index, "draft"),
            skipped=skipped,
        )
        materials = inner.get("materials")
        if isinstance(materials, Mapping):
            for group, raw_items in materials.items():
                if group == "drafts" or not isinstance(raw_items, list):
                    continue
                for item_index, item in enumerate(raw_items):
                    if isinstance(item, dict):
                        _sanitize_paths(
                            item,
                            group=str(group),
                            location=("materials", str(group), item_index),
                            path_map=path_map,
                            path_base=path_base,
                            audio_cache_root=audio_cache_root,
                            cleared=cleared_paths,
                            preserved=preserved_paths,
                            preserve_visual_resource_refs=preserve_visual_resource_refs,
                        )
        _repair_nested_duplicate_ids(inner, repaired_ids)
    return result, {
        "skipped_items": skipped,
        "cleared_paths": cleared_paths,
        "preserved_paths": preserved_paths,
        "repaired_ids": repaired_ids,
        "deduplicated_materials": deduplicated_materials,
        "native_zero_offsets": native_zero_offsets,
    }


def _generate_slot_values(
    candidate: Mapping[str, Any],
    copy_table: Mapping[str, tuple[str, ...]] = T14_SLOT_COPY,
    *, strict: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    template_id = _identifier(candidate.get("template_id"))
    actual_copy = copy_table.get(template_id, ())
    text_values: dict[str, str] = {}
    skipped: list[dict[str, Any]] = []
    value_index = 0
    for slot in _candidate_slots(candidate, "text"):
        slot_id = _identifier(slot.get("slot_id"))
        if slot.get("decorative_locked") or not slot.get("required"):
            skipped.append(
                {
                    "kind": "text_slot",
                    "slot_id": slot_id,
                    "reason": "decorative_or_optional_slot_preserved",
                    "default_text": slot.get("default_text"),
                }
            )
            continue
        if slot.get("requires_manual_slot_mapping") or slot.get("requires_manual_style_mapping"):
            raise FiveTemplateTrialError(f"{template_id} 的 {slot_id} 不是可自动填充槽")
        if value_index < len(actual_copy):
            value = actual_copy[value_index]
        else:
            if strict:
                raise FiveTemplateTrialError(f"{template_id} 的 {slot_id} 缺少显式文案；禁止补入示例文案")
            fallback_index = (value_index - len(actual_copy)) % len(T14_FALLBACK_COPY)
            value = T14_FALLBACK_COPY[fallback_index]
        text_values[slot_id] = value
        value_index += 1
    return {"text_slots": text_values, "media_slots": {}}, skipped


def generate_slot_values(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Public helper used by tests and callers that need the T14 slot copy."""

    values, _skipped = _generate_slot_values(candidate)
    return values


def _candidate_inner_indexes(candidate: Mapping[str, Any]) -> tuple[int, ...]:
    indexes: set[int] = set()
    for kind in ("text", "video", "image"):
        for slot in _candidate_slots(candidate, kind):
            if not slot.get("required") and not _segment_refs(slot):
                continue
            for locator in _locators(slot):
                try:
                    indexes.add(int(locator["inner_draft_index"]))
                except (KeyError, TypeError, ValueError):
                    raise FiveTemplateTrialError(
                        f"{candidate.get('template_id')} 的 slot locator 缺少 inner_draft_index"
                    ) from None
    return tuple(sorted(indexes or {0}))


def _local_span(inner: Mapping[str, Any], *, disable_audio: bool) -> int:
    duration = inner.get("duration")
    span = int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else 0
    tracks = inner.get("tracks")
    if not isinstance(tracks, list):
        return max(span, 1)
    for track in tracks:
        if not isinstance(track, Mapping):
            continue
        track_type = _identifier(track.get("type")).lower()
        if disable_audio and track_type == "audio":
            continue
        if track_type not in {"text", "video", "audio"}:
            continue
        for segment in _as_list(track.get("segments")):
            if not isinstance(segment, Mapping):
                continue
            timerange = segment.get("target_timerange")
            if not isinstance(timerange, Mapping):
                continue
            start = timerange.get("start")
            segment_duration = timerange.get("duration")
            if isinstance(start, (int, float)) and isinstance(segment_duration, (int, float)):
                span = max(span, int(start) + int(segment_duration))
    return max(span, 1)


def _draft_end(draft: Mapping[str, Any]) -> int:
    raw_duration = draft.get("duration")
    end = int(raw_duration) if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool) else 0
    for track in _as_list(draft.get("tracks")):
        if not isinstance(track, Mapping):
            continue
        for segment in _as_list(track.get("segments")):
            if not isinstance(segment, Mapping):
                continue
            timerange = segment.get("target_timerange")
            if not isinstance(timerange, Mapping):
                continue
            start = timerange.get("start")
            duration = timerange.get("duration")
            if isinstance(start, (int, float)) and isinstance(duration, (int, float)):
                end = max(end, int(start) + int(duration))
    return max(end, 0)


def _clip_inner_to_sampling_window(
    inner: dict[str, Any],
    *,
    max_duration_us: int,
    preserve_audio: bool = False,
) -> dict[str, int]:
    """Trim an imported inner draft to the screening window.

    Only the tail of a segment is trimmed (the window always starts at zero),
    so its source start remains stable.  When a source timerange is present,
    its duration is scaled by the same target/source ratio.  This preserves
    speed semantics instead of changing a segment into a different source
    slice.  Segments wholly after the window are omitted from the copied
    sampling fragment.
    """

    trimmed = 0
    dropped = 0
    tracks = inner.get("tracks")
    if isinstance(tracks, list):
        for track in tracks:
            if not isinstance(track, dict):
                continue
            segments = track.get("segments")
            if not isinstance(segments, list):
                continue
            kept: list[Any] = []
            for segment in segments:
                if not isinstance(segment, dict):
                    kept.append(segment)
                    continue
                timerange = segment.get("target_timerange")
                if not isinstance(timerange, dict):
                    kept.append(segment)
                    continue
                start = timerange.get("start")
                duration = timerange.get("duration")
                if not isinstance(start, (int, float)) or isinstance(start, bool):
                    kept.append(segment)
                    continue
                if not isinstance(duration, (int, float)) or isinstance(duration, bool):
                    kept.append(segment)
                    continue
                start_us = int(start)
                duration_us = int(duration)
                if preserve_audio and track.get("type") == "audio" and start_us + duration_us > max_duration_us:
                    raise FiveTemplateTrialError("原配音频超出节点窗口；不能裁掉或缩短原配声音来装入预设")
                if start_us >= max_duration_us:
                    dropped += 1
                    continue
                allowed = max_duration_us - start_us
                if duration_us > allowed:
                    old_duration = duration_us
                    timerange["duration"] = allowed
                    source = segment.get("source_timerange")
                    if isinstance(source, dict):
                        source_duration = source.get("duration")
                        if isinstance(source_duration, (int, float)) and not isinstance(source_duration, bool):
                            source["duration"] = int(round(int(source_duration) * allowed / old_duration))
                    trimmed += 1
                kept.append(segment)
            track["segments"] = kept
    raw_duration = inner.get("duration")
    if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool):
        inner["duration"] = min(int(raw_duration), max_duration_us)
    else:
        inner["duration"] = max_duration_us
    return {"trimmed_segment_count": trimmed, "dropped_segment_count": dropped}


def _parse_audio_map(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        raw, separator, local = value.partition("=")
        if not separator or not raw or not local:
            raise FiveTemplateTrialError("--audio-map 格式必须是 原始路径=本地路径")
        local_path = Path(local).expanduser().resolve()
        if not local_path.is_file():
            raise FiveTemplateTrialError(f"--audio-map 的本地文件不存在: {local_path}")
        result[raw] = str(local_path)
    return result


def _parse_template_offsets(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        template_id, separator, raw_offset = value.partition("=")
        if not separator or not template_id or not raw_offset:
            raise FiveTemplateTrialError("--template-offset 格式必须是 模板ID=微秒")
        try:
            offset_us = int(raw_offset)
        except ValueError as exc:
            raise FiveTemplateTrialError("--template-offset 的微秒值必须是整数") from exc
        if offset_us < 0:
            raise FiveTemplateTrialError("--template-offset 不能为负数")
        result[template_id] = offset_us
    return result


def _validate_media_paths(media: Sequence[str | Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for value in media:
        path = Path(value).expanduser().resolve()
        if path.name.lower() in FORBIDDEN_OLD_T14_MEDIA:
            raise FiveTemplateTrialError(f"禁止使用旧 T14 帧: {path}")
        if not path.is_file():
            raise FiveTemplateTrialError(f"--media 文件不存在: {path}")
        result.append(path)
    return tuple(result)


def _sample_metadata() -> dict[str, Any]:
    def path_text(name: str) -> str:
        path = SOURCE_DIR / name
        return str(path.resolve()) if path.exists() else str(path)

    return {
        "sample_id": "T14_Shuyang_Mandarin_blind",
        "video": path_text("T14_Shuyang_Mandarin_blind_source.mp4"),
        "zh_srt": path_text("T14_Shuyang_Mandarin_blind_source.zh-Hans.srt"),
        "en_srt": path_text("T14_Shuyang_Mandarin_blind_source.en.srt"),
        "approved_media_frames": [
            path_text("T14_actual_frame_08.jpg"),
            path_text("T14_actual_frame_18.jpg"),
            path_text("T14_actual_frame_28.jpg"),
            path_text("T14_actual_frame_38.jpg"),
        ],
        "copy_excerpt": {
            "opening": "我叫 Li Shuang，我從北京來；20（20s)年以前來到日本",
            "family": "我有先生，還有兩個兒子；他們在上海讀小學，然後會說英文",
            "languages": "現在呢，也在日本讀中學，也會說日文",
            "closing": "希望你們有機會來日本，也來中國旅遊學中文和日文來敬請國際交流",
        },
    }


def build_five_template_trial(
    base_content: Mapping[str, Any] | str | Path,
    registry: Mapping[str, Any] | str | Path,
    preset_root: str | Path,
    media: Sequence[str | Path],
    *,
    trial_start_us: int = DEFAULT_TRIAL_START_US,
    gap_us: int = GAP_US,
    disable_audio: bool = True,
    audio_path_map: Mapping[str, str | Path] | None = None,
    visual_path_maps: Mapping[str, Mapping[str, str | Path]] | None = None,
    audio_cache_root: str | Path | None = None,
    allow_audio_rehydrate: bool = False,
    preserve_visual_resource_refs: bool = False,
    allowed_existing_missing_paths: Iterable[str | Path] = (),
    id_factory: Callable[[], str] | None = None,
    template_ids: Sequence[str] = FIVE_TEMPLATE_IDS,
    template_offsets_us: Mapping[str, int] | None = None,
    slot_copy_table: Mapping[str, Sequence[str]] | None = None,
    max_template_duration_us: int | None = MAX_TEMPLATE_DURATION_US,
) -> dict[str, Any]:
    """Build one to five selected templates into one independent in-memory draft."""

    if isinstance(trial_start_us, bool) or not isinstance(trial_start_us, int) or trial_start_us < 0:
        raise FiveTemplateTrialError("trial_start_us 必须是非负整数微秒")
    if isinstance(gap_us, bool) or not isinstance(gap_us, int) or gap_us < 0:
        raise FiveTemplateTrialError("gap_us 必须是非负整数微秒")
    if max_template_duration_us is not None and (
        isinstance(max_template_duration_us, bool)
        or not isinstance(max_template_duration_us, int)
        or max_template_duration_us <= 0
    ):
        raise FiveTemplateTrialError("max_template_duration_us 必须是正整数微秒或 None")
    if allow_audio_rehydrate and (disable_audio or audio_cache_root is None):
        raise FiveTemplateTrialError(
            "allow_audio_rehydrate 只能与启用音频和 audio_cache_root 一起使用"
        )
    if not isinstance(preserve_visual_resource_refs, bool):
        raise FiveTemplateTrialError("preserve_visual_resource_refs 必须明确是 bool")
    base_draft = _read_json(base_content, "base-content")
    registry_value = _read_json(registry, "registry")
    chosen_template_ids = tuple(template_ids)
    if not 1 <= len(chosen_template_ids) <= 5 or len(set(chosen_template_ids)) != len(
        chosen_template_ids
    ):
        raise FiveTemplateTrialError("template_ids 必须包含 1 到 5 个不重复模板")
    candidates = _candidate_map(registry_value, chosen_template_ids)
    root = Path(preset_root).expanduser().resolve()
    if not root.is_dir():
        raise FiveTemplateTrialError(f"preset-root 不存在: {root}")
    media_paths = _validate_media_paths(media)
    audio_cache_root_value = (
        Path(audio_cache_root).expanduser().resolve()
        if audio_cache_root is not None
        else None
    )
    aggregate = copy.deepcopy(base_draft)
    base_duration_us = _draft_end(base_draft)
    if base_duration_us <= 0:
        raise FiveTemplateTrialError("base-content 必须提供大于 0 的原片 duration 或有效时间线")
    id_factory_value = id_factory
    # Preflight all selected blocks before importing any of them.  This prevents a
    # successful-looking build from placing the trial after the source video.
    # Inner drafts in one preset are layers/variants of the same block, so the
    # block span is their maximum local end, not their sum.
    plans: list[dict[str, Any]] = []
    planned_total_span = 0
    for template_id in chosen_template_ids:
        candidate = candidates[template_id]
        source_path, source_payload = _candidate_source(candidate, root)
        copy_table = (
            slot_copy_table
            if slot_copy_table is not None
            else (T14_SEMANTIC_SLOT_COPY if template_offsets_us else T14_SLOT_COPY)
        )
        slot_values, slot_skips = _generate_slot_values(candidate, copy_table, strict=slot_copy_table is not None)
        media_map, media_slot_values, media_mappings, media_skips = _build_media_path_map(
            candidate, source_payload, media_paths
        )
        slot_values["media_slots"] = media_slot_values
        template_visual_path_map = dict(
            (visual_path_maps or {}).get(template_id, {})
        )
        effective_path_map = dict(template_visual_path_map)
        # Explicit current-video media is more specific than a recovered
        # preset dependency, and audio mappings remain the final authority for
        # audio-only paths.
        effective_path_map.update(media_map)
        effective_path_map.update(dict(audio_path_map or {}))
        sanitized_source, sanitization = _sanitize_source_payload(
            source_payload,
            path_map=effective_path_map,
            path_base=root,
            audio_cache_root=audio_cache_root_value,
            preserve_visual_resource_refs=preserve_visual_resource_refs,
        )
        sanitized_inners = _inner_refs(sanitized_source)
        inner_indexes = set(_candidate_inner_indexes(candidate))
        if not disable_audio:
            inner_indexes.update(index for index, inner in enumerate(sanitized_inners)
                                 if any(track.get("type") == "audio" and track.get("segments")
                                        for track in inner.get("tracks", [])))
        inner_indexes = tuple(sorted(inner_indexes))
        sampling_reports: dict[str, dict[str, int]] = {}
        for inner_index in inner_indexes:
            if inner_index >= len(sanitized_inners):
                raise FiveTemplateTrialError(
                    f"{template_id} 的 inner_draft_index={inner_index} 超出模板源范围"
                )
            sampling = (
                _clip_inner_to_sampling_window(
                    sanitized_inners[inner_index],
                    max_duration_us=max_template_duration_us,
                    preserve_audio=not disable_audio,
                )
                if max_template_duration_us is not None
                else {"trimmed_segment_count": 0, "dropped_segment_count": 0}
            )
            sampling_reports[str(inner_index)] = sampling
            if sampling["dropped_segment_count"]:
                # A slot whose only segment starts after the sample window no
                # longer represents the native preset.  Refuse that preset
                # instead of silently producing an incomplete visual sample.
                candidate_segments = [
                    segment
                    for track in sanitized_inners[inner_index].get("tracks", [])
                    if isinstance(track, Mapping)
                    for segment in _as_list(track.get("segments"))
                    if isinstance(segment, Mapping)
                ]
                if not candidate_segments:
                    raise FiveTemplateTrialError(
                        f"{template_id} inner_draft={inner_index} 的内容全部落在{max_template_duration_us}us采样窗口外"
                    )
        source_inner = extract_inner_drafts(sanitized_source)
        block_duration = max(
            (_local_span(source_inner[index], disable_audio=disable_audio) for index in inner_indexes),
            default=0,
        )
        if block_duration <= 0:
            raise FiveTemplateTrialError(f"{template_id} 没有可排布的正时长 inner draft")
        plans.append(
            {
                "template_id": template_id,
                "candidate": candidate,
                "source_path": source_path,
                "effective_path_map": effective_path_map,
                "visual_path_rebind_count": len(template_visual_path_map),
                "sanitized_source": sanitized_source,
                "source_inner": source_inner,
                "inner_indexes": inner_indexes,
                "block_duration": block_duration,
                "sampling_window_us": max_template_duration_us,
                "sampling": sampling_reports,
                "slot_values": slot_values,
                "slot_skips": slot_skips,
                "media_mappings": media_mappings,
                "media_skips": media_skips,
                "sanitization": sanitization,
            }
        )
        planned_total_span += block_duration
    explicit_offsets = dict(template_offsets_us or {})
    if explicit_offsets:
        missing_offsets = [item["template_id"] for item in plans if item["template_id"] not in explicit_offsets]
        if missing_offsets:
            raise FiveTemplateTrialError("缺少模板语义时点: " + ", ".join(missing_offsets))
        placements = sorted(
            (
                explicit_offsets[item["template_id"]],
                explicit_offsets[item["template_id"]] + item["block_duration"],
                item["template_id"],
            )
            for item in plans
        )
        for previous, current in zip(placements, placements[1:]):
            if current[0] < previous[1]:
                raise FiveTemplateTrialError(
                    f"语义模板发生重叠: {previous[2]} 与 {current[2]}"
                )
        trial_start_us = min(item[0] for item in placements)
        final_template_end_us = max(item[1] for item in placements)
        planned_total_span = final_template_end_us - trial_start_us
    else:
        planned_total_span += gap_us * max(len(plans) - 1, 0)
        final_template_end_us = trial_start_us + planned_total_span
    if trial_start_us > base_duration_us:
        raise FiveTemplateTrialError(
            f"trial_start_us={trial_start_us} 已超过 base_duration_us={base_duration_us}"
        )
    if final_template_end_us > base_duration_us:
        raise FiveTemplateTrialError(
            "所选模板放不进原片时长："
            f"trial_start_us={trial_start_us}, planned_total_span_us={planned_total_span}, "
            f"final_template_end_us={final_template_end_us}, base_duration_us={base_duration_us}"
        )

    cursor = trial_start_us
    template_manifests: list[dict[str, Any]] = []
    audio_rehydrations: list[dict[str, Any]] = []
    visual_rehydrations: list[dict[str, Any]] = []

    for plan in plans:
        template_id = plan["template_id"]
        candidate = plan["candidate"]
        source_path = plan["source_path"]
        effective_path_map = plan["effective_path_map"]
        sanitized_source = plan["sanitized_source"]
        source_inner = plan["source_inner"]
        inner_indexes = plan["inner_indexes"]
        slot_values = plan["slot_values"]
        template_start = explicit_offsets.get(template_id, cursor)
        inner_manifests: list[dict[str, Any]] = []
        imported_track_count = 0
        imported_material_count = 0
        skipped_items = (
            plan["slot_skips"]
            + plan["media_skips"]
            + plan["sanitization"]["skipped_items"]
            + plan["sanitization"]["cleared_paths"]
            + plan["sanitization"]["deduplicated_materials"]
        )
        for inner_index in inner_indexes:
            try:
                result: TrialBuildResult = build_trial_draft(
                    aggregate,
                    registry_value,
                    template_id,
                    slot_values,
                    outer_preset=sanitized_source,
                    preset_root=root,
                    draft_index=inner_index,
                    offset=template_start,
                    path_map=effective_path_map,
                    path_base=root,
                    audio_cache_root=audio_cache_root_value,
                    allow_audio_rehydrate=allow_audio_rehydrate,
                    allow_visual_rehydrate=preserve_visual_resource_refs,
                    disable_audio=disable_audio,
                    unsupported_track_policy="skip",
                    **({"id_factory": id_factory_value} if id_factory_value is not None else {}),
                )
            except (TrialDraftError, TrialDependencyError, ValueError) as exc:
                report = getattr(exc, "report", None)
                detail = f"；校验明细={json.dumps(report, ensure_ascii=False)}" if report else ""
                raise FiveTemplateTrialError(
                    f"{template_id} inner_draft={inner_index} 构建失败: {exc}{detail}"
                ) from exc
            aggregate = result.draft
            imported_track_count += int(result.report.get("imported_track_count", 0))
            imported_material_count += int(result.report.get("imported_material_count", 0))
            inner_span = _local_span(source_inner[inner_index], disable_audio=disable_audio)
            inner_manifests.append(
                {
                    "inner_draft_index": inner_index,
                    "start_us": template_start,
                    "end_us": template_start + inner_span,
                    "duration_us": inner_span,
                    "build_report": result.report,
                }
            )
            audio_rehydrations.extend(result.report.get("audio_rehydrations", []))
            visual_rehydrations.extend(result.report.get("visual_rehydrations", []))
            skipped_items.extend(result.report.get("skipped_tracks", []))

        template_end = max((item["end_us"] for item in inner_manifests), default=template_start)
        if template_end > base_duration_us:
            raise FiveTemplateTrialError(
                f"{template_id} 超出 base_duration_us={base_duration_us}: end_us={template_end}"
            )
        template_manifests.append(
            {
                "template_id": template_id,
                "display_name": candidate.get("display_name"),
                "source_path": str(source_path),
                "start_us": template_start,
                "end_us": template_end,
                "duration_us": template_end - template_start,
                "sampling_window_us": plan["sampling_window_us"],
                "sampling": plan["sampling"],
                "inner_drafts": inner_manifests,
                "slot_values": slot_values,
                "filled_text_slots": sorted(slot_values["text_slots"]),
                "media_mappings": media_mappings,
                "skipped_items": skipped_items,
                "sanitization": {
                    "repaired_ids": plan["sanitization"]["repaired_ids"],
                    "cleared_external_paths": plan["sanitization"]["cleared_paths"],
                    "preserved_external_paths": plan["sanitization"]["preserved_paths"],
                    "deduplicated_materials": plan["sanitization"]["deduplicated_materials"],
                },
                "imported_track_count": imported_track_count,
                "imported_material_count": imported_material_count,
            }
        )
        if not explicit_offsets:
            cursor = template_end + gap_us

    pending_audio_paths = {
        item["resolved_path"]
        for item in audio_rehydrations
        if item.get("pending_rehydrate")
    }
    pending_visual_paths = {
        item["resolved_path"]
        for item in visual_rehydrations
        if item.get("pending_rehydrate")
    }
    inherited_missing_paths = {str(Path(path)) for path in allowed_existing_missing_paths}
    final_validation = validate_trial_draft(
        aggregate,
        path_base=root,
        allowed_missing_paths=pending_audio_paths | pending_visual_paths | inherited_missing_paths,
    )
    ordered_manifests = sorted(template_manifests, key=lambda item: item["start_us"])
    required_gap_us = 0 if explicit_offsets else gap_us
    blocks_non_overlapping = all(
        current["start_us"] >= previous["end_us"] + required_gap_us
        for previous, current in zip(ordered_manifests, ordered_manifests[1:])
    )
    if not final_validation.ok:
        raise FiveTemplateTrialError("所选模板合并后的 draft 未通过结构校验")
    if _draft_end(aggregate) > base_duration_us:
        raise FiveTemplateTrialError(
            "合并结果延长了原片 duration，拒绝输出："
            f"base_duration_us={base_duration_us}, result_duration_us={_draft_end(aggregate)}"
        )

    manifest = {
        "schema": "five_template_blind_trial_v1",
        "sample": _sample_metadata(),
        "template_ids": list(chosen_template_ids),
        "slot_copy_source": "caller" if slot_copy_table is not None else "t14_default",
        "base_duration_us": base_duration_us,
        "trial_start_us": trial_start_us,
        "final_template_end_us": final_template_end_us,
        "planned_total_span_us": planned_total_span,
        "gap_us": gap_us,
        "max_template_duration_us": max_template_duration_us,
        "disable_audio": disable_audio,
        "audio_rehydrate": {
            "enabled": allow_audio_rehydrate,
            "cache_root": str(audio_cache_root_value) if audio_cache_root_value else None,
            "resolved": audio_rehydrations,
            "inherited_allowed_missing_paths": sorted(inherited_missing_paths),
        },
        "visual_resource_rehydrate": {
            "enabled": preserve_visual_resource_refs,
            "resolved": visual_rehydrations,
            "pending_paths": sorted(pending_visual_paths),
        },
        "media_policy": {
            "explicit_cli_media_only": True,
            "forbidden_old_frame_names": sorted(FORBIDDEN_OLD_T14_MEDIA),
            "provided_media": [str(path) for path in media_paths],
        },
        "templates": template_manifests,
        "structure_validation": {
            "template_blocks_non_overlapping": blocks_non_overlapping,
            "minimum_inter_template_gap_us": min(
                (
                    current["start_us"] - previous["end_us"]
                    for previous, current in zip(ordered_manifests, ordered_manifests[1:])
                ),
                default=required_gap_us,
            ),
            "final_validation": final_validation.to_dict(),
        },
    }
    return {"draft": aggregate, "manifest": manifest}


def _workspace_output_path(output: str | Path) -> Path:
    path = Path(output).expanduser().resolve()
    allowed_root = PROJECT_ROOT / "jianying-adapter" / "trial_outputs" / "five_template_blind_test"
    try:
        path.relative_to(allowed_root.resolve())
    except ValueError as exc:
        raise FiveTemplateTrialError(
            f"--output 必须位于工作区 trial_outputs/five_template_blind_test: {path}"
        ) from exc
    return path


def write_trial_output(envelope: Mapping[str, Any], output: str | Path) -> Path:
    path = _workspace_output_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _example_command() -> str:
    root = "E:\\AutoCUT"
    source = root + "\\video_trials\\batch_talking_head_pilot\\sources"
    return (
        "D:\\python\\python.exe scripts\\build_five_template_trial.py `\n"
        f"  --base-content <T14已解密draft_content.json> `\n"
        "  --registry preset_catalog\\candidate_25_v1.json `\n"
        "  --preset-root " + root + "\\jianying_presets\\剪映1000个高级感字幕预设 `\n"
        f"  --media {source}\\T14_actual_frame_08.jpg `\n"
        f"  --media {source}\\T14_actual_frame_18.jpg `\n"
        f"  --media {source}\\T14_actual_frame_28.jpg `\n"
        f"  --media {source}\\T14_actual_frame_38.jpg `\n"
        "  --output trial_outputs\\five_template_blind_test\\T14_five_template_trial.json"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将 1–5 套选定剪映预设合并进一个工作区 JSON，供用户后续主观验收。",
        epilog=(
            "本轮样本是 T14_Shuyang_Mandarin_blind_vertical.mp4；文案来自同目录双语 SRT，"
            "素材展示只接收显式 --media 的 T14_actual_frame_08/18/28/38.jpg。\n\n"
            "示例（PowerShell）：\n" + _example_command()
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-content", type=Path, required=True, help="已解密的 base draft JSON")
    parser.add_argument("--registry", type=Path, required=True, help="candidate_25_v1.json")
    parser.add_argument("--preset-root", type=Path, required=True, help="1947 份预设的根目录")
    parser.add_argument(
        "--media",
        type=Path,
        action="append",
        default=[],
        help="显式素材路径，可重复；素材展示模板缺少该参数会立即拒绝",
    )
    parser.add_argument("--output", type=Path, required=True, help="工作区内的 JSON 输出路径")
    parser.add_argument(
        "--template-id",
        action="append",
        default=[],
        help="指定一个模板 ID；可重复 1–5 次且不能重复。省略时使用默认模板集合。",
    )
    parser.add_argument(
        "--template-offset",
        action="append",
        default=[],
        help="模板语义时点，格式为 模板ID=微秒；使用时所选模板都必须提供。",
    )
    parser.add_argument(
        "--trial-start-us",
        type=int,
        default=DEFAULT_TRIAL_START_US,
        help=f"所选模板试剪块在原片内的起点（微秒，默认 {DEFAULT_TRIAL_START_US} = 3.5 秒）",
    )
    parser.add_argument(
        "--enable-audio",
        action="store_true",
        help="显式启用预设音频；同时提供 --audio-map 或 --audio-cache-root",
    )
    parser.add_argument(
        "--audio-map",
        action="append",
        default=[],
        help="音频路径映射，可重复，格式为 原始路径=本地文件",
    )
    parser.add_argument(
        "--audio-cache-root",
        type=Path,
        help="当前剪映 User Data/Cache/music，用原文件名和 effect_id 重绑官方音效",
    )
    parser.add_argument(
        "--allow-audio-rehydrate",
        action="store_true",
        help="允许尚未缓存的官方音效在首次打开草稿时由剪映下载",
    )
    parser.add_argument(
        "--preserve-visual-resource-refs",
        action="store_true",
        help="隔离恢复探针专用：保留带远程 ID 的字体/动画路径，不得用于普通生产候选",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audio_map = _parse_audio_map(args.audio_map)
    template_offsets = _parse_template_offsets(args.template_offset)
    envelope = build_five_template_trial(
        args.base_content,
        args.registry,
        args.preset_root,
        args.media,
        trial_start_us=args.trial_start_us,
        disable_audio=not args.enable_audio,
        audio_path_map=audio_map,
        audio_cache_root=args.audio_cache_root,
        allow_audio_rehydrate=args.allow_audio_rehydrate,
        preserve_visual_resource_refs=args.preserve_visual_resource_refs,
        template_ids=tuple(args.template_id) if args.template_id else FIVE_TEMPLATE_IDS,
        template_offsets_us=template_offsets,
    )
    output = write_trial_output(envelope, args.output)
    print(json.dumps(envelope["manifest"], ensure_ascii=False, indent=2))
    print(f"写入: {output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_TRIAL_START_US",
    "FIVE_TEMPLATE_IDS",
    "FiveTemplateTrialError",
    "FORBIDDEN_OLD_T14_MEDIA",
    "T14_SLOT_COPY",
    "build_five_template_trial",
    "build_parser",
    "generate_slot_values",
    "main",
    "write_trial_output",
]
