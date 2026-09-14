from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/audit_resource_recovery_post_open.py"


@pytest.fixture(scope="module")
def audit_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_resource_recovery_post_open", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _draft(fonts: list[tuple[str, str]], animations: list[tuple[str, str]]) -> dict:
    return {
        "platform": {"app_version": "8.8.0"},
        "last_modified_platform": {"app_version": "8.8.0"},
        "materials": {
            "texts": [
                {"id": f"font-{index}", "font_resource_id": resource_id, "font_path": path}
                for index, (resource_id, path) in enumerate(fonts)
            ],
            "material_animations": [
                {
                    "id": f"animation-{index}",
                    "animations": [
                        {"resource_id": resource_id, "third_resource_id": "0", "path": path}
                    ],
                }
                for index, (resource_id, path) in enumerate(animations)
            ],
        },
    }


def _fixture(tmp_path: Path) -> tuple[dict, dict, dict, dict]:
    cache = tmp_path / "Cache"
    baseline_font = cache / "effect/old/font-hash"
    baseline_font.mkdir(parents=True)
    (baseline_font / "font.ttf").write_bytes(b"font")
    new_font = cache / "effect/new/font-new"
    new_font.mkdir(parents=True)
    (new_font / "font.ttf").write_bytes(b"new-font")
    rewritten = cache / "effect/new/animation-rewritten"
    rewritten.mkdir(parents=True)
    (rewritten / "a.bin").write_bytes(b"a")
    (rewritten / "b.bin").write_bytes(b"bb")

    before = _draft(
        [("FONT-OLD", "/missing/font-hash/font.ttf"), ("FONT-NEW", "/missing/font-new/font.ttf")],
        [("ANIM-REWRITTEN", "/missing/old-animation"), ("ANIM-MISSING", "/missing/still-missing")],
    )
    after = _draft(
        [("FONT-OLD", str(baseline_font / "font.ttf")), ("FONT-NEW", str(new_font / "font.ttf"))],
        [("ANIM-REWRITTEN", str(rewritten)), ("ANIM-MISSING", "/missing/still-missing")],
    )
    manifest = {
        "schema": "probe.v1",
        "template_id": "TEMPLATE-1",
        "source": {"sha256": "SOURCE"},
        "source_only_project_metadata": [
            {"key": "draft_file_path", "value": "/seller/subdraft/draft_content.json"}
        ],
    }
    probe = {"draft": before, "manifest": {"template_id": "TEMPLATE-1"}}
    baseline = {
        "schema": "baseline.v1",
        "captured_at": "2026-09-03T00:00:00+08:00",
        "cache_root": str(cache),
        "entries": [{"relative_path": "effect/old/font-hash"}],
        "absent_entries": [{"relative_path": "effect/new/font-new"}],
    }
    return manifest, probe, after, baseline


def test_four_runtime_statuses_and_partial_gate(tmp_path: Path, audit_module: ModuleType) -> None:
    manifest, probe, after, baseline = _fixture(tmp_path)
    report = audit_module.audit_post_open(manifest, probe, after, baseline)
    by_id = {row["remote_id"]: row for row in report["runtime_visual_dependencies"]}

    assert by_id["FONT-OLD"]["status"] == "already_existing_before_baseline"
    assert by_id["FONT-NEW"]["status"] == "newly_materialized_after_baseline"
    assert by_id["ANIM-REWRITTEN"]["status"] == "rewritten_to_existing_local"
    assert by_id["ANIM-MISSING"]["status"] == "unchanged_missing"
    assert report["summary"]["fully_recovered"] is False
    assert report["summary"]["recovery_status"] == "partially_recovered"
    assert report["summary"]["by_kind"]["font"]["fully_recovered"] is True
    assert report["summary"]["by_kind"]["animation"]["fully_recovered"] is False


def test_recursive_local_evidence_is_hashed_and_deterministic(
    tmp_path: Path, audit_module: ModuleType
) -> None:
    manifest, probe, after, baseline = _fixture(tmp_path)
    first = audit_module.audit_post_open(manifest, probe, after, baseline)
    second = audit_module.audit_post_open(manifest, probe, after, baseline)
    assert first == second
    row = next(
        item
        for item in first["runtime_visual_dependencies"]
        if item["remote_id"] == "ANIM-REWRITTEN"
    )
    evidence = row["local_evidence"]
    assert evidence["recursive_file_count"] == 2
    assert evidence["nonempty_file_count"] == 2
    assert evidence["total_bytes"] == 3
    assert len(evidence["tree_sha256"]) == 64
    assert all(len(item["sha256"]) == 64 for item in evidence["files"])


def test_wrapper_metadata_is_excluded_and_auditor_is_read_only(
    tmp_path: Path, audit_module: ModuleType
) -> None:
    manifest, probe, after, baseline = _fixture(tmp_path)
    before = json.dumps(after, sort_keys=True)
    report = audit_module.audit_post_open(manifest, probe, after, baseline)
    assert json.dumps(after, sort_keys=True) == before
    assert report["source_wrapper_metadata"]["excluded"] is True
    assert report["source_wrapper_metadata"]["count"] == 1
    assert report["policy"]["read_only"] is True
    assert report["policy"]["live_draft_written"] is False
    assert report["policy"]["jianying_started_or_stopped"] is False


def test_fully_recovered_requires_every_visual_dependency(
    tmp_path: Path, audit_module: ModuleType
) -> None:
    manifest, probe, after, baseline = _fixture(tmp_path)
    recovered = Path(baseline["cache_root"]) / "effect/new/still-missing"
    recovered.mkdir(parents=True)
    (recovered / "effect.bin").write_bytes(b"ok")
    after["materials"]["material_animations"][1]["animations"][0]["path"] = str(recovered)
    baseline["absent_entries"].append({"relative_path": "effect/new/still-missing"})
    report = audit_module.audit_post_open(manifest, probe, after, baseline)
    assert report["summary"]["closed_count"] == 4
    assert report["summary"]["fully_recovered"] is True
    assert report["summary"]["recovery_status"] == "fully_recovered"

