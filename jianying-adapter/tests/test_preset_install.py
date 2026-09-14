import copy
import json
from pathlib import Path

import pytest

from jianying_adapter import preset_install as installer


def fixture_package(root: Path, folder: str, *, preset_id="preset-1", text="字幕", effect="effect-123", resource=None):
    directory = root / folder
    metadata = {"id": preset_id, "name": folder, "create_time": 10, "rough_cut_start": 0, "rough_cut_duration": 3_000_000, "cover_path": "old/cover.jpeg", "version": "3.0"}
    inner = {"id": "draft-A", "platform": {"app_version": "8.8.0"}, "duration": 3_000_000,
             "materials": {"texts": [{"id": "text-A", "content": json.dumps({"text": text, "styles": []})}],
                           "material_animations": [{"id": "anim-A", "animations": [{"resource_id": effect, "type": "in"}]}]},
             "tracks": [{"id": "track-A", "type": "text", "segments": [{"id": "segment-A", "material_id": "text-A", "extra_material_refs": ["anim-A"], "target_timerange": {"start": 0, "duration": 3_000_000}}]}]}
    if resource:
        inner["materials"]["texts"][0]["font_path"] = str(resource)
    payload = {"materials": {"drafts": [{"draft": inner}]}}
    installer.write_json(directory / (folder + ".json"), metadata)
    (directory / (folder + ".jpeg")).write_bytes(b"valid cover bytes")
    installer.write_json(directory / "preset_draft/draft_content.json", payload)
    return directory, metadata, payload


def target_root(tmp_path):
    target = tmp_path / "Combination/Presets"
    installer.write_json(target / installer.STORE, {"preset_virtual_store": [{"type": 0, "value": []}, {"type": 1, "value": []}]})
    return target


def test_catalog_binding_requires_install_and_rebases_alias_slot_ids(tmp_path):
    source_root = tmp_path / "source"
    source, _, payload = fixture_package(source_root, "original")
    target = target_root(tmp_path)
    manifest = installer.prepare(source_root, target, tmp_path / "cache", tmp_path / "work")
    path = source / "preset_draft/draft_content.json"
    catalog = {"preset_root": str(source_root), "candidates": [{
        "template_id": "T", "source_path": str(path), "display_name": "字幕",
        "slots": {"text": [{"default_text": "字幕", "locators": [{"material_id": "old-alias-text"}]}]},
        "status": "candidate_unvalidated"}]}
    unchanged, report = installer.bind_catalog(catalog, [manifest])
    assert report["bound_count"] == 0
    assert unchanged == catalog
    import shutil
    row = manifest["packages"][0]
    shutil.copytree(row["stage"], row["target"])
    bound, report = installer.bind_catalog(catalog, [manifest])
    assert report["bound_count"] == 1
    candidate = bound["candidates"][0]
    assert candidate["source_path"] == str(Path(row["target"]) / "preset_draft/draft_content.json")
    assert candidate["slots"]["text"][0]["locators"][0]["material_id"] == "text-A"
    assert candidate["status"] == "candidate_unvalidated"
    assert candidate["original_source_path"] == str(path)
    assert catalog["candidates"][0]["slots"]["text"][0]["locators"][0]["material_id"] == "old-alias-text"
    # A locally edited destination is not the package that this manifest verified.
    installer.write_json(Path(candidate["source_path"]), {"changed": True})
    _, report = installer.bind_catalog(catalog, [manifest])
    assert report["bound_count"] == 0


