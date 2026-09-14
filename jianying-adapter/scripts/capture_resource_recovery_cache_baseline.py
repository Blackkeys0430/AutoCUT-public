"""Capture a read-only cache-presence baseline immediately before opening a batch."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_phase1_resource_recovery_batches import capture_cache_baseline, read_json, write_json  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    baseline = capture_cache_baseline(args.cache_root, read_json(args.manifest))
    baseline["captured_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json(args.output, baseline)
    print(json.dumps({"output": str(args.output), "present": len(baseline["entries"]), "absent": len(baseline["absent_entries"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

