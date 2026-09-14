import io
import json
import hashlib
import stat
import struct
import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from jianying_adapter import preset_resources as recovery


RID = "6996426076205370631"
OTHER_RID = "6896679424402476296"
AUDIO_URL = "https://v26-artist.vlabvod.com/audio?signature=not-to-be-saved"
ZIP_URL = "https://lf26-faceu-file-sign.bytecdn.com/package?signature=not-to-be-saved"
IMAGE_URL = "https://p3-heycan-jy-sign.byteimg.com/preview.png"
MUSIC_URL = "https://v26-jianying.vlabvod.com/music?signature=not-to-be-saved"


def md5(data):
    return hashlib.md5(data).hexdigest()


def archive_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def font_bytes(weight=400):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "A"])
    builder.setupCharacterMap({65: "A"})
    builder.setupGlyf({name: TTGlyphPen(None).glyph() for name in [".notdef", "A"]})
    builder.setupHorizontalMetrics({name: (600, 0) for name in [".notdef", "A"]})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "优设标题黑", "styleName": "Regular",
                           "fullName": "优设标题黑", "psName": "YouSheBiaoTiHei"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800,
                     usWinDescent=200, usWeightClass=weight)
    builder.setupPost()
    stream = io.BytesIO()
    builder.save(stream)
    return stream.getvalue()


def without_utf8_flags(data):
    """Reproduce the actual font ZIP's UTF-8 bytes without its UTF-8 bit."""
    result = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            offset = member.header_offset + 6
            struct.pack_into("<H", result, offset, struct.unpack_from("<H", result, offset)[0] & ~0x800)
        offset = archive.start_dir
        for _ in archive.infolist():
            struct.pack_into("<H", result, offset + 8, struct.unpack_from("<H", result, offset + 8)[0] & ~0x800)
            name_len, extra_len, comment_len = struct.unpack_from("<HHH", result, offset + 28)
            offset += 46 + name_len + extra_len + comment_len
    return bytes(result)


def item(identifier=RID, resource_type=3, **business):
    return {"common_attr": {
        "id": identifier, "effect_id": identifier, "effect_type": resource_type,
        "title": "resource", "md5": "api-hash-is-not-the-downloaded-byte-hash",
        "business_info": {"json_str": json.dumps({"is_vip": False, "paid_type": "free", "is_expired": False, **business}),
                          "sign": "must-not-be-saved"},
        "download_info": {"url": AUDIO_URL if resource_type == 3 else IMAGE_URL},
        "item_urls": [ZIP_URL],
    }}


def audio_dep(data=b"ID3actual source audio", identifier=RID, **overrides):
    return {"kind": "audio", "value": "Z:/User Data/Cache/music/" + md5(data) + ".mp3",
            "remote_ids": [identifier], "resolved_path": None, **overrides}


def package_dep(data, kind="package", entry="", identifier=RID, cache_id=None):
    return {"kind": kind, "value": f"Z:/User Data/Cache/effect/{cache_id or identifier}/{md5(data)}" + ("/" + entry if entry else ""),
            "remote_ids": [identifier], "resolved_path": None}


class FakeClient:
    def __init__(self, items=(), data=None, *, failures=(), unexpected=(), fallback_items=()):
        self.items = {row["common_attr"]["id"]: row for row in items}
        self.fallback_items = {row["common_attr"]["id"]: row for row in fallback_items}
        self.data = data or {}
        self.failures, self.unexpected = set(failures), list(unexpected)
        self.queries, self.query_sources, self.downloads = [], [], []

    def query(self, ids, *, source=None):
        self.queries.append(ids)
        self.query_sources.append(source)
        if len(self.queries) in self.failures:
            raise OSError("https://example.invalid/private?token=do-not-save")
        rows = self.items if source is None else self.fallback_items
        return [rows[identifier] for identifier in reversed(ids) if identifier in rows] + self.unexpected

    def download(self, url):
        self.downloads.append(url)
        value = self.data[url]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def run_recovery(tmp_path, monkeypatch):
    # Exercise the same filesystem containment on POSIX CI, while the real CLI
    # still requires E:. This machine's test invocation uses an E: basetemp.
    if tmp_path.drive.upper() != "E:":
        monkeypatch.setattr(recovery, "_require_e_drive", lambda path: None)
    previous = tmp_path / "round5"
    previous.mkdir()
    manifest = previous / "import_manifest.json"

    def run(dependencies, client, extra_packages=()):
        payload = {"workdir": str(previous), "source_root": str(tmp_path / "source"),
                   "target_root": str(tmp_path / "native"),
                   "packages": [{"status": "deferred", "content_identity": "preset-A", "dependencies": dependencies}, *extra_packages]}
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        report = recovery.recover(manifest, tmp_path / "recovered", client=client)
        saved = json.loads((tmp_path / "recovered/resource-recovery.json").read_text(encoding="utf-8"))
        assert saved == report
        text = json.dumps(saved)
        assert "not-to-be-saved" not in text and "must-not-be-saved" not in text
        assert all("resolved_path" not in row for row in report["results"])
        return report

    return run


