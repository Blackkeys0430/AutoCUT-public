"""Word-boundary helpers that do not alter the project's EDL schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .transcript import TimedWord


@dataclass(frozen=True)
class SafeCut:
    start: float
    end: float
    padding: float


def _validate_padding(padding_ms: int | float) -> float:
    padding = float(padding_ms) / 1000.0
    if not 0.03 <= padding <= 0.2:
        raise ValueError("padding must be between 30 and 200 milliseconds")
    return padding


def snap_cut_to_words(
    candidate_start: float,
    candidate_end: float,
    words: Iterable[TimedWord],
    *,
    padding_ms: int | float = 30,
    media_duration: float | None = None,
) -> SafeCut:
    """Snap to the first/last covered word and add bounded edge padding."""

    if candidate_end <= candidate_start:
        raise ValueError("candidate_end must be greater than candidate_start")
    padding = _validate_padding(padding_ms)
    ordered = tuple(sorted(words, key=lambda word: (word.start, word.end)))
    covered = tuple(word for word in ordered if word.end > candidate_start and word.start < candidate_end)
    if not covered:
        raise ValueError("candidate range does not intersect any timed word")
    start = max(0.0, covered[0].start - padding)
    end = covered[-1].end + padding
    if media_duration is not None:
        if media_duration < 0:
            raise ValueError("media_duration must be non-negative")
        end = min(end, float(media_duration))
    if end <= start:
        raise ValueError("padding/clamp leaves an empty cut")
    return SafeCut(start, end, padding)


def clamp_fade_duration(duration_us: int, fade_us: int = 30_000) -> int:
    """Clamp each side of a fade so in+out never exceeds a short segment."""

    if duration_us < 0 or fade_us < 0:
        raise ValueError("durations must be non-negative")
    return min(fade_us, duration_us // 2)


def safe_audio_fade(duration_us: int, fade_us: int = 30_000) -> tuple[int, int]:
    value = clamp_fade_duration(duration_us, fade_us)
    return value, value
