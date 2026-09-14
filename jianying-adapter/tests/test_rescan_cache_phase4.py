from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from scripts import rescan_cache_phase4 as module


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def make_inputs(tmp_path: Path) -> tuple[dict[str, Path], Path, datetime]:
    paths = {name: tmp_path / f"{name}.json" for name in ("inventory", "closure", "v4", "phase1", "phase2", "v1", "auto")}
    key = "resource:/old/resource.bin"
    write_json(paths["inventory"], {"records": [{"resource_key": key, "kind": "resource", "expected_filename": "resource.bin", "expected_extension": ".bin", "remote_resource_ids": ["remote-1"], "resolution": {"status": "missing_after_cache_and_explicit_url"}}]})
    write_json(paths["closure"], {"records": [{"template_id": "TARGET-01", "scope": "phase1_285", "unresolved_dependency_keys": [key], "phase2_status_before": None}]})
    write_json(paths["v4"], {"schema": "v4", "records": [{"template_id": "TARGET-01", "production": {"status": "blocked"}}]})
    write_json(paths["phase1"], {"records": [{"template_id": "TARGET-01", "dependencies": [{"kind": "resource", "original_path": "/old/resource.bin", "resolution": "remote_rehydration_required"}]}]})
    write_json(paths["phase2"], {"records": []})
    write_json(paths["v1"], {"records": []})
    write_json(paths["auto"], {"candidates": []})
    cache = tmp_path / "cache"
    cache.mkdir()
    resource = cache / "resource.bin"
    resource.write_bytes(b"real-resource-bytes")
    since = datetime(2026, 9, 3, 1, 59, 0)
    stamp = since.timestamp() + 10
    os.utime(resource, (stamp, stamp))
    wrong_suffix = cache / "effect" / "123456789" / "wrong.texture"
    wrong_suffix.parent.mkdir(parents=True)
    wrong_suffix.write_bytes(b"not-the-declared-extension")
    os.utime(wrong_suffix, (stamp, stamp))
    return paths, cache, since


def test_phase4_matches_new_cache_and_closes_phase1(tmp_path: Path) -> None:
    paths, cache, since = make_inputs(tmp_path)
    v5, audit, inventory, closure = module.build_phase4(
        phase3_inventory_path=paths["inventory"], phase3_closure_path=paths["closure"], phase3_v3_path=paths["v4"],
        phase1_manifest_path=paths["phase1"], phase2_manifest_path=paths["phase2"], v1_path=paths["v1"],
        auto_path=paths["auto"], cache_root=cache, source_root=tmp_path / "no-source", since=since,
        workbench=tmp_path / "workbench", enforce_scope=False,
    )
    assert audit["scope"]["new_cache_file_count"] == 2
    assert audit["scope"]["phase3_remaining_dependency_expected"] == 731
    assert audit["matches"]["phase3_remaining_new_hit_count"] == 1
    assert audit["matches"]["phase3_remaining_still_missing_count"] == 0
    assert inventory["changed_matches"]
    assert closure["records"][0]["phase4_status"] == "production_ready"
    assert v5["records"][0]["production"]["phase4_ready_for_jianying_8_8"] is True


def test_phase4_is_deterministic(tmp_path: Path) -> None:
    paths, cache, since = make_inputs(tmp_path)
    kwargs = {
        "phase3_inventory_path": paths["inventory"], "phase3_closure_path": paths["closure"], "phase3_v3_path": paths["v4"],
        "phase1_manifest_path": paths["phase1"], "phase2_manifest_path": paths["phase2"], "v1_path": paths["v1"],
        "auto_path": paths["auto"], "cache_root": cache, "source_root": tmp_path / "no-source", "since": since,
        "workbench": tmp_path / "workbench", "enforce_scope": False,
    }
    assert module.build_phase4(**kwargs) == module.build_phase4(**kwargs)
