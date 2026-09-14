from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import jianying_adapter.timeline_view as timeline_view_module
from jianying_adapter.cut_safety import clamp_fade_duration, safe_audio_fade, snap_cut_to_words
from jianying_adapter.timeline_view import draw_timeline, silence_spans, timeline_x, render_timeline_view
from jianying_adapter.transcript import TimedWord, load_timed_words, pack_transcript


def test_pack_transcript_segments_schema_joins_chinese_without_spaces() -> None:
    value = {
        "segments": [
            {"start": 0.0, "end": 0.8, "words": [
                {"start": 0.0, "end": 0.3, "word": "你"},
                {"start": 0.3, "end": 0.8, "text": "好"},
            ]},
            {"start": 1.5, "end": 1.9, "words": [
                {"start": 1.5, "end": 1.9, "word": "世界"},
            ]},
        ]
    }
    packed = pack_transcript(value, silence_threshold=0.5)
    assert "[0.000-0.800] 你好" in packed
    assert "[1.500-1.900] 世界" in packed
    assert "你 好" not in packed


def test_zero_gap_segments_remain_two_phrase_lines() -> None:
    value = {
        "segments": [
            {"start": 0.0, "end": 1.0, "words": [{"start": 0.0, "end": 1.0, "word": "第一句"}]},
            {"start": 1.0, "end": 2.0, "words": [{"start": 1.0, "end": 2.0, "word": "第二句"}]},
        ]
    }
    packed = pack_transcript(value)
    assert "[0.000-1.000] 第一句" in packed
    assert "[1.000-2.000] 第二句" in packed
    assert packed.count("[") == 2


def test_pack_transcript_files_schema_and_flat_words_support_speaker_change() -> None:
    value = {
        "files": [{
            "source_path": "a.mp4",
            "segments": [{"start": 0.0, "end": 0.2, "speaker": "A", "words": [
                {"start": 0.0, "end": 0.2, "word": "Hello"},
            ]}],
        }],
        "words": [
            {"start": 0.3, "end": 0.5, "word": "world", "speaker": "B"},
        ],
    }
    words = load_timed_words(value)
    assert [word.text for word in words] == ["Hello", "world"]
    packed = pack_transcript(value)
    assert "[0.000-0.200] Hello" in packed
    assert "[0.300-0.500] world" in packed


def test_speaker_id_is_supported_at_segment_and_word_levels() -> None:
    value = {
        "segments": [{"start": 0.0, "end": 0.2, "speaker_id": "segment-A", "words": [
            {"start": 0.0, "end": 0.1, "word": "甲"},
            {"start": 0.1, "end": 0.2, "word": "乙", "speaker_id": "word-B"},
        ]}],
    }
    value["segments"].append({"start": 0.3, "end": 0.4, "speaker": "segment-C", "words": [
        {"start": 0.3, "end": 0.4, "word": "丙", "speaker": None, "speaker_id": None},
    ]})
    words = load_timed_words(value)
    assert [word.speaker for word in words] == ["segment-A", "word-B", "segment-C"]


def test_cut_safety_snaps_and_clamps_padding() -> None:
    words = (TimedWord(1.0, 1.2, "甲"), TimedWord(1.3, 1.5, "乙"))
    cut = snap_cut_to_words(1.1, 1.4, words, padding_ms=100, media_duration=1.55)
    assert cut.start == 0.9
    assert cut.end == 1.55
    assert safe_audio_fade(40_000) == (20_000, 20_000)
    assert clamp_fade_duration(1_000_000) == 30_000


def test_timeline_pure_layout_and_mocked_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    words = (TimedWord(0.0, 0.2, "甲"), TimedWord(0.8, 1.0, "乙"))
    assert timeline_x(-1, 0, 1, 10, 100) == 10
    assert timeline_x(2, 0, 1, 10, 100) == 110
    assert silence_spans(words) == ((0.2, 0.8),)
    assert silence_spans(words, window=(0, 1.5)) == ((0.2, 0.8), (1.0, 1.5))
    image = draw_timeline([Image.new("RGB", (100, 60), "red")], [], words, 0, 1)
    assert image.size[0] == 1600

    def fake_runner(command, **kwargs):
        if "s16le" in command:
            return SimpleNamespace(stdout=struct.pack("<hhhh", 0, 1000, -1000, 0))
        pattern = Path(command[-1])
        frame_path = Path(str(pattern).replace("%03d", "001"))
        Image.new("RGB", (160, 90), "blue").save(frame_path)
        return SimpleNamespace(stdout=b"")

    output = tmp_path / "timeline.png"
    render_timeline_view("input.mp4", 0, 1, output, n_frames=1, runner=fake_runner)
    assert output.is_file()
    with Image.open(output) as rendered:
        assert rendered.size == (1600, 596)


