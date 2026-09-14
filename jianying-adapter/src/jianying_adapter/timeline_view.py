"""On-demand filmstrip/waveform inspection for a bounded video interval."""

from __future__ import annotations

import subprocess
import struct
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

from .transcript import TimedWord, load_timed_words_file


WINDOWS_FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\simsun.ttc"),
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
)


def choose_font(size: int, candidates: Sequence[Path] = WINDOWS_FONT_CANDIDATES) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in candidates:
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def timeline_x(time_s: float, start_s: float, end_s: float, left: int, width: int) -> int:
    if end_s <= start_s:
        return left
    ratio = max(0.0, min(1.0, (time_s - start_s) / (end_s - start_s)))
    return left + round(ratio * width)


def clip_words_to_window(words: Iterable[TimedWord], start_s: float, end_s: float) -> tuple[TimedWord, ...]:
    if end_s <= start_s:
        return ()
    clipped: list[TimedWord] = []
    for word in words:
        if word.end <= start_s or word.start >= end_s:
            continue
        clipped.append(
            TimedWord(
                max(word.start, start_s),
                min(word.end, end_s),
                word.text,
                word.speaker,
                word.source_path,
            )
        )
    return tuple(sorted(clipped, key=lambda word: (word.start, word.end)))


def silence_spans(
    words: Iterable[TimedWord],
    threshold: float = 0.4,
    window: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], ...]:
    if threshold < 0:
        raise ValueError("threshold must be non-negative")
    ordered = tuple(sorted(words, key=lambda word: (word.start, word.end)))
    spans = [
        (previous.end, current.start)
        for previous, current in zip(ordered, ordered[1:])
        if current.start - previous.end >= threshold
    ]
    if window is None:
        return tuple(spans)
    start_s, end_s = window
    visible = clip_words_to_window(ordered, start_s, end_s)
    if not visible:
        return ((start_s, end_s),) if end_s - start_s >= threshold else ()
    spans = [
        (start_s, visible[0].start),
        *[
            (previous.end, current.start)
            for previous, current in zip(visible, visible[1:])
        ],
        (visible[-1].end, end_s),
    ]
    return tuple((left, right) for left, right in spans if right - left >= threshold)


def _decode_pcm16(data: bytes) -> list[int]:
    usable = len(data) - (len(data) % 2)
    return list(struct.unpack(f"<{usable // 2}h", data[:usable])) if usable else []


def _run(command: list[str], **kwargs: Any) -> Any:
    return subprocess.run(command, check=True, **kwargs)


def _extract_frames(
    video: Path,
    start_s: float,
    duration_s: float,
    n_frames: int,
    ffmpeg: str,
    directory: Path,
    runner: Callable[..., Any],
) -> list[Path]:
    fps = n_frames / duration_s
    pattern = directory / "frame-%03d.png"
    runner(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start_s:.6f}", "-t", f"{duration_s:.6f}",
         "-i", str(video), "-vf", f"fps={fps:.8f}", "-frames:v", str(n_frames), "-y", str(pattern)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return sorted(directory.glob("frame-*.png"))[:n_frames]


def _extract_audio(
    video: Path,
    start_s: float,
    duration_s: float,
    ffmpeg: str,
    runner: Callable[..., Any],
) -> list[int]:
    result = runner(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start_s:.6f}", "-t", f"{duration_s:.6f}",
         "-i", str(video), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return _decode_pcm16(getattr(result, "stdout", b"") or b"")