def test_audio_exact_bytes_deduplicate_queries_and_exclude_other_inputs(run_recovery):
    data = b"ID3actual source audio"
    dep = audio_dep(data)
    client = FakeClient([item()], {AUDIO_URL: data})
    report = run_recovery([dep, dict(dep), {**dep, "kind": "video"}, {**dep, "kind": "image"},
                           {**dep, "kind": "algorithm"}, {**dep, "resolved_path": "already-local"}], client,
                          [{"status": "prepared", "dependencies": [audio_dep(identifier=OTHER_RID)]}])
    assert client.queries == [[RID]] and client.downloads == [AUDIO_URL]
    assert report["summary"]["mapped_original_paths"] == 1
    assert report["summary"]["missing_dependency_occurrences"] == 2
    mapped = report["items"][0]
    assert mapped["original_only"] is True and mapped["download_md5"] == md5(data)
    assert Path(mapped["resolved_path"]).read_bytes() == data


@pytest.mark.parametrize("business", [
    {"is_vip": True}, {"paid_type": "subscribe"}, {"is_expired": True},
    {"is_vip": "false"}, {"is_expired": None}, {"paid_type": None},
])
def test_non_free_or_unknown_state_never_requests_media(run_recovery, business):
    client = FakeClient([item(**business)])
    report = run_recovery([audio_dep()], client)
    assert not client.downloads and not report["items"]
    assert report["results"][0]["status"] == "not_explicitly_free"


@pytest.mark.parametrize("kind,returned_type", [("audio", 4), ("font", 2), ("package", 3)])
def test_checks_returned_resource_type_not_lookup_type(run_recovery, kind, returned_type):
    data = archive_bytes({"content.json": "{}"})
    dep = audio_dep() if kind == "audio" else package_dep(data, kind, "font.ttf" if kind == "font" else "")
    client = FakeClient([item(resource_type=returned_type)])
    report = run_recovery([dep], client)
    assert not client.downloads and not report["items"]
    assert report["results"][0]["status"] == "resource_type_mismatch_or_unsupported"


def test_audio_actual_hash_mismatch_never_maps_even_when_api_hash_claims_match(run_recovery):
    dep = audio_dep(b"ID3original audio")
    metadata = item()
    metadata["common_attr"]["md5"] = md5(b"ID3original audio")
    client = FakeClient([metadata], {AUDIO_URL: b"ID3different newer audio"})
    report = run_recovery([dep], client)
    assert not report["items"] and report["summary"]["mapped_downloads"] == 0
    assert report["results"][0]["status"] == "original_hash_mismatch"
    assert "download_path" in report["downloads"][0]


@pytest.mark.parametrize("resource_type", [1, 2, 13, 18, 92])
def test_zip_uses_original_url_and_preserves_native_root_and_subentry(run_recovery, resource_type):
    data = archive_bytes({"AmazingFeature/content.json": "{}", "lumi_hub_path/config.json": "{}", "license.txt": "keep"})
    dep = package_dep(data)
    # The same cache ID can supply multiple original entry paths, but is fetched once.
    subentry = package_dep(data, entry="lumi_hub_path")
    client = FakeClient([item(resource_type=resource_type)], {ZIP_URL: data})
    report = run_recovery([dep, subentry], client)
    root = Path(report["items"][0]["resolved_path"])
    assert root.name == md5(data) and (root / "AmazingFeature/content.json").is_file()
    assert (root / "license.txt").read_text() == "keep"
    assert Path(report["items"][1]["resolved_path"]) == root / "lumi_hub_path"
    assert client.downloads == [ZIP_URL]
    assert report["summary"]["mapped_original_paths"] == 2


def test_zip_hash_mismatch_and_missing_entry_are_not_success(run_recovery):
    original = archive_bytes({"content.json": "old"})
    actual = archive_bytes({"content.json": "new"})
    client = FakeClient([item(resource_type=2)], {ZIP_URL: actual})
    report = run_recovery([package_dep(original), package_dep(actual, entry="missing_lumi")], client)
    assert not report["items"] and report["summary"]["mapped_downloads"] == 0
    assert [row["status"] for row in report["results"]] == ["original_hash_mismatch", "original_package_entry_missing"]


