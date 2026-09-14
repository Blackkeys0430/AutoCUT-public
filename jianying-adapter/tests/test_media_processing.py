from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from jianying_adapter.cli import main
from jianying_adapter.material_library import (
    attach_material_handoff, material_handoff, probe_media, process_material, register_material, validate_plan_materials,
)
from jianying_adapter.media_ledger import DEFAULT_POLICY, load_json, validate_media_ledger
from jianying_adapter.media_processing import video_frames
from jianying_adapter.shared_operations import add_broll


pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                reason="real local FFmpeg is required")


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def source_video(path, *, alphas=None, vfr=False):
    alphas = alphas or [255] * 12
    frames = b"".join(Image.new("RGBA", (48, 32), (20 + i * 15, 12, 24, alpha)).tobytes()
                      for i, alpha in enumerate(alphas))
    command = ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "48x32", "-r", "12", "-i", "pipe:0"]
    if vfr:
        command += ["-vf", "settb=1/12000,setpts=if(lt(N\\,6)\\,N*1000\\,6000+(N-6)*2000)", "-fps_mode", "vfr", "-an"]
    else:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-c:a", "pcm_s16le"]
    command += ["-c:v", "qtrle", "-pix_fmt", "argb", "-video_track_timescale", "1000000", str(path)]
    subprocess.run(command, input=frames, capture_output=True, check=True)
    return path


def pixels(path, *, image=False):
    if image:
        with Image.open(path) as im:
            return [im.convert("RGBA").getpixel((0, 0))]
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-an",
                             "-pix_fmt", "rgba", "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
                            capture_output=True, check=True)
    return [tuple(result.stdout[i:i + 4]) for i in range(0, len(result.stdout), 48 * 32 * 4)]


@pytest.fixture
def source(tmp_path):
    path = source_video(tmp_path / "source.mov")
    probe = probe_media(path, "video")
    config = {"library_root": str(tmp_path / "library")}
    request = {"id": "source", "node_id": "technical", "visual_event_id": "sample",
               "purpose": "Synthetic frame-order sample", "composition": "Whole frame",
               "truth_role": "illustration", "media_type": "video", "query_en": "technical sample"}
    review = {"decision": "accept", "scope": "video_frames", "reviewed_path": str(path),
              "source_timerange": {"start": 0, "duration": probe["duration_us"]},
              "content": "Synthetic numbered color frames", "composition": "Whole frame", "quality": "Fixture pixel values known"}
    selection = {"asset_id": "source", "local_path": str(path), "source_kind": "local", "provider": "test-fixture",
                 "authorization_source": "Synthetic fixture created for this test", "license_status": "user_authorized",
                 "editorial_only": False, "attribution_required": False, "visual_review": review}
    asset = register_material(request, selection, config)
    return tmp_path, config, request, asset


def processing(mode="video", **changes):
    parameters = {"mode": mode, "source_timerange": {"start": 166667, "duration": 666666}}
    if mode == "video":
        parameters.update(output_fps="12", output_format="qtrle_mov")
    parameters.update(changes)
    return {"asset_id": "processed", "source_asset_id": "source",
            "authorization_source": "Run the requested shared-module test", "parameters": parameters}


def reviewed_selection(result):
    selection = copy.deepcopy(result["selection"])
    selection["visual_review"] = {"decision": "accept", "reviewed_path": selection["local_path"],
        "scope": "image" if result["media_type"] == "image" else "video_frames",
        "content": "Decoded output compared with synthetic source pixels", "composition": "Whole frame", "quality": "Frame mapping verified"}
    if result["media_type"] == "video":
        selection["visual_review"]["source_timerange"] = {"start": 0, "duration": result["probe"]["duration_us"]}
    return selection


@pytest.mark.parametrize("alpha,visible,transparent", [(0, False, True), (128, True, True), (255, True, False)])
def test_video_alpha_uses_actual_pixels_not_just_pixel_format(tmp_path, alpha, visible, transparent):
    path = source_video(tmp_path / "alpha.mov", alphas=[alpha] * 12)
    result = probe_media(path, "video")
    assert result["alpha_channel"] is True
    assert result["alpha"] is transparent and result["visible"] is visible
    assert result["alpha_evidence"]["decoded_frames"] == 12
    assert result["alpha_evidence"]["minimum"] == alpha * 257
    assert result["alpha_evidence"]["maximum"] == alpha * 257


