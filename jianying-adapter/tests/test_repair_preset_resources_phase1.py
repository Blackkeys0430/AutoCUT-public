from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts import repair_preset_resources_phase1 as module


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def make_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    v1_path = tmp_path / "standardized_preset_catalog_v1.json"
    auto_path = tmp_path / "all_auto_candidates_v1.json"
    local_root = tmp_path / "local-resources"
    local_root.mkdir()
    (local_root / "recover.ttf").write_bytes(b"font")

    target = {
        "template_id": "TARGET-01",
        "source": {
            "source_hash": "a" * 64,
            "source_path_absolute": "/source/TARGET-01/draft_content.json",
        },
        "jianying": {"compatibility_tier": "exact_8_8"},
        "canvas": [{"width": 1080, "height": 1920, "ratio": "original"}],
        "slot_counts": {"image": 0, "video": 0},
        "text_tracks": [{"role": "main_emphasis", "slot_id": "text_01"}],
        "visual_validation": "pending",
        "production": {
            "status": "blocked",
            "blocked_reasons": ["resource_missing"],
        },
    }
    excluded = copy.deepcopy(target)
    excluded["template_id"] = "EXCLUDED-01"
    excluded["production"]["blocked_reasons"] = ["resource_missing", "manual_mapping_required"]
    write_json(v1_path, {"schema": "test", "records": [target, excluded]})
    write_json(auto_path, {
        "candidates": [{
            "template_id": "TARGET-01",
            "dependencies": {
                "path_dependencies": [
                    {
                        "kind": "font",
                        "original_path": "/Users/YOUR_USER/recover.ttf",
                        "exists_on_current_machine": False,
                    },
                    {
                        "kind": "resource",
                        "original_path": "/Users/YOUR_USER/missing.bin",
                        "exists_on_current_machine": False,
                    },
                ],
                "remote_resource_ids": [
                    {"kind": "resource", "remote_resource_id": "remote-1"},
                ],
            },
        }],
    })
    return v1_path, auto_path, local_root


def test_phase1_scopes_repairs_and_preserves_v1(tmp_path: Path) -> None:
    v1_path, auto_path, local_root = make_inputs(tmp_path)
    before = json.loads(v1_path.read_text(encoding="utf-8"))

    v2, audit, manifest, failures = module.build_phase1(
        v1_path=v1_path,
        auto_path=auto_path,
        local_roots=(local_root,),
    )

    assert audit["target_count"] == 1
    assert audit["success_count"] == 0
    assert audit["failed_count"] == 1
    assert len(manifest["records"]) == len(failures) == 1
    row = manifest["records"][0]
    assert row["template_id"] == "TARGET-01"
    assert row["repair_status"] == "failed_unresolved"
    assert row["actions"]["clearable_stale_path_count"] == 2
    assert row["actions"]["local_resource_resolvable_count"] == 1
    assert row["actions"]["remote_rehydration_required_count"] == 1
    assert row["actions"]["source_mutation_performed"] is False
    assert row["actions"]["freeze_performed"] is False
    decisions = {item["original_path"]: item for item in row["dependencies"]}
    assert decisions["/Users/YOUR_USER/recover.ttf"]["resolution"] == "local_resource_resolvable"
    assert decisions["/Users/YOUR_USER/recover.ttf"]["path_class"] == "stale_external_path"
    assert decisions["/Users/YOUR_USER/missing.bin"]["resolution"] == "remote_rehydration_required"
    assert decisions["/Users/YOUR_USER/missing.bin"]["safe_to_clear_stale_path"] is True
    assert v2["records"][0]["production"]["status"] == "blocked"
    assert v2["records"][0]["resource_repair_phase1"]["resource_complete_proof"] is False
    assert json.loads(v1_path.read_text(encoding="utf-8")) == before


def test_phase1_is_deterministic_on_consecutive_runs(tmp_path: Path) -> None:
    v1_path, auto_path, local_root = make_inputs(tmp_path)
    first = module.build_phase1(v1_path=v1_path, auto_path=auto_path, local_roots=(local_root,))
    second = module.build_phase1(v1_path=v1_path, auto_path=auto_path, local_roots=(local_root,))
    assert first == second
