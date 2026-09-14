from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from jianying_adapter.cli import main
from jianying_adapter.material_library import (
    attach_material_handoff, plan_material_requests,
    generation_brief, material_handoff, register_material, search_materials,
    validate_plan_materials,
)
from jianying_adapter.media_ledger import DEFAULT_POLICY, load_json, validate_media_ledger
from jianying_adapter.shared_operations import add_broll


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path):
    config = {"library_root": str(tmp_path / "library"),
              "navigation_data": str(tmp_path / "navigation.json"),
              "keyword_guide": str(tmp_path / "keywords.md")}
    write_json(Path(config["navigation_data"]), {"sites": [{"id": "test-image", "name": "Test site", "category": "image",
        "url": "https://example.org/images/", "commercial_allowed": True, "keywords_en": ["shop owner"]}]})
    Path(config["keyword_guide"]).write_text("| 店主 | shop owner |\n", encoding="utf-8")
    request = {"id": "shop-scene", "node_id": "shop", "visual_event_id": "choice", "purpose": "展示经营店铺的情境",
               "composition": "店主在左，右侧预留文字区域", "truth_role": "illustration", "media_type": "image",
               "query_zh": "店主", "query_en": "shop owner", "requirements": {"min_width": 100, "min_height": 80}}
    source = tmp_path / "source.png"
    Image.new("RGBA", (320, 180), (20, 80, 120, 128)).save(source)
    review = {"decision": "accept", "reviewed_path": str(source), "scope": "image",
              "content": "Test fixture colored rectangle", "composition": "Whole frame is visible", "quality": "Pixels decode"}
    selection = {"asset_id": "shop-001", "local_path": str(source), "source_kind": "local", "provider": "user",
                 "authorization_source": "Test fixture generated for this test", "license_status": "user_authorized",
                 "editorial_only": False, "attribution_required": False, "visual_review": review}
    return tmp_path, config, request, selection


def usage_for(asset, selection):
    review = {**selection["visual_review"], "reviewed_path": asset["local_path"]}
    return {"asset_id": asset["asset_id"], "start_us": 0, "end_us": 1_000_000,
            "clip": {"alpha": 1.0, "scale": {"x": 0.5, "y": 0.5}, "transform": {"x": 0.0, "y": 0.0}},
            "visual_review": review}


def plan_for(handoff):
    return {"operations": [handoff["operation"]], "brolls": [handoff["broll"]], "visual_events": [{
        "id": handoff["visual_event_id"], "start_us": 0, "end_us": 1_000_000,
        "audience_need": "Test material use", "primary_visual": "media", "composition": "Test fixture whole frame",
        "techniques": [{"kind": "media", "operation_ids": [handoff["operation"]["id"]]}],
        "supporting_visual": {"status": "ready", "purpose": "Test material use", "operation_ids": [handoff["operation"]["id"]]}}]}


def test_declared_requests_survive_partial_handoff_and_cannot_be_hidden(setup):
    folder, config, request, selection = setup
    asset = register_material(request, selection, config)
    first = write_json(folder / 'first.json', request)
    second_request = {**request, 'id': 'second-scene', 'node_id': 'second'}
    second = write_json(folder / 'second.json', second_request)
    first_handoff = material_handoff(first, usage_for(asset, selection), config)
    p = plan_for(first_handoff)
    p['operations'], p['brolls'], p['visual_events'][0]['techniques'] = [], [], []
    support = p['visual_events'][0]['supporting_visual']
    support.update(status='needs_asset', operation_ids=[], request_paths=[str(first), str(second)])
    plan_path = folder / 'candidate_plan.json'
    assert plan_material_requests(p, plan_path)['pending_count'] == 2
    assert not validate_plan_materials(p, plan_path)['ok']
    attached = attach_material_handoff(p, plan_path, first_handoff)
    assert not p['operations']  # Original plan is never rewritten in place.
    assert attached['visual_events'][0]['supporting_visual']['status'] == 'needs_asset'
    assert plan_material_requests(attached, plan_path)['pending_count'] == 1
    assert not validate_plan_materials(attached, plan_path)['ok']
    hidden = json.loads(json.dumps(attached))
    hidden['visual_events'][0]['supporting_visual']['status'] = 'not_needed'
    assert any('not_needed' in e for e in validate_plan_materials(hidden, plan_path)['errors'])
    second_handoff = material_handoff(second, usage_for(asset, selection), config)
    attached = attach_material_handoff(attached, plan_path, second_handoff)
    assert attached['visual_events'][0]['supporting_visual']['status'] == 'ready'
    assert len(attached['operations']) == len(attached['brolls']) == 2
    assert validate_plan_materials(attached, plan_path)['ok']
    assert attach_material_handoff(attached, plan_path, second_handoff) == attached


