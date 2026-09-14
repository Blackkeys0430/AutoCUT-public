from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


ADAPTER = Path(__file__).parents[1]
BUILD_SCRIPT = ADAPTER / "scripts/build_phase1_resource_recovery_batches.py"
AUDIT_SCRIPT = ADAPTER / "scripts/audit_resource_recovery_post_open.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_module() -> ModuleType:
    return _load("build_phase1_resource_recovery_batches", BUILD_SCRIPT)


@pytest.fixture(scope="module")
def audit_module() -> ModuleType:
    return _load("audit_resource_recovery_post_open_batch", AUDIT_SCRIPT)


def test_full_queue_is_285_and_existing_probe_is_not_reopened(build_module: ModuleType) -> None:
    queue = build_module.plan_queue()
    assert queue["scope_count"] == 285
    assert queue["existing_evidence_count"] == 1
    assert queue["pending_execution_count"] == 284
    assert queue["planned_batch_count"] == 6
    assert queue["batch_sizes"] == [50, 50, 50, 50, 50, 34]
    existing = next(item for item in queue["items"] if item["template_id"] == "JIANYING-25-05")
    assert existing["execution_status"] == "existing_evidence_no_duplicate_open"
    assert existing["batch"] is None
    first_batch = [item for item in queue["items"] if item["batch"] == 1]
    assert len(first_batch) == 50
    assert all(item["template_id"] != "JIANYING-25-05" for item in first_batch)


def test_batch_one_candidate_is_complete_workspace_only_and_deterministic(
    build_module: ModuleType,
) -> None:
    queue = build_module.plan_queue()
    first = build_module.build_batch(queue, 1)
    second = build_module.build_batch(queue, 1)
    assert first == second
    manifest = first["manifest"]
    assert manifest["template_count"] == 50
    assert manifest["structure_ready"] is True
    assert all(manifest["checks"].values())
    assert len(manifest["templates"]) == 50
    assert all(item["runtime_visual_dependencies"] for item in manifest["templates"])
    assert all(item["all_runtime_visual_ids_and_paths_preserved"] for item in manifest["templates"])
    assert all(item["source_wrapper_metadata_excluded"] for item in manifest["templates"])
    assert manifest["policy"]["live_write_or_registration"] is False
    assert manifest["download_success"] is None
    assert manifest["visual_success"] is None


def test_cache_baseline_records_present_and_absent(tmp_path: Path, build_module: ModuleType) -> None:
    cache = tmp_path / "Cache"
    existing = cache / "effect/111/hash-a"
    existing.mkdir(parents=True)
    (existing / "effect.bin").write_bytes(b"ok")
    manifest = {
        "templates": [
            {
                "runtime_visual_dependencies": [
                    {"remote_id": "A", "path": "/seller/effect/111/hash-a"},
                    {"remote_id": "B", "path": "/seller/effect/222/hash-b"},
                ]
            }
        ]
    }
    baseline = build_module.capture_cache_baseline(cache, manifest)
    assert baseline["captured_at"] is None
    assert any(row["relative_path"].endswith("hash-a") for row in baseline["entries"])
    assert any(row["relative_path"].endswith("hash-b") for row in baseline["absent_entries"])


def test_batch_auditor_keeps_per_template_partial_gate(
    tmp_path: Path, audit_module: ModuleType
) -> None:
    cache = tmp_path / "Cache"
    ready = cache / "effect/1/ready"
    ready.mkdir(parents=True)
    (ready / "a.bin").write_bytes(b"ready")
    before_draft = {
        "materials": {
            "texts": [],
            "material_animations": [
                {"animations": [{"resource_id": "R1", "path": "/old/ready"}]},
                {"animations": [{"resource_id": "R2", "path": "/old/missing"}]},
            ],
        }
    }
    live = {
        "platform": {"app_version": "8.8.0"},
        "last_modified_platform": {"app_version": "8.8.0"},
        "materials": {
            "texts": [],
            "material_animations": [
                {"animations": [{"resource_id": "R1", "path": str(ready)}]},
                {"animations": [{"resource_id": "R2", "path": "/old/missing"}]},
            ],
        },
    }
    manifest = {
        "schema": "huoke.phase1-resource-recovery-batch-candidate.v1",
        "batch": 1,
        "templates": [
            {
                "template_id": "T1",
                "start_us": 1,
                "end_us": 2,
                "runtime_visual_dependencies": audit_module.collect_visual_dependencies(before_draft),
                "source_wrapper_metadata": [],
            }
        ],
    }
    baseline = {
        "schema": "baseline.v1",
        "cache_root": str(cache),
        "captured_at": "now",
        "entries": [],
        "absent_entries": [],
    }
    report = audit_module.audit_batch_post_open(manifest, {"draft": before_draft}, live, baseline)
    assert report["templates"][0]["summary"]["fully_recovered"] is False
    assert report["summary"]["all_templates_fully_recovered"] is False
    assert report["summary"]["fully_recovered_template_count"] == 0
    assert report["summary"]["partially_recovered_template_count"] == 1


