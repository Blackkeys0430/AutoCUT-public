import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jianying_adapter.video_effects import (
    add_video_effect, validate_video_effects, validate_video_effect_plan,
)
from jianying_adapter.candidate_plan import validate_timeline_equivalence, get_shared_operation_registry
from jianying_adapter.visual_planning import validate_visual_events


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case(tmp_path, scope="global"):
    # Small synthetic resource retains the observed engine shell. Tests do not
    # claim this synthetic payload produces a rendered native effect.
    root = tmp_path / "blur"
    write(root / "config.json", {"effect": {"Link": [{"path": "AmazingFeature/", "type": "AmazingFeature"}]}})
    write(root / "extra.json", {"setting": {"effect_adjust_params": [
        {"effect_key": "effects_adjust_blur", "default": .5, "min": 0., "max": 1.} ]}})
    write(root / "AmazingFeature/sticker.config", {"min_version": "10.62.0"})
    # A serialized dependency with its real native length prefix.
    (root / "AmazingFeature/test.frag").write_bytes(b"shader fixture")
    ref = b"test.frag"
    (root / "AmazingFeature/main.scene").write_bytes(b"%SerializedFormat%@" + len(ref).to_bytes(4, "little") + ref)
    manifest = tmp_path / "resource.json"
    write(manifest, {"schema": "jianying-adapter.video-effect-resource.v1", "effect_name": "模糊",
                     "effect_id": "634025", "resource_id": "6739752823140913675", "resource_path": str(root),
                     "files": {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*") if p.is_file()},
                     "engine_min_version": "10.62.0"})
    state = tmp_path / "state.json"
    write(state, {"project_id": "test", "current_authorizations": [
        {"option": "video_effects", "enabled": True, "source": "user"}]})
    spec = {"id": "blur", "kind": "add_video_effect", "scope": scope, "effect_name": "模糊",
            "effect_id": "634025", "resource_id": "6739752823140913675", "parameters": {"effects_adjust_blur": 40},
            "resource_manifest_path": str(manifest), "resource_manifest_sha256": digest(manifest),
            "authorization_source": "user", "visual_event_id": "background"}
    if scope == "global":
        spec.update(start_us=100000, end_us=900000)
    else:
        spec.update(track_name="JY_ROUGH_CUT_VIDEO", segment_id="video")
    plan = {"project_state": str(state), "options": {"video_effects": {"enabled": True, "authorization_source": "user"}},
            "operations": [spec], "project_format": {"visual_planning_version": 1}, "visual_events": [
                {"id": "background", "start_us": 0, "end_us": 1000000, "audience_need": "local focus",
                 "primary_visual": "person", "composition": "blur background",
                 "techniques": [{"kind": "video_effect", "operation_ids": ["blur"]}]}]}
    segment = {"id": "video", "material_id": "media", "source_timerange": {"start": 400000, "duration": 1000000},
               "target_timerange": {"start": 0, "duration": 1000000}, "speed": 1, "volume": .7,
               "visible": True, "extra_material_refs": ["mask"], "clip": {"alpha": 1, "scale": {"x": 1.1, "y": 1.1}},
               "common_keyframes": [{"property_type": "KFTypeVolume", "keyframe_list": [{"time_offset": 0, "values": [.7]}]}]}
    draft = {"duration": 1000000, "materials": {"videos": [{"id": "media"}], "common_mask": [{"id": "mask"}]},
             "tracks": [{"id": "track", "name": "JY_ROUGH_CUT_VIDEO", "type": "video", "segments": [segment]}]}
    base = tmp_path / "base.json"
    write(base, draft)
    plan["base_draft"] = str(base)
    return draft, spec, SimpleNamespace(plan=plan, plan_path=tmp_path / "plan.json")


@pytest.mark.parametrize("scope", ["global", "segment"])
def test_sdk_output_roundtrip_and_original_unchanged(tmp_path, scope):
    draft, spec, ctx = case(tmp_path, scope)
    before = copy.deepcopy(draft)
    result, report = get_shared_operation_registry().handlers["add_video_effect"](draft, spec, ctx)
    assert draft == before
    material = result["materials"]["video_effects"][0]
    assert material["type"] == "video_effect"
    assert material["apply_target_type"] == (2 if scope == "global" else 0)
    assert material["adjust_params"][0]["value"] == .4
    assert report["resource"]["engine_version_is_not_app_version"]
    assert report["native_visual_qa"] == "pending"
    assert validate_visual_events(ctx.plan)["ok"]
    assert validate_timeline_equivalence(ctx.plan, result)["ok"]
    if scope == "global":
        assert result["tracks"][0] == before["tracks"][0]
        assert result["tracks"][-1]["type"] == "effect"
        assert result["tracks"][-1]["segments"][0]["source_timerange"] is None
    else:
        result["tracks"][0]["segments"][0]["extra_material_refs"].pop()
        assert result["tracks"] == before["tracks"]


@pytest.mark.parametrize("field,value", [
    ("parameters", {"effects_adjust_blur": -1}), ("parameters", {"effects_adjust_blur": 101}),
    ("parameters", {"effects_adjust_blur": float("nan")}), ("parameters", {"effects_adjust_blur": float("inf")}),
    ("parameters", {"effects_adjust_blur": True}), ("parameters", {"unknown": 50}), ("parameters", [50, 50]),
    ("effect_name", "模糊 "), ("effect_name", "NotAnEnum"), ("effect_id", "wrong"), ("resource_id", "wrong"),
    ("scope", "face"), ("authorization_source", "not-user"), ("start_us", -1), ("end_us", 1000001),
    ("resource_manifest_sha256", "bad"), ("unknown_field", True), ("visual_event_id", "elsewhere"),
])
def test_bad_spec_atomic_rejection(tmp_path, field, value):
    draft, spec, ctx = case(tmp_path)
    spec[field] = value
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError):
        add_video_effect(draft, spec, ctx)
    assert draft == before


