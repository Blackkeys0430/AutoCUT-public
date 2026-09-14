"""Disable Jianying's known close-to-tray preference after a process-zero gate.

This repairs one known preference only. It does not prove that Jianying 8.8 will
leave no hidden processes after a later normal close.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ADAPTER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ADAPTER_ROOT / "src"))

from jianying_adapter.jianying_settings import repair_real_quit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-data", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()
    result = repair_real_quit(args.user_data, args.backup_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
