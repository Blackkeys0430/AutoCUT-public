"""Build the deterministic production ledger for all 285 phase-1 presets."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ADAPTER = Path(__file__).resolve().parents[1]
PROJECT = ADAPTER.parent
TRIAL = PROJECT / "video_trials/字幕预设黑幕筛选_20260902"
BATCH_ROOT = TRIAL / "phase1_resource_recovery_batches"
DEFAULT_QUEUE = BATCH_ROOT / "phase1_285_queue_plan.json"
DEFAULT_SINGLE = TRIAL / "resource_recovery_probes/JIANYING-25-05_post_open_runtime_audit.json"
DEFAULT_BATCH_REPORTS = tuple(
    BATCH_ROOT / f"batch_{index:02d}_post_open_runtime_audit.json"
    for index in range(1, 7)
)
DEFAULT_OUTPUT = BATCH_ROOT / "phase1_285_post_open_runtime_ledger.json"
KNOWN_STATUSES = (
    "already_existing_before_baseline",
    "rewritten_to_existing_local",
    "newly_materialized_after_baseline",
    "unchanged_missing",
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _template_row(template_id: str, summary: Mapping[str, Any], source: Path) -> dict[str, Any]:
    dependencies = int(summary.get("dependency_count") or 0)
    closed = int(summary.get("closed_count") or 0)
    unresolved = int(summary.get("unresolved_count") or 0)
    if closed + unresolved != dependencies:
        raise ValueError(f"{template_id} dependency arithmetic mismatch in {source}")
    fully = summary.get("fully_recovered") is True
    status = str(summary.get("recovery_status") or "")
    if fully != (status == "fully_recovered"):
        raise ValueError(f"{template_id} recovery status mismatch in {source}")
    status_counts = Counter({str(key): int(value) for key, value in (summary.get("status_counts") or {}).items()})
    # Older per-template batch summaries keep status counts inside by_kind.
    if not status_counts:
        for kind_summary in (summary.get("by_kind") or {}).values():
            if isinstance(kind_summary, Mapping):
                status_counts.update(
                    {str(key): int(value) for key, value in (kind_summary.get("status_counts") or {}).items()}
                )
    if sum(status_counts.values()) != dependencies:
        raise ValueError(f"{template_id} status count mismatch in {source}")
    unknown = sorted(set(status_counts) - set(KNOWN_STATUSES))
    if unknown:
        raise ValueError(f"{template_id} has unknown runtime statuses: {unknown}")
    return {
        "template_id": template_id,
        "dependency_count": dependencies,
        "closed_count": closed,
        "unresolved_count": unresolved,
        "recovery_status": status,
        "fully_recovered": fully,
        "status_counts": {key: status_counts.get(key, 0) for key in KNOWN_STATUSES},
        "source_audit_file": str(source),
    }


def build_ledger(
    queue_path: Path = DEFAULT_QUEUE,
    single_path: Path = DEFAULT_SINGLE,
    batch_paths: Sequence[Path] = DEFAULT_BATCH_REPORTS,
) -> dict[str, Any]:
    queue = read_json(queue_path)
    ordered_ids = [str(item.get("template_id") or "") for item in queue.get("items", []) if isinstance(item, Mapping)]
    if len(ordered_ids) != 285 or len(set(ordered_ids)) != 285 or "" in ordered_ids:
        raise ValueError("phase1 queue must contain exactly 285 unique non-empty template IDs")

    collected: list[dict[str, Any]] = []
    single = read_json(single_path)
    collected.append(_template_row(str(single.get("template_id") or ""), single.get("summary") or {}, single_path))
    for path in batch_paths:
        report = read_json(path)
        for item in report.get("templates", []) or []:
            if not isinstance(item, Mapping):
                continue
            collected.append(
                _template_row(str(item.get("template_id") or ""), item.get("summary") or {}, path)
            )

    result_ids = [row["template_id"] for row in collected]
    counts = Counter(result_ids)
    duplicates = sorted(template_id for template_id, count in counts.items() if count > 1)
    missing = sorted(set(ordered_ids) - set(result_ids))
    unexpected = sorted(set(result_ids) - set(ordered_ids))
    if len(collected) != 285 or duplicates or missing or unexpected:
        raise ValueError(
            f"phase1 result coverage failed: rows={len(collected)}, duplicates={duplicates}, "
            f"missing={missing}, unexpected={unexpected}"
        )
    by_id = {row["template_id"]: row for row in collected}
    ordered_rows = [by_id[template_id] for template_id in ordered_ids]
    aggregate_statuses: Counter[str] = Counter()
    for row in ordered_rows:
        aggregate_statuses.update(row["status_counts"])
    dependency_count = sum(row["dependency_count"] for row in ordered_rows)
    closed_count = sum(row["closed_count"] for row in ordered_rows)
    unresolved_count = sum(row["unresolved_count"] for row in ordered_rows)
    checks = {
        "queue_count_285": len(ordered_ids) == 285,
        "result_count_285": len(ordered_rows) == 285,
        "unique_count_285": len(set(result_ids)) == 285,
        "duplicate_count_zero": not duplicates,
        "missing_count_zero": not missing,
        "unexpected_count_zero": not unexpected,
        "dependency_arithmetic": closed_count + unresolved_count == dependency_count,
        "status_arithmetic": sum(aggregate_statuses.values()) == dependency_count,
    }
    return {
        "schema": "huoke.phase1-285-post-open-runtime-ledger.v1",
        "status": "complete_runtime_audit_ledger",
        "inputs": {
            "queue": str(queue_path),
            "single_probe_audit": str(single_path),
            "batch_audits": [str(path) for path in batch_paths],
        },
        "checks": checks,
        "coverage": {
            "expected_template_count": 285,
            "actual_template_count": len(ordered_rows),
            "unique_template_count": len(set(result_ids)),
            "duplicate_template_ids": duplicates,
            "missing_template_ids": missing,
            "unexpected_template_ids": unexpected,
        },
        "summary": {
            "template_count": len(ordered_rows),
            "fully_recovered_template_count": sum(row["fully_recovered"] for row in ordered_rows),
            "partially_recovered_template_count": sum(not row["fully_recovered"] for row in ordered_rows),
            "dependency_reference_count": dependency_count,
            "closed_dependency_reference_count": closed_count,
            "unresolved_dependency_reference_count": unresolved_count,
            "already_existing_before_baseline_count": aggregate_statuses["already_existing_before_baseline"],
            "rewritten_to_existing_local_count": aggregate_statuses["rewritten_to_existing_local"],
            "newly_materialized_after_baseline_count": aggregate_statuses["newly_materialized_after_baseline"],
            "unchanged_missing_count": aggregate_statuses["unchanged_missing"],
            "all_templates_fully_recovered": all(row["fully_recovered"] for row in ordered_rows),
        },
        "templates": ordered_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总 phase1 285 条前台打开后资源审计结果")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--single", type=Path, default=DEFAULT_SINGLE)
    parser.add_argument("--batch-audit", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    batch_paths = tuple(args.batch_audit) if args.batch_audit else DEFAULT_BATCH_REPORTS
    ledger = build_ledger(args.queue, args.single, batch_paths)
    if not all(ledger["checks"].values()):
        raise RuntimeError("ledger checks failed")
    write_json(args.output, ledger)
    print(json.dumps(ledger["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