def test_timeline_view_only_passes_window_words_and_draws_labels(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "long.json"
    transcript.write_text(
        '''{"words": [
          {"start": 0, "end": 1, "word": "before"},
          {"start": 10, "end": 10.2, "word": "in-window"},
          {"start": 10.8, "end": 11, "word": "also-window"},
          {"start": 40, "end": 41, "word": "after"}
        ]}''',
        encoding="utf-8",
    )
    seen: list[TimedWord] = []
    real_draw_timeline = timeline_view_module.draw_timeline

    def capture(frames, samples, words, start_s, end_s, **kwargs):
        seen.extend(words)
        return real_draw_timeline(frames, samples, words, start_s, end_s, **kwargs)

    monkeypatch.setattr(timeline_view_module, "draw_timeline", capture)
    draw_calls: list[str] = []
    real_draw = timeline_view_module.ImageDraw.Draw

    def recording_draw(image):
        draw = real_draw(image)
        original_text = draw.text

        def text(position, value, *args, **kwargs):
            draw_calls.append(str(value))
            return original_text(position, value, *args, **kwargs)

        draw.text = text
        return draw

    monkeypatch.setattr(timeline_view_module.ImageDraw, "Draw", recording_draw)

    def fake_runner(command, **kwargs):
        if "s16le" in command:
            return SimpleNamespace(stdout=struct.pack("<hh", 0, 1000))
        pattern = Path(command[-1])
        Image.new("RGB", (160, 90), "blue").save(Path(str(pattern).replace("%03d", "001")))
        return SimpleNamespace(stdout=b"")

    output = tmp_path / "window.png"
    render_timeline_view("input.mp4", 10, 12, output, transcript=transcript, n_frames=1, runner=fake_runner)
    assert [word.text for word in seen] == ["in-window", "also-window"]
    assert "in-window" in draw_calls
    assert "also-window" in draw_calls
    assert "before" not in draw_calls
    assert "after" not in draw_calls


def test_dense_word_labels_rotate_rows_instead_of_overlapping(monkeypatch) -> None:
    real_draw = timeline_view_module.ImageDraw.Draw
    calls: list[tuple[str, tuple[int, int]]] = []

    def recording_draw(image):
        draw = real_draw(image)
        original_text = draw.text

        def text(position, value, *args, **kwargs):
            calls.append((str(value), position))
            return original_text(position, value, *args, **kwargs)

        draw.text = text
        return draw

    monkeypatch.setattr(timeline_view_module.ImageDraw, "Draw", recording_draw)
    words = (
        TimedWord(0.10, 0.11, "AAA"),
        TimedWord(0.11, 0.12, "BBB"),
        TimedWord(0.12, 0.13, "CCC"),
    )
    draw_timeline([], [], words, 0, 1, width=500)
    positions = {value: position for value, position in calls if value in {"AAA", "BBB", "CCC"}}
    assert set(positions) == {"AAA", "BBB", "CCC"}
    assert len({position[1] for position in positions.values()}) == 3


def test_files_with_overlapping_times_are_separate_sections() -> None:
    value = {
        "files": [
            {"source_path": "source-a.mp4", "segments": [{"start": 0, "end": 1, "text": "甲"}]},
            {"source_path": "source-b.mp4", "segments": [{"start": 0, "end": 1, "text": "乙"}]},
        ]
    }
    packed = pack_transcript(value)
    assert "## source-a.mp4" in packed
    assert "## source-b.mp4" in packed
    assert packed.index("## source-a.mp4") < packed.index("## source-b.mp4")
    assert packed.index("[0.000-1.000] 甲") < packed.index("## source-b.mp4")


def test_files_with_overlapping_flat_words_keep_their_own_sections() -> None:
    value = {
        "files": [
            {"source_path": "a.wav", "words": [{"start": 0, "end": 0.2, "word": "one"}]},
            {"source_path": "b.wav", "words": [{"start": 0, "end": 0.2, "word": "two"}]},
        ]
    }
    packed = pack_transcript(value)
    a_section, b_section = packed.split("## b.wav")
    assert "## a.wav" in a_section
    assert "one" in a_section
    assert "two" not in a_section
    assert "two" in b_section


def test_timeline_view_selects_matching_file_and_rejects_ambiguous_multi_file(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "files.json"
    transcript.write_text(
        '''{"files": [
          {"source_path": "C:/clips/a.mp4", "words": [{"start": 10, "end": 10.2, "word": "A"}]},
          {"source_path": "D:/clips/b.mp4", "words": [{"start": 10, "end": 10.2, "word": "B"}]}
        ]}''',
        encoding="utf-8",
    )
    selected: list[TimedWord] = []
    real_draw_timeline = timeline_view_module.draw_timeline

    def capture(frames, samples, words, start_s, end_s, **kwargs):
        selected.extend(words)
        return real_draw_timeline(frames, samples, words, start_s, end_s, **kwargs)

    monkeypatch.setattr(timeline_view_module, "draw_timeline", capture)

    def fake_runner(command, **kwargs):
        if "s16le" in command:
            return SimpleNamespace(stdout=struct.pack("<hh", 0, 1000))
        pattern = Path(command[-1])
        Image.new("RGB", (160, 90), "blue").save(Path(str(pattern).replace("%03d", "001")))
        return SimpleNamespace(stdout=b"")

    output = tmp_path / "selected.png"
    render_timeline_view("D:/clips/b.mp4", 10, 11, output, transcript=transcript, n_frames=1, runner=fake_runner)
    assert [word.text for word in selected] == ["B"]
    selected.clear()
    render_timeline_view("b.mp4", 10, 11, tmp_path / "basename.png", transcript=transcript, n_frames=1, runner=fake_runner)
    assert [word.text for word in selected] == ["B"]
    try:
        render_timeline_view("same.mp4", 0, 1, tmp_path / "ambiguous.png", transcript=transcript, n_frames=1, runner=fake_runner)
    except ValueError as exc:
        assert "uniquely match" in str(exc)
    else:
        raise AssertionError("ambiguous multi-file transcript should fail")
