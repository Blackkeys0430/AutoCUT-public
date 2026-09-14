from __future__ import annotations

import argparse
import json
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()

    videos = sorted(args.directory.glob("*.mp4"))
    model_path = Path(r"E:\AutoCUT\models\faster-whisper-large-v3-turbo")
    model = WhisperModel(
        str(model_path),
        device="cuda",
        compute_type="int8_float16",
        local_files_only=True,
    )
    for video in videos:
        segments, info = model.transcribe(
            str(video),
            language="zh",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
        )
        rows = [
            {
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": segment.text.strip(),
            }
            for segment in segments
        ]
        payload = {
            "video": video.name,
            "language": info.language,
            "duration": info.duration,
            "segments": rows,
        }
        video.with_suffix(".transcript.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        video.with_suffix(".transcript.txt").write_text(
            "\n".join(
                f"[{row['start']:07.2f} --> {row['end']:07.2f}] {row['text']}"
                for row in rows
            ),
            encoding="utf-8",
        )
        print(video.name, len(rows), "segments")


if __name__ == "__main__":
    main()
