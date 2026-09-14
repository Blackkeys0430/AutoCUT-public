import copy
import hashlib
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from jianying_adapter.action_audio import _native, mix_action_audio, validate_action_audio
from jianying_adapter.candidate_plan import get_shared_operation_registry, validate_timeline_equivalence, build_contract_sha256
from jianying_adapter.text_animation import (
    apply_text_animation, validate_text_animations, validate_text_animation_plan, resolve_text_intro_anchor,
)
from jianying_adapter.visual_planning import validate_visual_events


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), "utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case(tmp_path):
    resource = tmp_path / "intro"
    # Synthetic resource is a file-binding fixture, not native visual evidence.
    write(resource / "config.json", {"test_fixture": True})
    manifest = tmp_path / "manifest.json"
    write(manifest, {"schema": "jianying-adapter.text-animation-resource.v1", "enum": "TextIntro", "member": "轻微放大",
                     "effect_id": "1644262", "resource_id": "6763469998330483213", "resource_path": str(resource),
                     "files": {"config.json": digest(resource / "config.json")}})
    state = tmp_path / "state.json"
    write(state, {"project_id": "test", "current_authorizations": [
        {"option": "text_animation", "enabled": True, "source": "user"},
        {"option": "action_sfx", "enabled": True, "source": "user"}]})
    spec = {"id": "capacity_intro", "kind": "apply_text_animation", "track_name": "JY_ZH_SUBTITLES", "segment_id": "caption",
            "enum": "TextIntro", "member": "轻微放大", "effect_id": "1644262", "resource_id": "6763469998330483213",
            "duration_us": 300000, "resource_manifest_path": str(manifest), "resource_manifest_sha256": digest(manifest),
            "authorization_source": "user", "visual_event_id": "capacity"}
    n = _native()
    native_text = n.TextSegment("我这个40毫升的", n.trange(500000, 2000000))
    text = native_text.export_json()
    text["id"] = "caption"
    text["clip"]["transform"] = {"x": .1, "y": -.6}
    text["common_keyframes"] = [{"property_type": "KFTypePositionX", "keyframe_list": [{"time_offset": 0, "values": [.1]}]}]
    text["extra_material_refs"] = ["empty-animation", "other-resource"]
    draft = {"duration": 3000000, "materials": {"texts": [native_text.export_material()],
             "videos": [{"id": "video-material", "path": "unchanged.mov"}],
             "material_animations": [{"id": "empty-animation", "type": "sticker_animation", "animations": []}],
             "text_shapes": [{"id": "other-resource"}]}, "tracks": [
                 {"name": "JY_ROUGH_CUT_VIDEO", "type": "video", "segments": [{"id": "video", "material_id": "video-material",
                  "volume": 1., "target_timerange": {"start": 0, "duration": 3000000}, "source_timerange": {"start": 0, "duration": 3000000}}]},
                 {"name": "JY_ZH_SUBTITLES", "type": "text", "attribute": 0, "flag": 0, "segments": [text]}]}
    plan = {"project_state": str(state), "options": {"text_animation": {"enabled": True, "authorization_source": "user"}},
            "audio_authorization_source": "user", "operations": [spec], "project_format": {"visual_planning_version": 1},
            "visual_events": [{"id": "capacity", "start_us": 0, "end_us": 3000000, "audience_need": "compare capacity",
                "primary_visual": "text", "composition": "retain the complete sentence", "techniques": [
                    {"kind": "text_animation", "operation_ids": ["capacity_intro"]}]}]}
    return draft, spec, SimpleNamespace(plan=plan, plan_path=tmp_path / "plan.json")