def test_material_use_cli_attaches_all_bindings_to_a_new_plan(setup, capsys):
    folder, config, request, selection = setup
    asset = register_material(request, selection, config)
    request_path = write_json(folder / 'request.json', request)
    usage = write_json(folder / 'usage.json', usage_for(asset, selection))
    config_path = write_json(folder / 'config.json', config)
    p = {'visual_events': [{'id': 'choice', 'start_us': 0, 'end_us': 1_000_000,
         'supporting_visual': {'status': 'needs_asset', 'purpose': request['purpose'],
                               'request_paths': [str(request_path)], 'operation_ids': []}, 'techniques': []}]}
    original = write_json(folder / 'plan.json', p)
    output = folder / 'next.json'
    assert main(['material-use', str(request_path), str(usage), '--config', str(config_path),
                 '--plan', str(original), '--output', str(output)]) == 0
    result = load_json(output)
    assert len(result['operations']) == len(result['brolls']) == 1
    assert result['visual_events'][0]['techniques'][0]['operation_ids'] == [result['operations'][0]['id']]
    assert validate_plan_materials(result, output)['ok']
    assert load_json(original) == p
    assert 'writer_allowed' not in result and 'current_video_ready' not in result
    assert main(['material-use', str(request_path), str(usage), '--config', str(config_path),
                 '--plan', str(original), '--output', str(output)]) == 2
    assert load_json(original) == p


def test_freeze_search_use_and_native_image_roundtrip(setup):
    folder, config, request, selection = setup
    result = register_material(request, selection, config)
    assert Path(result["preview_path"]).is_file()
    assert Path(result["local_path"]).read_bytes() == Path(selection["local_path"]).read_bytes()
    with Image.open(result["local_path"]) as im:
        assert im.mode == "RGBA" and im.getchannel("A").getextrema() == (128, 128)
    ledger = load_json(result["ledger_path"])
    assert validate_media_ledger(ledger, policy=load_json(DEFAULT_POLICY)).ok
    found = search_materials(request, config)
    assert found["local_matches"][0]["asset_id"] == result["asset_id"]
    assert found["web_search_executed"] is False
    assert "commercial_allowed" not in found["source_candidates"][0]
    assert found["source_candidates"][0]["item_license_check_required"] is True
    request_path = write_json(folder / "request.json", request)
    handoff = material_handoff(request_path, usage_for(result, selection), config)
    plan = plan_for(handoff)
    context = SimpleNamespace(plan_path=folder / "plan.json", plan=plan)
    draft, report = add_broll({"materials": {}, "tracks": []}, handoff["operation"], context)
    assert report["media_asset"]["asset_id"] == "shop-001"
    assert draft["materials"]["videos"][0]["type"] == "photo"
    assert draft["materials"]["videos"][0]["width"] == 320
    assert validate_plan_materials(plan, context.plan_path, draft)["ok"]
    # Detect an actual material substitution at assembly / Writer readback.
    draft["materials"]["videos"][0]["path"] = selection["local_path"]
    assert not validate_plan_materials(plan, context.plan_path, draft)["ok"]
    # Asset bytes changing after selection must stop an operation before mutation.
    Path(result["local_path"]).write_bytes(b"changed")
    clean = {"materials": {}, "tracks": []}
    with pytest.raises(ValueError, match="media_sha256_mismatch"):
        add_broll(clean, handoff["operation"], context)
    assert clean == {"materials": {}, "tracks": []}


@pytest.mark.parametrize("failure", ["unviewed", "wrong_file", "small", "opaque", "blank", "unlicensed", "broken"])
def test_bad_selection_is_not_registered(setup, failure):
    _, config, request, selection = setup
    if failure == "unviewed":
        selection["visual_review"] = {}
    elif failure == "wrong_file":
        selection["visual_review"]["reviewed_path"] = str(Path(selection["local_path"]).with_name("other.png"))
    elif failure == "small":
        request["requirements"]["min_width"] = 1000
    elif failure == "opaque":
        Image.new("RGB", (320, 180)).save(selection["local_path"])
        request["requirements"]["alpha_required"] = True
    elif failure == "blank":
        Image.new("RGBA", (320, 180), (0, 0, 0, 0)).save(selection["local_path"])
    elif failure == "unlicensed":
        selection["source_kind"] = "download"
        selection["provider"] = "external"
        selection["license_status"] = "unverified"
    else:
        Path(selection["local_path"]).write_bytes(b"not media")
    with pytest.raises((ValueError, OSError)):
        register_material(request, selection, config)
    assert not (Path(config["library_root"]) / selection["asset_id"]).exists()