def test_sdk_default_and_named_parameter_mapping(tmp_path):
    draft, spec, ctx = case(tmp_path)
    spec["parameters"] = {}
    result, _ = add_video_effect(draft, spec, ctx)
    assert result["materials"]["video_effects"][0]["adjust_params"][0]["value"] == .5


def test_vip_enum_rejected_before_resource_use(tmp_path):
    from jianying_adapter.action_audio import _native
    draft, spec, ctx = case(tmp_path)
    vip = next(v for v in _native().VideoSceneEffectType if v.value.is_vip)
    spec.update(effect_name=vip.name, effect_id=vip.value.effect_id, resource_id=vip.value.resource_id)
    with pytest.raises(ValueError, match="VIP"):
        add_video_effect(draft, spec, ctx)


@pytest.mark.parametrize("change", ["option_disabled", "option_source", "state_revoked", "state_source", "missing_state"])
def test_authorization_checked_at_build_and_readback(tmp_path, change):
    draft, spec, ctx = case(tmp_path)
    result, _ = add_video_effect(draft, spec, ctx)
    state_path = Path(ctx.plan["project_state"])
    if change == "option_disabled": ctx.plan["options"]["video_effects"]["enabled"] = False
    if change == "option_source": ctx.plan["options"]["video_effects"]["authorization_source"] = "other"
    if change.startswith("state_"):
        state = json.loads(state_path.read_text("utf-8"))
        state["current_authorizations"][0]["status" if change == "state_revoked" else "source"] = "revoked" if change == "state_revoked" else "other"
        write(state_path, state)
    if change == "missing_state": state_path.unlink()
    with pytest.raises((ValueError, OSError)):
        add_video_effect(draft, spec, ctx)
    assert not validate_video_effects(ctx.plan, result)["ok"]


@pytest.mark.parametrize("change", ["hash", "inventory", "metadata", "dependency", "path_escape"])
def test_resource_failure_and_dependency_checks(tmp_path, change):
    draft, spec, ctx = case(tmp_path)
    result, _ = add_video_effect(draft, spec, ctx)
    manifest_path = Path(spec["resource_manifest_path"])
    manifest = json.loads(manifest_path.read_text("utf-8"))
    root = Path(manifest["resource_path"])
    if change == "hash": (root / "AmazingFeature/main.scene").write_bytes(b"changed")
    if change == "inventory": (root / "extra-file").write_bytes(b"extra")
    if change == "metadata":
        extra = root / "extra.json"
        write(extra, {"setting": {"effect_adjust_params": []}})
        manifest["files"]["extra.json"] = digest(extra)
    if change == "dependency":
        (root / "AmazingFeature/test.frag").unlink()
        manifest["files"].pop("AmazingFeature/test.frag")
    if change == "path_escape": manifest["files"]["../outside"] = "0" * 64
    if change in {"metadata", "dependency", "path_escape"}:
        write(manifest_path, manifest)
        spec["resource_manifest_sha256"] = digest(manifest_path)
    with pytest.raises(ValueError): add_video_effect(draft, spec, ctx)
    assert not validate_video_effects(ctx.plan, result)["ok"]


@pytest.mark.parametrize("scope", ["global", "segment"])
@pytest.mark.parametrize("field,value", [("type", "face_effect"), ("apply_target_type", 8), ("effect_id", "bad"),
                                       ("resource_id", "bad"), ("path", "missing"), ("value", 0),
                                       ("common_keyframes", [{"unexpected": 1}]), ("visible", False),
                                       ("adjust_params", []), ("apply_time_range", {"start": 1, "duration": 1})])