def test_alpha_probe_includes_late_reveal_and_webm_alpha_side_stream(tmp_path):
    source = source_video(tmp_path / "late.mov", alphas=[0] * 11 + [128])
    assert probe_media(source, "video")["visible"] is True
    webm = tmp_path / "late.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-an", "-c:v", "libvpx-vp9",
                    "-lossless", "1", "-pix_fmt", "yuva420p", str(webm)], check=True, capture_output=True)
    probe = probe_media(webm, "video")
    assert probe["pixel_format"] == "yuv420p"  # Native ffprobe decoder hides the alpha side stream.
    assert probe["alpha"] and probe["visible"]
    assert probe["alpha_evidence"]["decoder"] == "libvpx-vp9"
    assert probe["alpha_evidence"]["decoded_frames"] == 12


def test_webm_duration_excludes_nonzero_container_offset(tmp_path):
    source = source_video(tmp_path / "source.mov", alphas=[128] * 12)
    webm = tmp_path / "shifted.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-an", "-c:v", "libvpx-vp9",
                    "-lossless", "1", "-pix_fmt", "yuva420p", "-output_ts_offset", "5", str(webm)],
                   check=True, capture_output=True)
    probe = probe_media(webm, "video")
    assert probe["start_time_us"] == 5_000_000
    assert probe["duration_us"] == 1_000_000
    assert probe["duration_evidence"] == "video_packet_pts"
    assert probe["alpha"] and probe["visible"]


@pytest.mark.parametrize("reverse,speed,step,fps", [(False, 1, 1, "12"), (True, 1, 1, "12"),
    (True, 2, 1, "24"), (True, 2.5, 2, "30000/1001"), (False, 0.5, 3, "10")])
def test_video_processing_maps_every_output_frame_to_actual_source(source, reverse, speed, step, fps):
    _, config, request, _ = source
    result = process_material(request, processing(reverse=reverse, speed=speed, frame_step=step, output_fps=fps), config)
    rows = result["selection"]["derivation"]["frame_map"]
    output_pixels = pixels(result["selection"]["local_path"])
    assert len(output_pixels) == len(rows)
    assert [p[0] for p in output_pixels] == [20 + 15 * row["source_frame_index"] for row in rows]
    assert all(166667 <= row["source_time_us"] < 833333 for row in rows)
    assert result["probe"]["audio_stream_count"] == 0
    assert "visual_review" not in result["selection"]
    assert not (Path(config["library_root"]) / "processed").exists()  # Rendering never auto-approves/registers.


@pytest.mark.parametrize("frame", ["first", "last"])
def test_extract_frame_uses_half_open_range_and_real_pts(source, frame):
    _, config, request, asset = source
    frames = video_frames(Path(asset["local_path"]), asset["probe"])
    selected = [f for f in frames if 166667 <= f["source_time_us"] < 833333]
    expected = selected[0 if frame == "first" else -1]
    result = process_material({**request, "media_type": "image"}, processing("frame", frame=frame), config)
    assert result["selection"]["derivation"]["frame_map"][0]["source_time_us"] == expected["source_time_us"]
    assert pixels(result["selection"]["local_path"], image=True)[0][0] == 20 + 15 * expected["source_frame_index"]


def test_vfr_source_uses_display_timestamps_and_correct_reverse_mapping(source):
    folder, config, request, _ = source
    parent = load_json(Path(config["library_root"]) / "source" / "ledger.json")["assets"][0]
    source_path = source_video(folder / "vfr.mov", vfr=True)
    probe = probe_media(source_path, "video")
    parent.update(asset_id="vfr", local_path=str(source_path))
    parent["visual_review"].update(reviewed_path=str(source_path), source_timerange={"start": 0, "duration": probe["duration_us"]})
    register_material(request, parent, config)
    spec = processing(reverse=True, frame_step=2, output_fps="24")
    spec["source_asset_id"] = "vfr"
    spec["parameters"]["source_timerange"] = {"start": 0, "duration": probe["duration_us"]}
    result = process_material(request, spec, config)
    rows = result["selection"]["derivation"]["frame_map"]
    assert [p[0] for p in pixels(result["selection"]["local_path"])] == [20 + 15 * row["source_frame_index"] for row in rows]