def completed_search():
    # Synthetic search outcomes exercise the branch; no live search is claimed.
    return [{"stage": "local", "query": "shop", "source": "test catalog", "outcome": "no_results", "reason": "fixture empty"},
            {"stage": "web", "query": "shop owner", "source": "https://example.org", "outcome": "rejected", "reason": "fixture crop",
             "reviewed_candidates": [{"source": "https://example.org/item", "scope": "image", "reason": "fixture subject is cropped"}]}]


def test_generation_requires_search_and_cannot_replace_evidence(setup):
    _, _, request, _ = setup
    with pytest.raises(ValueError, match="local"):
        generation_brief(request)
    request["search_attempts"] = completed_search()
    request["requirements"]["alpha_required"] = True
    brief = generation_brief(request)
    assert brief["tool"] == "image_gen" and brief["generated"] is False
    assert "alpha" in brief["arguments"]["prompt"]
    assert request["composition"] in brief["arguments"]["prompt"]
    request["truth_role"] = "evidence"
    with pytest.raises(ValueError, match="事实证据"):
        generation_brief(request)
    request["truth_role"] = "illustration"
    request["search_attempts"][1]["reviewed_candidates"] = []
    with pytest.raises(ValueError, match="已查看"):
        generation_brief(request)


def test_generated_asset_stores_provenance_and_reuse_needs_new_review(setup):
    folder, config, request, selection = setup
    request["search_attempts"] = completed_search()
    selection.update(source_kind="generated", provider="image_gen", license_status="project_generated",
                     generation={"method": "test fixture, not an actual image_gen call", "description": "test pixels"})
    result = register_material(request, selection, config)
    request_path = write_json(folder / "reuse.json", request)
    usage = usage_for(result, selection)
    usage["visual_review"] = selection["visual_review"]
    with pytest.raises(ValueError, match="不是同一文件"):
        material_handoff(request_path, usage, config)
    usage = usage_for(result, selection)
    request["truth_role"] = "evidence"
    write_json(request_path, request)
    with pytest.raises(ValueError, match="事实证据"):
        material_handoff(request_path, usage, config)
    assert search_materials(request, config)["local_matches"] == []


def test_registration_never_overwrites_and_forbids_c_drive(setup):
    _, config, request, selection = setup
    result = register_material(request, selection, config)
    original = Path(result["local_path"]).read_bytes()
    with pytest.raises(ValueError, match="已存在"):
        register_material(request, selection, config)
    assert Path(result["local_path"]).read_bytes() == original
    if Path("C:/").is_absolute():
        with pytest.raises(ValueError, match="C 盘"):
            register_material(request, selection, {**config, "library_root": "C:/forbidden-test"})


def test_material_cli_search_register_use(setup, capsys):
    folder, config, request, selection = setup
    conf = write_json(folder / "config.json", config)
    req = write_json(folder / "request.json", request)
    sel = write_json(folder / "selection.json", selection)
    common = ["--config", str(conf)]
    assert main(["material-search", str(req), *common]) == 0
    assert json.loads(capsys.readouterr().out)["web_search_executed"] is False
    assert main(["material-register", str(req), str(sel), *common]) == 0
    registered = json.loads(capsys.readouterr().out)
    usage = write_json(folder / "usage.json", usage_for(registered, selection))
    output = folder / "handoff.json"
    assert main(["material-use", str(req), str(usage), *common, "--output", str(output)]) == 0
    capsys.readouterr()
    assert load_json(output)["operation"]["media_asset"]["asset_id"] == "shop-001"


def test_video_source_window_and_native_material(setup):
    folder, config, request, selection = setup
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("local ffmpeg/ffprobe required")
    source = folder / "video.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30",
                    "-t", "2", "-pix_fmt", "yuv420p", str(source)], check=True, capture_output=True)
    request["media_type"] = "video"
    request["requirements"]["duration_us"] = 1_000_000
    selection["local_path"] = str(source)
    selection["visual_review"].update(reviewed_path=str(source), scope="video_segment",
                                      source_timerange={"start": 500_000, "duration": 1_000_000})
    result = register_material(request, selection, config)
    assert Path(result["preview_path"]).is_file()
    usage = usage_for(result, selection)
    usage["source_start_us"] = 500_000
    request_path = write_json(folder / "request.json", request)
    handoff = material_handoff(request_path, usage, config)
    plan = plan_for(handoff)
    draft, _ = add_broll({"materials": {}, "tracks": []}, handoff["operation"], SimpleNamespace(plan_path=request_path, plan=plan))
    assert draft["materials"]["videos"][0]["type"] == "video"
    assert validate_plan_materials(plan, request_path, draft)["ok"]
    usage["source_start_us"] = 0
    with pytest.raises(ValueError, match="已查看"):
        material_handoff(request_path, usage, config)