def test_writer_material_tampering(tmp_path, scope, field, value):
    draft, spec, ctx = case(tmp_path, scope)
    result, _ = add_video_effect(draft, spec, ctx)
    result["materials"]["video_effects"][0][field] = value
    assert not validate_timeline_equivalence(ctx.plan, result)["ok"]


@pytest.mark.parametrize("scope", ["global", "segment"])
@pytest.mark.parametrize("change", ["duplicate_material", "wrong_bucket", "missing_material", "duplicate_ref", "wrong_target", "invisible", "hidden_track", "undeclared"])
def test_writer_attachment_tampering(tmp_path, scope, change):
    draft, spec, ctx = case(tmp_path, scope)
    result, _ = add_video_effect(draft, spec, ctx)
    material = result["materials"]["video_effects"][0]
    owner_track = result["tracks"][-1] if scope == "global" else result["tracks"][0]
    owner = owner_track["segments"][0]
    if change == "duplicate_material": result["materials"]["video_effects"].append(copy.deepcopy(material))
    if change == "wrong_bucket": result["materials"]["effects"] = result["materials"].pop("video_effects")
    if change == "missing_material": result["materials"]["video_effects"] = []
    if change == "duplicate_ref": owner["extra_material_refs"].append(material["id"])
    if change == "wrong_target":
        other = copy.deepcopy(owner)
        other["id"] = "other"
        other["target_timerange"] = {"start": 1, "duration": 999999}
        owner_track["segments"] = [other]
    if change == "invisible": owner["visible"] = False
    if change == "hidden_track": owner_track["visible"] = False
    if change == "undeclared": ctx.plan["operations"] = []
    assert not validate_timeline_equivalence(ctx.plan, result)["ok"]


def test_wrong_global_track_and_timing(tmp_path):
    draft, spec, ctx = case(tmp_path)
    result, _ = add_video_effect(draft, spec, ctx)
    result["tracks"][-1]["type"] = "video"
    assert not validate_video_effects(ctx.plan, result)["ok"]
    result["tracks"][-1]["type"] = "effect"
    result["tracks"][-1]["segments"][0]["source_timerange"] = {"start": 0, "duration": 800000}
    assert not validate_video_effects(ctx.plan, result)["ok"]


@pytest.mark.parametrize("field,value", [("effect_mask", {"id": "mask"}), ("enable_mask", True),
                                       ("bind_segment_id", "another"), ("algorithm_artifact_path", "alternate/path"),
                                       ("transparent_params", {"alpha": .1})])
def test_native_alternate_scope_fields_are_rejected(tmp_path, field, value):
    draft, spec, ctx = case(tmp_path, "segment")
    result, _ = add_video_effect(draft, spec, ctx)
    result["materials"]["video_effects"][0][field] = value
    assert not validate_video_effects(ctx.plan, result)["ok"]


def test_segment_timing_drift_inside_event_is_rejected(tmp_path):
    draft, spec, ctx = case(tmp_path, "segment")
    result, _ = add_video_effect(draft, spec, ctx)
    result["tracks"][0]["segments"][0]["target_timerange"] = {"start": 1, "duration": 999998}
    assert not validate_video_effects(ctx.plan, result)["ok"]


def test_segment_after_declared_split_checks_exact_native_interval(tmp_path):
    draft, spec, ctx = case(tmp_path, "segment")
    # Build exactly the shape produced by the existing split operation. The
    # source footage stays continuous, and only the middle segment is affected.
    split = {"id": "cut", "kind": "split_video_track", "track_name": "JY_ROUGH_CUT_VIDEO",
             "segment_id": "video", "cut_points_us": [200000, 700000], "segment_ids": ["a", "b", "c"]}
    ctx.plan["operations"].insert(0, split)
    original = draft["tracks"][0]["segments"][0]
    segments = []
    for identifier, start, end in zip(split["segment_ids"], [0, 200000, 700000], [200000, 700000, 1000000]):
        segment = copy.deepcopy(original)
        segment.update(id=identifier, target_timerange={"start": start, "duration": end - start},
                       source_timerange={"start": 400000 + start, "duration": end - start})
        segments.append(segment)
    draft["tracks"][0]["segments"] = segments
    spec["segment_id"] = "b"
    result, _ = add_video_effect(draft, spec, ctx)
    assert result["tracks"][0]["segments"][0] == segments[0]
    assert result["tracks"][0]["segments"][2] == segments[2]
    assert validate_video_effects(ctx.plan, result)["ok"]
    result["tracks"][0]["segments"][1]["target_timerange"]["start"] += 1
    assert not validate_video_effects(ctx.plan, result)["ok"]


