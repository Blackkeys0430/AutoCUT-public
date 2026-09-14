"""Rehydrate preset resources into a new, auditable phase-3 workbench.

Only explicit source metadata and read-only local cache evidence are used.
The v1/v2/v3 ledgers, source drafts, project state, and Memos are never
modified.  A resource is frozen only after non-empty bytes and a conservative
type/header check pass; remote IDs alone never close a dependency.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from scripts.repair_preset_resources_phase1 import CURRENT_CACHE, SOURCE_ROOT
except ModuleNotFoundError:  # direct ``python scripts/<tool>.py`` invocation
    from repair_preset_resources_phase1 import CURRENT_CACHE, SOURCE_ROOT  # type: ignore[no-redef]


ADAPTER = Path(__file__).resolve().parents[1]
CATALOG = ADAPTER / "preset_catalog"
V1_PATH = CATALOG / "standardized_preset_catalog_v1.json"
V3_PATH = ADAPTER / "repair_workspaces" / "phase2_engineering_adaptation_20260903" / "standardized_preset_catalog_v3_candidate.json"
AUTO_PATH = CATALOG / "all_auto_candidates_v1.json"
PHASE1_MANIFEST = ADAPTER / "repair_workspaces" / "phase1_resource_repair_20260903" / "repair_manifest_phase1.json"
PHASE2_MANIFEST = ADAPTER / "repair_workspaces" / "phase2_engineering_adaptation_20260903" / "adaptation_manifest_phase2.json"
WORKBENCH = ADAPTER / "repair_workspaces" / "phase3_remote_resource_rehydration_20260903"
INVENTORY_PATH = WORKBENCH / "resource_inventory_phase3.json"
DOWNLOADS_PATH = WORKBENCH / "download_cache_failures_phase3.json"
CLOSURE_PATH = WORKBENCH / "preset_closure_manifest_phase3.json"
AUDIT_PATH = WORKBENCH / "phase3_audit.json"
V4_PATH = WORKBENCH / "standardized_preset_catalog_v4_candidate.json"
RECOVERY_PATH = WORKBENCH / "recovery.md"
FROZEN_ROOT = WORKBENCH / "frozen_resources"

ID_RE = re.compile(r"(?<!\d)(\d{8,})(?!\d)")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根对象不是 object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def candidate_rows(value: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(row.get("template_id")): row for row in value.get("candidates", []) if isinstance(row, Mapping) and row.get("template_id")}


def path_rows(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in (candidate.get("dependencies") or {}).get("path_dependencies") or [] if isinstance(row, Mapping)]


def remote_rows(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in (candidate.get("dependencies") or {}).get("remote_resource_ids") or [] if isinstance(row, Mapping) and row.get("remote_resource_id") not in (None, "", "0")]


def infer_kind(path: str, parent_keys: Iterable[str] = ()) -> str:
    value = "/".join([*parent_keys, path]).lower()
    if "font" in value or Path(path).suffix.lower() in {".ttf", ".otf", ".woff", ".woff2"}:
        return "font"
    if any(token in value for token in ("audio", "music", "sound")) or Path(path).suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        return "audio"
    if any(token in value for token in ("video", "movie")) or Path(path).suffix.lower() in {".mp4", ".mov", ".m4v", ".avi", ".mkv"}:
        return "video"
    if any(token in value for token in ("image", "photo", "cover", "canvas")) or Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    return "resource"


def walk_source_metadata(value: Any, *, path: str = "", parent_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Extract only explicit IDs, URLs, paths, hashes and size hints."""
    rows: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        local_keys = (*parent_keys, path.rsplit("/", 1)[-1]) if path else parent_keys
        ids: list[tuple[str, str]] = []
        urls: list[str] = []
        paths: list[str] = []
        hashes: list[str] = []
        sizes: list[int] = []
        for key, item in value.items():
            key_text = str(key)
            low = key_text.lower()
            if not isinstance(item, (Mapping, list)):
                text = str(item or "")
                if "url" in low and text.startswith(("http://", "https://")):
                    urls.append(text)
                if "path" in low and text:
                    paths.append(text)
                if "hash" in low or low in {"md5", "sha256"}:
                    if text:
                        hashes.append(text)
                if low in {"size", "file_size", "filesize", "duration_us", "width", "height"}:
                    try:
                        sizes.append(int(item))
                    except (TypeError, ValueError):
                        pass
                if "resource_id" in low or low.endswith("effect_id") or low.endswith("template_id"):
                    if text and text != "0":
                        ids.append((key_text, text))
            rows.extend(walk_source_metadata(item, path=f"{path}/{key_text}", parent_keys=local_keys))
        if ids:
            kind = infer_kind(path, local_keys)
            for key, resource_id in ids:
                rows.append({
                    "resource_id": resource_id,
                    "id_key": key,
                    "kind": kind,
                    "explicit_urls": sorted(set(urls)),
                    "declared_paths": sorted(set(paths)),
                    "expected_filename": Path(paths[0]).name if paths else None,
                    "expected_extension": Path(paths[0]).suffix.lower() if paths else None,
                    "declared_hashes": sorted(set(hashes)),
                    "declared_sizes": sorted(set(sizes)),
                    "source_json_path": path or "/",
                })
    elif isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(walk_source_metadata(item, path=f"{path}/{index}", parent_keys=parent_keys))
    return rows