def test_nonzero_source_pts_and_transparent_freeze(source):
    folder, config, request, _ = source
    original = source_video(folder / "transparent.mov", alphas=[128] * 12)
    shifted = folder / "shifted.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(original), "-map", "0:v:0", "-c", "copy",
                    "-output_ts_offset", "5", str(shifted)], check=True, capture_output=True)
    parent = load_json(Path(config["library_root"]) / "source" / "ledger.json")["assets"][0]
    parent.update(asset_id="shifted", local_path=str(shifted))
    parent["visual_review"]["reviewed_path"] = str(shifted)
    asset = register_material(request, parent, config)
    assert asset["probe"]["start_time_us"] == 5_000_000
    spec = processing("frame", frame="last")
    spec["source_asset_id"] = "shifted"
    result = process_material({**request, "media_type": "image", "requirements": {"alpha_required": True}}, spec, config)
    assert pixels(result["selection"]["local_path"], image=True)[0] == (155, 12, 24, 128)
    assert result["selection"]["derivation"]["frame_map"][0]["source_time_us"] == 750000


def test_grayscale_h264_and_transparent_prores_output(source):
    folder, config, request, _ = source
    result = process_material(request, processing(reverse=True, saturation=0, output_format="h264_mp4"), config)
    assert result["probe"]["codec"] == "h264"
    assert all(max(p[:3]) - min(p[:3]) <= 2 for p in pixels(result["selection"]["local_path"]))
    parent = load_json(Path(config["library_root"]) / "source" / "ledger.json")["assets"][0]
    path = source_video(folder / "alpha.mov", alphas=[128] * 12)
    parent.update(asset_id="alpha", local_path=str(path))
    parent["visual_review"]["reviewed_path"] = str(path)
    register_material(request, parent, config)
    spec = processing(output_format="prores4444_mov")
    spec.update(asset_id="alpha-prores", source_asset_id="alpha")
    alpha = process_material({**request, "requirements": {"alpha_required": True}}, spec, config)
    assert alpha["probe"]["codec"] == "prores"
    assert alpha["probe"]["alpha"] and alpha["probe"]["visible"]


def test_processed_asset_registers_and_roundtrips_through_existing_shared_operation(source):
    folder, config, request, _ = source
    result = process_material(request, processing(reverse=True), config)
    output_pixels = pixels(result["selection"]["local_path"])
    assert output_pixels[0][0] > output_pixels[-1][0]
    selection = reviewed_selection(result)
    asset = register_material(request, selection, config)
    request_path = write(folder / "request.json", request)
    review = {**selection["visual_review"], "reviewed_path": asset["local_path"]}
    usage = {"asset_id": asset["asset_id"], "start_us": 0, "end_us": result["probe"]["duration_us"],
             "clip": {"alpha": 1.0, "scale": {"x": 1.0, "y": 1.0}}, "visual_review": review}
    handoff = material_handoff(request_path, usage, config)
    plan = {"operations": [handoff["operation"]], "brolls": [handoff["broll"]], "visual_events": [{
        "id": "sample", "supporting_visual": {"status": "ready", "purpose": request["purpose"],
        "operation_ids": [handoff["operation"]["id"]]}}]}
    draft, _ = add_broll({"materials": {}, "tracks": []}, handoff["operation"], SimpleNamespace(plan_path=request_path, plan=plan))
    assert validate_plan_materials(plan, request_path, draft)["ok"]
    assert draft["tracks"][0]["segments"][0]["volume"] == 0
    ledger = load_json(asset["ledger_path"])
    assert Path(ledger["assets"][0]["processing_record_path"]).parent == Path(asset["ledger_path"]).parent
    # A processed illustration cannot be reused as evidence by changing only
    # the current request, even though the local source file still exists.
    write(request_path, {**request, "truth_role": "evidence"})
    with pytest.raises(ValueError, match="示意素材"):
        material_handoff(request_path, usage, config)
    write(request_path, request)
    # The output remains bound to its original parent even after registration.
    parent_path = Path(config["library_root"]) / "source" / "ledger.json"
    original = load_json(parent_path)["assets"][0]
    Path(original["local_path"]).write_bytes(b"changed source")
    assert not validate_plan_materials(plan, request_path, draft)["ok"]


