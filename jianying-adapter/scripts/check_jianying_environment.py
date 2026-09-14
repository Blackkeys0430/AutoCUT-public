"""Read-only Jianying 8.8 environment gate; prints one structured JSON report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jianying_adapter.jianying_environment import inspect_jianying_environment  # noqa: E402


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=("before_launch", "after_launch"))
    parser.add_argument("--profile-kind", required=True, choices=("production", "test"))
    parser.add_argument("--expected-exe", type=Path, required=True)
    parser.add_argument("--logical-root", type=Path, required=True)
    parser.add_argument("--physical-root", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument(
        "--computer-use-proof",
        type=Path,
        help="可选诊断 JSON；仅开启 --require-computer-use-proof 时读取并参与检查",
    )
    parser.add_argument(
        "--require-computer-use-proof",
        action="store_true",
        help="显式启用 CUA/SKY/Doctor 诊断门禁",
    )
    parser.add_argument("--target-draft-name")
    parser.add_argument(
        "--registration-expectation", choices=("absent", "unique"), default="absent"
    )
    parser.add_argument("--lock-expectation", choices=("none", "target_locked"), default="none")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.require_computer_use_proof and args.computer_use_proof is None:
        parser.error("--require-computer-use-proof 需要同时提供 --computer-use-proof")
    if args.computer_use_proof is not None and not args.require_computer_use_proof:
        parser.error("--computer-use-proof 需要同时启用 --require-computer-use-proof")
    report = inspect_jianying_environment(
        stage=args.stage,
        profile_kind=args.profile_kind,
        expected_exe=args.expected_exe,
        logical_root=args.logical_root,
        physical_root=args.physical_root,
        profiles_root=args.profiles_root,
        computer_use_proof=(read_object(args.computer_use_proof) if args.require_computer_use_proof else None),
        require_computer_use_proof=args.require_computer_use_proof,
        target_draft_name=args.target_draft_name,
        registration_expectation=args.registration_expectation,
        lock_expectation=args.lock_expectation,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
