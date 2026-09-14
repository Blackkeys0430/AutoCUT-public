"""Prepare, recover resources, or install Combination packages; never writes timeline drafts."""
import argparse
import json
import sys
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[1]
for folder in (ADAPTER / "src", ADAPTER / "vendor/python-deps"):
    sys.path.insert(0, str(folder))
from jianying_adapter.preset_install import apply, prepare, bind_catalog, read_json, write_json
from jianying_adapter.preset_resources import recover


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    for flag in ("source-root", "target-root", "cache-root", "workdir"):
        prep.add_argument("--" + flag, required=True, type=Path)
    prep.add_argument("--resource-root", action="append", default=[], type=Path)
    prep.add_argument("--resource-evidence", action="append", default=[], type=Path)
    install = sub.add_parser("apply")
    install.add_argument("--manifest", required=True, type=Path)
    recovery = sub.add_parser("recover", help="recover exact free resources into a new E-drive workspace")
    recovery.add_argument("--manifest", required=True, type=Path)
    recovery.add_argument("--workdir", required=True, type=Path)
    catalog = sub.add_parser("bind-catalog", help="bind existing automatic template IDs to verified installed resources")
    catalog.add_argument("--catalog", required=True, type=Path)
    catalog.add_argument("--manifest", required=True, action="append", type=Path)
    catalog.add_argument("--output", required=True, type=Path)
    catalog.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        result = prepare(args.source_root, args.target_root, args.cache_root, args.workdir, tuple(args.resource_root), tuple(args.resource_evidence))
    elif args.mode == "recover":
        result = recover(args.manifest, args.workdir)
    elif args.mode == "bind-catalog":
        bound, result = bind_catalog(read_json(args.catalog), [read_json(path) for path in args.manifest])
        write_json(args.output, bound)
        write_json(args.report, result)
        result = {"bound_count": result["bound_count"], "skipped_count": len(result["skipped"]),
                  "output": str(args.output), "report": str(args.report), "production_qualification_changed": False}
    else:
        result = apply(args.manifest)
    print(json.dumps(result.get("summary", result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
