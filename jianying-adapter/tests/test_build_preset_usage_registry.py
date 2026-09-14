from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_preset_usage_registry.py"
SPEC = importlib.util.spec_from_file_location("build_preset_usage_registry", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def records_by_id() -> dict[str, dict]:
    registry = MODULE.build_registry()
    return {record["template_id"]: record for record in registry["records"]}


def test_registry_covers_unified_library_once() -> None:
    registry = MODULE.build_registry()
    ids = [record["template_id"] for record in registry["records"]]
    assert len(ids) == 781
    assert len(ids) == len(set(ids))
    assert sum(registry["summary"]["category_counts"].values()) == 781
    assert sum(registry["summary"]["production_gate_counts"].values()) == 781
    assert registry["summary"]["production_gate_counts"]["classified_on_demand_preflight_required"] == 672
    assert registry["policy"]["no_exhaustive_batch_visual_review"] is True


def test_every_record_has_actionable_usage_contract() -> None:
    for record in MODULE.build_registry()["records"]:
        assert record["semantic_use"]["use_when"]
        assert record["semantic_use"]["avoid_when"]
        assert record["timing_contract"]["recommended_minimum_node_seconds"] > 0
        assert record["layout_contract"]["ordinary_caption_policy"]
        assert record["current_video_contract"]["required_render_phases"] == ["entry", "stable", "exit"]
        assert "intentional_overlap" in record["current_video_contract"]["subject_overlap_policies"]
        assert record["technical_gate"]["production_gate"]


def test_known_accepted_variants_keep_correct_semantics() -> None:
    records = records_by_id()
    list_template = records["JIANYING-25-01"]
    assert list_template["primary_category"] == "parallel_list"
    assert list_template["content_contract"]["total_text_slot_count"] == 4
    assert list_template["content_contract"]["editable_text_slot_count"] == 4
    assert list_template["technical_gate"]["production_gate"] == "guarded_production_eligible"
    question_template = records["JIANYING-25-08"]
    assert question_template["primary_category"] == "question_hook"
    assert question_template["content_contract"]["total_text_slot_count"] == 3
    assert question_template["content_contract"]["editable_text_slot_count"] == 2
    assert "？" in question_template["content_contract"]["fixed_decorative_text"]
    assert question_template["technical_gate"]["production_gate"] == "guarded_production_eligible"


def test_external_presets_receive_explicit_usage_categories() -> None:
    records = records_by_id()
    assert records["MODERN-88-01"]["primary_category"] == "keyword_emphasis"
    assert records["MODERN-88-02"]["primary_category"] == "kinetic_short_text"


def test_published_usage_artifacts_match_the_generator_and_keep_unknown_sources_explicit():
    registry = MODULE.build_registry()
    assert json.loads(MODULE.OUTPUT_JSON.read_text(encoding='utf-8-sig')) == registry
    assert MODULE.OUTPUT_MD.read_text(encoding='utf-8-sig') == MODULE.markdown(registry)
    assert all('同期同词只留一份' in row['layout_contract']['ordinary_caption_policy'] for row in registry['records'])
    unknown_sources = [row for row in registry['records'] if not row['source'].get('resolved_source_path')]
    assert all(row['visual_features']['evidence'] == 'unknown' for row in unknown_sources)
