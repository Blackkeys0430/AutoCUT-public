from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/summarize_phase1_resource_recovery.py"


@pytest.fixture(scope="module")
def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("summarize_phase1_resource_recovery", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(SCRIPT)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _summary(closed: int, missing: int) -> dict:
    dependency_count = closed + missing
    fully = missing == 0
    return {
        "dependency_count": dependency_count,
        "closed_count": closed,
        "unresolved_count": missing,
        "fully_recovered": fully,
        "recovery_status": "fully_recovered" if fully else "partially_recovered",
        "by_kind": {
            "font": {
                "status_counts": {
                    "already_existing_before_baseline": closed,
                    "unchanged_missing": missing,
                }
            }
        },
    }


def _fixture(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    ids = [f"T-{index:03d}" for index in range(285)]
    queue = _write(tmp_path / "queue.json", {"items": [{"template_id": item} for item in ids]})
    single = _write(
        tmp_path / "single.json",
        {"template_id": ids[0], "summary": _summary(1, 1)},
    )
    batches: list[Path] = []
    remaining = ids[1:]
    sizes = [50, 50, 50, 50, 50, 34]
    cursor = 0
    for batch, size in enumerate(sizes, 1):
        batch_ids = remaining[cursor : cursor + size]
        cursor += size
        batches.append(
            _write(
                tmp_path / f"batch_{batch}.json",
                {
                    "templates": [
                        {"template_id": item, "summary": _summary(2, 0)}
                        for item in batch_ids
                    ]
                },
            )
        )
    return queue, single, batches


def test_ledger_is_complete_ordered_and_deterministic(tmp_path: Path, module: ModuleType) -> None:
    queue, single, batches = _fixture(tmp_path)
    first = module.build_ledger(queue, single, batches)
    second = module.build_ledger(queue, single, batches)
    assert first == second
    assert all(first["checks"].values())
    assert first["coverage"]["actual_template_count"] == 285
    assert first["coverage"]["duplicate_template_ids"] == []
    assert first["coverage"]["missing_template_ids"] == []
    assert first["templates"][0]["template_id"] == "T-000"
    assert first["templates"][-1]["template_id"] == "T-284"
    assert first["summary"]["fully_recovered_template_count"] == 284
    assert first["summary"]["partially_recovered_template_count"] == 1
    assert first["summary"]["unchanged_missing_count"] == 1


def test_duplicate_or_missing_result_is_rejected(tmp_path: Path, module: ModuleType) -> None:
    queue, single, batches = _fixture(tmp_path)
    value = json.loads(batches[-1].read_text(encoding="utf-8"))
    value["templates"][-1]["template_id"] = value["templates"][0]["template_id"]
    batches[-1].write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="coverage failed"):
        module.build_ledger(queue, single, batches)


def test_dependency_arithmetic_mismatch_is_rejected(tmp_path: Path, module: ModuleType) -> None:
    queue, single, batches = _fixture(tmp_path)
    value = json.loads(single.read_text(encoding="utf-8"))
    value["summary"]["dependency_count"] = 99
    single.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="arithmetic mismatch"):
        module.build_ledger(queue, single, batches)