@pytest.mark.parametrize("unsafe", ["../escape.txt", "/absolute.txt", "C:\\escape.txt", "dir\\..\\escape.txt",
                                    "\\\\server\\share\\escape.txt", "dir//../escape.txt"])
def test_zip_traversal_rejected_before_extracting_any_entry(run_recovery, unsafe):
    data = archive_bytes({"valid/content.json": "{}", unsafe: "escape"})
    client = FakeClient([item(resource_type=2)], {ZIP_URL: data})
    report = run_recovery([package_dep(data)], client)
    assert not report["items"] and report["results"][0]["status"] == "unsafe_zip_or_entry_path"
    assert not list(Path(report["workdir"]).rglob("content.json"))


def test_zip_symlink_rejected(run_recovery):
    stream = io.BytesIO()
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(link, "../../outside")
    data = stream.getvalue()
    report = run_recovery([package_dep(data)], FakeClient([item(resource_type=2)], {ZIP_URL: data}))
    assert not report["items"] and report["results"][0]["status"] == "zip_link_special_or_encrypted_entry"


def test_zip_internal_double_separator_preserves_normal_sticker_members(run_recovery):
    data = archive_bytes({"26A43F02FC234691AF589E0614BAF8DF/": b"",
                          "26A43F02FC234691AF589E0614BAF8DF//frames.json": "{}",
                          "26A43F02FC234691AF589E0614BAF8DF//image.png": b"PNG bytes",
                          "config.json": "{}", "infoSticker.lua": b"retained, never executed"})
    report = run_recovery([package_dep(data)], FakeClient([item(resource_type=2)], {ZIP_URL: data}))
    root = Path(report["items"][0]["resolved_path"])
    assert (root / "26A43F02FC234691AF589E0614BAF8DF/frames.json").read_text() == "{}"
    assert (root / "26A43F02FC234691AF589E0614BAF8DF/image.png").read_bytes() == b"PNG bytes"
    assert (root / "infoSticker.lua").read_bytes() == b"retained, never executed"


def test_uuid_audio_filename_uses_only_valid_uuid_and_actual_terminal_hash(run_recovery):
    data = b"ID3actual source audio"
    base = "Z:/Projects/example/materials/audio/"
    first = "81BE0536-E5C5-431d-AAA3-BB51C9FDA8FC_" + md5(data) + ".mp3"
    different = "5267E4B2-B9A1-492b-A0D4-C765DF575113_" + md5(b"ID3other version") + ".mp3"
    guessed = "arbitrary_prefix_" + md5(data) + ".mp3"
    report = run_recovery([audio_dep(data, value=base + first), audio_dep(data, value=base + different),
                           audio_dep(data, value=base + guessed)], FakeClient([item()], {AUDIO_URL: data}))
    assert [row["status"] for row in report["results"]] == ["mapped", "original_hash_mismatch", "original_hash_unavailable"]
    assert len(report["items"]) == 1 and report["items"][0]["download_md5"] == md5(data)


def test_online_material_without_id_layer_still_requires_actual_package_hash(run_recovery):
    data = archive_bytes({"config.json": "{}"})
    original = {**package_dep(data), "value": "Z:/User Data/CACHE/onlineMaterial/" + md5(data)}
    mismatch = {**original, "value": "Z:/User Data/Cache/onlineMaterial/" + md5(b"old zip version")}
    client = FakeClient([item(resource_type=92)], {ZIP_URL: data})
    report = run_recovery([original, mismatch], client)
    assert client.queries == [[RID]]
    assert [row["status"] for row in report["results"]] == ["mapped", "original_hash_mismatch"]
    assert len(report["items"]) == 1


def test_font_utf8_without_flag_short_cache_id_case_and_appledouble(run_recovery):
    data = without_utf8_flags(archive_bytes({
        "优设标题黑.ttf": font_bytes(), "__MACOSX/._优设标题黑.ttf": b"AppleDouble metadata",
        "config.json": json.dumps({"font_path": "优设标题黑.ttf"}, ensure_ascii=False),
    }))
    dep = package_dep(data, "font", "优设标题黑.TTF", cache_id="349479")
    report = run_recovery([dep], FakeClient([item(resource_type=10)], {ZIP_URL: data}))
    mapped = report["items"][0]
    assert Path(mapped["resolved_path"]).name == "优设标题黑.ttf"
    assert mapped["font_metadata"]["weight_class"] == 400
    assert "YouSheBiaoTiHei" in mapped["font_metadata"]["postscript_name"]
    assert len(mapped["zip_filename_decodings"]) == 2
    assert (Path(mapped["archive_root"]) / "__MACOSX/._优设标题黑.ttf").read_bytes() == b"AppleDouble metadata"


