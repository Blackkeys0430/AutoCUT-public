from __future__ import annotations

import copy
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "enrich_screened_preset_catalog.py"


def load_module():
    spec = importlib.util.spec_from_file_location("enrich_screened_preset_catalog", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_objective_palette_layout_and_size_rules() -> None:
    module = load_module()
    assert module._palette_family(["#FFFFFF", "#000000"]) == "neutral_monochrome"
    assert module._palette_family(["#FFDF00", "#FFFFFF"]) == "warm_accent"
    assert module._size_tier([14.0, 42.0]) == "large"
    layout = module._layout([{"x": -0.4, "y": -0.5}, {"x": -0.35, "y": 0.2}])
    assert layout["horizontal_band"] == "negative_x"
    assert layout["arrangement"] == "vertical_stack"


def test_real_242_backfill_is_complete_disjoint_and_non_promoting() -> None:
    module = load_module()
    before_catalog = module.read_json(module.CATALOG_PATH)
    before_production = {row["template_id"]: copy.deepcopy(row["production"])
                         for row in before_catalog["records"]}
    catalog, usage, profiles, report = module.build_outputs()
    after_production = {row["template_id"]: row["production"] for row in catalog["records"]}
    assert before_production == after_production
    assert report["ok"] is True
    assert report["scope"] == {
        "approved_template_count": 242,
        "deferred_template_count": 400,
        "standardized_catalog_match_count": 242,
        "usage_registry_match_count": 203,
        "catalog_only_generated_id_count": 39,
        "duplicate_approved_id_count": 0,
        "omitted_approved_id_count": 0,
    }
    assert len(profiles["profiles"]) == len({row["template_id"] for row in profiles["profiles"]}) == 242
    assert sum("selection_profile" in row for row in catalog["records"]) >= 242
    assert sum("selection_profile" in row for row in usage["records"]) >= 203
    assert all(row["library_screening"]["current_video_ready"] is False for row in profiles["profiles"])
    assert all(row["library_screening"]["production_ready_claimed"] is False for row in profiles["profiles"])


def test_real_build_is_deterministic() -> None:
    module = load_module()
    first = module.build_outputs()
    second = module.build_outputs()
    assert first == second
