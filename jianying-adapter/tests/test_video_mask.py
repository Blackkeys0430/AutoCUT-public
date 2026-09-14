import copy
import hashlib
import json
from pathlib import Path

import pytest

from jianying_adapter import candidate_plan
from jianying_adapter.candidate_plan import CandidateAssembler, get_shared_operation_registry
from jianying_adapter.shared_operations import shared_final_validator
from jianying_adapter.video_mask import apply_video_mask
from test_candidate_stages import _plan, _write


def draft_and_spec(tmp_path, shape="circle"):
    resource = tmp_path / "mask-resource"
    resource.mkdir(exist_ok=True)
    # Synthetic fixture retains the observed 8.8 circle shell, not vendor IDs.
    mask = {"id": "reference-mask", "type": "mask", "category": "video", "name": "圆形",
            "resource_id": "7356934080102928946", "resource_type": shape, "path": str(resource),
            "constant_material_id": "native-constant", "text_config": {"align_type": 15},
            "config": {"centerY": 0.29, "height": 0.43, "width": 0.76}}
    if shape == "rectangle":
        mask.update(resource_id="7356934318301647410", name="矩形")
        mask["config"].update(centerX=0, roundCorner=0.12, rotation=0, feather=0, invert=False)
    segment = {"id": "person", "material_id": "video", "speed": 1.0, "volume": 0.7,
               "source_timerange": {"start": 200000, "duration": 1000000},
               "target_timerange": {"start": 0, "duration": 1000000},
               "enable_adjust_mask": False, "clip": {"alpha": 1, "scale": {"x": .8, "y": .8}},
               "extra_material_refs": ["speed"], "common_keyframes": [
                   {"property_type": "KFTypeScaleX", "keyframe_list": [{"time_offset": 0, "values": [0.8]}]},
                   {"property_type": "KFTypeVolume", "keyframe_list": [{"time_offset": 0, "values": [0.7]}]}]}
    draft = {"materials": {"videos": [{"id": "video", "width": 1080, "height": 1920, "path": "rough.mp4"}],
                            "speeds": [{"id": "speed", "speed": 1}]},
             "tracks": [{"name": "PERSON", "type": "video", "segments": [segment]}]}
    reference = {"platform": {"app_version": "8.8.0"}, "last_modified_platform": {"app_version": "8.8.0"},
                 "materials": {"common_mask": [mask]}, "tracks": [{"type": "video", "segments": [{
                     "id": "reference-segment", "enable_adjust_mask": False, "clip": {"alpha": 1},
                     "extra_material_refs": [mask["id"]]}]}]}
    ref_path = tmp_path / "native-reference.json"
    _write(ref_path, reference)
    spec = {"id": "mask", "kind": "apply_video_mask", "track_name": "PERSON", "segment_id": "person",
            "shape": shape, "center_x_px": 0, "center_y_px": 192, "size_ratio": .3,
            "rotation_deg": 0, "feather_percent": 0, "invert": False,
            "native_reference": {"path": str(ref_path), "sha256": hashlib.sha256(ref_path.read_bytes()).hexdigest(),
                                 "segment_id": "reference-segment"}}
    if shape == "rectangle":
        spec.update(width_ratio=.6, round_corner_percent=12)
    return draft, spec


@pytest.mark.parametrize("shape", ["circle", "rectangle"])
def test_mask_preserves_segment_and_native_reference_shell(tmp_path, shape):
    draft, spec = draft_and_spec(tmp_path, shape)
    before = copy.deepcopy(draft)
    report = get_shared_operation_registry().handlers["apply_video_mask"](draft, spec, None)
    mask = draft["materials"]["common_mask"][0]
    assert mask["resource_id"] == ("7356934080102928946" if shape == "circle" else "7356934318301647410")
    assert mask["config"]["height"] == .3
    assert mask["config"]["centerY"] == .2
    assert mask["config"]["width"] == pytest.approx(.3 * 1920 / 1080 if shape == "circle" else .6)
    if shape == "rectangle": assert mask["config"]["roundCorner"] == .12
    assert mask["constant_material_id"] == "native-constant"
    assert "masks" not in draft["materials"]
    assert report["native_visual_qa"] == "pending"
    assert draft["tracks"][0]["segments"][0]["extra_material_refs"] == ["speed", mask["id"]]
    draft["tracks"][0]["segments"][0]["extra_material_refs"].pop()
    draft["materials"].pop("common_mask")
    assert draft == before