@pytest.mark.parametrize("files,expected_status", [
    ({"other.ttf": b"not the source font"}, "original_font_filename_missing_or_ambiguous"),
    ({"__MACOSX/original.ttf": b"metadata"}, "original_font_filename_missing_or_ambiguous"),
    ({"one/original.ttf": b"one", "two/ORIGINAL.TTF": b"two"}, "original_font_filename_missing_or_ambiguous"),
    ({"original.ttf": b"not a font"}, "TTLibError"),
])
def test_font_no_filename_guess_or_appledouble_acceptance(run_recovery, files, expected_status):
    data = archive_bytes(files)
    report = run_recovery([package_dep(data, "font", "original.ttf")], FakeClient([item(resource_type=10)], {ZIP_URL: data}))
    assert not report["items"] and report["results"][0]["status"] == expected_status


def test_font_weight_must_be_valid(run_recovery):
    data = archive_bytes({"original.ttf": font_bytes(weight=0)})
    report = run_recovery([package_dep(data, "font", "original.ttf")], FakeClient([item(resource_type=10)], {ZIP_URL: data}))
    assert not report["items"] and report["results"][0]["status"] == "invalid_font_names_weight_or_cmap"


def test_missing_ids_placeholders_and_unexpected_returned_id(run_recovery):
    client = FakeClient(unexpected=[item(OTHER_RID)])
    report = run_recovery([audio_dep(remote_ids=[]), audio_dep(value="##_material_placeholder_shared_##"),
                           audio_dep(value="Z:/Cache/music/" + "a" * 32 + ".mp3")], client)
    assert not client.downloads and not report["items"]
    assert [row["status"] for row in report["results"]] == [
        "missing_remote_id", "bare_placeholder_without_original_hash", "remote_id_not_returned"]


def test_cache_id_is_used_when_node_id_missing(run_recovery):
    data = archive_bytes({"content.json": "{}"})
    dep = package_dep(data)
    dep["remote_ids"] = []
    client = FakeClient([item(resource_type=2)], {ZIP_URL: data})
    report = run_recovery([dep], client)
    assert client.queries == [[RID]] and report["items"][0]["remote_ids"] == [RID]


def test_query_failure_does_not_stop_other_ids(run_recovery, monkeypatch):
    monkeypatch.setattr(recovery, "BATCH_SIZE", 1)
    data = b"ID3actual source audio"
    client = FakeClient([item(), item(OTHER_RID)], {AUDIO_URL: data}, failures={1})
    report = run_recovery([audio_dep(data), audio_dep(data, OTHER_RID, value="Z:/different/" + md5(data) + ".mp3")], client)
    assert report["summary"]["mapped_original_paths"] == 1
    assert report["results"][0]["status"] == "OSError"
    assert "do-not-save" not in json.dumps(report)


def test_download_failure_does_not_stop_other_ids(run_recovery):
    data = b"ID3actual source audio"
    other_url = "https://v26-artist.vlabvod.com/second"
    other = item(OTHER_RID)
    other["common_attr"]["download_info"]["url"] = other_url
    client = FakeClient([item(), other], {AUDIO_URL: OSError("private?token=do-not-save"), other_url: data})
    report = run_recovery([audio_dep(data), audio_dep(data, OTHER_RID, value="Z:/other/" + md5(data) + ".mp3")], client)
    assert report["results"][0]["status"] == "OSError"
    assert report["summary"]["mapped_original_paths"] == 1
    assert client.downloads == [AUDIO_URL, other_url]
    assert "do-not-save" not in json.dumps(report)


def source3_music(identifier=RID, source=3, **business):
    row = item(identifier, resource_type=4, **business)
    row["common_attr"]["source"] = source
    row["common_attr"]["item_urls"] = [MUSIC_URL]
    row["song"] = {"duration": 1}
    return row