def draw_waveform(draw: ImageDraw.ImageDraw, samples: Sequence[int], box: tuple[int, int, int, int], color: str = "#63D7C8") -> None:
    left, top, right, bottom = box
    draw.rectangle(box, fill="#172127", outline="#3B5058")
    center = (top + bottom) // 2
    half = max(1, (bottom - top) // 2 - 5)
    if not samples:
        draw.line((left, center, right, center), fill="#527078", width=1)
        return
    columns = max(1, right - left)
    for column in range(columns):
        begin = column * len(samples) // columns
        end = max(begin + 1, (column + 1) * len(samples) // columns)
        peak = max(abs(value) for value in samples[begin:end]) / 32768.0
        height = max(1, round(peak * half))
        x = left + column
        draw.line((x, center - height, x, center + height), fill=color, width=1)


def draw_timeline(
    frames: Sequence[Image.Image],
    samples: Sequence[int],
    words: Sequence[TimedWord],
    start_s: float,
    end_s: float,
    *,
    width: int = 1600,
    frame_height: int = 330,
) -> Image.Image:
    margin = 36
    waveform_top = frame_height + 28
    waveform_bottom = waveform_top + 150
    height = waveform_bottom + 88
    image = Image.new("RGBA", (width, height), "#0E1519")
    draw = ImageDraw.Draw(image)
    label_font = choose_font(22)
    small_font = choose_font(18)
    usable_width = width - margin * 2
    visible_words = clip_words_to_window(words, start_s, end_s)
    if frames:
        cell_width = usable_width // len(frames)
        for index, frame in enumerate(frames):
            frame = frame.convert("RGB")
            frame.thumbnail((cell_width - 6, frame_height - 6), Image.Resampling.LANCZOS)
            x = margin + index * cell_width + (cell_width - frame.width) // 2
            y = 3 + (frame_height - frame.height) // 2
            image.paste(frame, (x, y))
            draw.rectangle((margin + index * cell_width, 0, margin + (index + 1) * cell_width - 2, frame_height), outline="#31434A")
    else:
        draw.rectangle((margin, 0, width - margin, frame_height), outline="#31434A")
        draw.text((margin + 12, 12), "no frames", font=label_font, fill="#AFC1C5")
    draw_waveform(draw, samples, (margin, waveform_top, width - margin, waveform_bottom))
    draw.text((margin, waveform_bottom + 12), f"{start_s:.3f}s", font=small_font, fill="#C5D5D8")
    end_label = f"{end_s:.3f}s"
    end_box = draw.textbbox((0, 0), end_label, font=small_font)
    draw.text((width - margin - (end_box[2] - end_box[0]), waveform_bottom + 12), end_label, font=small_font, fill="#C5D5D8")
    for second in range(int(start_s) + 1, int(end_s) + 1):
        x = timeline_x(second, start_s, end_s, margin, usable_width)
        draw.line((x, waveform_bottom + 38, x, waveform_bottom + 55), fill="#80969B", width=1)
        draw.text((x + 3, waveform_bottom + 57), f"{second}s", font=small_font, fill="#80969B")
    for silence_start, silence_end in silence_spans(visible_words, window=(start_s, end_s)):
        left = timeline_x(silence_start, start_s, end_s, margin, usable_width)
        right = timeline_x(silence_end, start_s, end_s, margin, usable_width)
        if right > left:
            draw.rectangle((left, waveform_top, right, waveform_bottom), fill="#784E2B40")
    label_right_edges = [margin - 4, margin - 4, margin - 4]
    for word in visible_words:
        x = timeline_x(word.start, start_s, end_s, margin, usable_width)
        draw.line((x, waveform_top, x, waveform_bottom), fill="#F2C94C", width=1)
        label = word.text.strip()
        if label:
            label_box = draw.textbbox((0, 0), label, font=small_font)
            label_width = label_box[2] - label_box[0]
            if label_width > usable_width:
                continue
            label_x = min(max(margin, x + 4), width - margin - label_width)
            for row, right_edge in enumerate(label_right_edges):
                if label_x >= right_edge + 4:
                    draw.text((label_x, waveform_top + 4 + row * 24), label, font=small_font, fill="#F2C94C")
                    label_right_edges[row] = label_x + label_width
                    break
    return image


def render_timeline_view(
    video: str | Path,
    start_s: float,
    end_s: float,
    output: str | Path,
    *,
    transcript: str | Path | None = None,
    n_frames: int = 8,
    ffmpeg: str = "ffmpeg",
    runner: Callable[..., Any] = _run,
) -> Path:
    video_path = Path(video)
    if end_s <= start_s:
        raise ValueError("END must be greater than START")
    if n_frames <= 0:
        raise ValueError("n_frames must be positive")
    words = load_timed_words_file(transcript, source_path=video_path) if transcript else ()
    words = clip_words_to_window(words, start_s, end_s)
    duration_s = end_s - start_s
    with tempfile.TemporaryDirectory(prefix="jianying-timeline-") as temp:
        frame_paths = _extract_frames(video_path, start_s, duration_s, n_frames, ffmpeg, Path(temp), runner)
        samples = _extract_audio(video_path, start_s, duration_s, ffmpeg, runner)
        frames = [Image.open(path) for path in frame_paths]
        try:
            image = draw_timeline(frames, samples, words, start_s, end_s)
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            image.convert("RGB").save(output_path)
        finally:
            for frame in frames:
                frame.close()
    return Path(output)
