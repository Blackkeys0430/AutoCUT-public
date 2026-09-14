"""Explicit track or segment style variants; native motion stays untouched."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


def _validate_font(font, text):
    path = Path(font["path"])
    # The native system font uses an explicit empty resource id. Its local
    # file and glyph coverage must still be bound and checked.
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != font.get("sha256") or not isinstance(font.get("id"), str):
        raise ValueError("style font must be a bound local resource")
    from fontTools.ttLib import TTFont
    with TTFont(path) as face:
        glyphs = face.getBestCmap() or {}
        if any(ord(c) not in glyphs for c in text if not c.isspace()):
            raise ValueError("selected font lacks required glyphs")


def _native_border(border):
    if not isinstance(border, Mapping) or set(border) != {"color", "alpha", "width"}:
        raise ValueError("border requires exactly color, alpha and width")
    color = border["color"]
    if (not isinstance(color, list) or len(color) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in color)):
        raise ValueError("border color must contain three finite values in [0,1]")
    for key, maximum in (("alpha", 1), ("width", 100)):
        value = border[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
            raise ValueError(f"border {key} must be finite in [0,{maximum}]")
    from .animation_recipes import _ensure_vendored_import_paths
    _ensure_vendored_import_paths()
    from pyJianYingDraft.text_segment import TextBorder
    return TextBorder(color=tuple(color), alpha=border["alpha"], width=border["width"]).export_json()


def _apply_spans(payload, spans):
    """Split existing native runs at exact, nonoverlapping, text-checked ranges."""
    text = payload["text"]
    if any(ord(c) > 0xffff for c in text):
        raise ValueError("span styling currently requires BMP text")
    if not isinstance(spans, list) or not spans:
        raise ValueError("spans must be a nonempty list")
    cursor = 0
    for style in payload["styles"]:
        start, end = style.get("range", [None, None])
        if start != cursor or not isinstance(end, int) or end <= start or end > len(text):
            raise ValueError("existing style ranges must partition the text")
        cursor = end
    if cursor != len(text):
        raise ValueError("existing style ranges must cover the text")
    checked = []
    for span in spans:
        if set(span) - {"range", "text", "size_multiplier", "font", "color", "bold"}:
            raise ValueError("unsupported span field")
        bounds = span.get("range")
        if not isinstance(bounds, list) or len(bounds) != 2 or any(type(v) is not int for v in bounds):
            raise ValueError("span range must have two integer endpoints")
        start, end = bounds
        if not 0 <= start < end <= len(text) or text[start:end] != span.get("text"):
            raise ValueError("span text/range mismatch")
        if any(start < b and end > a for a, b, _ in checked):
            raise ValueError("overlapping spans")
        multiplier = span.get("size_multiplier", 1.0)
        if type(multiplier) not in (float, int) or not math.isfinite(multiplier) or not 0.5 <= multiplier <= 2.0:
            raise ValueError("span size_multiplier must be finite in [0.5,2]")
        if "bold" in span and type(span["bold"]) is not bool:
            raise ValueError("span bold must be boolean")
        if "color" in span:
            color = span["color"]
            if not isinstance(color, list) or len(color) != 3 or any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in color):
                raise ValueError("invalid span color")
        if "font" in span:
            _validate_font(span["font"], text[start:end])
        checked.append((start, end, span))
    output = []
    for style in payload["styles"]:
        start, end = style["range"]
        cuts = sorted({start, end, *(v for a, b, _ in checked for v in (a, b) if start < v < end)})
        for left, right in zip(cuts, cuts[1:]):
            run = copy.deepcopy(style)
            run["range"] = [left, right]
            span = next((s for a, b, s in checked if a <= left and right <= b), None)
            if span:
                run["size"] = float(run["size"]) * span.get("size_multiplier", 1.0)
                if "font" in span:
                    run["font"] = {k: span["font"][k] for k in ("id", "path")}
                if "bold" in span:
                    run["bold"] = span["bold"]
                if "color" in span:
                    run["fill"] = {"content": {"render_type": "solid", "solid": {"color": span["color"]}}}
                    run["useLetterColor"] = True
            output.append(run)
    payload["styles"] = output


def apply_text_style_variant(draft: dict, spec: Mapping, context: Any):
    authorization = spec.get("authorization_source")
    if not authorization or not spec.get("design_reason"):
        raise ValueError("style variant requires authorization_source and design_reason")
    if authorization != context.plan.get("text_style_authorization_source"):
        raise ValueError("style variant authorization mismatch")
    state = json.loads(Path(context.plan["project_state"]).read_text("utf-8-sig"))
    if not any(a.get("option") == "text_style_revision" and a.get("enabled") is True
               and a.get("source") == authorization for a in state.get("current_authorizations", [])):
        raise ValueError("style variant lacks current project authorization")
    changes = spec.get("tracks") or []
    if not isinstance(changes, list) or not changes or any(not isinstance(c, Mapping) for c in changes):
        raise ValueError("style variant requires explicit text targets")
    targets = []
    for change in changes:
        name, segment_id = change.get("track_name"), change.get("segment_id")
        if not isinstance(name, str) or not name.strip() or ("segment_id" in change and
                (not isinstance(segment_id, str) or not segment_id.strip())):
            raise ValueError("style target requires an exact track and optional nonempty segment_id")
        if any(name == old_name and (segment_id is None or old_id is None or segment_id == old_id)
               for old_name, old_id in targets):
            raise ValueError("style variant requires unique nonoverlapping targets")
        targets.append((name, segment_id))
    result = copy.deepcopy(draft)
    materials = {m["id"]: m for m in result["materials"]["texts"]}
    rows = []
    for change in changes:
        allowed = {"track_name", "segment_id", "font", "color", "italic", "shadow_alpha", "shadow_distance", "remove_strokes", "spans", "letter_spacing", "alignment", "border"}
        if set(change) - allowed:
            raise ValueError("unsupported style field")
        if "border" in change and change.get("remove_strokes") is True:
            raise ValueError("border conflicts with remove_strokes=true")
        border = _native_border(change["border"]) if "border" in change else None
        selected = [t for t in result["tracks"] if t.get("name") == change.get("track_name") and t.get("type") == "text"]
        if len(selected) != 1 or not selected[0].get("segments"):
            raise ValueError("style target must be one existing nonempty text track")
        segments = selected[0]["segments"]
        if "segment_id" in change:
            segments = [s for s in segments if s.get("id") == change["segment_id"]]
            if len(segments) != 1:
                raise ValueError("style segment_id must match one existing text segment")
        font = change.get("font")
        if font is not None:
            path = Path(font["path"])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != font.get("sha256") or not isinstance(font.get("id"), str):
                raise ValueError("style font must be a bound local resource")
        # Zero removes native tracking without assuming a pixels-per-unit
        # conversion for nonzero Jianying letter spacing.
        if "letter_spacing" in change and (type(change["letter_spacing"]) not in (int, float) or change["letter_spacing"] != 0):
            raise ValueError("style letter_spacing currently supports only explicit zero")
        # Native TextStyle alignment: 0 left, 1 center, 2 right. Omitting the
        # field preserves the original preset's paragraph layout.
        if "alignment" in change and (type(change["alignment"]) is not int or change["alignment"] not in (0, 1, 2)):
            raise ValueError("style alignment must be an integer in {0,1,2}")
        color = change.get("color")
        if color is not None and (not isinstance(color, list) or len(color) != 3 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in color)):
            raise ValueError("style color must contain three finite values in [0,1]")
        for key, limit in (("shadow_alpha", 1), ("shadow_distance", 100)):
            if key in change and (isinstance(change[key], bool) or not isinstance(change[key], (int, float)) or
                                  not math.isfinite(change[key]) or not 0 <= change[key] <= limit):
                raise ValueError("invalid shadow parameter")
        for key in ("italic", "remove_strokes"):
            if key in change and not isinstance(change[key], bool):
                raise ValueError("style flags must be boolean")
        for segment in segments:
            material = materials[segment["material_id"]]
            # A shared material must not silently change an unselected track.
            owners = [t for t in result["tracks"] if any(s.get("material_id") == material["id"] for s in t.get("segments", []))]
            if len(owners) != 1:
                raise ValueError("style material is shared by multiple tracks")
            if "segment_id" in change and sum(s.get("material_id") == material["id"]
                    for s in selected[0]["segments"]) != 1:
                raise ValueError("style material is shared by another segment")
            payload = json.loads(material["content"])
            if not payload.get("styles"):
                raise ValueError("style target has no text styles")
            if font:
                _validate_font(font, payload["text"])
                material["font_path"] = font["path"]
                material["font_id"] = font["id"]
                material["font_resource_id"] = font["id"]
                # These describe the replaced font, not the style or motion.
                # Match the native exporter's empty resource-list defaults;
                # each content run below carries the selected local font.
                material["fonts"] = []
                for key in ("font_name", "font_category_id", "font_category_name", "font_team_id", "font_third_resource_id", "font_url"):
                    if key in material:
                        material[key] = ""
                if "font_title" in material:
                    material["font_title"] = "none"
                if "font_source_platform" in material:
                    material["font_source_platform"] = 0
            if "letter_spacing" in change:
                material["letter_spacing"] = 0.0
            if "alignment" in change:
                material["alignment"] = change["alignment"]
            # Native drafts may mirror run shadows on the material. Keep
            # explicit edits consistent without creating a missing effect.
            has_existing_shadow = bool(material.get("has_shadow")) or any(
                style.get("shadows") for style in payload["styles"]
            )
            for key in ("shadow_alpha", "shadow_distance"):
                if key in change and key in material:
                    material[key] = change[key]
            if "shadow_alpha" in change and "has_shadow" in material:
                material["has_shadow"] = has_existing_shadow and change["shadow_alpha"] > 0
            for style in payload["styles"]:
                if font:
                    style["font"] = {"id": font["id"], "path": font["path"]}
                if color is not None:
                    style["fill"] = {"content": {"render_type": "solid", "solid": {"color": color}}}
                    style["useLetterColor"] = True
                if "italic" in change:
                    style["italic"] = change["italic"]
                if change.get("remove_strokes"):
                    style["strokes"] = []
                if border is not None:
                    style["strokes"] = [copy.deepcopy(border)]
                for shadow in style.get("shadows", []):
                    for source, target in (("shadow_alpha", "alpha"), ("shadow_distance", "distance")):
                        if source in change:
                            shadow[target] = change[source]
            if "spans" in change:
                _apply_spans(payload, change["spans"])
                material["font_size"] = max(s["size"] for s in payload["styles"])
            material["content"] = json.dumps(payload, ensure_ascii=False)
            rows.append({"track_name": selected[0]["name"], "material_id": material["id"], "text": payload["text"], "style_run_count": len(payload["styles"])})
    return result, {"changed_segments": rows, "native_motion_modified": False, "native_visual_qa": "pending"}