def test_batch_auditor_marks_complete_empty_inventory_without_fabricating_resources(
    tmp_path: Path, audit_module: ModuleType
) -> None:
    cache = tmp_path / "Cache/effect"
    cache.mkdir(parents=True)
    manifest = {
        "schema": "huoke.phase1-resource-recovery-batch-candidate.v1",
        "batch": 2,
        "templates": [
            {
                "template_id": "NO-RUNTIME-VISUAL-DEPS",
                "start_us": 1,
                "end_us": 2,
                "runtime_visual_dependencies": [],
                "runtime_visual_dependency_inventory_complete": True,
                "source_wrapper_metadata": [],
            }
        ],
    }
    live = {
        "platform": {"app_version": "8.8.0"},
        "last_modified_platform": {"app_version": "8.8.0"},
        "materials": {"texts": [], "material_animations": []},
    }
    baseline = {
        "schema": "baseline.v1",
        "cache_root": str(cache.parent),
        "captured_at": "now",
        "complete_recursive_snapshot": True,
        "entries": [],
        "absent_entries": [],
    }
    report = audit_module.audit_batch_post_open(
        manifest, {"draft": live}, live, baseline
    )
    summary = report["templates"][0]["summary"]
    assert summary["dependency_count"] == 0
    assert summary["no_runtime_visual_dependency"] is True
    assert summary["fully_recovered"] is True
    assert report["summary"]["all_templates_fully_recovered"] is True


def test_phase2_batch_auditor_infers_non_text_visual_kinds(
    tmp_path: Path, audit_module: ModuleType
) -> None:
    cache = tmp_path / "Cache"
    recovered = cache / "effect/634025/hash"
    recovered.mkdir(parents=True)
    (recovered / "effect.bin").write_bytes(b"ok")
    live = {
        "platform": {"app_version": "8.8.0"},
        "last_modified_platform": {"app_version": "8.8.0"},
        "materials": {
            "video_effects": [
                {"effect_id": "634025", "resource_id": "REMOTE", "path": str(recovered)}
            ]
        },
    }
    manifest = {
        "schema": "huoke.phase2-engineering-black-screen-batch-candidate.v1",
        "batch": 1,
        "templates": [
            {
                "template_id": "T1",
                "start_us": 1,
                "end_us": 2,
                "runtime_visual_dependencies": [
                    {"remote_id": "634025", "path": "/seller/effect/634025/hash"}
                ],
                "runtime_visual_dependency_inventory_complete": True,
                "source_runtime_visual_dependencies": [],
                "builder_report": {
                    "inner_drafts": [
                        {
                            "build_report": {
                                "visual_rehydrations": [
                                    {
                                        "kind": "media",
                                        "location": "$.materials.video_effects[0].path",
                                        "original_path": "/seller/effect/634025/hash",
                                        "remote_resource_ids": ["634025", "REMOTE"],
                                    }
                                ]
                            }
                        }
                    ]
                },
            }
        ],
    }
    baseline = {
        "schema": "baseline.v1",
        "cache_root": str(cache),
        "captured_at": "now",
        "complete_recursive_snapshot": True,
        "entries": [],
        "absent_entries": [],
    }
    report = audit_module.audit_batch_post_open(manifest, {"draft": live}, live, baseline)
    row = report["templates"][0]["runtime_visual_dependencies"][0]
    assert row["kind"] == "video_effect"
    assert row["after_path"] == str(recovered)
    assert row["runtime_dependency_closed"] is True
    assert report["summary"]["all_templates_fully_recovered"] is True