@pytest.mark.parametrize("defect", ["license", "source_range", "output_duration", "output_hash", "truth"])
def test_derivation_cannot_launder_source_or_change_mapping(source, defect):
    _, config, request, _ = source
    result = process_material(request, processing(reverse=True), config)
    selection = reviewed_selection(result)
    if defect == "license":
        selection["license_status"] = "verified_commercial"
    elif defect == "source_range":
        selection["derivation"]["frame_map"][0]["source_time_us"] = 9999999
    elif defect == "output_duration":
        selection["derivation"]["frame_map"][-1]["output_duration_us"] += 9999
    elif defect == "output_hash":
        selection["derivation"]["output_sha256"] = "0" * 64
    else:
        request = {**request, "truth_role": "evidence"}
    with pytest.raises(ValueError, match="media_derivation_invalid"):
        register_material(request, selection, config)
    assert not (Path(config["library_root"]) / "processed").exists()


def test_processing_cli_is_shared_and_never_overwrites(source, capsys):
    folder, config, request, _ = source
    req = write(folder / "request.json", {**request, "media_type": "image"})
    spec = write(folder / "processing.json", processing("frame", frame="last"))
    cfg = write(folder / "config.json", config)
    output = folder / "result.json"
    args = ["material-process", str(req), str(spec), "--config", str(cfg), "--output", str(output)]
    assert main(args) == 0
    first = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == first
    assert "visual_review" not in load_json(output)["selection"]


def test_legacy_video_probe_is_refreshed_without_rewriting_ledger(source):
    folder, config, request, _ = source
    path = source_video(folder / "alpha.mov", alphas=[128] * 12)
    ledger_path = Path(config["library_root"]) / "source" / "ledger.json"
    original = load_json(ledger_path)["assets"][0]
    original.update(asset_id="alpha", local_path=str(path))
    original["visual_review"]["reviewed_path"] = str(path)
    asset = register_material(request, original, config)
    ledger = load_json(asset["ledger_path"])
    current_probe = ledger["assets"][0]["probe"]
    ledger["assets"][0]["probe"] = {key: current_probe[key] for key in ("width", "height", "duration_us", "visible")}
    ledger["assets"][0]["probe"]["alpha"] = False
    write(Path(asset["ledger_path"]), ledger)
    before = Path(asset["ledger_path"]).read_bytes()
    req = write(folder / "alpha-request.json", {**request, "requirements": {"alpha_required": True}})
    review = {**original["visual_review"], "reviewed_path": asset["local_path"]}
    handoff = material_handoff(req, {"asset_id": "alpha", "start_us": 0, "end_us": 1000000,
        "clip": {"alpha": 1, "scale": {"x": 1, "y": 1}}, "visual_review": review}, config)
    assert handoff["operation"]["source_path"] == asset["local_path"]
    assert Path(asset["ledger_path"]).read_bytes() == before


