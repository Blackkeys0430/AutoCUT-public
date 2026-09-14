from __future__ import annotations

import copy
import json

import pytest

from jianying_adapter.subtitles import (
    KeywordSpan,
    SubtitleCard,
    SubtitlePlan,
    build_bilingual_subtitles,
    validate_subtitle_plan,
)


def _plan() -> SubtitlePlan:
    return SubtitlePlan(
        zh_template_track="ZH_TEMPLATE",
        en_template_track="EN_TEMPLATE",
        cards=(
            SubtitleCard(0, 1_000_000, "重点内容", "Key point", (KeywordSpan(0, 2),)),
            SubtitleCard(1_200_000, 800_000, "第二句", "Second line"),
        ),
    )


def test_builds_two_tracks_with_many_cards_and_preserves_native_templates(subtitle_content: dict) -> None:
    ids = iter(f"NEW-{index}" for index in range(20))
    original_video_track = copy.deepcopy(subtitle_content["tracks"][0])
    result = build_bilingual_subtitles(subtitle_content, _plan(), id_factory=lambda: next(ids))
    output = result.content
    text_tracks = [track for track in output["tracks"] if track["type"] == "text"]
    assert len(text_tracks) == 2
    assert [len(track["segments"]) for track in text_tracks] == [2, 2]
    assert output["tracks"][0] == original_video_track
    assert text_tracks[0]["native_track"] == "keep-zh"
    assert text_tracks[1]["native_track"] == "keep-en"
    assert [segment["target_timerange"] for segment in text_tracks[0]["segments"]] == [
        {"start": 0, "duration": 1_000_000},
        {"start": 1_200_000, "duration": 800_000},
    ]


def test_keyword_style_is_yellow_and_larger(subtitle_content: dict) -> None:
    ids = iter(f"KEY-{index}" for index in range(20))
    output = build_bilingual_subtitles(subtitle_content, _plan(), id_factory=lambda: next(ids)).content
    zh_track = next(track for track in output["tracks"] if track.get("name") == "JY_ZH_SUBTITLES")
    material_id = zh_track["segments"][0]["material_id"]
    material = next(item for item in output["materials"]["texts"] if item["id"] == material_id)
    styles = json.loads(material["content"])["styles"]
    assert styles[0]["range"] == [0, 2]
    assert styles[0]["size"] > styles[1]["size"]
    assert styles[0]["fill"]["content"]["solid"]["color"] == pytest.approx([1.0, 214 / 255, 0.0])
    assert material["native"] == "keep-zh"


@pytest.mark.parametrize(
    "plan",
    [
        SubtitlePlan("ZH_TEMPLATE", "EN_TEMPLATE", (SubtitleCard(0, 10, "中", "A"), SubtitleCard(5, 10, "文", "B"))),
        SubtitlePlan("ZH_TEMPLATE", "EN_TEMPLATE", (SubtitleCard(0, 10, "中文", "A", (KeywordSpan(0, 3),)),)),
        SubtitlePlan("ZH_TEMPLATE", "EN_TEMPLATE", (SubtitleCard(0, 10, "中文", "A", (KeywordSpan(0, 2), KeywordSpan(1, 2))),)),
    ],
)
def test_invalid_or_overlapping_plan_is_rejected(plan: SubtitlePlan) -> None:
    with pytest.raises(ValueError):
        validate_subtitle_plan(plan)
