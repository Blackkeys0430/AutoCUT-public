from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "jianying-adapter"
sys.path.insert(0, str(ADAPTER / "src"))

from jianying_adapter.preset_registry import _inner_drafts  # noqa: E402


CATALOG = ADAPTER / "preset_catalog" / "all_auto_candidates_v1.json"
OUTPUT = ADAPTER / "preset_catalog" / "visual_gate_candidates_v1.json"


def preview_size(path: Path | None) -> tuple[int, int] | None:
    if path is None or not path.exists():
        return None
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def required_slots(candidate: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [
        slot
        for slot in candidate.get("slots", {}).get(kind, [])
        if isinstance(slot, Mapping)
        and slot.get("required")
        and not slot.get("decorative_locked")
    ]


def inner_canvas(draft: Mapping[str, Any]) -> dict[str, Any]:
    config = draft.get("canvas_config", {})
    if not isinstance(config, Mapping):
        return {}
    return {
        "width": config.get("width"),
        "height": config.get("height"),
        "ratio": config.get("ratio"),
    }


def segment_geometry(draft: Mapping[str, Any], locator: Mapping[str, Any]) -> list[dict[str, Any]]:
    tracks = draft.get("tracks", [])
    result: list[dict[str, Any]] = []
    for ref in locator.get("segment_refs", []):
        try:
            track = tracks[int(ref["track_index"])]
            segment = track.get("segments", [])[int(ref["segment_index"])]
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        clip = segment.get("clip", {}) if isinstance(segment, Mapping) else {}
        transform = clip.get("transform", {}) if isinstance(clip, Mapping) else {}
        result.append(
            {
                "track_index": ref.get("track_index"),
                "segment_index": ref.get("segment_index"),
                "start": ref.get("start"),
                "duration": ref.get("duration"),
                "render_index": segment.get("render_index"),
                "scale": transform.get("scale"),
                "scale_x": transform.get("scale_x"),
                "scale_y": transform.get("scale_y"),
                "x": transform.get("x"),
                "y": transform.get("y"),
                "rotation": transform.get("rotation"),
                "keyframe_refs": segment.get("common_keyframes"),
            }
        )
    return result


def iter_locators(slot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    locators = slot.get("locators")
    if isinstance(locators, list):
        return [item for item in locators if isinstance(item, Mapping)]
    locator = slot.get("locator")
    return [locator] if isinstance(locator, Mapping) else []


def is_vertical(canvas: Mapping[str, Any], size: tuple[int, int] | None) -> bool:
    width = canvas.get("width")
    height = canvas.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
        return height / width >= 1.6
    if size:
        return size[1] / size[0] >= 1.6
    return False


def main() -> None:
    registry = json.loads(CATALOG.read_text(encoding="utf-8"))
    preset_root = Path(registry["preset_root"])
    title_rows: list[dict[str, Any]] = []
    pip_rows: list[dict[str, Any]] = []

    for candidate in registry.get("candidates", []):
        source = preset_root / candidate["source_path"]
        try:
            payload = json.loads(source.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        drafts = _inner_drafts(payload)
        if not drafts:
            continue
        preview = preset_root / candidate["preview_path"] if candidate.get("preview_path") else None
        size = preview_size(preview)
        canvases = [inner_canvas(draft) for draft in drafts]
        vertical = any(is_vertical(canvas, size) for canvas in canvases)
        text_slots = required_slots(candidate, "text")
        video_slots = required_slots(candidate, "video")
        image_slots = required_slots(candidate, "image")

        if vertical and len(text_slots) == 1:
            text = str(text_slots[0].get("default_text") or "")
            if len(text.strip()) >= 7:
                title_rows.append(
                    {
                        "template_id": candidate.get("template_id"),
                        "display_name": candidate.get("display_name"),
                        "source_path": candidate.get("source_path"),
                        "preview_path": candidate.get("preview_path"),
                        "preview_size": size,
                        "canvases": canvases,
                        "default_text": text,
                        "default_text_length": len(text.strip()),
                        "font_size": text_slots[0].get("default_style", {}).get("font_size"),
                        "missing_path_count": candidate.get("dependencies", {}).get("missing_path_count"),
                        "semantic_tags": candidate.get("semantic_tags", []),
                    }
                )

        visual_slots = [*video_slots, *image_slots]
        if vertical and len(visual_slots) >= 2:
            slot_geometries: list[dict[str, Any]] = []
            for slot in visual_slots:
                geometries: list[dict[str, Any]] = []
                for locator in iter_locators(slot):
                    draft_index = int(locator.get("inner_draft_index", 0))
                    if 0 <= draft_index < len(drafts):
                        geometries.extend(segment_geometry(drafts[draft_index], locator))
                slot_geometries.append(
                    {
                        "slot_id": slot.get("slot_id"),
                        "kind": slot.get("kind"),
                        "default_resource": slot.get("default_resource"),
                        "geometries": geometries,
                    }
                )
            pip_rows.append(
                {
                    "template_id": candidate.get("template_id"),
                    "display_name": candidate.get("display_name"),
                    "source_path": candidate.get("source_path"),
                    "preview_path": candidate.get("preview_path"),
                    "preview_size": size,
                    "canvases": canvases,
                    "visual_slot_count": len(visual_slots),
                    "slots": slot_geometries,
                    "missing_path_count": candidate.get("dependencies", {}).get("missing_path_count"),
                    "semantic_tags": candidate.get("semantic_tags", []),
                }
            )

    title_rows.sort(key=lambda row: (row["missing_path_count"] or 0, -row["default_text_length"], row["display_name"]))
    pip_rows.sort(key=lambda row: (row["missing_path_count"] or 0, row["visual_slot_count"], row["display_name"]))
    output = {
        "schema": "visual_gate_candidates_v1",
        "title_candidates": title_rows,
        "pip_candidates": pip_rows,
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "title_candidates": len(title_rows), "pip_candidates": len(pip_rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