@pytest.mark.parametrize("mode", ["frame", "video"])
def test_derived_media_reaches_v2_assembler_with_native_shrink(source, mode, capsys):
    """Synthetic integration only: this fixture is never a real speech/draft proof."""
    from test_candidate_plan import make_plan
    from jianying_adapter.creative_plan import validate_creative_plan
    from jianying_adapter.media_crop import fit_region

    folder, config, request, _ = source
    request.update(media_type="image" if mode == "frame" else "video", visual_requirement_id="frame_order",
                   subject="Synthetic frame sequence", observable="Final frame hold" if mode == "frame" else "Reverse sequence and shrink",
                   time_behavior="still" if mode == "frame" else "graphic_motion")
    spec = processing(mode, source_timerange={"start": 0, "duration": 1_000_000},
                      **({"frame": "last"} if mode == "frame" else {"reverse": True}))
    processed = process_material(request, spec, config)
    actual = pixels(processed["selection"]["local_path"], image=mode == "frame")
    assert actual[0][0] == 185
    if mode == "video": assert actual[-1][0] == 20
    selection = reviewed_selection(processed)
    asset = register_material(request, selection, config)
    framing = {"canvas_size": [1080, 1920], "target_rect": [120, 120, 960, 680], "content_region": [0, 0, 1, 1]}
    clip = fit_region([48, 32], framing["canvas_size"], framing["target_rect"])["clip"]
    review = {**selection["visual_review"], "reviewed_path": asset["local_path"],
              "source_crop": [0, 0, 1, 1], "clip": copy.deepcopy(clip)}
    req = write(folder / "request.json", request)
    handoff = material_handoff(req, {"asset_id": asset["asset_id"], "start_us": 0, "end_us": 1_000_000,
                               "source_crop": [0, 0, 1, 1], "framing": framing, "clip": clip, "visual_review": review}, config)
    path = make_plan(folder)
    plan = load_json(path)
    plan["project_format"]["visual_planning_version"] = 2
    state_path = Path(plan["project_state"])
    state = {**load_json(state_path), "status": "planning", "visual_planning_min_version": 2}
    write(state_path, state)
    font = Path("C:/Windows/Fonts/msyh.ttc")  # Existing font read only; all fixture outputs remain under tmp_path.
    if not font.is_file(): pytest.skip("Windows font fixture unavailable")
    base = load_json(plan["base_draft"])
    base["materials"]["texts"][0]["content"] = json.dumps({"text": "完整语音", "styles": [
        {"range": [0, 4], "size": 18, "font": {"path": str(font)}}]}, ensure_ascii=False)
    base["tracks"][0]["segments"][0]["clip"] = {"scale": {"x": 1, "y": 1}, "transform": {"x": 0, "y": -.7}}
    write(Path(plan["base_draft"]), base)
    plan["creative_brief"] = {"viewer_takeaway": "Synthetic processing integration", "progression": "One fixture interval",
                              "visual_strategy": "Keep the color-frame sequence visible above the fixture subtitle",
                              "sound_strategy": "Processed media is silent"}
    need = {key: request[key] for key in ("subject", "observable", "time_behavior")}
    need.update(id="frame_order", delivery="media", request_path=str(req), operation_ids=[])
    plan["visual_events"] = [{"id": "sample", "start_us": 0, "end_us": 1_000_000, "speech_ids": ["s1"],
        "expression_role": "context", "audience_need": request["purpose"], "primary_visual": "media",
        "composition": request["composition"], "handbook_refs": ["handbook/broll"], "review_focus": "Read back the actual processed layer",
        "visual_requirements": [need], "techniques": [], "supporting_visual": {"status": "needs_asset",
        "purpose": request["purpose"], "request_paths": [str(req)], "operation_ids": []}}]
    plan = attach_material_handoff(plan, path, handoff)
    if mode == "video":
        frames = [{"time_offset_us": time, "scale_x": clip["scale"]["x"] * factor, "scale_y": clip["scale"]["y"] * factor,
                   "position_x": clip["transform"]["x"], "position_y": clip["transform"]["y"]}
                  for time, factor in ((0, 1), (1_000_000, .7))]
        plan["operations"].append({"id": "shrink", "kind": "animate_broll_transform", "node_id": handoff["broll"]["node_id"],
            "track_name": handoff["broll"]["track_name"], "position_space": "normalized", "interpolation": "linear", "keyframes": frames})
        plan["visual_events"][0]["techniques"].append({"kind": "keyframes", "operation_ids": ["shrink"]})
    write(path, plan)
    assert main(["candidate-preview", str(path)]) == 0
    candidate = load_json(plan["preview_output"])
    report = validate_creative_plan(plan, draft=candidate, plan_path=path)
    assert report["ok"], report["errors"]
    assert not report["native_visual_verified"]
    assert validate_plan_materials(plan, path, candidate)["ok"]
    assert load_json(plan["preview_manifest"])["writer_allowed"] is False
    assert load_json(state_path) == state
    layer = next(t for t in candidate["tracks"] if t.get("name") == handoff["broll"]["track_name"])
    segment = layer["segments"][0]
    assert segment["target_timerange"]["duration"] == 1_000_000 and segment["volume"] == 0
    if mode == "video":
        assert len(segment["common_keyframes"]) == 4
        broken = copy.deepcopy(candidate)
        next(t for t in broken["tracks"] if t.get("name") == layer["name"])["segments"][0]["common_keyframes"] = []
        assert not validate_creative_plan(plan, draft=broken, plan_path=path)["ok"]
    capsys.readouterr()
