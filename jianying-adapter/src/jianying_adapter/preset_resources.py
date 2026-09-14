"""Recover exact, explicitly free preset resources into a new E-drive workspace.

This supplies path-scoped evidence to the existing preset installer. It does not
write native caches, libraries or drafts, and never substitutes a newer ID match
for the source file/package hash.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_ENDPOINT = "https://lv-api-sinfonlinea.ulikecam.com/artist/v1/effect/mget_item"
API_QUERY = {
    "effect_sdk_version": "16.4.0", "channel": "jianyingpro_0", "aid": "3704",
    "version_name": "5.9.0", "language": "zh-Hans", "region": "CN",
    "version_code": "5.9.0", "device_platform": "windows", "biz_id": "2",
    "version_code_num": "329984", "device_type": "x86_64",
}
API_HEADERS = {
    "User-Agent": "JianyingPro/5.9.0.11632 (Windows 10.0.19045; app_id:3704)",
    "Content-Type": "application/json", "Accept": "application/json",
}
# These are the response types verified against the anonymous endpoint, not the
# constant effect_type=4 in its lookup request. Unknown types remain unresolved.
RESOURCE_TYPES = {"audio": {3}, "font": {10}, "package": {1, 2, 13, 18, 92}}
BATCH_SIZE = 10
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
CACHE_PACKAGE = re.compile(r"/Cache/[^/]+/(\d+)/([a-fA-F0-9]{32})(?:/(.*))?$", re.I)
ONLINE_PACKAGE = re.compile(r"/Cache/onlineMaterial/([a-fA-F0-9]{32})(?:/(.*))?$", re.I)
CACHE_ID = re.compile(r"/Cache/[^/]+/(\d{8,})(?:/|$)", re.I)
UUID_AUDIO = re.compile(r"/materials/audio/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}_([a-f0-9]{32})\.mp3$", re.I)
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


class RecoveryError(ValueError):
    """A stable, credential-free reason that can be persisted in the report."""


def _error_reason(exc: Exception) -> str:
    # HTTP and library exception strings can contain signed URLs; do not persist
    # them. Our own errors contain fixed codes only.
    return str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__


def _media_url(url: object) -> str:
    if not isinstance(url, str):
        raise RecoveryError("missing_media_url")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment
            or not any((parsed.hostname or "").endswith(suffix)
                       for suffix in (".vlabvod.com", ".bytecdn.com", ".byteimg.com"))):
        raise RecoveryError("untrusted_media_url")
    return url


class _MediaRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _media_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class AnonymousResourceClient:
    """Anonymous requests only; no cookies, auth handlers or local account data."""

    def __init__(self):
        self.opener = build_opener(_MediaRedirects())

    def query(self, ids: list[str], *, source: int | None = None) -> list[dict]:
        body = {"items": [{"id": identifier, "effect_type": 4} for identifier in ids]}
        if source is not None:
            for row in body["items"]:
                row["source"] = source
        request = Request(API_ENDPOINT + "?" + urlencode(API_QUERY),
                          data=json.dumps(body).encode("utf-8"), headers=API_HEADERS)
        with self.opener.open(request, timeout=20) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise RecoveryError("query_response_too_large")
        payload = json.loads(raw)
        if str(payload.get("ret")) != "0":
            raise RecoveryError("query_not_successful")
        rows = (payload.get("data") or {}).get("effect_item_list")
        if not isinstance(rows, list):
            raise RecoveryError("invalid_query_items")
        return rows

    def download(self, url: str) -> bytes:
        with self.opener.open(Request(_media_url(url)), timeout=30) as response:
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise RecoveryError("download_too_large")
        return data


def _require_e_drive(path: Path) -> None:
    if path.drive.upper() != "E:":
        raise RecoveryError("recovery_requires_e_drive")


def _workspace(manifest: Path, payload: dict, workdir: Path) -> Path:
    """Bind the new directory to the input manifest's existing report parent."""
    recorded = payload.get("workdir")
    if not isinstance(recorded, str) or not recorded:
        raise RecoveryError("manifest_workdir_missing")
    previous = Path(recorded).resolve(strict=True)
    manifest = manifest.resolve(strict=True)
    destination = workdir.resolve()
    for path in (previous, manifest, destination):
        _require_e_drive(path)
    if not previous.is_dir() or manifest.parent != previous:
        raise RecoveryError("manifest_outside_recorded_workdir")
    if destination == previous.parent or not destination.is_relative_to(previous.parent):
        raise RecoveryError("recovery_outside_report_parent")
    for field in ("source_root", "target_root", "cache_root"):
        if payload.get(field) and destination.is_relative_to(Path(payload[field]).resolve()):
            raise RecoveryError("recovery_inside_native_or_source_root")
    if workdir.exists() or workdir.is_symlink() or destination.exists():
        raise RecoveryError("recovery_workdir_must_be_new")
    destination.mkdir(parents=True, exist_ok=False)
    return destination


