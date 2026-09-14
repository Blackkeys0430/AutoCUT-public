from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def draft_root_alias(tmp_path: Path):
    """A real local directory alias, never linked to the user's active drafts."""
    physical = tmp_path / "E-physical"
    physical.mkdir()
    logical = tmp_path / "C-logical"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(logical), str(physical)],
                       check=True, capture_output=True)
    else:
        logical.symlink_to(physical, target_is_directory=True)
    assert os.path.samefile(logical, physical)
    return logical, physical


def add_subtitle_templates(content: dict) -> dict:
    styled = {
        "text": "TEMPLATE",
        "styles": [
            {
                "range": [0, 8],
                "size": 13.0,
                "bold": True,
                "fill": {"content": {"solid": {"color": [1.0, 1.0, 1.0], "alpha": 1.0}}},
            }
        ],
    }
    content.setdefault("materials", {})["texts"] = [
        {"id": "SYNTH-ZH-MATERIAL", "type": "subtitle", "content": json.dumps(styled), "native": "keep-zh"},
        {"id": "SYNTH-EN-MATERIAL", "type": "subtitle", "content": json.dumps(styled), "native": "keep-en"},
    ]
    content.setdefault("tracks", []).extend(
        [
            {
                "id": "SYNTH-ZH-TEMPLATE",
                "type": "text",
                "name": "ZH_TEMPLATE",
                "native_track": "keep-zh",
                "segments": [
                    {
                        "id": "SYNTH-ZH-SEGMENT",
                        "material_id": "SYNTH-ZH-MATERIAL",
                        "target_timerange": {"start": 0, "duration": 1_000_000},
                        "clip": {"transform": {"y": -0.67}},
                    }
                ],
            },
            {
                "id": "SYNTH-EN-TEMPLATE",
                "type": "text",
                "name": "EN_TEMPLATE",
                "native_track": "keep-en",
                "segments": [
                    {
                        "id": "SYNTH-EN-SEGMENT",
                        "material_id": "SYNTH-EN-MATERIAL",
                        "target_timerange": {"start": 0, "duration": 1_000_000},
                        "clip": {"transform": {"y": -0.58}},
                    }
                ],
            },
        ]
    )
    return content


@pytest.fixture
def synthetic_content() -> dict:
    return {
        "id": "SYNTH-TIMELINE-001",
        "duration": 5_000_000,
        "render_index_track_mode_on": True,
        "canvas_config": {"width": 1080, "height": 1920},
        "config": {"synthetic": True},
        "materials": {"videos": [{"id": "SYNTH-MAT-001", "type": "video"}]},
        "tracks": [
            {
                "id": "SYNTH-TRACK-001",
                "type": "video",
                "segments": [{"id": "SYNTH-SEG-001", "material_id": "SYNTH-MAT-001"}],
            }
        ],
    }


@pytest.fixture
def synthetic_draft(tmp_path: Path, synthetic_content: dict) -> Path:
    root = tmp_path / "SYNTH-DRAFT"
    timeline_id = synthetic_content["id"]
    timeline = root / "Timelines" / timeline_id
    timeline.mkdir(parents=True)
    (root / "draft_content.json").write_text(json.dumps(synthetic_content), encoding="utf-8")
    (timeline / "draft_content.json").write_text(json.dumps(copy.deepcopy(synthetic_content)), encoding="utf-8")
    (root / "draft_meta_info.json").write_text(
        json.dumps({"draft_id": "SYNTH-DRAFT-001", "draft_name": "SYNTH Draft"}),
        encoding="utf-8",
    )
    (root / "timeline_layout.json").write_text(
        json.dumps({"activeTimeline": timeline_id, "dockItems": [{"timelineIds": [timeline_id]}]}),
        encoding="utf-8",
    )
    (root / "Timelines" / "project.json").write_text(
        json.dumps(
            {
                "id": "SYNTH-PROJECT-001",
                "main_timeline_id": timeline_id,
                "timelines": [{"id": timeline_id}],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def subtitle_content(synthetic_content: dict) -> dict:
    return add_subtitle_templates(copy.deepcopy(synthetic_content))