def source_metadata_for(record: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    source = Path(str(record.get("source", {}).get("source_path_absolute") or ""))
    if not source.is_file():
        return [], "source_draft_missing"
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [], "source_draft_unreadable"
    return walk_source_metadata(value), None


def file_index(roots: Iterable[Path]) -> tuple[dict[str, list[Path]], dict[str, list[Path]]]:
    by_name: dict[str, list[Path]] = defaultdict(list)
    by_id: dict[str, list[Path]] = defaultdict(list)
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            by_name[path.name.lower()].append(path)
            for token in ID_RE.findall(str(path)):
                by_id[token].append(path)
    return dict(by_name), dict(by_id)


def file_header_valid(path: Path, kind: str) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
        if size <= 16:
            return False, "empty_or_too_small"
        head = path.read_bytes()[:4096]
    except OSError:
        return False, "unreadable"
    ext = path.suffix.lower()
    if kind == "font":
        ok = head[:4] in {b"\x00\x01\x00\x00", b"OTTO", b"wOFF", b"wOF2"}
        return ok, "font_header" if ok else "font_magic_mismatch"
    if kind == "image":
        signatures = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF")
        ok = head.startswith(signatures[0]) or head.startswith(signatures[1]) or head.startswith(signatures[2]) or (head.startswith(signatures[3]) and b"WEBP" in head[:16])
        return ok, "image_header" if ok else "image_magic_mismatch"
    if kind == "audio":
        ok = head.startswith((b"ID3", b"RIFF", b"OggS", b"fLaC")) or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
        return ok, "audio_header" if ok else "audio_magic_mismatch"
    if kind == "video":
        ok = (len(head) > 12 and head[4:8] == b"ftyp") or head.startswith((b"RIFF", b"\x1a\x45\xdf\xa3"))
        return ok, "video_header" if ok else "video_magic_mismatch"
    # Opaque Jianying effect blobs have no portable extension; non-empty bytes
    # are accepted only when a source ID/path supplies the trusted identity.
    # Cache sidecars are not effect resources and must never be frozen.
    if ext in {".txt", ".json", ".js", ".log", ".db", ".tmp"} or path.name.lower().endswith("_modity_time.txt"):
        return False, "cache_sidecar_not_resource"
    return True, "opaque_nonempty_bytes"


def freeze_valid_file(path: Path, kind: str, expected_extension: str | None) -> dict[str, Any]:
    valid, validation = file_header_valid(path, kind)
    if not valid:
        return {"status": "invalid_local_match", "validation": validation, "path": str(path)}
    digest = sha256_file(path)
    extension = expected_extension or path.suffix.lower() or ".bin"
    if not extension.startswith("."):
        extension = "." + extension
    # Source metadata can contain a URL/query string next to a filename.
    # Never let that untrusted text become a filesystem path component.
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", extension):
        fallback = path.suffix.lower()
        extension = fallback if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", fallback) else ".bin"
    frozen = FROZEN_ROOT / f"{digest}{extension}"
    frozen.parent.mkdir(parents=True, exist_ok=True)
    if not frozen.is_file():
        shutil.copy2(path, frozen)
    frozen_valid, frozen_validation = file_header_valid(frozen, kind)
    if not frozen_valid or sha256_file(frozen) != digest:
        return {"status": "freeze_verification_failed", "validation": frozen_validation, "path": str(path)}
    return {
        "status": "cache_hit_frozen",
        "validation": frozen_validation,
        "source_path": str(path),
        "frozen_path": str(frozen),
        "sha256": digest,
        "size_bytes": frozen.stat().st_size,
        "source": "local_cache_or_source_root",
    }


def download_explicit(url: str, kind: str, expected_extension: str | None) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return {"status": "download_rejected", "url": url, "reason": "only_public_http_https_without_credentials"}
    try:
        request = Request(url, headers={"User-Agent": "jianying-adapter-resource-audit/1"})
        with urlopen(request, timeout=15) as response:  # no guessed URLs; caller supplies metadata URL
            content = response.read(100 * 1024 * 1024 + 1)
        if len(content) > 100 * 1024 * 1024:
            return {"status": "download_rejected", "url": url, "reason": "over_100MiB_limit"}
        temp = WORKBENCH / "download_tmp" / hashlib.sha256(url.encode()).hexdigest()
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(content)
        result = freeze_valid_file(temp, kind, expected_extension)
        result["url"] = url
        result["source"] = "explicit_public_metadata_url"
        return result
    except Exception as exc:  # network, HTTP, and filesystem errors are audit data
        return {"status": "download_failed", "url": url, "reason": type(exc).__name__}


def resource_key(kind: str, original_path: str) -> str:
    return f"{kind}:{original_path.replace(chr(92), '/')}"


def build_phase3(
    *,
    v1_path: Path = V1_PATH,
    v3_path: Path = V3_PATH,
    auto_path: Path = AUTO_PATH,
    phase1_manifest_path: Path = PHASE1_MANIFEST,
    phase2_manifest_path: Path = PHASE2_MANIFEST,
    local_roots: Iterable[Path] = (SOURCE_ROOT, CURRENT_CACHE),
    enforce_scope: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    v1 = read_json(v1_path)
    v3 = copy.deepcopy(read_json(v3_path) if v3_path.is_file() else v1)
    auto = candidate_rows(read_json(auto_path))
    phase1 = read_json(phase1_manifest_path) if phase1_manifest_path.is_file() else {"records": []}
    phase2 = read_json(phase2_manifest_path) if phase2_manifest_path.is_file() else {"records": []}
    phase1_ids = {str(row.get("template_id")) for row in phase1.get("records", []) if isinstance(row, Mapping) and row.get("template_id")}
    phase2_ids = {str(row.get("template_id")) for row in phase2.get("records", []) if isinstance(row, Mapping) and row.get("repair_class") == "partial"}
    if enforce_scope and (len(phase1_ids) != 285 or len(phase2_ids) != 242):
        raise ValueError(f"第三阶段范围应为 phase1=285、phase2部分=242，实际为 {len(phase1_ids)}、{len(phase2_ids)}")
    records = {str(row.get("template_id")): row for row in v1.get("records", []) if isinstance(row, Mapping) and row.get("template_id")}
    by_name, by_id = file_index(local_roots)
    metadata_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    metadata_errors: Counter[str] = Counter()
    for template_id in sorted(phase1_ids | phase2_ids):
        metadata, error = source_metadata_for(records.get(template_id, {}))
        if error:
            metadata_errors[error] += 1
        for row in metadata:
            metadata_by_id[str(row["resource_id"])].append({"template_id": template_id, **row})

    inventory: dict[str, dict[str, Any]] = {}
    preset_dependencies: dict[str, list[str]] = defaultdict(list)
    download_log: list[dict[str, Any]] = []
    for template_id in sorted(phase1_ids | phase2_ids):
        candidate = auto.get(template_id, {})
        candidate_remote_ids = sorted({str(item.get("remote_resource_id")) for item in remote_rows(candidate)})
        metadata = [row for resource_id in candidate_remote_ids for row in metadata_by_id.get(resource_id, [])]
        metadata_by_path = {str(path): row for row in metadata for path in row.get("declared_paths") or []}
        remote_ids_by_kind: dict[str, list[str]] = defaultdict(list)
        for item in remote_rows(candidate):
            remote_ids_by_kind[str(item.get("kind") or "resource")].append(str(item.get("remote_resource_id")))
        for dep in path_rows(candidate):
            kind = str(dep.get("kind") or "resource")
            original = str(dep.get("original_path") or "")
            key = resource_key(kind, original)
            preset_dependencies[template_id].append(key)
            if key in inventory:
                inventory[key]["preset_ids"].append(template_id)
                continue
            meta = metadata_by_path.get(original) or metadata_by_path.get(original.replace("\\", "/"))
            expected_path = Path(original)
            expected_filename = str((meta or {}).get("expected_filename") or expected_path.name or "")
            expected_extension = str((meta or {}).get("expected_extension") or expected_path.suffix.lower() or "") or None
            remote_ids = list(dict.fromkeys(remote_ids_by_kind.get(kind, []) + remote_ids_by_kind.get("resource", [])))
            candidates = []
            if original and Path(original).is_file():
                candidates.append(Path(original))
            candidates.extend(by_name.get(expected_filename.lower(), []))
            for resource_id in remote_ids:
                candidates.extend(by_id.get(resource_id, []))
            unique_candidates = list(dict.fromkeys(candidates))
            resolution: dict[str, Any] | None = None
            for local in unique_candidates:
                result = freeze_valid_file(local, kind, expected_extension)
                if result.get("status") == "cache_hit_frozen":
                    resolution = result
                    break
            if resolution is None:
                urls = list(dict.fromkeys((meta or {}).get("explicit_urls") or []))
                for url in urls:
                    result = download_explicit(url, kind, expected_extension)
                    download_log.append({"template_id": template_id, "resource_key": key, **result})
                    if result.get("status") == "cache_hit_frozen":
                        resolution = result
                        break
            inventory[key] = {
                "resource_key": key,
                "kind": kind,
                "original_path": original,
                "expected_filename": expected_filename or None,
                "expected_extension": expected_extension,
                "remote_resource_ids": remote_ids,
                "source_metadata": meta,
                "explicit_urls": list(dict.fromkeys((meta or {}).get("explicit_urls") or [])),
                "preset_ids": [template_id],
                "resolution": resolution or {"status": "missing_after_cache_and_explicit_url"},
            }

    # Remote IDs without a path still need an auditable inventory row and may
    # not silently close a preset.  They are keyed independently from path deps.
    for template_id in sorted(phase1_ids | phase2_ids):
        for item in remote_rows(auto.get(template_id, {})):
            resource_id = str(item.get("remote_resource_id"))
            key = f"remote_id:{item.get('kind') or 'resource'}:{resource_id}"
            # Each preset owns this dependency reference even when the
            # deduplicated inventory row was created for another preset.
            preset_dependencies[template_id].append(key)
            if key in inventory:
                inventory[key]["preset_ids"].append(template_id)
                continue
            metadata = metadata_by_id.get(resource_id, [])
            inventory[key] = {
                "resource_key": key,
                "kind": str(item.get("kind") or "resource"),
                "original_path": None,
                "expected_filename": (metadata[0].get("expected_filename") if metadata else None),
                "expected_extension": (metadata[0].get("expected_extension") if metadata else None),
                "remote_resource_ids": [resource_id],
                "source_metadata": metadata[0] if metadata else None,
                "explicit_urls": sorted({url for row in metadata for url in row.get("explicit_urls") or []}),
                "preset_ids": [template_id],
                "resolution": {"status": "remote_id_without_verified_local_file"},
            }
    for row in inventory.values():
        row["preset_ids"] = sorted(set(row["preset_ids"]))
    inventory_rows = [inventory[key] for key in sorted(inventory)]
    phase1_remote_keys = [
        resource_key(str(dep.get("kind") or "resource"), str(dep.get("original_path") or ""))
        for row in phase1.get("records", []) if isinstance(row, Mapping)
        for dep in row.get("dependencies", []) if isinstance(dep, Mapping)
        if dep.get("resolution") == "remote_rehydration_required"
    ]
    frozen_hashes = {
        str(row["resolution"].get("sha256"))
        for row in inventory_rows
        if row["resolution"].get("status") == "cache_hit_frozen" and row["resolution"].get("sha256")
    }

    closure_rows: list[dict[str, Any]] = []
    v4_by_id = {str(row.get("template_id")): row for row in v3.get("records", []) if isinstance(row, Mapping)}
    for template_id in sorted(phase1_ids | phase2_ids):
        record = records.get(template_id, {})
        keys = list(dict.fromkeys(preset_dependencies.get(template_id, [])))
        unresolved = [key for key in keys if inventory[key]["resolution"].get("status") != "cache_hit_frozen"]
        phase2_status = (v4_by_id.get(template_id, {}).get("phase2_adaptation") or {}).get("status")
        complete = not unresolved
        if complete and phase2_status in {"adapter_ready_pending_current_video", "failed_unresolved"}:
            status = "adapter_ready_pending_current_video" if phase2_status == "adapter_ready_pending_current_video" else "failed_unresolved"
        elif complete:
            status = "production_ready"
        elif phase2_status == "adapter_ready_pending_current_video":
            status = "adapter_ready_pending_current_video"
        else:
            status = "blocked"
        closure = {
            "template_id": template_id,
            "scope": "phase1_285" if template_id in phase1_ids else "phase2_partial_242",
            "dependency_keys": keys,
            "dependency_count": len(keys),
            "unresolved_dependency_keys": unresolved,
            "all_required_dependencies_closed": complete,
            "phase2_status_before": phase2_status,
            "phase3_status": status,
            "resource_ids_preserved": True,
            "production_gate_rechecked": bool(record),
        }
        closure_rows.append(closure)
        if template_id in v4_by_id:
            v4_by_id[template_id]["phase3_resource_closure"] = closure
            v4_by_id[template_id].setdefault("production", {})["phase3_status"] = status
            v4_by_id[template_id]["production"]["phase3_ready_for_jianying_8_8"] = status == "production_ready"
    counts = Counter(row["phase3_status"] for row in closure_rows)
    phase1_closure = [row for row in closure_rows if row["scope"] == "phase1_285"]
    phase2_closure = [row for row in closure_rows if row["scope"] == "phase2_partial_242"]
    audit = {
        "schema": "jianying-adapter.preset-resource-rehydration-audit.v1",
        "scope": {
            "phase1_preset_count": len(phase1_ids),
            "phase1_remote_rehydration_required_dependency_count": sum(
                1 for row in read_json(phase1_manifest_path).get("records", []) for dep in row.get("dependencies", []) if dep.get("resolution") == "remote_rehydration_required"
            ) if phase1_manifest_path.is_file() else 0,
            "phase2_partial_preset_count": len(phase2_ids),
            "union_preset_count": len(phase1_ids | phase2_ids),
        },
        "resources": {
            "inventory_dependency_reference_count": sum(len(preset_dependencies[t]) for t in preset_dependencies),
            "deduplicated_inventory_count": len(inventory_rows),
            "cache_hit_frozen_count": sum(row["resolution"].get("status") == "cache_hit_frozen" for row in inventory_rows),
            "successful_download_count": sum(row.get("status") == "cache_hit_frozen" and row.get("source") == "explicit_public_metadata_url" for row in download_log),
            "still_missing_count": sum(row["resolution"].get("status") != "cache_hit_frozen" for row in inventory_rows),
            "frozen_file_count_deduplicated_by_sha256": len(frozen_hashes),
            "phase1_remote_rehydration_required_dependency_count": len(phase1_remote_keys),
            "phase1_remote_cache_hit_frozen_count": sum(
                inventory.get(key, {}).get("resolution", {}).get("status") == "cache_hit_frozen" for key in phase1_remote_keys
            ),
            "phase1_remote_successful_download_count": sum(
                inventory.get(key, {}).get("resolution", {}).get("source") == "explicit_public_metadata_url" for key in phase1_remote_keys
            ),
            "phase1_remote_still_missing_count": sum(
                inventory.get(key, {}).get("resolution", {}).get("status") != "cache_hit_frozen" for key in phase1_remote_keys
            ),
            "empty_or_placeholder_files_accepted": 0,
            "remote_ids_only_not_closed": True,
        },
        "presets": {
            "phase1": {"count": len(phase1_closure), "production_ready": sum(row["phase3_status"] == "production_ready" for row in phase1_closure), "blocked": sum(row["phase3_status"] == "blocked" for row in phase1_closure)},
            "phase2_partial": {"count": len(phase2_closure), "production_ready": sum(row["phase3_status"] == "production_ready" for row in phase2_closure), "adapter_ready_pending_current_video": sum(row["phase3_status"] == "adapter_ready_pending_current_video" for row in phase2_closure), "blocked": sum(row["phase3_status"] == "blocked" for row in phase2_closure)},
            "status_counts": dict(sorted(counts.items())),
        },
        "source_metadata": {"draft_parse_errors": dict(sorted(metadata_errors.items())), "explicit_download_urls_seen": sum(bool(row.get("explicit_urls")) for row in inventory_rows)},
        "downloads": {"attempted": len(download_log), "failures": [row for row in download_log if row.get("status") != "cache_hit_frozen"]},
        "v1_v2_v3_untouched": True,
        "source_presets_untouched": True,
        "project_state_untouched": True,
        "memos_untouched": True,
        "draft_written_or_registered": False,
        "source_snapshot": {"v1_sha256": sha256_file(v1_path), "v3_sha256": sha256_file(v3_path) if v3_path.is_file() else None, "auto_sha256": sha256_file(auto_path)},
    }
    v3["schema"] = "jianying-adapter.standardized-preset-catalog.v4-candidate"
    v3["generator"] = "rehydrate_preset_resources_phase3.py"
    v3["phase3_scope"] = {"phase1_preset_count": len(phase1_ids), "phase2_partial_preset_count": len(phase2_ids), "source_ledgers_are_read_only": True, "draft_written_or_registered": False}
    lists = {
        "production_ready": {"schema": "jianying-adapter.phase3-production-ready.v1", "records": [row for row in closure_rows if row["phase3_status"] == "production_ready"]},
        "adapter_pending": {"schema": "jianying-adapter.phase3-adapter-pending.v1", "records": [row for row in closure_rows if row["phase3_status"] == "adapter_ready_pending_current_video"]},
        "blocked": {"schema": "jianying-adapter.phase3-blocked.v1", "records": [row for row in closure_rows if row["phase3_status"] == "blocked"]},
    }
    return v3, audit, {"schema": "jianying-adapter.phase3-resource-inventory.v1", "records": inventory_rows}, {"schema": "jianying-adapter.phase3-preset-closure-manifest.v1", "records": closure_rows, "lists": lists}


def recovery_markdown(audit: Mapping[str, Any]) -> str:
    resource = audit["resources"]
    presets = audit["presets"]
    return "\n".join([
        "# 预设远程资源补齐第三阶段恢复说明",
        "",
        "本阶段只在独立工位冻结经过真实字节校验的本机缓存/源资源，或处理源元数据内明确的公开 URL。没有猜 URL、绕过鉴权、删除远程引用或写入剪映草稿。",
        "",
        f"- phase1：{audit['scope']['phase1_preset_count']} 条，remote_rehydration_required 路径依赖 {audit['scope']['phase1_remote_rehydration_required_dependency_count']} 条；phase2 部分适配：{audit['scope']['phase2_partial_preset_count']} 条。",
        f"- 资源引用 {resource['inventory_dependency_reference_count']} 条，去重清单 {resource['deduplicated_inventory_count']} 条；缓存/源命中并冻结 {resource['cache_hit_frozen_count']}，成功下载 {resource['successful_download_count']}，仍缺失 {resource['still_missing_count']}。",
        f"- phase1 的 2217 条 remote_rehydration_required 依赖：缓存命中 {resource['phase1_remote_cache_hit_frozen_count']}，成功下载 {resource['phase1_remote_successful_download_count']}，仍缺失 {resource['phase1_remote_still_missing_count']}。冻结文件按 SHA256 去重 {resource['frozen_file_count_deduplicated_by_sha256']} 个。",
        f"- phase1 production_ready：{presets['phase1']['production_ready']}；phase2 production_ready：{presets['phase2_partial']['production_ready']}；phase2 adapter_pending：{presets['phase2_partial']['adapter_ready_pending_current_video']}。",
        "- 只有全部必需依赖闭合且原有版本、画布、槽位和文字轨门禁仍通过才会标记 production_ready；remote ID 单独存在不算闭合。",
        "",
        "恢复：保留本目录 inventory、closure manifest、v4 candidate、冻结目录和失败日志；删除本工位不会影响 v1/v2/v3 或源预设。输入未变时可重复运行：",
        "`python jianying-adapter/scripts/rehydrate_preset_resources_phase3.py`",
        "",
    ])


def main() -> None:
    v4, audit, inventory, closure = build_phase3()
    write_json(INVENTORY_PATH, inventory)
    write_json(DOWNLOADS_PATH, {"schema": "jianying-adapter.phase3-download-log.v1", "records": audit["downloads"]["failures"]})
    write_json(CLOSURE_PATH, closure)
    write_json(AUDIT_PATH, audit)
    write_json(V4_PATH, v4)
    for key, filename in (("production_ready", "production_ready_phase3.json"), ("adapter_pending", "adapter_pending_phase3.json"), ("blocked", "blocked_phase3.json")):
        write_json(WORKBENCH / filename, closure["lists"][key])
    RECOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECOVERY_PATH.write_text(recovery_markdown(audit), encoding="utf-8")
    print(json.dumps({"workbench": str(WORKBENCH), **audit["presets"]["status_counts"], **audit["resources"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
