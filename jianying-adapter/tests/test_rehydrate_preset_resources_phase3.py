from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts import rehydrate_preset_resources_phase3 as module


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def make_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    v1_path = tmp_path / "v1.json"
    v3_path = tmp_path / "v3.json"
    auto_path = tmp_path / "auto.json"
    phase1_path = tmp_path / "phase1.json"
    phase2_path = tmp_path / "phase2.json"
    local_root = tmp_path / "cache"
    local_root.mkdir()
    # Valid TTF signature plus non-empty body; no placeholder/empty file is used.
    (local_root / "recover.ttf").write_bytes(b"\x00\x01\x00\x00" + b"font-data" * 8)
    record = {
        "template_id": "TARGET-01",
        "source": {"source_hash": "a" * 64},
        "jianying": {"compatibility_tier": "exact_8_8"},
        "canvas": [{"width": 1080, "height": 1920}],
        "slot_counts": {"image": 0, "video": 0},
        "text_tracks": [{"role": "main_emphasis", "max_chars": 8}],
        "visual_validation": "pending",
        "production": {"status": "blocked", "blocked_reasons": ["resource_missing"]},
    }
    write_json(v1_path, {"schema": "v1", "records": [record]})
    write_json(v3_path, {"schema": "v3", "records": [copy.deepcopy(record)]})
    write_json(auto_path, {"candidates": [{
        "template_id": "TARGET-01",
        "dependencies": {
            "path_dependencies": [{"kind": "font", "original_path": "/old/recover.ttf"}],
            "remote_resource_ids": [{"kind": "font", "remote_resource_id": "remote-1"}],
        },
    }]})
    write_json(phase1_path, {"records": [{
        "template_id": "TARGET-01",
        "dependencies": [{"resolution": "remote_rehydration_required"}],
    }]})
    write_json(phase2_path, {"records": []})
    return v1_path, v3_path, auto_path, phase1_path, phase2_path, local_root


def test_phase3_freezes_valid_cache_but_does_not_close_remote_id(tmp_path: Path, monkeypatch) -> None:
    v1, v3, auto, phase1, phase2, local_root = make_inputs(tmp_path)
    monkeypatch.setattr(module, "WORKBENCH", tmp_path / "workbench")
    monkeypatch.setattr(module, "FROZEN_ROOT", tmp_path / "workbench" / "frozen_resources")
    v4, audit, inventory, closure = module.build_phase3(
        v1_path=v1, v3_path=v3, auto_path=auto, phase1_manifest_path=phase1,
        phase2_manifest_path=phase2, local_roots=(local_root,), enforce_scope=False,
    )
    path_row = next(row for row in inventory["records"] if row["original_path"] == "/old/recover.ttf")
    assert path_row["resolution"]["status"] == "cache_hit_frozen"
    assert Path(path_row["resolution"]["frozen_path"]).is_file()
    assert path_row["resolution"]["size_bytes"] > 0
    assert audit["resources"]["cache_hit_frozen_count"] == 1
    assert audit["resources"]["still_missing_count"] == 1
    assert closure["records"][0]["all_required_dependencies_closed"] is False
    assert closure["records"][0]["phase3_status"] == "blocked"
    assert v4["records"][0]["production"]["phase3_status"] == "blocked"


def test_phase3_is_deterministic_on_consecutive_runs(tmp_path: Path, monkeypatch) -> None:
    v1, v3, auto, phase1, phase2, local_root = make_inputs(tmp_path)
    monkeypatch.setattr(module, "WORKBENCH", tmp_path / "workbench")
    monkeypatch.setattr(module, "FROZEN_ROOT", tmp_path / "workbench" / "frozen_resources")
    kwargs = {"v1_path": v1, "v3_path": v3, "auto_path": auto,
              "phase1_manifest_path": phase1, "phase2_manifest_path": phase2,
              "local_roots": (local_root,), "enforce_scope": False}
    assert module.build_phase3(**kwargs) == module.build_phase3(**kwargs)
