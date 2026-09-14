"""Finalize one objectively accepted draft in the active Jianying 8.8 test profile."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jianying_adapter.test_draft_finalizer import (  # noqa: E402
    build_test_draft_finalization_plan,
    finalize_test_draft,
    read_object,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draft-name", required=True)
    parser.add_argument("--expected-exe", type=Path, required=True)
    parser.add_argument("--logical-root", type=Path, required=True)
    parser.add_argument("--physical-root", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    purpose = parser.add_mutually_exclusive_group(required=True)
    purpose.add_argument("--acceptance-evidence", type=Path)
    purpose.add_argument("--cleanup-request", type=Path,
                         help="Explicit user cleanup with exact draft_name, draft_id and authorization_source; no visual acceptance claim")
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--quarantine-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = build_test_draft_finalization_plan(
        draft_name=args.draft_name,
        expected_exe=args.expected_exe,
        logical_root=args.logical_root,
        physical_root=args.physical_root,
        profiles_root=args.profiles_root,
        acceptance_evidence=read_object(args.acceptance_evidence) if args.acceptance_evidence else {},
        cleanup_request=read_object(args.cleanup_request) if args.cleanup_request else None,
        backup_root=args.backup_root,
        quarantine_root=args.quarantine_root,
        report_path=args.report,
    )
    if not args.apply:
        print(json.dumps({
            "status": "dry_run_ok",
            "profile_kind": plan["profile_kind"],
            "draft_name": plan["draft_name"],
            "environment_gate": plan["environment"],
            "acceptance_evidence": plan["acceptance_evidence"],
            "mutations_performed": False,
        }, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(finalize_test_draft(plan), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