@pytest.mark.parametrize("change", ["wrong_technique", "wrong_operation", "wrong_event", "wrong_window", "duplicate"])
def test_effect_visual_binding(tmp_path, change):
    draft, spec, ctx = case(tmp_path)
    visual = ctx.plan["visual_events"][0]
    if change == "wrong_technique": visual["techniques"][0]["kind"] = "keyframes"
    if change == "wrong_operation": visual["techniques"][0]["operation_ids"] = ["absent"]
    if change == "wrong_event": spec["visual_event_id"] = "another"
    if change == "wrong_window": visual["end_us"] = 800000
    if change == "duplicate": visual["techniques"].append(copy.deepcopy(visual["techniques"][0]))
    with pytest.raises(ValueError): add_video_effect(draft, spec, ctx)
    if change in {"wrong_technique", "wrong_operation", "wrong_event"}:
        assert not validate_visual_events(ctx.plan)["ok"]


def test_short_segment_requires_explicit_split_and_duplicate_is_atomic(tmp_path):
    draft, spec, ctx = case(tmp_path, "segment")
    before = copy.deepcopy(draft)
    spec["start_us"] = 100000
    with pytest.raises(ValueError): add_video_effect(draft, spec, ctx)
    assert draft == before
    spec.pop("start_us")
    result, _ = add_video_effect(draft, spec, ctx)
    frozen = copy.deepcopy(result)
    with pytest.raises(ValueError): add_video_effect(result, spec, ctx)
    assert result == frozen


def test_preview_defers_native_evidence_but_seal_requires_it(tmp_path):
    draft, spec, ctx = case(tmp_path)
    assert validate_video_effect_plan(ctx.plan, plan_path=ctx.plan_path, require_native_evidence=False)["ok"]
    assert not validate_video_effect_plan(ctx.plan, plan_path=ctx.plan_path, require_native_evidence=True)["ok"]
    spec["current_video_evidence"] = {"current_video_ready": True, "native_visual_verified": True}
    assert not validate_video_effect_plan(ctx.plan, plan_path=ctx.plan_path, require_native_evidence=True)["ok"]


def test_legacy_unmarked_effects_unchanged_but_reserved_undeclared_rejected():
    draft = {"materials": {"video_effects": [{"id": "old-effect"}]}, "tracks": []}
    assert validate_video_effects({"operations": []}, draft)["ok"]
    draft["materials"]["video_effects"][0]["id"] = "JY_VFX_hidden"
    assert not validate_video_effects({"operations": []}, draft)["ok"]


def test_effect_targets_new_muted_background_and_checks_its_declared_range(tmp_path):
    from PIL import Image
    from jianying_adapter.shared_operations import add_broll, set_track_render_order
    draft, spec, context = case(tmp_path, "segment")
    source = tmp_path / "background.png"
    Image.new("RGB", (64, 64), (32, 48, 64)).save(source)
    background = {"id": "background-media", "kind": "add_broll", "node_id": "background",
        "track_name": "JY_BROLL_background", "segment_id": "new-background", "source_path": str(source),
        "start_us": 100000, "end_us": 900000, "source_timerange": {"start": 0, "duration": 800000},
        "segment": {"volume": 0}, "clip": {"alpha": 1, "scale": {"x": 1, "y": 1}}}
    spec.update(track_name=background["track_name"], segment_id=background["segment_id"])
    context.plan["operations"] = [background, spec]
    original = copy.deepcopy(draft["tracks"][0]["segments"][0])
    draft, _ = add_broll(draft, background, context)
    draft, _ = add_video_effect(draft, spec, context)
    set_track_render_order(draft, {"ordered_track_names": ["JY_ROUGH_CUT_VIDEO", background["track_name"]]}, context)
    assert draft["tracks"][0]["segments"][0] == {**original, "render_index": 0, "track_render_index": 0}
    report = validate_video_effects(context.plan, draft, plan_path=context.plan_path)
    assert report["ok"], report["errors"]
    assert report["effects"][0]["interval_us"] == [100000, 900000]
    assert draft["tracks"][1]["attribute"] == 1 and draft["tracks"][1]["flag"] == 2
    for defect in ("time", "track", "collision"):
        changed = copy.deepcopy(context.plan)
        if defect == "time": changed["operations"][0]["start_us"] += 1
        elif defect == "track": changed["operations"][1]["track_name"] = "JY_ROUGH_CUT_VIDEO"
        else: changed["operations"].insert(0, copy.deepcopy(background))
        assert not validate_video_effects(changed, draft, plan_path=context.plan_path)["ok"], defect