def test_unbound_event_fails_but_existing_plans_still_load(setup):
    folder, config, request, selection = setup
    result = register_material(request, selection, config)
    req = write_json(folder / "request.json", request)
    handoff = material_handoff(req, usage_for(result, selection), config)
    plan = plan_for(handoff)
    plan["visual_events"][0]["id"] = "wrong-event"
    assert not validate_plan_materials(plan, req)["ok"]
    del handoff["operation"]["media_asset"]
    assert validate_plan_materials(plan, req)["ok"]


def test_candidate_entry_rechecks_frozen_material(setup):
    from test_candidate_plan import make_plan
    from jianying_adapter.candidate_plan import validate_actual_content, validate_candidate_plan

    folder, config, request, selection = setup
    path = make_plan(folder)
    plan = load_json(path)
    plan["project_format"]["visual_planning_version"] = 1
    asset = register_material(request, selection, config)
    req = write_json(folder / "material-request.json", request)
    handoff = material_handoff(req, usage_for(asset, selection), config)
    plan.update(plan_for(handoff))
    result = validate_candidate_plan(plan, plan_path=path)
    assert result.ok, result.errors
    draft, _ = add_broll(load_json(plan["base_draft"]), handoff["operation"], SimpleNamespace(plan_path=path, plan=plan))
    assert validate_actual_content(draft, plan, plan_path=path)["ok"]
    draft["tracks"][-1]["segments"][0]["source_timerange"]["start"] = 50
    assert not validate_actual_content(draft, plan, plan_path=path)["ok"]
    Path(asset["local_path"]).write_bytes(b"modified frozen media")
    assert any("media_sha256_mismatch" in error for error in validate_candidate_plan(plan, plan_path=path).errors)


@pytest.mark.parametrize("substitute", [False, True])
def test_writer_transaction_checks_material_readback(setup, monkeypatch, substitute):
    from test_candidate_plan import make_plan
    from test_preview_writer_transaction import _transaction
    from jianying_adapter.candidate_plan import validate_actual_content

    folder, config, request, selection = setup
    writer, args, state_path, manifest, before_state, before_manifest, before_meta = _transaction(folder, monkeypatch)
    (folder / "base").mkdir()
    base_plan_path = make_plan(folder / "base")
    base_plan = load_json(base_plan_path)
    asset = register_material(request, selection, config)
    req = write_json(folder / "material-request.json", request)
    handoff = material_handoff(req, usage_for(asset, selection), config)
    plan = load_json(args.plan)
    plan.update(plan_for(handoff))
    plan["content_gate"] = load_json(base_plan["semantic_gate"])["content_gate"]
    draft, _ = add_broll(load_json(base_plan["base_draft"]), handoff["operation"], SimpleNamespace(plan_path=args.plan, plan=plan))
    draft.update(id="test-id", platform={"app_version": "8.8.0"}, last_modified_platform={"app_version": "8.8.0"})
    write_json(folder / "preview" / "draft_content.json", draft)
    write_json(args.plan, plan)
    monkeypatch.setattr(writer, "validate_actual_content", validate_actual_content)
    assert validate_actual_content(draft, plan, plan_path=args.plan)["ok"]
    if substitute:
        original = writer.materializer.apply_candidate

        def wrong_material(operation):
            result = original(operation)
            path = args.draft_root / "TEST_ONLY" / "draft_content.json"
            actual = load_json(path)
            actual["materials"]["videos"][0]["path"] = selection["local_path"]
            write_json(path, actual)
            return result

        monkeypatch.setattr(writer.materializer, "apply_candidate", wrong_material)
        with pytest.raises(RuntimeError, match="可恢复回滚"):
            writer.main()
        assert load_json(args.registration)["status"] == "writer_failed_rolled_back"
        assert state_path.read_bytes() == before_state
        assert (args.draft_root / "root_meta_info.json").read_bytes() == before_meta
        assert not (args.draft_root / "TEST_ONLY").exists()
    else:
        assert writer.main() == 0
        registration = load_json(args.registration)
        assert registration["actual_caption_coverage"]["material_library"]["checked_assets"][0]["asset_id"] == asset["asset_id"]
    assert manifest.read_bytes() == before_manifest
