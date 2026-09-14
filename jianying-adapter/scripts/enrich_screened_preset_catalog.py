"""Backfill deterministic selection profiles for the 242 screened text presets.

The screening result is deliberately recorded as a library-screening fact.  It
does not change ``visual_validation``, ``production.status``,
``ready_for_jianying_8_8`` or any current-video readiness field.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping


ADAPTER = Path(__file__).resolve().parents[1]
PROJECT = ADAPTER.parent
CATALOG_PATH = ADAPTER / "preset_catalog" / "standardized_preset_catalog_v1.json"
USAGE_PATH = ADAPTER / "preset_catalog" / "preset_usage_registry_v1.json"
TRIAL = PROJECT / "video_trials" / "字幕预设黑幕筛选_20260902"
SCREENING = TRIAL / "screening_500"
QUEUE_PATH = SCREENING / "fully_recovered_500_queue.json"
SET_VALIDATION_PATH = SCREENING / "fully_recovered_500_set_validation.json"
RUNTIME_PATH = SCREENING / "fully_recovered_500_runtime_candidate_registry.json"
STATE_PATH = TRIAL / "project_state.json"
PROFILE_PATH = ADAPTER / "preset_catalog" / "screened_preset_selection_profiles_v1.json"
REPORT_PATH = SCREENING / "approved_242_catalog_backfill_report.json"
SOURCE_ROOT = PROJECT / "jianying_presets" / "剪映1000个高级感字幕预设"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()


def _catalog_base_fingerprint(value: Mapping[str, Any]) -> str:
    base = copy.deepcopy(value)
    base.get("policy", {}).pop("library_screening_does_not_equal_current_video_or_production_readiness", None)
    base.get("summary", {}).pop("library_screening_approved_count", None)
    base.get("summary", {}).pop("selection_profile_count", None)
    base.pop("enrichment_generator", None)
    for row in base.get("records") or []:
        row.pop("selection_profile", None)
        (row.get("screening") or {}).pop("library_screening", None)
    return json_sha256(base)


def _usage_base_fingerprint(value: Mapping[str, Any]) -> str:
    base = copy.deepcopy(value)
    base.get("policy", {}).pop("library_screening_does_not_equal_current_video_or_production_readiness", None)
    base.get("summary", {}).pop("library_screening_approved_record_count", None)
    for row in base.get("records") or []:
        row.pop("selection_profile", None)
        row.pop("library_screening", None)
    return json_sha256(base)


def _screening_state_fingerprint(value: Mapping[str, Any]) -> str:
    screening = copy.deepcopy(((value.get("artifacts") or {}).get("screening_500") or {}))
    for key in list(screening):
        if key.startswith("catalog_classification_"):
            screening.pop(key, None)
    return json_sha256(screening)


def _records_by_id(rows: Any) -> dict[str, dict[str, Any]]:
    return {
        str(row["template_id"]): row
        for row in rows or []
        if isinstance(row, dict) and row.get("template_id")
    }


def _resolve_source(candidate: Mapping[str, Any]) -> Path:
    path = Path(str(candidate.get("source_path") or ""))
    return path if path.is_absolute() else SOURCE_ROOT / path


def _inner_drafts(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    embedded = (source.get("materials") or {}).get("drafts") or []
    rows = [row.get("draft") for row in embedded if isinstance(row, Mapping) and isinstance(row.get("draft"), Mapping)]
    return rows or [source]


def _hex_from_rgb(values: Any) -> str | None:
    if not isinstance(values, list) or len(values) < 3:
        return None
    try:
        rgb = [max(0, min(255, round(float(value) * 255))) for value in values[:3]]
    except (TypeError, ValueError):
        return None
    return "#" + "".join(f"{value:02X}" for value in rgb)


def _normalise_hex(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text.startswith("#"):
        return None
    digits = text[1:]
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    if len(digits) not in {6, 8} or any(char not in "0123456789ABCDEF" for char in digits):
        return None
    return "#" + digits[:6]


def _content_fills(text_material: Mapping[str, Any]) -> list[str]:
    content = text_material.get("content")
    if not isinstance(content, str):
        return []
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return []
    colors: list[str] = []
    for style in parsed.get("styles") or []:
        if not isinstance(style, Mapping):
            continue
        solid = (((style.get("fill") or {}).get("content") or {}).get("solid") or {})
        color = _hex_from_rgb(solid.get("color"))
        if color:
            colors.append(color)
    return colors


def _slot_materials(candidate: Mapping[str, Any], drafts: list[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], Mapping[str, Any] | None]]:
    rows: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]] = []
    for slot in ((candidate.get("slots") or {}).get("text") or []):
        if not isinstance(slot, Mapping):
            continue
        material = None
        locators = slot.get("locators") or []
        if locators and isinstance(locators[0], Mapping):
            locator = locators[0]
            index = int(locator.get("inner_draft_index") or 0)
            material_id = str(locator.get("material_id") or "")
            if index < len(drafts):
                material = next(
                    (item for item in ((drafts[index].get("materials") or {}).get("texts") or [])
                     if isinstance(item, Mapping) and str(item.get("id") or "") == material_id),
                    None,
                )
        rows.append((slot, material))
    return rows


def _segment_facts(candidate: Mapping[str, Any], drafts: list[Mapping[str, Any]]) -> tuple[list[dict[str, float]], list[int], int]:
    points: list[dict[str, float]] = []
    starts: list[int] = []
    keyframe_count = 0
    for slot in ((candidate.get("slots") or {}).get("text") or []):
        for locator in slot.get("locators") or []:
            if not isinstance(locator, Mapping):
                continue
            index = int(locator.get("inner_draft_index") or 0)
            material_id = str(locator.get("material_id") or "")
            for ref in locator.get("segment_refs") or []:
                if isinstance(ref, Mapping):
                    starts.append(int(ref.get("start") or 0))
            if index >= len(drafts):
                continue
            for track in drafts[index].get("tracks") or []:
                if not isinstance(track, Mapping):
                    continue
                for segment in track.get("segments") or []:
                    if not isinstance(segment, Mapping) or str(segment.get("material_id") or "") != material_id:
                        continue
                    transform = ((segment.get("clip") or {}).get("transform") or {})
                    if transform:
                        points.append({"x": round(float(transform.get("x") or 0.0), 6),
                                       "y": round(float(transform.get("y") or 0.0), 6)})
                    keyframe_count += len(segment.get("common_keyframes") or []) + len(segment.get("keyframe_refs") or [])
    unique_points = [dict(item) for item in {tuple(sorted(point.items())) for point in points}]
    unique_points.sort(key=lambda item: (item["y"], item["x"]))
    return unique_points, sorted(set(starts)), keyframe_count


def _animation_facts(drafts: list[Mapping[str, Any]]) -> tuple[list[str], list[str], int, int]:
    names: list[str] = []
    types: list[str] = []
    count = 0
    max_duration = 0
    for draft in drafts:
        for material in ((draft.get("materials") or {}).get("material_animations") or []):
            if not isinstance(material, Mapping):
                continue
            for animation in material.get("animations") or []:
                if not isinstance(animation, Mapping):
                    continue
                count += 1
                if animation.get("name"):
                    names.append(str(animation["name"]))
                if animation.get("type"):
                    types.append(str(animation["type"]))
                max_duration = max(max_duration, int(animation.get("duration") or 0))
    return sorted(set(names)), sorted(set(types)), count, max_duration


def _palette_family(colors: Iterable[str]) -> str:
    unique = sorted(set(colors))
    chromatic: list[tuple[int, int, int]] = []
    for color in unique:
        rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
        if max(rgb) - min(rgb) > 24:
            chromatic.append(rgb)
    if not unique:
        return "unknown"
    if not chromatic:
        return "neutral_monochrome"
    if len(chromatic) > 1:
        return "mixed_color"
    red, green, blue = chromatic[0]
    return "warm_accent" if red >= blue and (red >= green or green >= blue) else "cool_accent"


def _size_tier(font_sizes: list[float]) -> str:
    if not font_sizes:
        return "unknown"
    value = max(font_sizes)
    if value <= 20:
        return "compact"
    if value <= 36:
        return "standard"
    if value <= 56:
        return "large"
    return "display"


def _layout(points: list[dict[str, float]]) -> dict[str, Any]:
    if not points:
        return {"coordinate_evidence": [], "horizontal_band": "unknown", "vertical_band": "unknown",
                "arrangement": "unknown"}
    xs, ys = [p["x"] for p in points], [p["y"] for p in points]
    x_mid, y_mid = median(xs), median(ys)
    x_spread, y_spread = max(xs) - min(xs), max(ys) - min(ys)
    horizontal = "negative_x" if x_mid < -0.2 else "positive_x" if x_mid > 0.2 else "center_x"
    vertical = "negative_y" if y_mid < -0.2 else "positive_y" if y_mid > 0.2 else "center_y"
    if len(points) == 1:
        arrangement = "single_anchor"
    elif y_spread >= 0.35 and x_spread < 0.35:
        arrangement = "vertical_stack"
    elif x_spread >= 0.35 and y_spread < 0.35:
        arrangement = "horizontal_row"
    elif x_spread >= 0.35 and y_spread >= 0.35:
        arrangement = "distributed"
    else:
        arrangement = "clustered"
    return {
        "coordinate_system": "jianying_normalized_transform_raw_sign",
        "coordinate_evidence": points,
        "horizontal_band": horizontal,
        "vertical_band": vertical,
        "arrangement": arrangement,
        "x_spread": round(x_spread, 6),
        "y_spread": round(y_spread, 6),
    }


def derive_profile(candidate: Mapping[str, Any], catalog_row: Mapping[str, Any], category_rule: Mapping[str, Any], queue_row: Mapping[str, Any], evidence_path: str) -> dict[str, Any]:
    source_path = _resolve_source(candidate)
    source_hash = file_sha256(source_path)
    expected_hash = str(candidate.get("source_hash") or "").upper()
    if not expected_hash or source_hash != expected_hash:
        raise ValueError(f"source hash mismatch: {candidate.get('template_id')}")
    source = read_json(source_path)
    drafts = _inner_drafts(source)
    slot_materials = _slot_materials(candidate, drafts)
    styles = [slot.get("default_style") or {} for slot, _material in slot_materials]
    text_materials = [material for _slot, material in slot_materials if material]
    colors = sorted(set(filter(None, [
        *(_normalise_hex(style.get("text_color")) for style in styles),
        *(color for material in text_materials for color in _content_fills(material)),
    ])))
    font_sizes = sorted(float(style.get("font_size") or 0.0) for style in styles if float(style.get("font_size") or 0.0) > 0)
    background = any(
        (_normalise_hex(style.get("background_color")) and float(style.get("background_alpha") or 0.0) > 0)
        or int(style.get("background_style") or 0) > 0
        for style in styles
    )
    border = any(_normalise_hex(style.get("border_color")) and float(style.get("border_width") or 0.0) > 0
                 and float(style.get("border_alpha") or 0.0) > 0 for style in styles)
    shadow = any(bool(style.get("has_shadow")) and float(style.get("shadow_alpha") or 0.0) > 0.05 for style in styles)
    rich_text = any(int(style.get("content_style_count") or 0) > 1 for style in styles)
    decorative = any(bool(slot.get("decorative_locked")) for slot, _material in slot_materials)
    editable_count = sum(not bool(slot.get("decorative_locked")) for slot, _material in slot_materials)
    points, starts, keyframe_count = _segment_facts(candidate, drafts)
    animation_names, animation_types, animation_count, max_animation_duration = _animation_facts(drafts)
    staggered = len(starts) > 1
    motion_score = animation_count + min(keyframe_count, 3) + int(staggered)
    motion_intensity = "low" if motion_score <= 1 else "medium" if motion_score <= 4 else "high"
    layout = _layout(points)
    family_tags: list[str] = []
    if background:
        family_tags.append("card_or_label")
    if rich_text or len(colors) > 1:
        family_tags.append("rich_color_accent")
    if decorative:
        family_tags.append("decorative_symbol")
    if editable_count >= 3 or layout["arrangement"] in {"vertical_stack", "horizontal_row", "distributed"}:
        family_tags.append("layered_text")
    if border or shadow:
        family_tags.append("outlined_or_shadowed")
    if not family_tags:
        family_tags.append("clean_text")
    primary_family = family_tags[0]
    if staggered:
        motion_pattern = "staggered"
    elif animation_count and len(animation_types) > 1:
        motion_pattern = "mixed_animation"
    elif animation_count:
        motion_pattern = "simultaneous_animation"
    elif keyframe_count:
        motion_pattern = "keyframed"
    else:
        motion_pattern = "static"
    text_tracks = catalog_row.get("text_tracks") or []
    limits = [row.get("max_chars") for row in text_tracks if isinstance(row, Mapping) and row.get("editable")]
    return {
        "template_id": str(candidate["template_id"]),
        "semantic_category": str(catalog_row.get("semantic_category") or "unclear"),
        "scene_selection": {
            "use_when": list(category_rule.get("use_when") or []),
            "avoid_when": list(category_rule.get("avoid_when") or []),
            "spoken_cues": list(category_rule.get("spoken_cues") or []),
            "recommended_zone": category_rule.get("zone"),
            "ordinary_caption_policy": category_rule.get("caption_policy"),
            "duration_floor_seconds": category_rule.get("duration_floor"),
            "editable_text_slot_count": editable_count,
            "max_chars_per_editable_slot": limits,
        },
        "visual_classification": {
            "primary_family": primary_family,
            "family_tags": family_tags,
            "palette_family": _palette_family(colors),
            "fill_colors": colors,
            "typography": {
                "size_tier": _size_tier(font_sizes),
                "font_size_min": min(font_sizes) if font_sizes else None,
                "font_size_max": max(font_sizes) if font_sizes else None,
                "rich_text_ranges": rich_text,
                "has_background": background,
                "has_border": border,
                "has_shadow": shadow,
            },
            "layout": layout,
            "motion": {
                "pattern": motion_pattern,
                "intensity": motion_intensity,
                "animation_count": animation_count,
                "animation_types": animation_types,
                "animation_names": animation_names,
                "max_animation_duration_us": max_animation_duration,
                "keyframe_count": keyframe_count,
                "text_start_times_us": starts,
            },
        },
        "library_screening": {
            "status": "approved_black_screen_no_media_missing",
            "project_id": "subtitle_preset_black_screening_20260902",
            "batch": int(queue_row["batch"]),
            "batch_order": int(queue_row["batch_order"]),
            "frontend_evidence": evidence_path,
            "scope": "library_screening_only",
            "current_video_ready": False,
            "production_ready_claimed": False,
        },
        "evidence": {
            "source_path": str(candidate.get("source_path") or ""),
            "source_sha256": source_hash,
            "source_hash_verified": True,
            "structure_hash": candidate.get("structure_hash"),
            "runtime_candidate_registry": str(RUNTIME_PATH),
            "style_source": "runtime text slots plus embedded native source draft",
        },
    }


def build_outputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    catalog = read_json(CATALOG_PATH)
    usage = read_json(USAGE_PATH)
    queue = read_json(QUEUE_PATH)
    validation = read_json(SET_VALIDATION_PATH)
    runtime = read_json(RUNTIME_PATH)
    state = read_json(STATE_PATH)
    queue_rows = _records_by_id(queue.get("items"))
    candidate_rows = _records_by_id(runtime.get("candidates"))
    catalog_rows = _records_by_id(catalog.get("records"))
    usage_rows = _records_by_id(usage.get("records"))
    approved_ids = list(queue_rows)
    approved_set = set(approved_ids)
    deferred_ids = set(validation.get("excluded_partially_recovered", {}).get("combined") or []) | {
        str(row.get("template_id")) for row in queue.get("deferred_embedded_media") or [] if isinstance(row, Mapping)
    }
    screening_state = (state.get("artifacts") or {}).get("screening_500") or {}
    if len(approved_ids) != 242 or len(approved_set) != 242:
        raise ValueError("approved screening scope must be exactly 242 unique templates")
    if set(candidate_rows) != approved_set or not approved_set <= set(catalog_rows):
        raise ValueError("queue, runtime registry and standardized catalog scope mismatch")
    if len(deferred_ids) != 400 or approved_set & deferred_ids:
        raise ValueError("deferred 400 scope is not intact and disjoint")
    if screening_state.get("status") != "completed" or screening_state.get("approved_template_count") != 242:
        raise ValueError("project_state does not confirm completed 242 screening")
    if screening_state.get("production_ready_claimed") is not False:
        raise ValueError("project_state production-ready boundary is not false")
    production_before = {tid: copy.deepcopy(catalog_rows[tid].get("production")) for tid in catalog_rows}
    deferred_before = {tid: json_sha256(catalog_rows[tid]) for tid in deferred_ids if tid in catalog_rows}
    profiles: list[dict[str, Any]] = []
    rules = usage.get("category_rules") or {}
    for queue_row in sorted(queue_rows.values(), key=lambda row: (int(row["batch"]), int(row["batch_order"]))):
        tid = str(queue_row["template_id"])
        category = str(catalog_rows[tid].get("semantic_category") or "unclear")
        batch = int(queue_row["batch"])
        batch_state = screening_state.get(f"batch_{batch:02d}") or {}
        evidence_path = str(batch_state.get("frontend_playback_evidence") or "")
        if batch == 1 and not evidence_path and batch_state.get("user_visual_decision") == "all_50_passed":
            evidence_path = f"{STATE_PATH}#artifacts.screening_500.batch_01"
        if not evidence_path:
            raise ValueError(f"missing frontend playback evidence: batch {batch}")
        profile = derive_profile(candidate_rows[tid], catalog_rows[tid], rules.get(category) or {}, queue_row, evidence_path)
        profiles.append(profile)
    profile_rows = _records_by_id(profiles)
    updated_catalog = copy.deepcopy(catalog)
    for row in updated_catalog["records"]:
        tid = str(row["template_id"])
        if tid not in profile_rows:
            continue
        profile = profile_rows[tid]
        row["selection_profile"] = {
            "semantic_category": profile["semantic_category"],
            "scene_selection": profile["scene_selection"],
            "visual_classification": profile["visual_classification"],
            "evidence_sha256": json_sha256(profile["evidence"]),
        }
        row["screening"]["library_screening"] = profile["library_screening"]
    updated_catalog["policy"]["library_screening_does_not_equal_current_video_or_production_readiness"] = True
    updated_catalog["enrichment_generator"] = "enrich_screened_preset_catalog.py"
    updated_catalog["summary"]["library_screening_approved_count"] = len(profiles)
    updated_catalog["summary"]["selection_profile_count"] = len(profiles)
    updated_usage = copy.deepcopy(usage)
    for row in updated_usage["records"]:
        tid = str(row["template_id"])
        if tid not in profile_rows:
            continue
        profile = profile_rows[tid]
        row["selection_profile"] = {
            "scene_selection": profile["scene_selection"],
            "visual_classification": profile["visual_classification"],
            "evidence_sha256": json_sha256(profile["evidence"]),
        }
        row["library_screening"] = profile["library_screening"]
    updated_usage["policy"]["library_screening_does_not_equal_current_video_or_production_readiness"] = True
    updated_usage["summary"]["library_screening_approved_record_count"] = len(approved_set & set(usage_rows))
    updated_catalog_rows = _records_by_id(updated_catalog["records"])
    production_after = {tid: updated_catalog_rows[tid].get("production") for tid in updated_catalog_rows}
    deferred_after = {tid: json_sha256(updated_catalog_rows[tid]) for tid in deferred_ids if tid in updated_catalog_rows}
    profile_doc = {
        "schema": "jianying-adapter.screened-preset-selection-profiles.v1",
        "generator": "enrich_screened_preset_catalog.py",
        "policy": {
            "classification_uses_native_structure_style_color_layout_and_animation_evidence": True,
            "display_name_is_not_a_classification_input": True,
            "black_screen_approval_is_library_screening_only": True,
            "current_video_ready_is_never_granted": True,
            "production_ready_is_never_granted": True,
        },
        "inputs": {
            "catalog": str(CATALOG_PATH), "catalog_base_sha256": _catalog_base_fingerprint(catalog),
            "usage_registry": str(USAGE_PATH), "usage_registry_base_sha256": _usage_base_fingerprint(usage),
            "queue": str(QUEUE_PATH), "queue_sha256": file_sha256(QUEUE_PATH),
            "runtime_candidate_registry": str(RUNTIME_PATH), "runtime_candidate_registry_sha256": file_sha256(RUNTIME_PATH),
            "set_validation": str(SET_VALIDATION_PATH), "set_validation_sha256": file_sha256(SET_VALIDATION_PATH),
            "project_state": str(STATE_PATH),
            "project_state_screening_evidence_sha256": _screening_state_fingerprint(state),
        },
        "summary": {
            "profile_count": len(profiles),
            "unique_template_count": len(profile_rows),
            "semantic_category_counts": dict(sorted(Counter(p["semantic_category"] for p in profiles).items())),
            "primary_visual_family_counts": dict(sorted(Counter(p["visual_classification"]["primary_family"] for p in profiles).items())),
            "palette_family_counts": dict(sorted(Counter(p["visual_classification"]["palette_family"] for p in profiles).items())),
            "motion_intensity_counts": dict(sorted(Counter(p["visual_classification"]["motion"]["intensity"] for p in profiles).items())),
            "layout_arrangement_counts": dict(sorted(Counter(p["visual_classification"]["layout"]["arrangement"] for p in profiles).items())),
        },
        "profiles": profiles,
    }
    checks = {
        "approved_242_unique": len(profiles) == len(profile_rows) == 242,
        "catalog_backfilled_242": sum("selection_profile" in row for row in updated_catalog["records"] if row["template_id"] in approved_set) == 242,
        "usage_registry_matching_records_backfilled": sum("selection_profile" in row for row in updated_usage["records"] if row["template_id"] in approved_set) == len(approved_set & set(usage_rows)),
        "catalog_only_generated_id_count_is_39": len(approved_set - set(usage_rows)) == 39,
        "no_approved_deferred_overlap": not bool(approved_set & deferred_ids),
        "deferred_400_unchanged": deferred_before == deferred_after,
        "all_production_fields_unchanged": production_before == production_after,
        "no_current_video_ready_granted": all(p["library_screening"]["current_video_ready"] is False for p in profiles),
        "no_production_ready_claimed": all(p["library_screening"]["production_ready_claimed"] is False for p in profiles),
        "all_source_hashes_verified": all(p["evidence"]["source_hash_verified"] for p in profiles),
    }
    report = {
        "schema": "huoke.approved-242-catalog-backfill-report.v1",
        "scope": {
            "approved_template_count": len(approved_set),
            "deferred_template_count": len(deferred_ids),
            "standardized_catalog_match_count": len(approved_set & set(catalog_rows)),
            "usage_registry_match_count": len(approved_set & set(usage_rows)),
            "catalog_only_generated_id_count": len(approved_set - set(usage_rows)),
            "duplicate_approved_id_count": len(approved_ids) - len(approved_set),
            "omitted_approved_id_count": len(approved_set - set(profile_rows)),
        },
        "classification_summary": profile_doc["summary"],
        "checks": checks,
        "ok": all(checks.values()),
        "outputs": {"profile_catalog": str(PROFILE_PATH), "standardized_catalog": str(CATALOG_PATH),
                    "usage_registry": str(USAGE_PATH)},
        "boundary": "242款仅回写黑幕资源筛选状态与选择标签；延期400款未改，未授予current_video_ready或production_ready。",
    }
    if not report["ok"]:
        raise ValueError(f"backfill checks failed: {[key for key, value in checks.items() if not value]}")
    return updated_catalog, updated_usage, profile_doc, report


def main() -> None:
    catalog, usage, profiles, report = build_outputs()
    write_json(CATALOG_PATH, catalog)
    write_json(USAGE_PATH, usage)
    write_json(PROFILE_PATH, profiles)
    write_json(REPORT_PATH, report)
    print(json.dumps({"profiles": len(profiles["profiles"]), "report_ok": report["ok"],
                      "report": str(REPORT_PATH)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