def _dependencies(manifest: dict) -> list[dict]:
    rows: dict[tuple[str, str], dict] = {}
    for package in manifest.get("packages", []):
        if package.get("status") != "deferred":
            continue
        for dep in package.get("dependencies", []):
            kind, original = dep.get("kind"), dep.get("value")
            if kind not in RESOURCE_TYPES or dep.get("resolved_path") or not isinstance(original, str) or not original:
                continue
            row = rows.setdefault((kind, original), {
                "kind": kind, "original_path": original, "remote_ids": [],
                "original_only": True, "occurrences": 0, "presets": [],
            })
            row["occurrences"] += 1
            identity = package.get("content_identity")
            if identity and identity not in row["presets"]:
                row["presets"].append(identity)
            ids = list(dep.get("remote_ids") or [])
            cached = CACHE_ID.search(original.replace("\\", "/"))
            if cached:
                ids.append(cached.group(1))
            for value in ids:
                identifier = str(value)
                if re.fullmatch(r"\d{8,}", identifier) and identifier not in row["remote_ids"]:
                    row["remote_ids"].append(identifier)
    return list(rows.values())


def _source_hash(row: dict) -> tuple[str, str]:
    raw = row["original_path"].replace("\\", "/")
    if re.fullmatch(r"##_.*placeholder.*_##", raw, re.I):
        raise RecoveryError("bare_placeholder_without_original_hash")
    if row["kind"] == "audio":
        leaf = PurePosixPath(raw)
        if leaf.suffix.lower() in AUDIO_EXTENSIONS and re.fullmatch(r"[a-fA-F0-9]{32}", leaf.stem):
            return leaf.stem.lower(), leaf.suffix.lower()
        prefixed = UUID_AUDIO.search(raw)
        if prefixed:
            return prefixed.group(1).lower(), ".mp3"
    else:
        match = CACHE_PACKAGE.search(raw)
        if match:
            suffix = match.group(3) or ""
            _safe_relative(suffix, allow_empty=True)
            if row["kind"] == "font" and not suffix:
                raise RecoveryError("original_font_filename_missing")
            return match.group(2).lower(), suffix
        online = ONLINE_PACKAGE.search(raw) if row["kind"] == "package" else None
        if online:
            suffix = online.group(2) or ""
            _safe_relative(suffix, allow_empty=True)
            return online.group(1).lower(), suffix
    raise RecoveryError("original_hash_unavailable")


def _safe_relative(name: str, *, allow_empty: bool = False) -> Path:
    normalized = name.replace("\\", "/")
    # Internal repeated separators occur in original native sticker ZIPs. They
    # denote the same directory; retain the leading-slash/drive and '..' checks.
    parts = [part for part in normalized.split("/") if part]
    if (not parts and not allow_empty or normalized.startswith("/")
            or PureWindowsPath(normalized).drive
            or any(part in ("", "..") or ":" in part or "\x00" in part
                   or part.endswith((" ", ".")) and part != "."
                   or PureWindowsPath(part).is_reserved() for part in parts)):
        raise RecoveryError("unsafe_zip_or_entry_path")
    return Path(*parts)


