"""Small, provider-neutral helpers for packing word-timed transcripts."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_PACKED_LINE_RE = re.compile(r"^\s*\[\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)\s*\]\s*(.*?)\s*$")


@dataclass(frozen=True)
class TimedWord:
    start: float
    end: float
    text: str
    speaker: str | None = None
    source_path: str | None = None


@dataclass(frozen=True)
class _Phrase:
    start: float
    end: float
    text: str


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _word_text(item: Mapping[str, Any]) -> str:
    value = item.get("word", item.get("text", ""))
    return str(value).strip() if value is not None else ""


def _speaker_value(item: Mapping[str, Any], inherited_speaker: str | None = None) -> str | None:
    value = item.get("speaker")
    if value is None:
        value = item.get("speaker_id")
    if value is None:
        value = inherited_speaker
    return str(value) if value is not None else None


def _as_word(
    item: Any,
    inherited_speaker: str | None = None,
    source_path: str | None = None,
) -> TimedWord | None:
    if not isinstance(item, Mapping):
        return None
    start = _number(item.get("start"))
    end = _number(item.get("end"))
    text = _word_text(item)
    if start is None or end is None or end <= start or not text:
        return None
    return TimedWord(start, end, text, _speaker_value(item, inherited_speaker), source_path)


def _segment_words(segment: Mapping[str, Any], source_path: str | None = None) -> list[TimedWord]:
    inherited_speaker = _speaker_value(segment)
    raw_words = segment.get("words")
    if isinstance(raw_words, list):
        return [word for item in raw_words if (word := _as_word(item, inherited_speaker, source_path))]
    fallback = _as_word(
        {
            "start": segment.get("start"),
            "end": segment.get("end"),
            "text": segment.get("text", ""),
            "speaker": inherited_speaker,
        },
        source_path=source_path,
    )
    return [fallback] if fallback else []


def _segment_phrase(segment: Mapping[str, Any], source_path: str | None = None) -> _Phrase | None:
    raw_words = segment.get("words")
    words = []
    if isinstance(raw_words, list):
        inherited_speaker = _speaker_value(segment)
        words = [
            word for item in raw_words
            if (word := _as_word(item, inherited_speaker, source_path))
        ]
    if words:
        text = join_word_text(words)
        if text:
            return _Phrase(words[0].start, words[-1].end, text)

    start = _number(segment.get("start"))
    end = _number(segment.get("end"))
    text_value = segment.get("text")
    text = _clean_token(str(text_value)) if text_value is not None else ""
    if start is None or end is None or end <= start or not text:
        return None
    return _Phrase(start, end, text)


def load_timed_words(value: Mapping[str, Any] | list[Any]) -> tuple[TimedWord, ...]:
    """Read the project's segment/files schemas and flat ``words`` schema.

    If a segment has no word list, its own interval is retained as one timed
    item. This keeps timeline-view useful for segment-only transcripts.
    """

    if isinstance(value, list):
        items = [word for item in value if (word := _as_word(item))]
        return tuple(sorted(items, key=lambda word: (word.start, word.end)))
    if not isinstance(value, Mapping):
        return ()

    items: list[TimedWord] = []
    raw_files = value.get("files")
    if isinstance(raw_files, list):
        for file_item in raw_files:
            if isinstance(file_item, Mapping):
                items.extend(_file_words(file_item))
    raw_segments = value.get("segments")
    if isinstance(raw_segments, list):
        for segment in raw_segments:
            if isinstance(segment, Mapping):
                items.extend(_segment_words(segment))
    raw_words = value.get("words")
    if isinstance(raw_words, list):
        items.extend(word for item in raw_words if (word := _as_word(item, _speaker_value(value))))
    return tuple(sorted(items, key=lambda word: (word.start, word.end)))


def _file_source(file_item: Mapping[str, Any]) -> str:
    for key in ("source_path", "file", "path"):
        value = file_item.get(key)
        if value:
            return str(value)
    return ""


def _file_words(file_item: Mapping[str, Any]) -> tuple[TimedWord, ...]:
    source_path = _file_source(file_item)
    items: list[TimedWord] = []
    segments = file_item.get("segments")
    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, Mapping):
                items.extend(_segment_words(segment, source_path))
    raw_words = file_item.get("words")
    if isinstance(raw_words, list):
        items.extend(
            word for item in raw_words
            if (word := _as_word(item, _speaker_value(file_item), source_path))
        )
    return tuple(sorted(items, key=lambda word: (word.start, word.end)))


def _segment_phrases(segments: Any, source_path: str | None = None) -> tuple[_Phrase, ...]:
    if not isinstance(segments, list):
        return ()
    phrases = [
        phrase
        for segment in segments
        if isinstance(segment, Mapping)
        and (phrase := _segment_phrase(segment, source_path))
    ]
    return tuple(phrases)


def read_transcript(path: str | Path) -> Mapping[str, Any] | list[Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))


def _select_file_words(value: Mapping[str, Any], source_path: str | Path | None) -> tuple[TimedWord, ...]:
    files = [item for item in value.get("files", []) if isinstance(item, Mapping)]
    if not files:
        return load_timed_words(value)
    if len(files) == 1:
        return _file_words(files[0])
    if source_path is None:
        raise ValueError("files transcript contains multiple sources; source_path is required for timeline-view")

    target_key = _normalized_path(source_path)
    exact = [item for item in files if _file_source(item) and _normalized_path(_file_source(item)) == target_key]
    if len(exact) == 1:
        return _file_words(exact[0])
    if len(exact) > 1:
        raise ValueError(f"files transcript source_path is not unique for {source_path}")

    target_name = Path(source_path).name.casefold()
    basename_matches = [
        item for item in files
        if _file_source(item) and Path(_file_source(item)).name.casefold() == target_name
    ]
    if len(basename_matches) == 1:
        return _file_words(basename_matches[0])
    raise ValueError(f"files transcript cannot uniquely match video source: {source_path}")


def load_timed_words_file(
    path: str | Path,
    source_path: str | Path | None = None,
) -> tuple[TimedWord, ...]:
    """Load JSON timings, selecting one source from multi-file input when needed."""

    source = Path(path)
    if source.suffix.casefold() not in {".md", ".markdown"}:
        value = read_transcript(source)
        if isinstance(value, Mapping) and isinstance(value.get("files"), list):
            return _select_file_words(value, source_path)
        return load_timed_words(value)
    words: list[TimedWord] = []
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        match = _PACKED_LINE_RE.match(line)
        if not match or not match.group(3):
            continue
        start, end = float(match.group(1)), float(match.group(2))
        if end > start:
            words.append(TimedWord(start, end, match.group(3)))
    return tuple(words)


def _clean_token(text: str) -> str:
    if _CJK_RE.search(text):
        return "".join(text.split())
    return " ".join(text.split())


def join_word_text(words: Iterable[TimedWord]) -> str:
    result = ""
    previous = ""
    for word in words:
        token = _clean_token(word.text)
        if not token:
            continue
        if not result:
            result = token
        elif _CJK_RE.search(previous) or _CJK_RE.search(token):
            result += token
        elif token[0] in ",.!?;:，。！？；：、)]}" or previous.endswith(("(", "[", "{")):
            result += token
        else:
            result += " " + token
        previous = token
    return result.strip()


def _packed_groups(words: tuple[TimedWord, ...], silence_threshold: float) -> list[list[TimedWord]]:
    if silence_threshold < 0:
        raise ValueError("silence_threshold must be non-negative")
    groups: list[list[TimedWord]] = []
    for word in words:
        if not groups:
            groups.append([word])
            continue
        previous = groups[-1][-1]
        speaker_changed = (
            previous.speaker is not None
            and word.speaker is not None
            and previous.speaker != word.speaker
        )
        if word.start - previous.end >= silence_threshold or speaker_changed:
            groups.append([word])
        else:
            groups[-1].append(word)
    return groups


def pack_transcript(value: Mapping[str, Any] | list[Any], silence_threshold: float = 0.5) -> str:
    lines = ["# Packed transcript", ""]

    def append_phrases(phrases: Iterable[_Phrase]) -> None:
        for phrase in phrases:
            lines.append(f"[{phrase.start:.3f}-{phrase.end:.3f}] {phrase.text}")

    def append_flat_words(words: tuple[TimedWord, ...]) -> None:
        for group in _packed_groups(words, silence_threshold):
            text = join_word_text(group)
            if text:
                lines.append(f"[{group[0].start:.3f}-{group[-1].end:.3f}] {text}")

    if isinstance(value, Mapping) and isinstance(value.get("files"), list):
        section_index = 0
        for index, file_item in enumerate(value["files"]):
            if not isinstance(file_item, Mapping):
                continue
            source = _file_source(file_item) or f"file-{index + 1}"
            if section_index or lines[-1] != "":
                lines.append("")
            lines.append(f"## {source}")
            lines.append("")
            segments = file_item.get("segments")
            if isinstance(segments, list):
                append_phrases(_segment_phrases(segments, source))
            else:
                append_flat_words(_file_words(file_item))
            section_index += 1

        root_segments = value.get("segments")
        root_words = value.get("words")
        if isinstance(root_segments, list):
            if section_index or lines[-1] != "":
                lines.append("")
            lines.append("## root")
            lines.append("")
            append_phrases(_segment_phrases(root_segments))
        elif isinstance(root_words, list):
            if section_index or lines[-1] != "":
                lines.append("")
            lines.append("## root")
            lines.append("")
            append_flat_words(tuple(word for item in root_words if (word := _as_word(item, _speaker_value(value)))))
    elif isinstance(value, Mapping) and isinstance(value.get("segments"), list):
        append_phrases(_segment_phrases(value["segments"]))
    else:
        append_flat_words(load_timed_words(value))
    return "\n".join(lines) + "\n"


def pack_transcript_file(input_path: str | Path, output_path: str | Path, silence_threshold: float = 0.5) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(pack_transcript(read_transcript(input_path), silence_threshold), encoding="utf-8")
    return output
