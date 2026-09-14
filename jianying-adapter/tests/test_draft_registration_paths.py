from __future__ import annotations

import argparse
import json
import importlib.util
import os
import sys
import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).parents[1]
LOCAL_DEPS = REPO_ROOT / "vendor" / "python-deps"
VENDOR_ROOT = REPO_ROOT / "vendor" / "pyJianYingDraft-source"
sys.path.insert(0, str(LOCAL_DEPS))
sys.path.insert(0, str(VENDOR_ROOT))

from pyJianYingDraft.draft_registration import DraftFolderRegistration
from pyJianYingDraft import DraftFolder


def _load_materialize_module():
    script = Path(__file__).parents[1] / "scripts" / "materialize_trial_draft.py"
    spec = importlib.util.spec_from_file_location("materialize_trial_draft", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_physical_io_preserves_logical_registration_with_alias_writes_denied(
    tmp_path: Path, monkeypatch, draft_root_alias
) -> None:
    logical, physical = draft_root_alias
    sentinel = {"draft_id": "KEEP", "draft_name": "existing", "draft_fold_path": str(logical / "existing")}
    root_meta = physical / "root_meta_info.json"
    root_meta.write_text(json.dumps({"root_path": str(logical), "all_draft_store": [sentinel]}), encoding="utf-8")
    real_open, real_makedirs = builtins.open, os.makedirs

    def is_logical(path):
        if isinstance(path, int):
            return False
        return Path(os.path.abspath(path)).is_relative_to(logical)

    def block_alias_open(path, mode="r", *args, **kwargs):
        if is_logical(path) and any(flag in mode for flag in ("w", "a", "x", "+")):
            raise FileExistsError("simulated Windows C alias write failure")
        return real_open(path, mode, *args, **kwargs)

    def block_alias_makedirs(path, *args, **kwargs):
        if is_logical(path):
            raise FileExistsError("simulated Windows C alias mkdir failure")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", block_alias_open)
    monkeypatch.setattr(os, "makedirs", block_alias_makedirs)
    folder = DraftFolder(str(physical), user_data_path=str(tmp_path / "host-user-data"),
                         registration_root_path=str(logical))
    script = folder.create_draft("NEW_TEST", 1080, 1920, 30)
    assert Path(script.save_path).is_relative_to(physical)
    script.save()
    first = json.loads(root_meta.read_text(encoding="utf-8"))
    entry = next(item for item in first["all_draft_store"] if item["draft_name"] == "NEW_TEST")
    assert entry["draft_fold_path"] == str(logical / "NEW_TEST")
    assert entry["draft_root_path"] == str(logical)
    assert entry["draft_json_file"] == str(logical / "NEW_TEST" / "draft_content.json")
    assert entry["draft_cover"] == str(logical / "NEW_TEST" / "draft_cover.jpg")
    # Even a pre-existing physical-path duplicate is folded into one logical identity.
    first["all_draft_store"].append(dict(entry, draft_fold_path=str(physical / "NEW_TEST")))
    root_meta.write_text(json.dumps(first), encoding="utf-8")
    folder.load_template("NEW_TEST").save()
    saved = json.loads(root_meta.read_text(encoding="utf-8"))
    assert len(saved["all_draft_store"]) == 2
    assert next(item for item in saved["all_draft_store"] if item["draft_id"] == "KEEP") == sentinel
    assert saved["root_path"] == str(logical)
    sidecar = json.loads((physical / "NEW_TEST" / "draft_meta_info.json").read_text(encoding="utf-8"))
    assert sidecar["draft_fold_path"] == str(logical / "NEW_TEST")
    assert sidecar["draft_root_path"] == str(logical)
    assert os.path.samefile(logical / "NEW_TEST", physical / "NEW_TEST")


def test_registration_alias_rejects_a_different_directory(tmp_path: Path) -> None:
    physical, wrong = tmp_path / "physical", tmp_path / "wrong"
    physical.mkdir()
    wrong.mkdir()
    with pytest.raises(ValueError, match="same physical draft root"):
        DraftFolder(str(physical), registration_root_path=str(wrong))
    assert list(physical.iterdir()) == []
    assert list(wrong.iterdir()) == []


def test_materializer_uses_verified_physical_root_and_keeps_logical_identity(draft_root_alias):
    logical, physical = draft_root_alias
    module = _load_materialize_module()
    args = argparse.Namespace(draft_root=logical, physical_root=physical, user_data=physical.parent)
    assert module._effective_draft_root(args) == logical
    assert module._storage_draft_root(args) == physical
    folder = module._open_draft_folder(args, physical, None)
    assert folder.folder_path == str(physical)
    assert folder._registration.registration_root_path == str(logical)


def test_explicit_draft_root_keeps_lexical_junction_entry(tmp_path: Path, monkeypatch) -> None:
    materialize = _load_materialize_module()
    lexical_entry = tmp_path / "C-local-entry"
    physical_target = tmp_path / "E-physical-target"

    def fail_if_resolved(_self, *args, **kwargs):
        raise AssertionError("explicit draft root must not call Path.resolve()")

    monkeypatch.setattr(Path, "resolve", fail_if_resolved)
    args = argparse.Namespace(draft_root=lexical_entry, user_data=tmp_path / "user-data")

    assert materialize._effective_draft_root(args) == Path(os.path.abspath(lexical_entry))
    assert str(materialize._effective_draft_root(args)) != str(physical_target)

    default_args = argparse.Namespace(draft_root=None, user_data=Path("relative-user-data"))
    assert materialize._effective_draft_root(default_args) == (
        Path(os.path.abspath(default_args.user_data)) / "Projects" / "com.lveditor.draft"
    )


def test_upsert_collapses_filesystem_aliases_and_keeps_caller_path(
    tmp_path: Path, monkeypatch
) -> None:
    registration_module = sys.modules[DraftFolderRegistration.__module__]
    c_alias = tmp_path / "C-local-entry" / "draft"
    e_alias = tmp_path / "E-physical-entry" / "draft"
    c_alias.mkdir(parents=True)
    e_alias.mkdir(parents=True)

    def fake_samefile(left, right):
        pair = {
            os.path.normcase(os.path.normpath(str(left))),
            os.path.normcase(os.path.normpath(str(right))),
        }
        expected = {
            os.path.normcase(os.path.normpath(str(c_alias))),
            os.path.normcase(os.path.normpath(str(e_alias))),
        }
        return pair == expected

    monkeypatch.setattr(registration_module.os.path, "samefile", fake_samefile)
    registration = DraftFolderRegistration(str(c_alias.parent))
    assert registration.filesystem_paths_equal(str(c_alias), str(e_alias)) is True
    root_entries = [
        {"draft_id": "OLD-E", "draft_fold_path": str(e_alias), "draft_name": "old-e"},
        {"draft_id": "OLD-C", "draft_fold_path": str(c_alias), "draft_name": "old-c"},
    ]
    root_entry = registration.build_root_meta_entry(
        existing_entry=root_entries[0],
        draft_name="new",
        draft_path=str(c_alias),
        draft_id="NEW",
        draft_new_version="141.0.0",
        tm_draft_create=1,
        tm_draft_modified=2,
        tm_duration=3,
        timeline_materials_size=4,
    )

    registration.upsert_root_meta_entry(root_entries, root_entry, str(c_alias), "NEW")

    assert root_entries == [root_entry]
    assert root_entry["draft_fold_path"] == str(c_alias)
    assert root_entry["draft_cover"] == os.path.join(str(c_alias), "draft_cover.jpg")


def test_sync_active_draft_clears_removed_timestamp_without_changing_cloud_fields(
    tmp_path: Path,
) -> None:
    registration = DraftFolderRegistration(str(tmp_path / "draft-root"))
    meta_path = tmp_path / "draft" / "draft_meta_info.json"
    meta_path.parent.mkdir()
    meta_info = {
        "draft_id": "DRAFT-001",
        "draft_name": "draft",
        "tm_draft_removed": 1788108743908,
        "draft_cloud_sync": True,
        "draft_cloud_template_id": "keep-template",
        "cloud_draft_cover": "keep-cover",
    }
    cloud_fields_before = {
        key: value for key, value in meta_info.items() if "cloud" in key
    }

    registration.sync_draft_meta_info(
        str(meta_path),
        meta_info=meta_info,
        sidecar_codec=None,
        script_file=SimpleNamespace(content={"materials": {}}, duration=2_000_000),
        context={},
        draft_name="draft",
        draft_path=str(meta_path.parent),
        draft_id="DRAFT-001",
        draft_new_version="141.0.0",
    )

    stored = json.loads(meta_path.read_text(encoding="utf-8"))
    assert stored["tm_draft_removed"] == 0
    assert {key: stored[key] for key in cloud_fields_before} == cloud_fields_before


def test_replace_official_audio_path_changes_only_matching_effect(
    tmp_path: Path,
) -> None:
    materialize = _load_materialize_module()
    replacement = tmp_path / "official.mp3"
    replacement.write_bytes(b"mp3")
    content = {
        "materials": {
            "audios": [
                {
                    "effect_id": "123",
                    "source_platform": 1,
                    "path": "missing.mp3",
                    "name": "弹跳声",
                },
                {
                    "effect_id": "456",
                    "source_platform": 1,
                    "path": "keep.mp3",
                },
            ]
        }
    }

    repaired, old_paths = materialize._replace_official_audio_path(
        content, effect_id="123", replacement=replacement
    )

    assert old_paths == ["missing.mp3"]
    assert repaired["materials"]["audios"][0]["path"] == str(replacement)
    assert repaired["materials"]["audios"][1]["path"] == "keep.mp3"
    assert content["materials"]["audios"][0]["path"] == "missing.mp3"
