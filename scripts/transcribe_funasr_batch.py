"""Transcribe local media with local FunASR models and native word timestamps.

No model download or source media modification is performed. Each input receives
an independent JSON artifact, suitable for the shared semantic evidence reader.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def native_segments(result: list[dict]) -> list[dict]:
    """Expose native milliseconds as adapter seconds without estimating timings.

    Chinese character stamps map only when their count exactly matches the
    punctuation-free text. Other tokenizations retain native sentence timing.
    The complete provider response remains unchanged in ``result``.
    """
    segments = []
    for row in result:
        for sentence in row.get("sentence_info", []):
            segment = {"start": sentence["start"] / 1000,
                       "end": sentence["end"] / 1000,
                       "text": sentence["text"]}
            chars = [char for char in sentence["text"]
                     if not char.isspace() and not unicodedata.category(char).startswith("P")]
            stamps = sentence.get("timestamp", [])
            if chars and len(chars) == len(stamps):
                segment["words"] = [
                    {"word": char, "start": pair[0] / 1000, "end": pair[1] / 1000}
                    for char, pair in zip(chars, stamps)
                ]
            segments.append(segment)
    if not segments:
        raise ValueError("FunASR did not return native sentence_info timing")
    return segments


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--vad-model", required=True, type=Path)
    parser.add_argument("--punc-model", required=True, type=Path)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--hotwords", default="")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    sources = [path.resolve() for path in args.inputs]
    for path in sources:
        if not path.is_file():
            raise FileNotFoundError(path)
    for path in (args.model, args.vad_model, args.punc_model):
        if not path.is_dir():
            raise FileNotFoundError(path)
    outputs = [args.output_dir / f"{path.stem}.funasr.json" for path in sources]
    if len({str(path.resolve()).casefold() for path in outputs}) != len(outputs):
        parser.error("Input stems must be unique within a batch")
    for output in outputs:
        if output.exists() and not args.overwrite:
            raise FileExistsError(output)

    from funasr import AutoModel

    model = AutoModel(
        model=str(args.model.resolve()),
        vad_model=str(args.vad_model.resolve()),
        punc_model=str(args.punc_model.resolve()),
        device=args.device,
        disable_update=True,
        disable_pbar=True,
        vad_kwargs={"max_single_segment_time": 30000},
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for source, output in zip(sources, outputs):
        started = time.perf_counter()
        result = model.generate(
            input=str(source), hotword=args.hotwords,
            batch_size_s=120, merge_vad=True, merge_length_s=15,
            sentence_timestamp=True, return_raw_text=True,
        )
        if not result or not any(item.get("text") for item in result):
            raise RuntimeError(f"No transcript returned for {source}")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        payload = {
            "schema_version": "funasr_local_transcription_v1",
            "engine": "funasr-paraformer-zh",
            "source_path": str(source), "media_sha256": digest,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": str(args.model.resolve()),
            "vad_model": str(args.vad_model.resolve()),
            "punc_model": str(args.punc_model.resolve()),
            "device": args.device, "hotwords": args.hotwords,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "result": result,
            "files": [{"source_path": str(source), "segments": native_segments(result)}],
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"source": str(source), "output": str(output.resolve()),
                          "elapsed_seconds": payload["elapsed_seconds"]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