def with_sound(tmp_path, anchor="text_intro_end"):
    draft, spec, ctx = case(tmp_path)
    sound = tmp_path / "sound.wav"
    with wave.open(str(sound), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\0\0" * 48000)
    event = {"id": "capacity_landing", "source_path": str(sound), "sha256": digest(sound), "source_start_us": 0,
             "duration_us": 200000, "anchor_offset_us": 10000, "gain_db": -20, "fade_in_us": 1000, "fade_out_us": 15000,
             "visual_event_id": "capacity", "action_operation_id": "capacity_intro", "action_anchor": anchor,
             "reason": "match the actual text entrance"}
    audio = {"id": "audio", "kind": "mix_action_audio", "authorization_source": "user", "dialogue_gain_db": 0, "events": [event]}
    ctx.plan["operations"].append(audio)
    ctx.plan["visual_events"][0]["techniques"].append({"kind": "sound", "operation_ids": ["audio"]})
    return draft, spec, ctx, audio, event


def test_apply_reuses_sdk_and_preserves_actual_text_layout_and_other_materials(tmp_path):
    draft, spec, ctx = case(tmp_path)
    before = copy.deepcopy(draft)
    result, report = get_shared_operation_registry().handlers["apply_text_animation"](draft, spec, ctx)
    assert draft == before
    assert report["intro_start_us"] == 500000 and report["intro_end_us"] == 800000
    assert report["native_visual_qa"] == "pending"
    assert result["materials"]["texts"] == before["materials"]["texts"]
    target = result["tracks"][1]["segments"][0]
    assert target["extra_material_refs"] == ["other-resource", "JY_TEXT_ANIM_capacity_intro"]
    restored = copy.deepcopy(target)
    restored["extra_material_refs"] = before["tracks"][1]["segments"][0]["extra_material_refs"]
    assert restored == before["tracks"][1]["segments"][0]
    assert result["tracks"][0] == before["tracks"][0]
    assert result["materials"]["material_animations"][0] == before["materials"]["material_animations"][0]
    native = result["materials"]["material_animations"][-1]
    assert native["type"] == "sticker_animation"
    assert native["animations"][0]["type"] == "in"
    assert native["animations"][0]["duration"] == 300000
    assert validate_visual_events(ctx.plan)["ok"]
    assert validate_timeline_equivalence(ctx.plan, result)["ok"]


@pytest.mark.parametrize("field,value", [("enum", "TextLoopAnim"), ("member", "轻微放大 "), ("member", "missing"),
    ("effect_id", "bad"), ("resource_id", "bad"), ("duration_us", 0), ("duration_us", -1), ("duration_us", True),
    ("duration_us", 2000001), ("duration_us", float("nan")), ("duration_us", .3), ("track_name", "JY_ROUGH_CUT_VIDEO"),
    ("track_name", "absent"), ("segment_id", "absent"), ("resource_manifest_sha256", "0" * 64),
    ("authorization_source", "other"), ("visual_event_id", "other"), ("start_us", 10)])
def test_invalid_text_animation_rejected_atomically(tmp_path, field, value):
    draft, spec, ctx = case(tmp_path)
    before = copy.deepcopy(draft)
    spec[field] = value
    with pytest.raises(ValueError): apply_text_animation(draft, spec, ctx)
    assert draft == before


def test_native_vip_rejected(tmp_path):
    from pyJianYingDraft.metadata.text_intro import TextIntro
    draft, spec, ctx = case(tmp_path)
    vip = next(v for v in TextIntro if v.value.is_vip)
    spec.update(member=vip.name, effect_id=vip.value.effect_id, resource_id=vip.value.resource_id)
    with pytest.raises(ValueError, match="VIP"): apply_text_animation(draft, spec, ctx)


def test_existing_animation_and_duplicate_material_are_not_replaced(tmp_path):
    draft, spec, ctx = case(tmp_path)
    result, _ = apply_text_animation(draft, spec, ctx)
    before = copy.deepcopy(result)
    with pytest.raises(ValueError, match="already has"): apply_text_animation(result, spec, ctx)
    assert result == before
    draft["materials"]["material_animations"].append(copy.deepcopy(result["materials"]["material_animations"][-1]))
    with pytest.raises(ValueError, match="duplicate"): apply_text_animation(draft, spec, ctx)


@pytest.mark.parametrize("change", ["hash", "missing", "unlisted", "manifest_identity", "outside"])
def test_local_resource_rechecked_at_write_and_readback(tmp_path, change):
    draft, spec, ctx = case(tmp_path)
    result, _ = apply_text_animation(draft, spec, ctx)
    manifest_path = Path(spec["resource_manifest_path"])
    manifest = json.loads(manifest_path.read_text("utf-8"))
    root = Path(manifest["resource_path"])
    if change == "hash": (root / "config.json").write_bytes(b"changed")
    if change == "missing": (root / "config.json").unlink()
    if change == "unlisted": (root / "extra").write_bytes(b"extra")
    if change == "manifest_identity": manifest["effect_id"] = "bad"
    if change == "outside": manifest["files"]["../outside"] = "0" * 64
    if change in {"manifest_identity", "outside"}:
        write(manifest_path, manifest)
        spec["resource_manifest_sha256"] = digest(manifest_path)
    with pytest.raises(ValueError): apply_text_animation(draft, spec, ctx)
    assert not validate_text_animations(ctx.plan, result)["ok"]


@pytest.mark.parametrize("field,value", [("duration", 200000), ("start", 1), ("type", "out"), ("id", "wrong"),
                                       ("resource_id", "wrong"), ("path", "wrong"), ("anim_adjust_params", [1])])
def test_native_animation_fields_checked_on_writer_roundtrip(tmp_path, field, value):
    draft, spec, ctx = case(tmp_path)
    result, _ = apply_text_animation(draft, spec, ctx)
    result["materials"]["material_animations"][-1]["animations"][0][field] = value
    assert not validate_timeline_equivalence(ctx.plan, result)["ok"]


@pytest.mark.parametrize("change", ["duplicate", "missing", "wrong_bucket", "wrong_ref", "extra_ref", "invisible", "hidden_track", "undeclared"])
def test_container_and_target_tampering_detected(tmp_path, change):
    draft, spec, ctx = case(tmp_path)
    result, _ = apply_text_animation(draft, spec, ctx)
    material = result["materials"]["material_animations"][-1]
    target = result["tracks"][1]["segments"][0]
    if change == "duplicate": result["materials"]["material_animations"].append(copy.deepcopy(material))
    if change == "missing": result["materials"]["material_animations"].pop()
    if change == "wrong_bucket": result["materials"]["effects"] = [result["materials"]["material_animations"].pop()]
    if change == "wrong_ref": target["extra_material_refs"][-1] = "wrong"
    if change == "extra_ref": result["tracks"][0]["segments"][0]["extra_material_refs"] = [material["id"]]
    if change == "invisible": target["visible"] = False
    if change == "hidden_track": result["tracks"][1]["flag"] = 2
    if change == "undeclared": ctx.plan["operations"] = []
    assert not validate_text_animations(ctx.plan, result)["ok"]


@pytest.mark.parametrize("anchor,expected", [("text_intro_start", 500000), ("text_intro_end", 800000)])
def test_sound_anchor_derived_from_compiled_native_entrance(tmp_path, anchor, expected):
    draft, spec, ctx, audio, event = with_sound(tmp_path, anchor)
    draft, _ = apply_text_animation(draft, spec, ctx)
    before = copy.deepcopy(event)
    result, report = mix_action_audio(draft, audio, ctx)
    assert event == before and "start_us" not in event and "action_time_us" not in event
    assert result["tracks"][-1]["segments"][0]["target_timerange"]["start"] == expected - 10000
    assert report["resolved_actions"][0]["action_time_us"] == expected
    assert resolve_text_intro_anchor(ctx.plan, result, spec["id"], anchor) == expected
    assert validate_timeline_equivalence(ctx.plan, result)["ok"]


def test_retiming_uses_actual_new_start_and_old_written_sound_fails_readback(tmp_path):
    draft, spec, ctx, audio, event = with_sound(tmp_path)
    draft, _ = apply_text_animation(draft, spec, ctx)
    first, _ = mix_action_audio(draft, audio, ctx)
    draft["tracks"][1]["segments"][0]["target_timerange"]["start"] += 100000
    second, report = mix_action_audio(draft, audio, ctx)
    assert report["resolved_actions"][0]["action_time_us"] == 900000
    assert validate_action_audio(ctx.plan, second)["ok"]
    first["tracks"][1]["segments"][0]["target_timerange"]["start"] += 100000
    assert not validate_action_audio(ctx.plan, first)["ok"]


def with_preset_sound(tmp_path, anchor="text_intro_end"):
    draft, spec, ctx, audio, event = with_sound(tmp_path, anchor)
    spec["duration_us"] = 500000
    draft, _ = apply_text_animation(draft, spec, ctx)
    # Reuse SDK-exported text/animation as a compiled single-track preset fixture.
    name = "JY_PRESET_152__NODE__capacity_t0"
    track = draft["tracks"][1]
    track["name"] = name
    segment = track["segments"][0]
    animation = draft["materials"]["material_animations"][-1]
    segment["extra_material_refs"] = ["preset-animation" if r == animation["id"] else r
                                      for r in segment["extra_material_refs"]]
    animation["id"] = "preset-animation"
    operation_id = spec["id"]
    spec.clear()
    spec.update(id=operation_id, kind="add_preset_group", template_id="152", node_id="capacity")
    ctx.plan["visual_events"][0]["techniques"][0]["kind"] = "text_preset"
    ctx.plan["presets"] = [{"template_id": "152", "node_id": "capacity", "start_us": 500000, "end_us": 2500000,
                            "actual_text_tracks": [{"track_name": name, "segment_count": 1, "text": "我这个40毫升的",
                                                    "start_us": 500000, "end_us": 2500000}]}]
    return draft, spec, ctx, audio, event


@pytest.mark.parametrize("anchor,expected", [("text_intro_start", 550000), ("text_intro_end", 1050000)])
def test_single_preset_sound_uses_actual_segment_and_animation_offset(tmp_path, anchor, expected):
    draft, spec, ctx, audio, event = with_preset_sound(tmp_path, anchor)
    draft["materials"]["material_animations"][-1]["animations"][0]["start"] = 50000
    before = copy.deepcopy(draft)
    result, report = mix_action_audio(draft, audio, ctx)
    assert draft == before
    assert report["resolved_actions"][0]["action_time_us"] == expected
    assert result["tracks"][-1]["segments"][0]["target_timerange"]["start"] == expected - 10000
    assert validate_timeline_equivalence(ctx.plan, result)["ok"]


def test_preset_entrance_500_to_300_ms_moves_sound_and_rejects_stale_sound(tmp_path):
    draft, spec, ctx, audio, event = with_preset_sound(tmp_path)
    first, report = mix_action_audio(draft, audio, ctx)
    assert report["resolved_actions"][0]["action_time_us"] == 1000000
    draft["materials"]["material_animations"][-1]["animations"][0]["duration"] = 300000
    second, report = mix_action_audio(draft, audio, ctx)
    assert report["resolved_actions"][0]["action_time_us"] == 800000
    assert second["tracks"][-1]["segments"][0]["target_timerange"]["start"] == 790000
    assert validate_timeline_equivalence(ctx.plan, second)["ok"]
    first["materials"]["material_animations"][-1]["animations"][0]["duration"] = 300000
    assert not validate_timeline_equivalence(ctx.plan, first)["ok"]


@pytest.mark.parametrize("template_id", ["152", "396"])
def test_single_preset_accepts_native_empty_auxiliary_video_track(tmp_path, template_id):
    draft, spec, ctx, audio, event = with_preset_sound(tmp_path)
    selection = ctx.plan["presets"][0]
    name = f"JY_PRESET_{template_id}__NODE__capacity_01"
    spec["template_id"] = selection["template_id"] = template_id
    selection["actual_text_tracks"][0]["track_name"] = draft["tracks"][1]["name"] = name
    auxiliary = {"name": f"JY_PRESET_{template_id}__NODE__capacity_AUX_VIDEO_01", "type": "video",
                 "attribute": 0, "flag": 0, "segments": []}
    draft["tracks"].append(auxiliary)
    result, _ = mix_action_audio(draft, audio, ctx)
    assert validate_timeline_equivalence(ctx.plan, result)["ok"]
    auxiliary["segments"] = [{"target_timerange": {"start": 500000, "duration": 2000000}}]
    with pytest.raises(ValueError, match="multiple tracks/segments"):
        mix_action_audio(draft, audio, ctx)


@pytest.mark.parametrize("change", ["undeclared", "wrong_node", "duplicate_selection", "duplicate_operation",
    "multi_declared", "wrong_name", "missing_track", "extra_track", "extra_segment", "wrong_material",
    "missing_container", "extra_container", "extra_animation", "not_in", "zero_duration", "overrun", "hidden", "shared_ref"])
def test_preset_sound_anchor_rejects_ambiguous_or_disabled_targets(tmp_path, change):
    draft, spec, ctx, audio, event = with_preset_sound(tmp_path)
    track = draft["tracks"][1]
    segment = track["segments"][0]
    container = draft["materials"]["material_animations"][-1]
    selection = ctx.plan["presets"][0]
    if change == "undeclared": ctx.plan["presets"] = []
    if change == "wrong_node": spec["node_id"] = "other"
    if change == "duplicate_selection": ctx.plan["presets"].append(copy.deepcopy(selection))
    if change == "duplicate_operation": ctx.plan["operations"].append({**spec, "id": "duplicate"})
    if change == "multi_declared": selection["actual_text_tracks"].append(copy.deepcopy(selection["actual_text_tracks"][0]))
    if change == "wrong_name": selection["actual_text_tracks"][0]["track_name"] += "missing"
    if change == "missing_track": draft["tracks"].pop()
    if change == "extra_track": draft["tracks"].append({**copy.deepcopy(track), "name": track["name"] + "extra"})
    if change == "extra_segment": track["segments"].append(copy.deepcopy(segment))
    if change == "wrong_material": segment["material_id"] = "video-material"
    if change == "missing_container": segment["extra_material_refs"] = []
    if change == "extra_container":
        draft["materials"]["material_animations"].append({**copy.deepcopy(container), "id": "extra"})
        segment["extra_material_refs"].append("extra")
    if change == "extra_animation": container["animations"].append(copy.deepcopy(container["animations"][0]))
    if change == "not_in": container["animations"][0]["type"] = "out"
    if change == "zero_duration": container["animations"][0]["duration"] = 0
    if change == "overrun": container["animations"][0]["duration"] = 2000001
    if change == "hidden": segment["visible"] = False
    if change == "shared_ref": draft["tracks"][0]["segments"][0]["extra_material_refs"] = [container["id"]]
    before = copy.deepcopy(draft)
    with pytest.raises(ValueError): mix_action_audio(draft, audio, ctx)
    assert draft == before


@pytest.mark.parametrize("field,value", [("action_time_us", 700000), ("start_us", 700000),
                                       ("action_anchor", "fake"), ("action_operation_id", "audio")])
def test_false_explicit_or_noncompiled_sound_anchor_rejected_atomically(tmp_path, field, value):
    draft, spec, ctx, audio, event = with_sound(tmp_path)
    draft, _ = apply_text_animation(draft, spec, ctx)
    before = copy.deepcopy(draft)
    event[field] = value
    with pytest.raises(ValueError): mix_action_audio(draft, audio, ctx)
    assert draft == before


def test_explicit_matching_anchor_times_are_accepted_and_animation_drift_is_rejected(tmp_path):
    draft, spec, ctx, audio, event = with_sound(tmp_path)
    event.update(action_time_us=800000, start_us=790000)
    draft, _ = apply_text_animation(draft, spec, ctx)
    result, _ = mix_action_audio(draft, audio, ctx)
    assert validate_action_audio(ctx.plan, result)["ok"]
    result["materials"]["material_animations"][-1]["animations"][0]["duration"] = 200000
    assert not validate_action_audio(ctx.plan, result)["ok"]


def test_sound_requires_animation_to_have_run_and_never_clamps_negative_start(tmp_path):
    draft, spec, ctx, audio, event = with_sound(tmp_path, "text_intro_start")
    with pytest.raises(ValueError): mix_action_audio(draft, audio, ctx)
    draft["tracks"][1]["segments"][0]["target_timerange"]["start"] = 0
    draft, _ = apply_text_animation(draft, spec, ctx)
    with pytest.raises(ValueError, match="nonnegative"): mix_action_audio(draft, audio, ctx)


def test_preview_can_defer_native_evidence_and_seal_cannot(tmp_path):
    draft, spec, ctx = case(tmp_path)
    assert validate_text_animation_plan(ctx.plan, plan_path=ctx.plan_path, require_native_evidence=False)["ok"]
    assert not validate_text_animation_plan(ctx.plan, plan_path=ctx.plan_path, require_native_evidence=True)["ok"]
    result, _ = apply_text_animation(draft, spec, ctx)
    preview = tmp_path / "preview.json"
    write(preview, result)
    ctx.plan["preview_output"] = str(preview)
    spec["current_video_evidence"] = {"current_video_ready": True, "native_visual_verified": True, "project_id": "test",
                                      "operation_id": spec["id"], "candidate_sha256": digest(preview),
                                      "build_contract_sha256": build_contract_sha256(ctx.plan)}
    report = validate_text_animation_plan(ctx.plan, plan_path=ctx.plan_path, require_native_evidence=True)
    assert not report["ok"] and any("阶段" in message for message in report["errors"])


def test_visual_event_wrong_operation_kind_or_event_is_rejected(tmp_path):
    draft, spec, ctx = case(tmp_path)
    ctx.plan["visual_events"][0]["techniques"][0]["kind"] = "keyframes"
    assert not validate_visual_events(ctx.plan)["ok"]
    with pytest.raises(ValueError): apply_text_animation(draft, spec, ctx)
    ctx.plan["visual_events"][0]["techniques"][0]["kind"] = "text_animation"
    spec["visual_event_id"] = "wrong"
    assert not validate_visual_events(ctx.plan)["ok"]


def test_unmarked_existing_animations_keep_legacy_behavior():
    draft = {"materials": {"material_animations": [{"id": "old", "animations": [{}]}]}, "tracks": []}
    assert validate_text_animations({"operations": []}, draft)["ok"]
