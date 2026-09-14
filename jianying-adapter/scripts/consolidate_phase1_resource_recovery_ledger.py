from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def compact_result(
    *,
    queue_item: dict[str, Any],
    template: dict[str, Any],
    audit_path: Path,
) -> dict[str, Any]:
    summary = template["summary"]
    status_counts = summary.get("status_counts")
    if status_counts is None:
        status_counts = Counter(
            dependency["status"]
            for dependency in template.get("runtime_visual_dependencies") or []
        )
    return {
        "template_id": queue_item["template_id"],
        "phase1_order": queue_item["phase1_order"],
        "display_name": queue_item.get("display_name"),
        "source_audit": str(audit_path),
        "source_audit_sha256": sha256(audit_path),
        "dependency_count": int(summary["dependency_count"]),
        "closed_count": int(summary["closed_count"]),
        "unresolved_count": int(summary["unresolved_count"]),
        "status_counts": dict(sorted(status_counts.items())),
        "recovery_status": summary["recovery_status"],
        "fully_recovered": bool(summary["fully_recovered"]),
    }


def build_ledger(
    *,
    queue_path: Path,
    probe_path: Path,
    batch_paths: list[Path],
) -> dict[str, Any]:
    queue = load_json(queue_path)
    expected_items = queue["items"]
    expected_ids = [item["template_id"] for item in expected_items]

    raw_results: list[tuple[dict[str, Any], Path]] = []
    probe = load_json(probe_path)
    raw_results.append((probe, probe_path))
    for path in batch_paths:
        audit = load_json(path)
        raw_results.extend((template, path) for template in audit["templates"])

    ids = [item[0]["template_id"] for item in raw_results]
    counts = Counter(ids)
    duplicate_ids = sorted(template_id for template_id, count in counts.items() if count > 1)
    missing_ids = sorted(set(expected_ids) - set(ids))
    unexpected_ids = sorted(set(ids) - set(expected_ids))
    by_id = {template["template_id"]: (template, path) for template, path in raw_results}

    templates = [
        compact_result(
            queue_item=item,
            template=by_id[item["template_id"]][0],
            audit_path=by_id[item["template_id"]][1],
        )
        for item in expected_items
        if item["template_id"] in by_id
    ]

    statuses: Counter[str] = Counter()
    for item in templates:
        statuses.update(item["status_counts"])
    fully = sum(1 for item in templates if item["fully_recovered"])
    partial = len(templates) - fully
    dependency_count = sum(item["dependency_count"] for item in templates)
    closed_count = sum(item["closed_count"] for item in templates)
    unresolved_count = sum(item["unresolved_count"] for item in templates)
    checks = {
        "queue_scope_is_285": queue.get("scope_count") == 285,
        "result_count_matches_queue": len(templates) == len(expected_ids),
        "template_ids_unique": not duplicate_ids,
        "no_missing_template_ids": not missing_ids,
        "no_unexpected_template_ids": not unexpected_ids,
        "dependency_accounting_balanced": dependency_count == closed_count + unresolved_count,
        "status_accounting_balanced": dependency_count == sum(statuses.values()),
    }
    return {
        "schema": "huoke.phase1-resource-recovery-ledger.v1",
        "inputs": {
            "queue": str(queue_path),
            "probe_audit": str(probe_path),
            "batch_audits": [str(path) for path in batch_paths],
        },
        "checks": checks,
        "ok": all(checks.values()),
        "integrity": {
            "expected_template_count": len(expected_ids),
            "result_template_count": len(templates),
            "duplicate_template_ids": duplicate_ids,
            "missing_template_ids": missing_ids,
            "unexpected_template_ids": unexpected_ids,
        },
        "summary": {
            "template_count": len(templates),
            "fully_recovered_template_count": fully,
            "partially_recovered_template_count": partial,
            "dependency_reference_count": dependency_count,
            "closed_dependency_reference_count": closed_count,
            "unresolved_dependency_reference_count": unresolved_count,
            "status_counts": dict(sorted(statuses.items())),
        },
        "templates": templates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--probe-audit", required=True, type=Path)
    parser.add_argument("--batch-audit", required=True, type=Path, action="append")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = build_ledger(
        queue_path=args.queue,
        probe_path=args.probe_audit,
        batch_paths=args.batch_audit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "ok": report["ok"], **report["summary"]}, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
