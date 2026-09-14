from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/consolidate_phase1_resource_recovery_ledger.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("consolidate_phase1_resource_recovery_ledger", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def summary(*, closed: int, missing: int) -> dict[str, object]:
    statuses = {}
    if closed:
        statuses["already_existing_before_baseline"] = closed
    if missing:
        statuses["unchanged_missing"] = missing
    return {
        "dependency_count": closed + missing,
        "closed_count": closed,
        "unresolved_count": missing,
        "status_counts": statuses,
        "fully_recovered": missing == 0,
        "recovery_status": "fully_recovered" if missing == 0 else "partially_recovered",
    }


def dependencies(*, closed: int, missing: int) -> list[dict[str, str]]:
    return ([{"status": "already_existing_before_baseline"}] * closed
            + [{"status": "unchanged_missing"}] * missing)


def test_ledger_preserves_queue_order_and_balances_counts(tmp_path: Path) -> None:
    items = [
        {"template_id": "A", "phase1_order": 1, "display_name": "a"},
        {"template_id": "B", "phase1_order": 2, "display_name": "b"},
    ]
    queue = write_json(tmp_path / "queue.json", {"scope_count": 285, "items": items})
    probe = write_json(tmp_path / "probe.json", {"template_id": "A", "summary": summary(closed=1, missing=1)})
    batch_summary = summary(closed=2, missing=0)
    batch_summary.pop("status_counts")
    batch = write_json(tmp_path / "batch.json", {"templates": [{
        "template_id": "B",
        "summary": batch_summary,
        "runtime_visual_dependencies": dependencies(closed=2, missing=0),
    }]})
    report = MODULE.build_ledger(queue_path=queue, probe_path=probe, batch_paths=[batch])
    assert report["ok"] is True
    assert [item["template_id"] for item in report["templates"]] == ["A", "B"]
    assert report["summary"] == {
        "template_count": 2,
        "fully_recovered_template_count": 1,
        "partially_recovered_template_count": 1,
        "dependency_reference_count": 4,
        "closed_dependency_reference_count": 3,
        "unresolved_dependency_reference_count": 1,
        "status_counts": {"already_existing_before_baseline": 3, "unchanged_missing": 1},
    }


@pytest.mark.parametrize("mode", ["missing", "duplicate", "unexpected"])
def test_ledger_rejects_queue_integrity_failures(tmp_path: Path, mode: str) -> None:
    queue_items = [{"template_id": "A", "phase1_order": 1}]
    if mode == "missing":
        queue_items.append({"template_id": "B", "phase1_order": 2})
    queue = write_json(tmp_path / "queue.json", {"scope_count": 285, "items": queue_items})
    probe_id = "X" if mode == "unexpected" else "A"
    probe = write_json(tmp_path / "probe.json", {"template_id": probe_id, "summary": summary(closed=1, missing=0)})
    batch_templates = []
    if mode == "duplicate":
        batch_templates.append({"template_id": "A", "summary": summary(closed=1, missing=0)})
    batch = write_json(tmp_path / "batch.json", {"templates": batch_templates})
    report = MODULE.build_ledger(queue_path=queue, probe_path=probe, batch_paths=[batch])
    assert report["ok"] is False
