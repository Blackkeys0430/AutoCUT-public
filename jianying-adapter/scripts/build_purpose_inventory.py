"""Build the complete read-only purpose inventory for local Jianying presets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from jianying_adapter.preset_registry import build_purpose_inventory, write_purpose_inventory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="为全部剪映预设生成用途分类，不修改任何源预设或草稿")
    parser.add_argument("catalog", type=Path, help="preset_catalog/raw_scan_v1.json")
    parser.add_argument("preset_root", type=Path, help="1947份预设所在根目录")
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8-sig"))
    inventory = build_purpose_inventory(catalog, args.preset_root)
    write_purpose_inventory(inventory, args.json_output, args.markdown_output)
    return 0 if not inventory["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
