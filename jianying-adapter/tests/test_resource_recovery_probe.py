from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
from types import ModuleType

import pytest


PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT / "video_trials/字幕预设黑幕筛选_20260902/build_resource_recovery_probe.py"


@pytest.fixture(scope="module")
def probe_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_resource_recovery_probe", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ids():
    counter = itertools.count(1)
    return lambda: f"DEFAULT-MODE-{next(counter):04d}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_default_builder_behavior_still_clears_missing_visual_paths(
    probe_module: ModuleType,
) -> None:
    registry = probe_module.read_json(probe_module.REGISTRY)
    candidate = probe_module._candidate(registry, probe_module.DEFAULT_TEMPLATE_ID)
    result = probe_module.trial_builder.build_five_template_trial(
        probe_module._probe_base(),
        {"candidates": [candidate]},
        probe_module.PRESET_ROOT,
        (),
        trial_start_us=1_000_000,
        disable_audio=True,
        template_ids=(probe_module.DEFAULT_TEMPLATE_ID,),
        slot_copy_table={probe_module.DEFAULT_TEMPLATE_ID: probe_module._slot_copy(candidate)},
        id_factory=_ids(),
    )

    sanitization = result["manifest"]["templates"][0]["sanitization"]
    assert sanitization["cleared_external_paths"]
    assert sanitization["preserved_external_paths"] == []
    assert result["manifest"]["visual_resource_rehydrate"] == {
        "enabled": False,
        "resolved": [],
        "pending_paths": [],
    }


def test_preserve_probe_keeps_visual_ids_paths_and_excludes_audio(
    probe_module: ModuleType,
) -> None:
    result = probe_module.build_probe()
    manifest = result["manifest"]

    assert manifest["structure_success"] is True
    assert manifest["checks"]["visual_reference_multiset_equal"] is True
    assert manifest["checks"]["no_visual_path_cleared"] is True
    assert manifest["actual_visual_dependencies"]["pending_paths"]
    assert manifest["excluded_audio"]["source_track_count"] == 2
    assert manifest["excluded_audio"]["probe_audio_track_count"] == 0
    assert all(track.get("type") != "audio" for track in result["draft"]["tracks"])
    serialized = json.dumps(result["draft"], ensure_ascii=False)
    assert "missing_preset_dependency_cleared_in_copy" not in serialized


def test_probe_is_double_8_8_vertical_and_workspace_only(
    probe_module: ModuleType,
) -> None:
    result = probe_module.build_probe()
    draft = result["draft"]
    policy = result["manifest"]["probe_policy"]

    assert draft["platform"]["app_version"] == "8.8.0"
    assert draft["last_modified_platform"]["app_version"] == "8.8.0"
    assert draft["canvas_config"]["width"] == 1080
    assert draft["canvas_config"]["height"] == 1920
    assert policy["live_draft_written"] is False
    assert policy["draft_registered"] is False
    assert policy["jianying_started_or_stopped"] is False
    with pytest.raises(ValueError, match="output root"):
        probe_module.output_paths(probe_module.DEFAULT_TEMPLATE_ID, PROJECT / "backups")


def test_probe_build_and_write_are_idempotent(probe_module: ModuleType) -> None:
    first = probe_module.build_probe()
    second = probe_module.build_probe()
    assert first == second

    output_root = probe_module.OUTPUT_ROOT / "pytest_idempotency"
    envelope_path, manifest_path = probe_module.write_probe(
        first, probe_module.DEFAULT_TEMPLATE_ID, output_root
    )
    first_hashes = (_sha256(envelope_path), _sha256(manifest_path))
    probe_module.write_probe(second, probe_module.DEFAULT_TEMPLATE_ID, output_root)
    assert (_sha256(envelope_path), _sha256(manifest_path)) == first_hashes
    envelope_path.unlink()
    manifest_path.unlink()
    output_root.rmdir()


def test_manifest_classifies_wrapper_paths_as_source_metadata(
    probe_module: ModuleType,
) -> None:
    manifest = probe_module.build_probe()["manifest"]
    rows = manifest["source_only_project_metadata"]
    assert {row["key"] for row in rows} == {
        "draft_config_path",
        "draft_cover_path",
        "draft_file_path",
    }
    assert all(
        row["classification"] == "source_wrapper_metadata_not_runtime_visual_dependency"
        for row in rows
    )
    assert manifest["download_success"] is None
    assert manifest["visual_success"] is None