@pytest.mark.parametrize("field,value", [
    ("size_ratio", 0), ("size_ratio", 1.1), ("size_ratio", float("nan")),
    ("center_x_px", 541), ("center_y_px", -961), ("center_y_px", True),
    ("rotation_deg", 361), ("rotation_deg", "0"), ("feather_percent", -1),
    ("feather_percent", 101), ("invert", 1), ("track_name", "missing"),
    ("segment_id", "missing"), ("shape", "heart"), ("width_ratio", .5),
    ("round_corner_percent", 12), ("unknown", 0),
    # V4 evidence has no non-zero centerX/rotation/feather config support.
    ("center_x_px", 10), ("rotation_deg", 20), ("feather_percent", 5), ("invert", True),
])
def test_invalid_parameters_do_not_mutate(tmp_path, field, value):
    draft, spec = draft_and_spec(tmp_path)
    spec[field] = value
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError): apply_video_mask(draft, spec, None)
    assert draft == before


@pytest.mark.parametrize("case", ["track_duplicate", "segment_duplicate", "material_duplicate", "missing_ref",
                                     "duplicate_ref", "wrong_track_type", "zero_alpha", "alpha_keyframe",
                                     "mask_keyframe", "unresolved_keyframes", "malformed_switch", "missing_dimensions",
                                     "existing_mask", "wrong_bucket_type"])
def test_existing_conflicts_rejected_without_mutation(tmp_path, case):
    draft, spec = draft_and_spec(tmp_path)
    t = draft["tracks"][0]; s = t["segments"][0]; m = draft["materials"]
    if case == "track_duplicate": draft["tracks"].append(copy.deepcopy(t))
    elif case == "segment_duplicate": t["segments"].append(copy.deepcopy(s))
    elif case == "material_duplicate": m["videos"].append(copy.deepcopy(m["videos"][0]))
    elif case == "missing_ref": s["extra_material_refs"].append("missing")
    elif case == "duplicate_ref": s["extra_material_refs"].append("speed")
    elif case == "wrong_track_type": t["type"] = "audio"
    elif case == "zero_alpha": s["clip"]["alpha"] = 0
    elif case == "alpha_keyframe": s["common_keyframes"].append({"property_type": "KFTypeAlpha"})
    elif case == "mask_keyframe": s["common_keyframes"].append({"property_type": "KFTypeMaskSize"})
    elif case == "unresolved_keyframes": s["keyframe_refs"] = ["unknown-motion"]
    elif case == "malformed_switch": s["enable_video_mask"] = "true"
    elif case == "missing_dimensions": m["videos"][0].pop("width")
    elif case == "wrong_bucket_type": m["common_mask"] = {}
    elif case == "existing_mask":
        apply_video_mask(draft, spec, None)
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError): apply_video_mask(draft, spec, None)
    assert draft == before


def test_unmasked_vendor_base_switches_normalized_only_after_success(tmp_path):
    draft, spec = draft_and_spec(tmp_path)
    segment = draft["tracks"][0]["segments"][0]
    segment.update(enable_adjust_mask=True, enable_video_mask=True)
    before = copy.deepcopy(draft)
    bad_spec = copy.deepcopy(spec)
    bad_spec["native_reference"]["sha256"] = "bad"
    with pytest.raises(ValueError): apply_video_mask(draft, bad_spec, None)
    assert draft == before  # A failed reference cannot partially normalize a base.
    report = apply_video_mask(draft, spec, None)
    assert report["normalized_mask_switches"] == {
        "enable_video_mask": {"before": True, "after": "absent"},
        "enable_adjust_mask": {"before": True, "after": False}}
    assert "enable_video_mask" not in segment
    assert segment["enable_adjust_mask"] is False
    segment["enable_video_mask"] = True; segment["enable_adjust_mask"] = True
    segment["extra_material_refs"].pop(); draft["materials"].pop("common_mask")
    assert draft == before


