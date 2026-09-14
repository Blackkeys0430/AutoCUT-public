from __future__ import annotations

import json
from pathlib import Path

from jianying_adapter.codec import DecodedDocument, JsonCodec
from jianying_adapter.validate import (
    validate_content_mirrors,
    validate_media_exists,
    validate_reference_graph,
    validate_subtitle_track_order,
    validate_timeline_directories,
)


def graph_inputs():
    timeline = "SYNTH-TIMELINE-001"
    return {
        "content": {"id": timeline},
        "meta": {"draft_id": "SYNTH-DRAFT-001"},
        "project": {
            "id": "SYNTH-PROJECT-001",
            "main_timeline_id": timeline,
            "timelines": [{"id": timeline}],
        },
        "timeline_layout": {
            "activeTimeline": timeline,
            "dockItems": [{"timelineIds": [timeline]}],
        },
        "timeline_directory_ids": [timeline],
    }


def test_reference_graph_success():
    assert validate_reference_graph(**graph_inputs()).ok


def test_reference_graph_dangling_fails():
    values = graph_inputs()
    values["timeline_directory_ids"] = []
    report = validate_reference_graph(**values)
    assert not report.ok
    assert any(issue.code == "reference_graph_invalid" for issue in report.issues)


def test_timeline_directory_requires_nonempty_mirror(tmp_path: Path):
    timeline = "SYNTH-TIMELINE-001"
    directory = tmp_path / "Timelines" / timeline
    directory.mkdir(parents=True)
    project = {"timelines": [{"id": timeline}]}
    report = validate_timeline_directories(tmp_path, project)
    assert not report.ok
    assert report.issues[0].code == "timeline_content_missing"
    (directory / "draft_content.json").write_text("{}", encoding="utf-8")
    assert validate_timeline_directories(tmp_path, project).ok


def test_content_mirrors_with_fake_codec(tmp_path: Path):
    expected = {"id": "SYNTH-TIMELINE-001", "tracks": []}
    mirror = tmp_path / "mirror.json"
    mirror.write_text(json.dumps(expected), encoding="utf-8")

    class FakeEncodedCodec(JsonCodec):
        def read(self, path):
            value = super().read(path).value
            return DecodedDocument(value, True)

    report = validate_content_mirrors(expected, [mirror], FakeEncodedCodec())
    assert report.ok
    assert report.facts["mirrors"][0]["encoded"] is True


def test_missing_media_report():
    content = {
        "materials": {
            "videos": [
                {"id": "SYNTH-MAT-A", "path": "SYNTH/exists.mp4"},
                {"id": "SYNTH-MAT-B", "path": "SYNTH/missing.mp4"},
            ]
        }
    }
    report = validate_media_exists(content, exists=lambda value: value.endswith("exists.mp4"))
    assert not report.ok
    assert report.facts["missing_media"] == ["SYNTH/missing.mp4"]


def test_subtitle_tracks_are_above_populated_visual_tracks_and_empty_visual_is_ignored():
    content = {
        "tracks": [
            {"type": "video", "name": "V8_A_ROLL", "segments": [{"id": "a"}]},
            {"type": "video", "name": "", "segments": []},
            {"type": "text", "name": "V8_ORDINARY_ZH", "segments": [{"id": "zh"}]},
            {"type": "text", "name": "重点包装文字", "segments": [{"id": "callout"}]},
        ]
    }
    report = validate_subtitle_track_order(content)
    assert report.ok
    assert report.facts["subtitle_track_indices"] == [2]

    content["tracks"].append({"type": "video", "name": "V8_PIP", "segments": [{"id": "pip"}]})
    report = validate_subtitle_track_order(content)
    assert not report.ok
    assert report.issues[0].code == "subtitle_track_below_visual"


def test_subtitle_name_variants_are_recognized_without_treating_all_text_as_subtitles():
    content = {
        "tracks": [
            {"type": "video", "name": "A-roll", "segments": [{"id": "a"}]},
            {"type": "text", "name": "ORDINARY_EN", "segments": [{"id": "en"}]},
            {"type": "text", "name": "JY_ZH", "segments": [{"id": "zh"}]},
            {"type": "text", "name": "包装标题", "segments": [{"id": "title"}]},
        ]
    }
    report = validate_subtitle_track_order(content)
    assert report.ok
    assert report.facts["subtitle_track_indices"] == [1, 2]
