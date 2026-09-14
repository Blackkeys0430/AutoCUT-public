import argparse
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "materialize_trial_draft.py"
    spec = importlib.util.spec_from_file_location("materialize_render_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("with_mirror", [False, True])
def test_apply_candidate_restores_explicit_render_indexes_after_save_reorder(tmp_path, monkeypatch, with_mirror):
    module = _module()
    root = tmp_path / "root"
    live = root / "DRAFT"
    live.mkdir(parents=True)
    candidate = tmp_path / "candidate.json"
    content = {"id": "base", "version": 1, "new_version": "141.0.0", "platform": {}, "last_modified_platform": {}, "tracks": [
        {"name": "base", "type": "video", "flag": 0, "segments": [{"id": "b", "render_index": 0, "track_render_index": 0, "target_timerange": {"start": 0, "duration": 2}}]},
        {"name": "broll", "type": "video", "flag": 2, "segments": [{"id": "r", "render_index": 1, "track_render_index": 0, "target_timerange": {"start": 1, "duration": 1}}]},
    ], "materials": {}}
    (live / "draft_content.json").write_text(json.dumps(content), encoding="utf-8")
    candidate.write_text(json.dumps(content), encoding="utf-8")
    if with_mirror:
        (live / "template-2.tmp").write_text(json.dumps(content), encoding="utf-8")

    class Codec:
        def decode(self, data): return json.loads(data.decode())
        def encode(self, value): return json.dumps(value).encode()
    codec = Codec()
    monkeypatch.setattr(module, "_codec", lambda _install: codec)
    monkeypatch.setattr(module, "load_json_object_with_codec", lambda path, **kw: (json.loads(Path(path).read_bytes()), None))
    monkeypatch.setattr(module, "write_json_object_with_codec", lambda path, value, **kw: Path(path).write_bytes(json.dumps(value).encode()))
    class Folder:
        def __init__(self, *_a, **_kw): pass
        def load_template(self, _name): return self
        def save(self):
            value = json.loads((live / "draft_content.json").read_bytes())
            for track in value["tracks"]:
                for segment in track["segments"]:
                    segment["render_index"] += 7
                    segment["track_render_index"] += 3
            (live / "draft_content.json").write_bytes(json.dumps(value).encode())
    monkeypatch.setattr(module, "DraftFolder", Folder)
    args = argparse.Namespace(candidate=candidate, draft_root=root, name="DRAFT", install_dir=tmp_path,
                              user_data=tmp_path, backup_dir=tmp_path / "backup", allow_candidate_rebase=False)
    result = module.apply_candidate(args)
    saved = json.loads((live / "draft_content.json").read_bytes())
    assert saved == content
    if with_mirror:
        assert json.loads((live / "template-2.tmp").read_bytes()) == content
    assert result["content_mirrors_deep_equal"] == "true"


def test_apply_candidate_without_explicit_render_indexes_keeps_save_generated_values(tmp_path, monkeypatch):
    module = _module(); root = tmp_path / "root"; live = root / "DRAFT"; live.mkdir(parents=True)
    value = {"id":"base", "version":1, "new_version":"141.0.0", "platform":{}, "last_modified_platform":{}, "tracks":[{"name":"base", "type":"video", "segments":[{"id":"b", "target_timerange":{"start":0,"duration":1}}]}], "materials":{}}
    (live / "draft_content.json").write_text(json.dumps(value), encoding="utf-8")
    candidate = tmp_path / "candidate.json"; candidate.write_text(json.dumps(value), encoding="utf-8")
    class Codec:
        def decode(self, data): return json.loads(data.decode())
    codec = Codec(); monkeypatch.setattr(module, "_codec", lambda _install: codec)
    monkeypatch.setattr(module, "load_json_object_with_codec", lambda path, **kw: (json.loads(Path(path).read_bytes()), None))
    monkeypatch.setattr(module, "write_json_object_with_codec", lambda path, value, **kw: Path(path).write_bytes(json.dumps(value).encode()))
    class Folder:
        def __init__(self, *_a, **_kw): pass
        def load_template(self, _name): return self
        def save(self):
            value = json.loads((live / "draft_content.json").read_bytes()); value["tracks"][0]["segments"][0]["render_index"] = 4
            (live / "draft_content.json").write_bytes(json.dumps(value).encode())
    monkeypatch.setattr(module, "DraftFolder", Folder)
    args = argparse.Namespace(candidate=candidate, draft_root=root, name="DRAFT", install_dir=tmp_path, user_data=tmp_path, backup_dir=tmp_path / "backup", allow_candidate_rebase=False)
    module.apply_candidate(args)
    saved = json.loads((live / "draft_content.json").read_bytes())
    assert saved["tracks"][0]["segments"][0]["render_index"] == 4


def test_apply_candidate_rejects_non_render_save_difference(tmp_path, monkeypatch):
    module = _module()
    root = tmp_path / "root"; live = root / "DRAFT"; live.mkdir(parents=True)
    value = {"id": "base", "version": 1, "new_version": "141.0.0", "platform": {}, "last_modified_platform": {}, "tracks": [{"name": "base", "type": "video", "segments":[{"id":"b","target_timerange":{"start":0,"duration":1},"render_index":0}]}], "materials": {}}
    (live / "draft_content.json").write_text(json.dumps(value), encoding="utf-8")
    candidate = tmp_path / "candidate.json"; candidate.write_text(json.dumps(value), encoding="utf-8")
    class Codec:
        def decode(self, data): return json.loads(data.decode())
    codec = Codec()
    monkeypatch.setattr(module, "_codec", lambda _install: codec)
    monkeypatch.setattr(module, "load_json_object_with_codec", lambda path, **kw: (json.loads(Path(path).read_bytes()), None))
    monkeypatch.setattr(module, "write_json_object_with_codec", lambda path, value, **kw: Path(path).write_bytes(json.dumps(value).encode()))
    class Folder:
        def __init__(self, *_a, **_kw): pass
        def load_template(self, _name): return self
        def save(self):
            value = json.loads((live / "draft_content.json").read_bytes()); value["duration"] = 99
            (live / "draft_content.json").write_bytes(json.dumps(value).encode())
    monkeypatch.setattr(module, "DraftFolder", Folder)
    args = argparse.Namespace(candidate=candidate, draft_root=root, name="DRAFT", install_dir=tmp_path,
                              user_data=tmp_path, backup_dir=tmp_path / "backup", allow_candidate_rebase=False)
    with pytest.raises(RuntimeError, match="differs beyond"):
        module.apply_candidate(args)