def test_content_identity_preserves_text_animation_and_instance_relationships(tmp_path):
    _, meta, payload = fixture_package(tmp_path, "source")
    same = copy.deepcopy(payload)
    inner = same["materials"]["drafts"][0]["draft"]
    inner["platform"] = {"app_version": "9.8.0"}
    inner["update_time"] = 999
    inner["id"] = "other-draft"
    inner["materials"]["texts"][0]["id"] = "other-text"
    inner["tracks"][0]["segments"][0]["material_id"] = "other-text"
    assert installer.content_identity(payload, meta) == installer.content_identity(same, meta)
    inner["materials"]["texts"][0]["content"] = '{"text":"不同字幕","styles":[]}'
    assert installer.content_identity(payload, meta) != installer.content_identity(same, meta)
    inner["materials"]["texts"][0]["content"] = payload["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["content"]
    inner["materials"]["material_animations"][0]["animations"][0]["resource_id"] = "other-effect"
    assert installer.content_identity(payload, meta) != installer.content_identity(same, meta)


def test_prepare_deduplicates_copies_but_reidentifies_same_id_variants(tmp_path):
    source = tmp_path / "source"
    fixture_package(source, "A")
    fixture_package(source, "Copy A")
    fixture_package(source, "B", text="另一个内容")
    target = target_root(tmp_path)
    result = installer.prepare(source, target, tmp_path / "cache", tmp_path / "work")
    rows = result["packages"]
    assert result["summary"]["source_draft_count"] == 3
    assert result["summary"]["duplicate_source_copies_skipped"] == 1
    assert len(rows) == 2
    assert len({r["install_id"] for r in rows}) == 2
    assert "preset-1" in {r["install_id"] for r in rows}
    assert all(r["status"] == "prepared" for r in rows)
    assert list(target.iterdir()) == [target / installer.STORE]


def test_dependency_must_exist_and_source_stays_unchanged(tmp_path):
    source = tmp_path / "source"
    missing, _, _ = fixture_package(source, "Missing", text="缺失资源款", resource="Z:/unavailable/font.ttf")
    resource = tmp_path / "font.ttf"
    resource.write_bytes(b"OTTO real font resource")
    valid, _, _ = fixture_package(source, "Valid", preset_id="second", resource=resource)
    before = installer.inventory(source)
    result = installer.prepare(source, target_root(tmp_path), tmp_path / "cache", tmp_path / "work")
    assert installer.inventory(source) == before
    assert result["summary"]["status_counts"] == {"deferred": 1, "prepared": 1}
    assert result["summary"]["resource_count"] == 1
    prepared = next(x for x in result["packages"] if x["status"] == "prepared")
    staged = installer.read_json(Path(prepared["stage"]) / "preset_draft/draft_content.json")
    path = staged["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["font_path"]
    assert "Resources/ImportedLibrary" in path.replace("\\", "/")


def test_same_id_same_cover_existing_preset_is_preserved(tmp_path):
    source = tmp_path / "source"
    fixture_package(source, "source")
    target = target_root(tmp_path)
    fixture_package(target, "existing")
    before = installer.inventory(target)
    result = installer.prepare(source, target, tmp_path / "cache", tmp_path / "work")
    assert result["summary"]["status_counts"] == {"already_installed": 1}
    assert installer.inventory(target) == before


def test_same_id_cover_time_with_different_words_is_not_skipped(tmp_path):
    source = tmp_path / "source"
    fixture_package(source, "source", text="不同文字")
    target = target_root(tmp_path)
    fixture_package(target, "existing")
    before = installer.inventory(target)
    result = installer.prepare(source, target, tmp_path / "cache", tmp_path / "work")
    assert result["summary"]["status_counts"] == {"prepared": 1}
    assert result["packages"][0]["install_id"] != "preset-1"
    assert installer.inventory(target) == before


def test_identical_resource_folders_with_different_names_share_one_target(tmp_path):
    source = tmp_path / "source"
    for name in ("effect-folder-A", "effect-folder-B"):
        installer.write_json(tmp_path / name / "content.json", {"resource": "identical effect"})
    for name, text, effect_folder in (("A", "字幕", "effect-folder-A"), ("B", "另一个文字", "effect-folder-B")):
        folder, _, payload = fixture_package(source, name, text=text)
        payload["materials"]["drafts"][0]["draft"]["materials"]["material_animations"][0]["animations"][0]["path"] = str(tmp_path / effect_folder)
        installer.write_json(folder / "preset_draft/draft_content.json", payload)
    result = installer.prepare(source, target_root(tmp_path), tmp_path / "cache", tmp_path / "work")
    assert result["summary"]["resource_count"] == 1
    assert len({x["dependencies"][0]["installed_path"] for x in result["packages"]}) == 1
    assert Path(result["resources"][0]["stage"]).is_dir()


def test_nested_base_content_paths_normalize_and_structured_paths_remain(tmp_path):
    _, metadata, payload = fixture_package(tmp_path, "A")
    first = payload["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]
    first["base_content"] = json.dumps({"text": "底稿", "font": {"path": "/old/font.ttf"}})
    second = copy.deepcopy(payload)
    second["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["base_content"] = json.dumps({"text": "底稿", "font": {"path": "E:/new/font.ttf"}})
    assert installer.content_identity(payload, metadata) == installer.content_identity(second, metadata)
    first["path"] = [{"x": 1, "y": 2}]
    assert installer.content_identity(payload, metadata) != installer.content_identity(second, metadata)


def test_empty_migration_fields_are_equal_after_normalization(tmp_path):
    _, metadata, payload = fixture_package(tmp_path, "A")
    payload["materials"]["drafts"][0]["draft"]["materials"]["texts"][0]["current_words"] = None
    payload["materials"]["drafts"][0]["draft"]["uneven_animation_template_info"] = None
    migrated = copy.deepcopy(payload)
    inner = migrated["materials"]["drafts"][0]["draft"]
    inner["materials"]["texts"][0]["current_words"] = {"words": []}
    inner["materials"]["texts"][0]["subtitle_keywords_config"] = None
    inner["uneven_animation_template_info"] = {"composition": "", "content": "", "order": "", "name": ""}
    assert installer.content_identity(payload, metadata) == installer.content_identity(migrated, metadata)
    inner["materials"]["texts"][0]["current_words"] = {"words": ["真实新增内容"]}
    assert installer.content_identity(payload, metadata) != installer.content_identity(migrated, metadata)


def test_runtime_evidence_must_resolve_to_actual_file(tmp_path):
    source = tmp_path / "source"
    fixture_package(source, "A", resource="Z:/old/original.ttf")
    actual = tmp_path / "restored.ttf"
    actual.write_bytes(b"OTTO restored exact font bytes")
    evidence = tmp_path / "queue.json"
    installer.write_json(evidence, {"items": [{"embedded_media_evidence": [{"path": "Z:/old/original.ttf", "resolved_path": str(actual)}]}]})
    result = installer.prepare(source, target_root(tmp_path), tmp_path / "cache", tmp_path / "work", resource_evidence=(evidence,))
    assert result["summary"]["status_counts"] == {"prepared": 1}
    assert result["packages"][0]["dependencies"][0]["resolution"] == "existing_runtime_evidence_exact_path"


def test_manifest_must_include_every_actual_rewritten_resource_target(tmp_path):
    source = tmp_path / "source"
    resource = tmp_path / "font.ttf"
    resource.write_bytes(b"OTTO font")
    fixture_package(source, "A", resource=resource)
    result = installer.prepare(source, target_root(tmp_path), tmp_path / "cache", tmp_path / "work")
    assert installer.verify_dependency_targets(result) == 1
    result["resources"][0]["target"] += "-wrong"
    with pytest.raises(ValueError, match="rewritten_dependency_missing_from_manifest"):
        installer.verify_dependency_targets(result)


def test_apply_preserves_existing_preset_and_merges_native_index(tmp_path, monkeypatch):
    if tmp_path.drive.upper() != "E:":
        pytest.skip("native install boundary requires the E drive")
    source = tmp_path / "source"
    fixture_package(source, "New")
    target = target_root(tmp_path)
    old, _, _ = fixture_package(target, "Old", preset_id="existing", text="已有文字")
    store = installer.read_json(target / installer.STORE)
    store["preset_virtual_store"][0]["value"].append({"id": "existing", "name": "Old", "custom": "keep"})
    store["preset_virtual_store"][1]["value"].append({"child_id": "existing", "parent_id": ""})
    installer.write_json(target / installer.STORE, store)
    before = installer.inventory(old)
    installer.prepare(source, target, tmp_path / "cache", tmp_path / "work")
    from jianying_adapter import jianying_environment
    monkeypatch.setattr(jianying_environment, "collect_jianying_processes", lambda: [])
    result = installer.apply(tmp_path / "work/import_manifest.json")
    assert result["installed_count"] == 1
    assert result["native_store_count"] == 2
    assert installer.inventory(old) == before
    after = installer.read_json(target / installer.STORE)
    assert after["preset_virtual_store"][0]["value"][0] == store["preset_virtual_store"][0]["value"][0]


def test_native_store_drift_is_rejected_before_copy(tmp_path, monkeypatch):
    if tmp_path.drive.upper() != "E:":
        pytest.skip("native install boundary requires the E drive")
    source = tmp_path / "source"
    fixture_package(source, "A")
    target = target_root(tmp_path)
    installer.prepare(source, target, tmp_path / "cache", tmp_path / "work")
    installer.write_json(target / installer.STORE, {"changed": True})
    from jianying_adapter import jianying_environment
    monkeypatch.setattr(jianying_environment, "collect_jianying_processes", lambda: [])
    with pytest.raises(ValueError, match="native_store_changed"):
        installer.apply(tmp_path / "work/import_manifest.json")
    assert not (target / "A").exists()


def test_cover_is_recovered_from_identical_source_copy(tmp_path):
    source = tmp_path / "source"
    first, _, _ = fixture_package(source, "A")
    (first / "A.jpeg").unlink()
    second, _, _ = fixture_package(source, "Longer complete copy")
    result = installer.prepare(source, target_root(tmp_path), tmp_path / "cache", tmp_path / "work")
    assert result["summary"]["status_counts"] == {"prepared": 1}
    assert result["packages"][0]["cover_source"] == str(second / "Longer complete copy.jpeg")
    assert not (first / "A.jpeg").exists()


def test_version_falls_back_to_wrapper_without_lowering_known_inner_version(tmp_path):
    _, _, payload = fixture_package(tmp_path, "A")
    inner = payload["materials"]["drafts"][0]["draft"]
    inner["platform"]["app_version"] = ""
    payload["last_modified_platform"] = {"app_version": "8.9.0"}
    assert installer.versions(payload) == ["8.9.0"]
    payload["last_modified_platform"]["app_version"] = "6.6.0"
    assert installer.versions(payload) == ["6.6.0"]
    inner["platform"]["app_version"] = "9.2.0"
    assert installer.versions(payload) == ["9.2.0"]
    assert installer.version_hold(installer.versions(payload)) == "newer_than_8_8"


def test_higher_version_still_reports_missing_dependencies(tmp_path):
    source = tmp_path / "source"
    folder, _, payload = fixture_package(source, "A", resource="Z:/absent/font.ttf")
    payload["materials"]["drafts"][0]["draft"]["platform"]["app_version"] = "9.2.0"
    installer.write_json(folder / "preset_draft/draft_content.json", payload)
    result = installer.prepare(source, target_root(tmp_path), tmp_path / "cache", tmp_path / "work")
    row = result["packages"][0]
    assert set(row["reasons"]) == {"newer_than_8_8", "missing_or_ambiguous_local_dependencies"}
    assert row["missing_count"] == 1
    assert row["dependencies"][0]["value"] == "Z:/absent/font.ttf"
    assert not (tmp_path / "work/packages").exists()


def test_renamed_package_selects_only_consistent_native_header(tmp_path):
    folder, meta, _ = fixture_package(tmp_path, "Renamed")
    meta["cover_path"] = "old/Presets/Renamed/Renamed.jpeg"
    installer.write_json(folder / "Renamed.json", meta)
    old = {**meta, "id": "old-id", "name": "Old", "cover_path": "old/Old.jpeg"}
    installer.write_json(folder / "Old.json", old)
    selected_path, selected = installer.package_metadata(folder)
    assert selected_path.name == "Renamed.json"
    assert selected["id"] == meta["id"]
    meta["name"] = "Ambiguous"
    installer.write_json(folder / "Renamed.json", meta)
    with pytest.raises(ValueError, match="expected_one_preset_metadata:2"):
        installer.package_metadata(folder)


def test_rich_text_resource_ids_and_audio_ids_are_recognized():
    content = {"text": "例子", "styles": [{"font": {"id": "7312719287796371978", "path": "font.ttf"},
                                               "effectStyle": {"id": "7166476751420329248", "path": "##_material_placeholder_shared_##"}}]}
    refs = installer.path_references({"materials": {"texts": [{"id": "material-uuid", "content": json.dumps(content)}],
                                                         "audios": [{"id": "audio-uuid", "music_id": "6940900821374684452", "path": "missing.mp3"}]}})
    assert refs[0]["remote_ids"] == ["7312719287796371978"]
    assert refs[1]["remote_ids"] == ["7166476751420329248"]
    assert refs[2]["remote_ids"] == ["6940900821374684452"]
    assert installer.resource_kind(refs[0]) == "font"
    assert installer.resource_kind(refs[1]) == "package"


def test_bare_placeholder_resolves_by_resource_identity_from_source_evidence(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    audio = cache / "sound.mp3"
    audio.write_bytes(b"ID3" + b"\0" * 32)
    resolver = installer.LocalResources(tmp_path / "source", cache)
    resolver.add_source_evidence({"materials": {"audios": [{"effect_id": "6940900821374684452", "path": str(audio)}]}}, tmp_path)
    path, basis = resolver.resolve("##_material_placeholder_shared_##", tmp_path, ["6940900821374684452"], "audio")
    assert path == audio.resolve()
    assert basis == "same_resource_identity_verified_local_bytes"
    assert resolver.resolve("##_material_placeholder_shared_##", tmp_path, ["9999999999999999999"], "audio")[0] is None
    assert resolver.resolve("##_material_placeholder_shared_##", tmp_path, ["6940900821374684452"], "font")[0] is None


def test_unscoped_placeholder_evidence_cannot_bind_unrelated_assets(tmp_path):
    resource = tmp_path / "sound.mp3"
    resource.write_bytes(b"ID3" + b"\0" * 32)
    evidence = tmp_path / "evidence.json"
    installer.write_json(evidence, {"original_path": "##_material_placeholder_shared_##", "resolved_path": str(resource)})
    resolver = installer.LocalResources(tmp_path / "source", tmp_path / "cache", resource_evidence=(evidence,))
    assert resolver.resolve("##_material_placeholder_shared_##", tmp_path, ["9999999999999999999"], "audio")[0] is None


def test_resource_identity_rejects_conflicting_audio_bytes_but_accepts_renamed_equal_bytes(tmp_path):
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    first.write_bytes(b"ID3first payload")
    second.write_bytes(b"ID3different payload")
    evidence = tmp_path / "evidence.json"
    installer.write_json(evidence, {"items": [{"original_path": "old/first.mp3", "resolved_path": str(p), "resource_id": "6940900821374684452"} for p in (first, second)]})
    resolver = installer.LocalResources(tmp_path / "source", tmp_path / "cache", resource_evidence=(evidence,))
    path, reason = resolver.resolve("##_material_placeholder_shared_##", tmp_path, ["6940900821374684452"], "audio")
    assert path is None
    assert reason == "ambiguous_local_resource_identity"
    second.write_bytes(first.read_bytes())
    resolver = installer.LocalResources(tmp_path / "source", tmp_path / "cache", resource_evidence=(evidence,))
    assert resolver.resolve("##_material_placeholder_shared_##", tmp_path, ["6940900821374684452"], "audio")[0] is not None


def test_repeated_import_recognizes_resolved_default_font_filename(tmp_path, monkeypatch):
    if tmp_path.drive.upper() != "E:":
        pytest.skip("native install boundary requires the E drive")
    source = tmp_path / "source"
    fixture_package(source, "A", resource="Z:/Application/Fonts/default.ttf")
    resource = tmp_path / "ActualDefault.otf"
    resource.write_bytes(b"OTTO" + b"\0" * 20)
    evidence = tmp_path / "mapping.json"
    installer.write_json(evidence, {"original_path": "Z:/Application/Fonts/default.ttf", "resolved_path": str(resource)})
    target = target_root(tmp_path)
    installer.prepare(source, target, tmp_path / "cache", tmp_path / "first", resource_evidence=(evidence,))
    from jianying_adapter import jianying_environment
    monkeypatch.setattr(jianying_environment, "collect_jianying_processes", lambda: [])
    installer.apply(tmp_path / "first/import_manifest.json")
    before = installer.inventory(target)
    second = installer.prepare(source, target, tmp_path / "cache", tmp_path / "second", resource_evidence=(evidence,))
    assert second["summary"]["status_counts"] == {"already_installed": 1}
    assert second["packages"][0]["existing_match_basis"] == "resolved_behavior_and_frozen_resources"
    assert installer.inventory(target) == before


def test_resource_package_resolution_preserves_entry_root_and_lumi_subpath(tmp_path):
    cache = tmp_path / "cache"
    package = cache / "effect" / "7416258989467374091" / ("a" * 32)
    installer.write_json(package / "AmazingFeature/content.json", {"shader": "actual nested feature"})
    installer.write_json(package / "lumi_hub_path/config.json", {"hub": "actual hub"})
    resolver = installer.LocalResources(tmp_path / "source", cache)
    original = "Z:/User Data/Cache/effect/7416258989467374091/" + "b" * 32
    assert resolver.resolve(original, tmp_path, ["7416258989467374091"], "package")[0] == package.resolve()
    assert resolver.resolve(original + "/lumi_hub_path", tmp_path, ["7416258989467374091"], "package")[0] == (package / "lumi_hub_path").resolve()


def test_package_root_cannot_replace_missing_lumi_subpath(tmp_path):
    cache = tmp_path / "cache"
    package = cache / "effect" / "7416258989467374091" / ("a" * 32)
    installer.write_json(package / "AmazingFeature/content.json", {"shader": "actual nested feature"})
    resolver = installer.LocalResources(tmp_path / "source", cache)
    original = "Z:/User Data/Cache/effect/7416258989467374091/" + "b" * 32 + "/lumi_hub_path"
    assert resolver.resolve(original, tmp_path, ["7416258989467374091"], "package")[0] is None


def test_algorithm_result_cannot_be_replaced_by_its_effect_package(tmp_path):
    cache = tmp_path / "cache"
    package = cache / "effect/7408076339699322112" / ("a" * 32)
    installer.write_json(package / "content.json", {"effect": "algorithm definition only"})
    resolver = installer.LocalResources(tmp_path / "source", cache)
    ref = {"value": "Z:/draft/video/figure_algorithm/961B6E93-9716-44D8-BE4F-7A9296AA51C3",
           "key": "path", "location": "/materials/effects/0/path", "group": "effects", "node": {}}
    assert installer.resource_kind(ref) == "algorithm"
    path, reason = resolver.resolve(ref["value"], tmp_path, ["7408076339699322112"], "algorithm")
    assert path is None
    assert reason == "missing_original_algorithm_output"
    assert installer.resource_kind({**ref, "value": "Z:/Resources/videoAlg/result.mp4"}) == "algorithm"


def test_font_directory_mapping_falls_back_to_actual_font_file(tmp_path):
    package = tmp_path / "cache/effect/6807742980271641102"
    font = package / ("a" * 32) / "SourceHanSerifCN-Heavy.otf"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"OTTO actual font")
    original = "Z:/old/SourceHanSerifCN-Heavy.otf"
    evidence = tmp_path / "mapping.json"
    installer.write_json(evidence, {"original_path": original, "resolved_path": str(package)})
    resolver = installer.LocalResources(tmp_path / "source", tmp_path / "cache", resource_evidence=(evidence,))
    resolved, basis = resolver.resolve(original, tmp_path, ["6807742980271641102"], "font")
    assert resolved == font.resolve()
    assert basis == "same_leaf_identical_payload"


@pytest.mark.parametrize("kind,filename", [("font", "font.otf"), ("audio", "sound.mp3")])
def test_exact_or_evidenced_media_path_must_match_required_type(tmp_path, kind, filename):
    path = tmp_path / "cache/effect/123456789" / filename
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a media file")
    evidence = tmp_path / "mapping.json"
    original = f"Z:/User Data/Cache/effect/123456789/{filename}"
    installer.write_json(evidence, {"original_path": original, "resolved_path": str(path)})
    resolver = installer.LocalResources(tmp_path / "source", tmp_path / "cache", resource_evidence=(evidence,))
    assert resolver.resolve(str(path), tmp_path, [], kind)[0] is None
    assert resolver.resolve(original, tmp_path, ["123456789"], kind)[0] is None


def test_downloaded_font_revision_cannot_spread_by_id_leaf_or_source_evidence(tmp_path):
    recovery = tmp_path / "recovery"
    font = recovery / "123456789" / ("a" * 32) / "font.ttf"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"OTTO exact source revision")
    original = "Z:/User Data/Cache/effect/123456789/" + "a" * 32 + "/font.ttf"
    other_revision = original.replace("a" * 32, "b" * 32)
    evidence = tmp_path / "recovery.json"
    installer.write_json(evidence, {"items": [{"kind": "font", "original_path": original,
        "resolved_path": str(font), "resource_id": "123456789", "original_only": True}]})
    resolver = installer.LocalResources(tmp_path / "source", tmp_path / "cache",
        resource_roots=(recovery,), resource_evidence=(evidence,))
    assert resolver.resolve(original, tmp_path, ["123456789"], "font")[0] == font.resolve()
    resolver.add_source_evidence({"materials": {"texts": [{"font_path": original,
        "font_id": "123456789"}]}}, tmp_path)
    assert resolver.resolve(other_revision, tmp_path, ["123456789"], "font")[0] is None
    assert resolver.resolve("##_material_placeholder_shared_##", tmp_path, ["123456789"], "font")[0] is None


def test_downloaded_package_revision_cannot_be_selected_through_cache_id_fallback(tmp_path):
    cache = tmp_path / "cache"
    package = cache / "effect/123456789" / ("a" * 32)
    installer.write_json(package / "content.json", {"effect": "verified original revision"})
    original = "Z:/User Data/Cache/effect/123456789/" + "a" * 32
    evidence = tmp_path / "recovery.json"
    installer.write_json(evidence, {"items": [{"kind": "package", "original_path": original,
        "resolved_path": str(package), "remote_ids": ["123456789"], "original_only": True}]})
    resolver = installer.LocalResources(tmp_path / "source", cache, resource_evidence=(evidence,))
    assert resolver.resolve(original, tmp_path, ["123456789"], "package")[0] == package.resolve()
    assert resolver.resolve(original.replace("a" * 32, "b" * 32), tmp_path, ["123456789"], "package")[0] is None