def _extract_zip(path: Path, destination: Path, original_font_names: set[str] | None = None) -> list[dict]:
    decoded_names = []
    expected_names = {name.casefold() for name in original_font_names or ()}
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if not any(not member.is_dir() for member in members):
            raise RecoveryError("empty_zip")
        if sum(member.file_size for member in members) > MAX_EXPANDED_BYTES:
            raise RecoveryError("expanded_zip_too_large")
        seen: set[str] = set()
        checked = []
        for member in members:
            name = member.filename
            if original_font_names and not member.flag_bits & 0x800:
                # Some original Jianying font ZIPs store UTF-8 bytes without the
                # ZIP UTF-8 flag. Decode only when the exact source font filename
                # confirms it; do not guess encodings or rename arbitrary files.
                try:
                    decoded = name.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    decoded = name
                leaf = PurePosixPath(decoded.replace("\\", "/")).name.casefold()
                if decoded != name and (leaf in expected_names
                                        or leaf.startswith("._") and leaf[2:] in expected_names):
                    decoded_names.append({"zip_name": name, "decoded_name": decoded,
                                          "basis": "utf8_without_flag_matches_original_filename"})
                    name = decoded
            relative = _safe_relative(name)
            mode = stat.S_IFMT(member.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or member.flag_bits & 1:
                raise RecoveryError("zip_link_special_or_encrypted_entry")
            normalized = relative.as_posix().casefold()
            if normalized in seen:
                raise RecoveryError("duplicate_zip_entry")
            seen.add(normalized)
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RecoveryError("unsafe_zip_or_entry_path")
            checked.append((member, target))
        # Validate all member paths before writing any member. Keep every level
        # of the archive: the native entry may be the root or a lumi subfolder.
        destination.mkdir(parents=True, exist_ok=False)
        for member, target in checked:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    return decoded_names


def _font_metadata(path: Path) -> dict:
    from fontTools.ttLib import TTFont

    with TTFont(path, lazy=False) as font:
        if "name" not in font or "OS/2" not in font:
            raise RecoveryError("font_names_or_weight_missing")
        names = {}
        for name_id, key in ((1, "family"), (2, "subfamily"), (4, "full_name"), (6, "postscript_name"),
                             (16, "typographic_family"), (17, "typographic_subfamily")):
            names[key] = sorted({record.toUnicode().strip() for record in font["name"].names
                                 if record.nameID == name_id and record.toUnicode().strip()})
        weight = font["OS/2"].usWeightClass
        if (not (names["family"] or names["typographic_family"])
                or not (names["subfamily"] or names["typographic_subfamily"])
                or not 1 <= weight <= 1000 or not font.getBestCmap()):
            raise RecoveryError("invalid_font_names_weight_or_cmap")
        return {**names, "weight_class": weight, "glyph_count": len(font.getGlyphOrder())}


def _is_audio(data: bytes) -> bool:
    return (data.startswith((b"ID3", b"OggS", b"fLaC"))
            or data[:4] == b"RIFF" and data[8:12] == b"WAVE"
            or data[4:8] == b"ftyp"
            or len(data) > 1 and data[0] == 255 and data[1] & 224 == 224)


def _resource_info(item: dict, kind: str) -> tuple[dict, str]:
    attr = item.get("common_attr") or {}
    resource_type = attr.get("effect_type")
    is_source3_music = (kind == "audio" and resource_type == 4
                        and type(attr.get("source")) is int and attr["source"] == 3)
    if type(resource_type) is not int or (resource_type not in RESOURCE_TYPES[kind] and not is_source3_music):
        raise RecoveryError("resource_type_mismatch_or_unsupported")
    try:
        business = json.loads((attr.get("business_info") or {}).get("json_str", "{}"))
    except (TypeError, ValueError):
        raise RecoveryError("not_explicitly_free") from None
    if (not isinstance(business, dict) or business.get("is_vip") is not False
            or business.get("paid_type") != "free" or business.get("is_expired") is not False):
        raise RecoveryError("not_explicitly_free")
    if kind == "audio" and not is_source3_music:
        url = (attr.get("download_info") or {}).get("url")
        field = "common_attr.download_info.url"
    else:
        urls = attr.get("item_urls")
        url = urls[0] if isinstance(urls, list) and urls else None
        field = "common_attr.item_urls[0]"
    url = _media_url(url)
    return {"matched_remote_id": str(attr["id"]), "title": attr.get("title"),
            "resource_type": resource_type, "resource_source": attr.get("source"), "api_md5": attr.get("md5"),
            "source_url_field": field, "source_domain": urlsplit(url).hostname,
            "paid_type": "free", "is_vip": False, "is_expired": False}, url


def recover(manifest: Path, workdir: Path, *, client: AnonymousResourceClient | None = None) -> dict:
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    workdir = _workspace(manifest, payload, workdir)
    client = client or AnonymousResourceClient()
    rows = _dependencies(payload)
    report = {"schema": "jianying-adapter.preset-resource-recovery.v1", "original_only": True,
              "manifest": str(manifest.resolve()), "workdir": str(workdir),
              "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "api_endpoint": API_ENDPOINT, "api_query": API_QUERY,
              "items": [], "results": rows, "downloads": [], "queries": []}
    eligible = []
    for row in rows:
        try:
            if not row["remote_ids"]:
                raise RecoveryError("missing_remote_id")
            row["expected_md5"], row["original_entry"] = _source_hash(row)
            eligible.append(row)
        except Exception as exc:
            row["status"] = _error_reason(exc)
    identifiers = list(dict.fromkeys(identifier for row in eligible for identifier in row["remote_ids"]))
    lookup: dict[str, dict] = {}
    query_errors = {}
    audio_ids = {identifier for row in eligible if row["kind"] == "audio" for identifier in row["remote_ids"]}
    for query_source in (None, 3):
        # Some source audio nodes retain a music ID, not an artist sound ID.
        # Retry only a successful default lookup's missing audio IDs; never
        # retry paid, wrong-type, ambiguous, or failed default responses here.
        request_ids = identifiers if query_source is None else [
            identifier for identifier in identifiers
            if identifier in audio_ids and identifier not in lookup and identifier not in query_errors]
        for offset in range(0, len(request_ids), BATCH_SIZE):
            batch = request_ids[offset:offset + BATCH_SIZE]
            query_record = {"remote_ids": batch}
            if query_source is not None:
                query_record["source"] = query_source
            try:
                found = client.query(batch) if query_source is None else client.query(batch, source=query_source)
                if not isinstance(found, list):
                    raise RecoveryError("invalid_query_items")
                duplicates = set()
                for item in found:
                    if not isinstance(item, dict) or not isinstance(item.get("common_attr"), dict):
                        continue
                    identifier = str(item["common_attr"].get("id") or "")
                    if identifier not in batch:
                        continue
                    if identifier in lookup:
                        duplicates.add(identifier)
                    lookup[identifier] = item
                for identifier in duplicates:
                    lookup.pop(identifier, None)
                    query_errors[identifier] = "ambiguous_returned_id"
                report["queries"].append({**query_record, "status": "queried",
                                           "returned_ids": [identifier for identifier in batch if identifier in lookup]})
            except Exception as exc:
                reason = _error_reason(exc)
                query_errors.update({identifier: reason for identifier in batch})
                report["queries"].append({**query_record, "status": "query_failed", "reason": reason})

    assets: dict[tuple[str, str], dict] = {}
    extracted: dict[tuple[str, str], dict] = {}
    font_names: dict[str, set[str]] = {}
    for row in eligible:
        if row["kind"] == "font":
            font_names.setdefault(row["expected_md5"], set()).add(PurePosixPath(row["original_entry"]).name)
    for row in eligible:
        row["attempts"] = []
        for identifier in row["remote_ids"]:
            attempt = {"remote_id": identifier}
            row["attempts"].append(attempt)
            try:
                if identifier in query_errors:
                    raise RecoveryError(query_errors[identifier])
                if identifier not in lookup:
                    raise RecoveryError("remote_id_not_returned")
                info, url = _resource_info(lookup[identifier], row["kind"])
                key = (row["kind"], identifier)
                if key not in assets:
                    asset = {**info, "kind": row["kind"]}
                    assets[key] = asset
                    report["downloads"].append(asset)
                    try:
                        data = client.download(url)
                        if len(data) > MAX_DOWNLOAD_BYTES:
                            raise RecoveryError("download_too_large")
                        if row["kind"] == "audio" and not _is_audio(data):
                            raise RecoveryError("invalid_audio_payload")
                        if row["kind"] != "audio" and not data.startswith(b"PK\x03\x04"):
                            raise RecoveryError("invalid_zip_payload")
                        actual_md5 = hashlib.md5(data).hexdigest()
                        suffix = row["original_entry"] if row["kind"] == "audio" else ".zip"
                        download = workdir / "downloads" / row["kind"] / identifier / (actual_md5 + suffix)
                        download.parent.mkdir(parents=True, exist_ok=True)
                        with download.open("xb") as stream:
                            stream.write(data)
                        asset.update(status="downloaded_unmapped", download_path=str(download),
                                     download_md5=actual_md5, bytes=len(data), mapped_original_count=0)
                    except Exception as exc:
                        asset.update(status="download_failed", reason=_error_reason(exc))
                asset = assets[key]
                if asset["status"] == "download_failed":
                    raise RecoveryError(asset["reason"])
                attempt["download_md5"] = asset["download_md5"]
                if asset["download_md5"] != row["expected_md5"]:
                    raise RecoveryError("original_hash_mismatch")
                target = Path(asset["download_path"])
                mapping = {**info, "kind": row["kind"], "original_path": row["original_path"],
                           "remote_ids": row["remote_ids"], "original_only": True,
                           "expected_md5": row["expected_md5"], "download_md5": asset["download_md5"]}
                if row["kind"] != "audio":
                    archive_key = (row["kind"], asset["download_md5"])
                    if archive_key not in extracted:
                        root = workdir / "resources" / row["kind"] / asset["download_md5"]
                        try:
                            decodings = _extract_zip(target, root, font_names.get(asset["download_md5"]))
                            extracted[archive_key] = {"root": root, "zip_filename_decodings": decodings}
                        except Exception as exc:
                            extracted[archive_key] = {"error": _error_reason(exc)}
                    unpacked = extracted[archive_key]
                    if "error" in unpacked:
                        raise RecoveryError(unpacked["error"])
                    root = unpacked["root"]
                    target = root / _safe_relative(row["original_entry"], allow_empty=True)
                    if row["kind"] == "font":
                        filename = PurePosixPath(row["original_entry"]).name
                        candidates = [path for path in root.rglob("*") if path.is_file() and path.name.casefold() == filename.casefold()
                                      and "__macosx" not in {part.casefold() for part in path.relative_to(root).parts}
                                      and not path.name.startswith("._")]
                        if len(candidates) != 1:
                            raise RecoveryError("original_font_filename_missing_or_ambiguous")
                        target = candidates[0]
                        mapping["font_metadata"] = _font_metadata(target)
                        mapping["archive_relative_path"] = target.relative_to(root).as_posix()
                        mapping["zip_filename_decodings"] = unpacked["zip_filename_decodings"]
                    elif not target.is_dir() or not any(path.is_file() for path in target.rglob("*")):
                        raise RecoveryError("original_package_entry_missing")
                    mapping["archive_root"] = str(root)
                mapping["resolved_path"] = str(target)
                row["mapping_index"] = len(report["items"])
                report["items"].append(mapping)
                row["status"] = attempt["status"] = "mapped"
                asset["mapped_original_count"] += 1
                asset["status"] = "mapped"
                break
            except Exception as exc:
                attempt["status"] = _error_reason(exc)
        if row.get("status") != "mapped":
            statuses = {attempt["status"] for attempt in row["attempts"]}
            row["status"] = next(iter(statuses)) if len(statuses) == 1 else "unresolved_multiple_reasons"
    report["summary"] = {
        "missing_dependency_occurrences": sum(row["occurrences"] for row in rows),
        "unique_original_paths": len(rows), "queried_remote_ids": len(identifiers),
        "mapped_original_paths": len(report["items"]), "unresolved_original_paths": len(rows) - len(report["items"]),
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "downloaded_files": sum("download_path" in asset for asset in report["downloads"]),
        "mapped_downloads": sum(asset.get("mapped_original_count", 0) > 0 for asset in report["downloads"]),
        "native_writes": False,
    }
    (workdir / "resource-recovery.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