def test_source3_fallback_only_queries_missing_audio_ids_and_uses_music_item_url(run_recovery):
    audio = b"ID3already found sound"
    music = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00test audio bytes"
    client = FakeClient([item()], {AUDIO_URL: audio, MUSIC_URL: music}, fallback_items=[source3_music(OTHER_RID)])
    report = run_recovery([audio_dep(audio), audio_dep(music, OTHER_RID)], client)
    assert client.queries == [[RID, OTHER_RID], [OTHER_RID]]
    assert client.query_sources == [None, 3]
    assert client.downloads == [AUDIO_URL, MUSIC_URL]
    mapped = report["items"][1]
    assert mapped["resource_type"] == 4 and mapped["resource_source"] == 3
    assert mapped["source_url_field"] == "common_attr.item_urls[0]"
    assert mapped["download_md5"] == md5(music) and mapped["original_only"] is True


@pytest.mark.parametrize("source", [None, 1, "3"])
def test_source3_fallback_does_not_accept_other_type4_sources(run_recovery, source):
    client = FakeClient(fallback_items=[source3_music(source=source)])
    report = run_recovery([audio_dep()], client)
    assert not client.downloads and not report["items"]
    assert report["results"][0]["status"] == "resource_type_mismatch_or_unsupported"


@pytest.mark.parametrize("kind", ["font", "package"])
def test_source3_music_cannot_be_used_as_another_resource_kind(kind):
    with pytest.raises(recovery.RecoveryError, match="resource_type_mismatch"):
        recovery._resource_info(source3_music(), kind)


@pytest.mark.parametrize("business", [{"is_vip": True}, {"is_expired": True}])
def test_source3_fallback_keeps_payment_gate(run_recovery, business):
    client = FakeClient(fallback_items=[source3_music(**business)])
    report = run_recovery([audio_dep()], client)
    assert not client.downloads and not report["items"]
    assert report["results"][0]["status"] == "not_explicitly_free"


def test_source3_fallback_still_requires_actual_original_md5(run_recovery):
    client = FakeClient(data={MUSIC_URL: b"ID3newer version"}, fallback_items=[source3_music()])
    report = run_recovery([audio_dep(b"ID3original version")], client)
    assert not report["items"] and report["results"][0]["status"] == "original_hash_mismatch"


def test_failed_default_query_does_not_trigger_source3_fallback(run_recovery):
    client = FakeClient(failures={1}, fallback_items=[source3_music()])
    report = run_recovery([audio_dep()], client)
    assert client.query_sources == [None]
    assert not client.downloads and not report["items"]


def test_workspace_requires_e_drive_and_new_report_sibling(tmp_path, monkeypatch):
    with pytest.raises(recovery.RecoveryError, match="requires_e_drive"):
        recovery._require_e_drive(PureWindowsPath("C:/anywhere"))
    if tmp_path.drive.upper() != "E:":
        monkeypatch.setattr(recovery, "_require_e_drive", lambda path: None)
    previous = tmp_path / "reports/round5"
    previous.mkdir(parents=True)
    manifest = previous / "import_manifest.json"
    manifest.write_text("{}")
    payload = {"workdir": str(previous), "source_root": str(tmp_path / "source")}
    with pytest.raises(recovery.RecoveryError, match="outside_report_parent"):
        recovery._workspace(manifest, payload, tmp_path / "outside")
    with pytest.raises(recovery.RecoveryError, match="must_be_new"):
        recovery._workspace(manifest, payload, previous)


def test_real_client_request_contract_with_in_memory_response():
    class Opener:
        def open(self, request, **kwargs):
            self.request = request
            return io.BytesIO(json.dumps({"ret": "0", "data": {"effect_item_list": []}}).encode())

    client = recovery.AnonymousResourceClient()
    client.opener = Opener()
    assert client.query([RID]) == []
    request = client.opener.request
    assert json.loads(request.data) == {"items": [{"id": RID, "effect_type": 4}]}
    assert "device_id" not in request.full_url
    assert not any(key.lower() in {"cookie", "authorization"} for key, _ in request.header_items())
    assert client.query([RID], source=3) == []
    assert json.loads(client.opener.request.data) == {"items": [{"id": RID, "effect_type": 4, "source": 3}]}


def test_cli_recover_dispatch(tmp_path, monkeypatch):
    import importlib.util
    import sys

    path = Path(__file__).parents[1] / "scripts/install_existing_presets.py"
    spec = importlib.util.spec_from_file_location("preset_resource_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, "recover", lambda manifest, workdir: calls.append((manifest, workdir)) or {"summary": {}})
    monkeypatch.setattr(sys, "argv", [str(path), "recover", "--manifest", str(tmp_path / "manifest.json"), "--workdir", str(tmp_path / "new")])
    module.main()
    assert calls == [(tmp_path / "manifest.json", tmp_path / "new")]
