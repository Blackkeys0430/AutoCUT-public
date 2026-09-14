"""Batch-transcribe local talking-head videos with word timestamps.

The script reads source media in place and writes all analysis artifacts to a
separate output directory. It never edits or copies the input media.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Optional source filename to include; repeat for multiple files.",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--vad-off-review",
        action="store_true",
        help="Run a second pass without VAD to expose fillers and false starts.",
    )
    return parser.parse_args()


def rounded(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


def load_model(model_path: Path) -> tuple[WhisperModel, str, str]:
    errors: list[str] = []
    for device, compute_type in (("cuda", "int8_float16"), ("cpu", "int8")):
        try:
            return (
                WhisperModel(
                    str(model_path),
                    device=device,
                    compute_type=compute_type,
                    local_files_only=True,
                ),
                device,
                compute_type,
            )
        except Exception as exc:
            errors.append(f"{device}/{compute_type}: {type(exc).__name__}: {exc}")
    raise RuntimeError("Unable to load local model:\n" + "\n".join(errors))


def transcribe_one(
    model: WhisperModel, video: Path, *, vad_filter: bool = True
) -> dict[str, Any]:
    initial_prompt = None
    if not vad_filter:
        initial_prompt = (
            "这是未经剪辑的普通话口播。请逐词记录，不要润色或省略，必须保留"
            "呃、嗯、啊、那个、重复、半句重来、口误和停顿附近的词。"
        )
    segments_iter, info = model.transcribe(
        str(video),
        language="zh",
        task="transcribe",
        beam_size=5 if vad_filter else 8,
        word_timestamps=True,
        vad_filter=vad_filter,
        condition_on_previous_text=False,
        temperature=0,
        repetition_penalty=1.15,
        no_repeat_ngram_size=3,
        compression_ratio_threshold=2.2,
        hallucination_silence_threshold=1.0,
        initial_prompt=initial_prompt,
    )
    segments: list[dict[str, Any]] = []
    for segment in segments_iter:
        words: list[dict[str, Any]] = []
        for item in segment.words or []:
            text = str(item.word or "").strip()
            if not text:
                continue
            words.append(
                {
                    "start": rounded(item.start),
                    "end": rounded(item.end),
                    "word": text,
                    "probability": round(float(item.probability or 0.0), 4),
                }
            )
        text = str(segment.text or "").strip()
        if not text and words:
            text = "".join(word["word"] for word in words)
        if not text:
            continue
        segments.append(
            {
                "start": rounded(segment.start),
                "end": rounded(segment.end),
                "text": text,
                "words": words,
            }
        )
    return {
        "source_path": str(video.resolve()),
        "duration": rounded(info.duration),
        "language": info.language,
        "segments": segments,
    }


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    model_path = args.model.resolve()
    aggregate_path = output_dir / "transcriptions.json"
    index_path = output_dir / "index.txt"

    if not input_dir.is_dir():
        raise FileNotFoundError(input_dir)
    if not model_path.is_dir():
        raise FileNotFoundError(model_path)
    if not args.overwrite and (aggregate_path.exists() or index_path.exists()):
        raise FileExistsError(f"Output already exists under {output_dir}")

    include = {name.casefold() for name in args.include}
    videos = sorted(
        (path for path in input_dir.iterdir() if path.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda path: path.name.lower(),
    )
    if include:
        videos = [video for video in videos if video.name.casefold() in include]
    if not videos:
        raise FileNotFoundError(f"No supported video files found under {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    model, device, compute_type = load_model(model_path)
    results: list[dict[str, Any]] = []
    vad_off_results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, video in enumerate(videos, start=1):
        print(f"[{index}/{len(videos)}] {video.name}", flush=True)
        try:
            results.append(transcribe_one(model, video))
            if args.vad_off_review:
                print(f"  VAD-off review: {video.name}", flush=True)
                vad_off_results.append(transcribe_one(model, video, vad_filter=False))
        except Exception as exc:
            failures.append(
                {"source_path": str(video.resolve()), "error": f"{type(exc).__name__}: {exc}"}
            )

    payload = {
        "schema_version": "talking_head_batch_transcription_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_dir": str(input_dir),
        "model": {
            "name": model_path.name,
            "path": str(model_path),
            "device": device,
            "compute_type": compute_type,
            "language": "zh",
            "word_timestamps": True,
            "vad_filter": True,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "requested_file_count": len(videos),
        "success_count": len(results),
        "failure_count": len(failures),
        "files": results,
        "failures": failures,
    }
    aggregate_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.vad_off_review:
        (output_dir / "filler_review_vad_off.json").write_text(
            json.dumps(
                {
                    "schema_version": "talking_head_vad_off_review_v1",
                    "created_at": payload["created_at"],
                    "model": payload["model"],
                    "instruction": "VAD关闭；用于发现语气词、重复、半句重来和口误，不替代语义判断。",
                    "files": vad_off_results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    lines = [
        "正规口播新素材｜本地逐词转写索引",
        f"模型: {model_path.name} ({device}/{compute_type})",
        f"成功/失败: {len(results)}/{len(failures)}",
        "",
    ]
    for result in results:
        lines.append(f"## {Path(result['source_path']).name}")
        lines.append(
            f"duration={result['duration']}s | segments={len(result['segments'])} | "
            f"words={sum(len(segment['words']) for segment in result['segments'])}"
        )
        lines.extend(
            f"[{segment['start']:07.3f} --> {segment['end']:07.3f}] {segment['text']}"
            for segment in result["segments"]
        )
        lines.append("")
    if failures:
        lines.append("## FAILED")
        lines.extend(f"{item['source_path']} | {item['error']}" for item in failures)
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "success": len(results),
        "failed": len(failures),
        "elapsed_seconds": payload["elapsed_seconds"],
        "output": str(aggregate_path),
    }, ensure_ascii=False), flush=True)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