@pytest.mark.parametrize("case", ["changed_hash", "version", "missing_version", "shape", "duplicate_mask",
                                     "missing_mask", "wrong_bucket", "video_flag", "adjust_flag", "alpha",
                                     "resource_missing", "missing_reference", "wrong_segment", "dangling_ref"])
def test_invalid_reference_rejected_without_mutation(tmp_path, case):
    draft, spec = draft_and_spec(tmp_path)
    ref = spec["native_reference"]; path = Path(ref["path"])
    data = json.loads(path.read_text(encoding="utf-8"))
    segment = data["tracks"][0]["segments"][0]
    mask = data["materials"]["common_mask"][0]
    if case == "version": data["platform"]["app_version"] = "11.4.0"
    elif case == "missing_version": data.pop("last_modified_platform")
    elif case == "shape": mask["resource_type"] = "rectangle"
    elif case == "duplicate_mask": data["materials"]["common_mask"].append(copy.deepcopy(mask))
    elif case == "missing_mask": segment["extra_material_refs"] = []
    elif case == "wrong_bucket": data["materials"]["masks"] = data["materials"].pop("common_mask")
    elif case == "video_flag": segment["enable_video_mask"] = True  # V5 conflict remains blocked.
    elif case == "adjust_flag": segment["enable_adjust_mask"] = True
    elif case == "alpha": segment["clip"]["alpha"] = 0
    elif case == "resource_missing": mask["path"] = str(tmp_path / "absent")
    elif case == "missing_reference": spec.pop("native_reference")
    elif case == "wrong_segment": ref["segment_id"] = "absent"
    elif case == "dangling_ref": segment["extra_material_refs"].append("missing")
    _write(path, data)
    ref["sha256"] = "bad-hash" if case == "changed_hash" else hashlib.sha256(path.read_bytes()).hexdigest()
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError): apply_video_mask(draft, spec, None)
    assert draft == before


def test_candidate_assembler_executes_mask_preview(tmp_path, monkeypatch):
    plan_path = _plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    base_path = Path(plan["base_draft"])
    base = json.loads(base_path.read_text(encoding="utf-8"))
    draft, spec = draft_and_spec(tmp_path)
    draft["tracks"][0]["segments"][0].update(enable_video_mask=True, enable_adjust_mask=True)
    base["tracks"].insert(0, draft["tracks"][0])
    base["materials"].update(draft["materials"])
    plan["operations"].insert(0, spec)
    _write(base_path, base); _write(plan_path, plan)
    monkeypatch.setattr(candidate_plan, "exact_process_identifier", lambda _path: {"ok": True})
    result = CandidateAssembler(plan_path, get_shared_operation_registry(), final_validator=shared_final_validator).preview()
    built = json.loads(Path(result["candidate"]).read_text(encoding="utf-8"))
    mask = built["materials"]["common_mask"][0]
    assert mask["id"] in built["tracks"][0]["segments"][0]["extra_material_refs"]
    assert "enable_video_mask" not in built["tracks"][0]["segments"][0]
    assert built["tracks"][0]["segments"][0]["enable_adjust_mask"] is False
    assert built["tracks"][0]["segments"][0]["common_keyframes"] == draft["tracks"][0]["segments"][0]["common_keyframes"]
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["writer_allowed"] is False
    assert manifest["phase"] == "preview"
    assert manifest["blockers"] == ["current_video_evidence_pending_seal"]
