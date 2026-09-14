"""Install existing Combination preset packages without writing user drafts.

Preparation is workspace-only.  Installation adds packages/resources and merges
the native virtual-store index; it never changes a source or an existing preset.
This deliberately has no creative assembly or production-qualification logic.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .preset_catalog import _StructureNormalizer
from .preset_trial import extract_inner_drafts

STORE = "CombinationPresetVirtualStore.json"
IGNORED_PATHS = {"draft_file_path", "draft_config_path", "draft_cover_path", "static_cover_image_path", "cover_path"}
SIDECARS = {"draft_meta_info.json", "attachment_pc_common.json", "draft.extra"}
NON_BEHAVIOR = {"smart_ads_info", "retouch_cover", "cover", "source", "path", "version", "material_name"}
MIGRATION_DEFAULTS = {"use_float_render": False, "draft_type": "video", "cloned_model_type": "", "current_words": {}, "subtitle_keywords_config": None, "sub_template_id": -1, "translate_original_text": "", "enter_from": "", "final_algorithm": "", "enable_hsl_curves": True, "uneven_animation_template_info": {"composition": "", "content": "", "order": ""}}


def migration_default(key: str, value: Any) -> bool:
    if key not in MIGRATION_DEFAULTS:
        return False
    if value is None:
        return True
    if isinstance(value, str) and isinstance(MIGRATION_DEFAULTS[key], (dict, list)):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return value == MIGRATION_DEFAULTS[key]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory(path: Path) -> dict[str, str]:
    if path.is_file():
        return {path.name: digest_file(path)}
    return {p.relative_to(path).as_posix(): digest_file(p) for p in sorted(path.rglob("*")) if p.is_file()}


def object_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class _ContentNormalizer(_StructureNormalizer):
    @staticmethod
    def _is_text_key(key: str) -> bool:
        return False  # The catalogue's layout identity intentionally erases text; this one must not.

    def normalize(self, value: Any, key: str = "", content_text_length: int | None = None) -> Any:
        if isinstance(value, dict):
            value = {k: v for k, v in value.items()
                     if not (k in NON_BEHAVIOR and not isinstance(v, (dict, list)))
                     and not migration_default(k, v)
                     and not (isinstance(v, list) and not v)}
        if isinstance(value, str) and (key == "content" or key.endswith("_content")) and value.startswith(("{", "[")):
            try:
                return self.normalize(json.loads(value), key + "_json", content_text_length)
            except ValueError:
                pass
        normalized = super().normalize(value, key, content_text_length)
        if isinstance(normalized, dict):
            normalized = {k: v for k, v in normalized.items() if not migration_default(k, v)}
        return normalized


def content_identity(payload: dict, metadata: dict) -> str:
    normalizer = _ContentNormalizer()
    inners = extract_inner_drafts(payload)
    # Retain external asset identities even though machine-specific paths are
    # omitted by the shared ID/structure normalizer.
    assets = []
    for inner in inners:
        for ref in path_references(inner):
            raw = ref["value"].replace("\\", "/")
            if ref["key"] not in IGNORED_PATHS and not ref["remote_ids"]:
                # Stable resource IDs are already retained in normalized JSON.
                # A cache hydration may populate an initially empty path, so
                # counting those path occurrences would create a false variant.
                marker = "/Resources/ImportedLibrary/"
                asset = raw.split(marker, 1)[1] if marker in raw else raw.rsplit("/", 1)[-1]
                assets.append((ref["location"], asset))
    return object_digest({"drafts": [normalizer.normalize(x) for x in inners], "assets": sorted(assets),
                          "start": metadata.get("rough_cut_start"), "duration": metadata.get("rough_cut_duration")})


def path_references(value: Any, location: str = "", group: str = "") -> list[dict]:
    rows = []
    if isinstance(value, dict):
        remote_ids = [str(value[k]) for k in ("resource_id", "effect_id", "font_resource_id", "third_resource_id") if value.get(k) not in (None, "", "0", 0)]
        # Rich-text font/effectStyle nodes use a numeric `id`, while normal
        # material/track IDs are UUIDs. Audio may retain only music_id/pgc_id.
        for key in ("id", "music_id", "pgc_id"):
            identifier = str(value.get(key) or "")
            if re.fullmatch(r"\d{8,}", identifier) and identifier not in remote_ids:
                remote_ids.append(identifier)
        for key, child in value.items():
            loc = f"{location}/{key}"
            child_group = key if location.endswith("/materials") else group
            if isinstance(child, str) and child and (key == "path" or key.endswith("_path") or key == "font_url"):
                if key not in IGNORED_PATHS and not child.startswith(("http:", "https:", "data:", "asset:", "builtin:")):
                    rows.append({"node": value, "key": key, "value": child, "location": loc, "group": group, "remote_ids": remote_ids})
            elif (key == "content" or key.endswith("_content")) and isinstance(child, str) and child.startswith(("{", "[")):
                try:
                    parsed = json.loads(child)
                except ValueError:
                    continue
                # Store parsed JSON with its owner, so changed font paths are
                # written back into the string by rewrite_paths.
                for row in path_references(parsed, loc, group):
                    row.setdefault("content_owner", value)
                    row.setdefault("content_key", key)
                    row.setdefault("content_value", parsed)
                    rows.append(row)
            else:
                rows.extend(path_references(child, loc, child_group))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            rows.extend(path_references(child, f"{location}/{i}", group))
    return rows


def package_metadata(folder: Path) -> tuple[Path, dict]:
    matches = []
    for path in sorted(folder.glob("*.json")):
        try:
            value = read_json(path)
        except (ValueError, OSError, UnicodeError):
            continue
        if isinstance(value, dict) and value.get("id") and "rough_cut_duration" in value:
            matches.append((path, value))
    if len(matches) > 1:
        # Some distributed packages retain the old header after a native rename.
        # The current directory, header filename, name and local cover must all
        # agree before selecting it; otherwise the ambiguity remains visible.
        named = [(path, value) for path, value in matches
                 if path.stem == folder.name == value.get("name")
                 and Path(str(value.get("cover_path", "")).replace("\\", "/")).stem == folder.name
                 and (folder / Path(str(value["cover_path"]).replace("\\", "/")).name).is_file()]
        if len(named) == 1:
            return named[0]
    if len(matches) != 1:
        raise ValueError(f"expected_one_preset_metadata:{len(matches)}")
    return matches[0]


def package_cover(folder: Path, metadata: dict) -> Path | None:
    leaf = Path(str(metadata.get("cover_path", "")).replace("\\", "/")).name
    for path in [folder / leaf, *sorted(folder.glob("*.jpeg")), *sorted(folder.glob("*.jpg")), *sorted(folder.glob("*.png"))]:
        if path.is_file() and path.stat().st_size:
            return path
    return None


def versions(payload: dict) -> list[str]:
    found = set()
    def collect(draft: dict) -> None:
        for key in ("platform", "last_modified_platform"):
            text = str((draft.get(key) or {}).get("app_version") or "")
            if text:
                found.add(text)
        for material in (draft.get("materials") or {}).get("drafts", []):
            if isinstance(material.get("draft"), dict):
                collect(material["draft"])
    for draft in extract_inner_drafts(payload):
        collect(draft)
    if not found:
        # An empty inner platform does not erase the outer package's recorded
        # save version. This is evidence for the hold, never a version rewrite.
        collect(payload)
    return sorted(found)


def version_hold(values: list[str]) -> str | None:
    parsed = [re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", x) for x in values]
    if not parsed or any(x is None for x in parsed):
        return "unknown_version"
    if any(tuple(int(v or 0) for v in x.groups()) > (8, 8, 0) for x in parsed if x):
        return "newer_than_8_8"
    return None


def resource_kind(ref: dict) -> str:
    raw = ref["value"].replace("\\", "/").lower()
    location = ref["location"].lower()
    if (any(marker in raw for marker in ("/figure_algorithm/", "/resources/matting/", "/resources/videoalg/"))
            or re.search(r"/(matting|video_algorithm)/", location)):
        # These are the author's generated, media-specific results. A parent
        # effect ID identifies the algorithm, not these missing output bytes.
        return "algorithm"
    suffix = Path(ref["value"].replace("\\", "/")).suffix.casefold()
    if suffix in {".ttf", ".otf", ".woff", ".woff2"}:
        return "font"
    if suffix in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        return "audio"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
        return "video"
    if ref["key"] in {"font_path", "font_url"} or "/font/path" in location or re.search(r"/fonts/\d+/path$", location):
        return "font"
    if ref["group"] == "audios" and ref["key"] == "path":
        return "audio"
    if ref["group"] == "videos" and ref["key"] == "path":
        return "image" if ref["node"].get("type") == "photo" else "video"
    return "package"


class LocalResources:
    def __init__(self, source_root: Path, cache_root: Path, resource_roots: tuple[Path, ...] = (), resource_evidence: tuple[Path, ...] = ()):
        self.source_root, self.cache_root = source_root, cache_root
        self.by_leaf: dict[str, list[Path]] = defaultdict(list)
        self.by_identity: dict[tuple[str, str], set[Path]] = defaultdict(set)
        for root in (source_root, cache_root, *resource_roots):
            for path in root.rglob("*"):
                if path.is_file() or path.is_dir() and re.fullmatch(r"[a-fA-F0-9]{32}", path.name):
                    self.by_leaf[path.name.casefold()].append(path)
                    kind = "package" if path.is_dir() else resource_kind({"value": str(path), "key": "path", "location": "", "group": "", "node": {}})
                    for part in path.parts:
                        if re.fullmatch(r"\d{8,}", part) and (kind != "package" or path.is_dir()):
                            self.by_identity[(kind, part)].add(path.resolve())
        self.resolved: dict[tuple, tuple[Path | None, str]] = {}
        self.signatures: dict[Path, dict[str, str]] = {}
        self.evidence_by_original: dict[str, set[Path]] = defaultdict(set)
        # A downloaded revision verified against one source path must not be
        # promoted to every revision sharing that resource ID or filename.
        self.original_only_paths: set[Path] = set()
        def visit_evidence(value: Any) -> None:
            if isinstance(value, dict):
                original = value.get("original_path") or value.get("path") or value.get("before_path")
                resolved = value.get("resolved_path") or (value.get("local_evidence") or {}).get("resolved_target")
                if isinstance(original, str) and isinstance(resolved, str) and original and resolved:
                    target = Path(resolved)
                    if self.usable(target):
                        if value.get("original_only") is True:
                            self.original_only_paths.add(target.resolve())
                        # A bare export placeholder is reused for unrelated
                        # assets. It is useful only together with resource IDs.
                        if not re.fullmatch(r"##_.*placeholder.*_##", original):
                            self.evidence_by_original[original.replace("\\", "/").casefold()].add(target.resolve())
                        kind = value.get("kind") or resource_kind({"value": str(target), "key": "path", "location": "", "group": "", "node": {}})
                        if target.is_dir():
                            kind = "package"
                        ids = [*value.get("remote_ids", []), value.get("resource_id")]
                        for identifier in ids:
                            if identifier and value.get("original_only") is not True:
                                self.by_identity[(kind, str(identifier))].add(target.resolve())
                for child in value.values():
                    visit_evidence(child)
            elif isinstance(value, list):
                for child in value:
                    visit_evidence(child)
        for path in resource_evidence:
            visit_evidence(read_json(path))

    def signature(self, path: Path) -> dict[str, str]:
        if path not in self.signatures:
            self.signatures[path] = inventory(path)
        return self.signatures[path]

    def byte_identity(self, path: Path) -> str:
        signature = self.signature(path)
        return object_digest(signature if path.is_dir() else {"file_bytes": next(iter(signature.values()))})

    def usable(self, path: Path) -> bool:
        if path.is_file():
            return path.stat().st_size > 0
        if path.is_dir():
            return any(p.is_file() and p.stat().st_size > 0 and p.suffix.lower() not in {".txt", ".lock", ".log"} for p in path.rglob("*"))
        return False

    def kind_usable(self, path: Path, kind: str) -> bool:
        if not self.usable(path):
            return False
        if kind == "package":
            return path.is_dir()
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            header = stream.read(20)
        if kind == "font":
            return header[:4] in {b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"wOFF", b"wOF2"}
        if kind == "audio":
            return header.startswith((b"ID3", b"RIFF", b"OggS", b"fLaC")) or len(header) > 1 and header[0] == 255 and header[1] & 224 == 224 or header[4:8] == b"ftyp"
        if kind == "image":
            return header.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8")) or header[:4] == b"RIFF" and header[8:12] == b"WEBP"
        if kind == "video":
            return header[4:8] == b"ftyp" or header.startswith(b"\x1aE\xdf\xa3")
        return False

    def add_source_evidence(self, payload: dict, folder: Path) -> None:
        """Associate recorded resource IDs with locally recovered source bytes."""
        for ref in path_references(payload):
            if not ref["remote_ids"] or re.fullmatch(r"##_.*placeholder.*_##", ref["value"]):
                continue
            kind = resource_kind(ref)
            path, _ = self.resolve(ref["value"], folder, [], kind)
            if path is not None and path not in self.original_only_paths and self.kind_usable(path, kind):
                for identifier in ref["remote_ids"]:
                    self.by_identity[(kind, identifier)].add(path)
        self.resolved.clear()

    def resolve(self, raw: str, folder: Path, remote_ids: list[str], kind: str = "") -> tuple[Path | None, str]:
        key = (raw, str(folder), tuple(remote_ids), kind)
        if key in self.resolved:
            return self.resolved[key]
        normalized = raw.replace("\\", "/")
        def matches_original_type(path: Path) -> bool:
            # A prior mapping is evidence for identity, not permission to use a
            # font package directory where the draft requires a font file.
            if kind in {"font", "audio", "image", "video"}:
                return self.kind_usable(path, kind)
            return self.usable(path)

        candidates = [Path(raw)]
        marker = "/User Data/Cache/"
        if marker in normalized:
            candidates.append(self.cache_root / normalized.split(marker, 1)[1])
        if not Path(raw).is_absolute() and not re.match(r"^[A-Za-z]:", raw):
            candidates.append(folder / normalized)
        if "_##/" in normalized:
            suffix = normalized.split("_##/", 1)[1]
            for parent in (folder, *folder.parents):
                if parent == self.source_root.parent:
                    break
                candidates.append(parent / suffix)
        for path in candidates:
            if matches_original_type(path):
                result = (path.resolve(), "exact_local_or_rebased_path")
                self.resolved[key] = result
                return result
        evidence = {p for p in self.evidence_by_original.get(normalized.casefold(), set()) if matches_original_type(p)}
        if evidence:
            unique = {self.byte_identity(p): p for p in evidence}
            if len(unique) == 1:
                result = (next(iter(unique.values())), "existing_runtime_evidence_exact_path")
                self.resolved[key] = result
                return result
        leaf = normalized.rsplit("/", 1)[-1]
        matches = [p.resolve() for p in self.by_leaf.get(leaf.casefold(), [])
                   if p.resolve() not in self.original_only_paths and matches_original_type(p)]
        # Exact cache hash/filename is preferred over a newer ID-only cache.
        if matches:
            unique = {self.byte_identity(p): p for p in matches}
            if len(unique) == 1:
                result = (next(iter(unique.values())), "same_leaf_identical_payload")
                self.resolved[key] = result
                return result
        if kind == "algorithm":
            result = (None, "missing_original_algorithm_output")
            self.resolved[key] = result
            return result
        # Use resource identity only within the corresponding original cache
        # category, and require an actual unambiguous package/file.
        match = re.search(r"/Cache/([^/]+)/([^/]+)", normalized)
        package_match = re.search(r"/Cache/([^/]+)/([^/]+)/([a-fA-F0-9]{32})(?:/(.*))?$", normalized)
        ids = set(remote_ids)
        if match:
            ids.add(match.group(2))
            roots = [self.cache_root / match.group(1) / x for x in ids]
            targets = []
            for root in roots:
                if not root.is_dir():
                    continue
                if Path(leaf).suffix:
                    targets.extend(p for p in root.rglob(leaf) if p.is_file())
                elif package_match and kind == "package":
                    # content.json may live inside AmazingFeature rather than
                    # at the package entry point. Preserve the original path's
                    # relative role, including its separate lumi_hub_path.
                    suffix = package_match.group(4) or ""
                    targets.extend(p / suffix for p in root.iterdir()
                                   if p.is_dir() and re.fullmatch(r"[a-fA-F0-9]{32}", p.name)
                                   and self.kind_usable(p / suffix, "package"))
            unique = {self.byte_identity(p): p for p in targets
                      if p.resolve() not in self.original_only_paths and matches_original_type(p)}
            if len(unique) == 1:
                result = (next(iter(unique.values())).resolve(), "exact_resource_id_local_package")
                self.resolved[key] = result
                return result
        if kind and remote_ids:
            targets = {p for identifier in remote_ids for p in self.by_identity.get((kind, identifier), set())
                       if p not in self.original_only_paths and self.kind_usable(p, kind)}
            if package_match and kind == "package":
                suffix = package_match.group(4) or ""
                targets = {p / suffix for p in targets
                           if re.fullmatch(r"[a-fA-F0-9]{32}", p.name)
                           and self.kind_usable(p / suffix, "package")}
            unique = {self.byte_identity(p): p for p in targets}
            if len(unique) == 1:
                result = (next(iter(unique.values())), "same_resource_identity_verified_local_bytes")
                self.resolved[key] = result
                return result
            if len(unique) > 1:
                result = (None, "ambiguous_local_resource_identity")
                self.resolved[key] = result
                return result
        result = (None, "missing_or_ambiguous_local_dependency")
        self.resolved[key] = result
        return result


def safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:95] or "预设"


def verify_dependency_targets(manifest: dict) -> int:
    """Every rewritten runtime path must belong to this installation manifest."""
    targets = {str(Path(row["target"]).resolve()).casefold() for row in manifest["resources"]}
    checked = 0
    for row in manifest["packages"]:
        if row["status"] != "prepared":
            continue
        payload = read_json(Path(row["stage"]) / "preset_draft/draft_content.json")
        for ref in path_references(payload):
            if str(Path(ref["value"]).resolve()).casefold() not in targets:
                raise ValueError(f"rewritten_dependency_missing_from_manifest:{row['install_name']}:{ref['location']}")
            checked += 1
    return checked


def prepare(source_root: Path, target_root: Path, cache_root: Path, workdir: Path, resource_roots: tuple[Path, ...] = (), resource_evidence: tuple[Path, ...] = ()) -> dict:
    source_root, target_root, workdir = source_root.resolve(), target_root.resolve(), workdir.resolve()
    if workdir.exists() and any(workdir.iterdir()):
        raise ValueError("preparation_workdir_must_be_new_or_empty")
    workdir.mkdir(parents=True, exist_ok=True)
    resolver = LocalResources(source_root, cache_root.resolve(), resource_roots, resource_evidence)
    existing = []
    for folder in sorted(target_root.iterdir()):
        if not folder.is_dir():
            continue
        meta_path, meta = package_metadata(folder)
        cover = package_cover(folder, meta)
        existing_payload = read_json(folder / "preset_draft/draft_content.json")
        existing.append({"name": folder.name, "id": str(meta["id"]), "cover_sha256": digest_file(cover) if cover else "", "create_time": meta.get("create_time"), "duration": meta.get("rough_cut_duration"), "content_identity": content_identity(existing_payload, meta)})
    occupied_names = {x["name"].casefold() for x in existing}
    occupied_ids = {x["id"].casefold() for x in existing}
    groups: dict[str, list[dict]] = defaultdict(list)
    invalid = []
    for path in sorted(source_root.rglob("draft_content.json")):
        try:
            meta_path, meta = package_metadata(path.parent.parent)
            payload = read_json(path)
            identity = content_identity(payload, meta)
            cover = package_cover(path.parent.parent, meta)
            groups[identity].append({"path": path, "meta_path": meta_path, "metadata": meta, "payload": payload, "cover": cover, "versions": versions(payload)})
        except (ValueError, OSError, UnicodeError) as exc:
            invalid.append({"source": str(path), "reason": str(exc)})
    for copies in groups.values():
        for item in copies:
            resolver.add_source_evidence(item["payload"], item["path"].parent.parent)
    rows, resources = [], {}
    for identity, copies in sorted(groups.items(), key=lambda x: str(x[1][0]["path"])):
        # Prefer an existing compatible representation of an identical effect.
        copies.sort(key=lambda x: (version_hold(x["versions"]) is not None, x["cover"] is None,
                                   len(str(x["path"])), str(x["path"])))
        item = copies[0]
        meta, cover = item["metadata"], item["cover"]
        # A cover from an identical-content copy may be reused even if that
        # copy was saved by a newer editor. The selected draft stays unchanged.
        if cover is None:
            cover = next((x["cover"] for x in copies if x["cover"] is not None), None)
        row = {"content_identity": identity, "source": str(item["path"]), "source_sha256": digest_file(item["path"]), "source_metadata": str(item["meta_path"]), "source_id": str(meta["id"]), "source_name": str(meta.get("name") or item["path"].parent.parent.name), "aliases": [str(x["path"]) for x in copies], "versions": item["versions"], "cover_source": str(cover) if cover else None, "status": "pending", "dependencies": []}
        rows.append(row)
        cover_hash = digest_file(cover) if cover else ""
        if any(str(meta["id"]).casefold() == x["id"].casefold() and identity == x["content_identity"] for x in existing):
            row["status"] = "already_installed"
            continue
        hold = version_hold(item["versions"])
        reasons = ([hold] if hold else []) + ([] if cover else ["missing_cover"])
        payload = copy.deepcopy(item["payload"])
        references = path_references(payload)
        failed, local_resources = [], {}
        for ref in references:
            kind = resource_kind(ref)
            path, basis = resolver.resolve(ref["value"], item["path"].parent.parent, ref["remote_ids"], kind)
            dep = {k: ref[k] for k in ("location", "key", "value", "group", "remote_ids")}
            dep.update(kind=kind, resolution=basis, resolved_path=str(path) if path else None)
            row["dependencies"].append(dep)
            if path is None:
                failed.append(dep)
                continue
            signature = resolver.signature(path)
            resource_key = object_digest(signature)
            resource_name = "package" if path.is_dir() else safe_name(path.name)
            target = target_root.parent / "Resources" / "ImportedLibrary" / resource_key[:24] / resource_name
            ref["node"][ref["key"]] = target.as_posix()
            local_resources[resource_key] = {"source": str(path), "target": str(target), "is_dir": path.is_dir(), "inventory": signature, "bytes": sum((path / rel).stat().st_size for rel in signature) if path.is_dir() else path.stat().st_size}
            dep["installed_path"] = str(target)
        if failed:
            reasons.append("missing_or_ambiguous_local_dependencies")
        if reasons:
            row.update(status="deferred", reasons=reasons, missing_count=len(failed))
            continue
        for ref in references:
            if "content_owner" in ref:
                ref["content_owner"][ref["content_key"]] = json.dumps(ref["content_value"], ensure_ascii=False, separators=(",", ":"))
        # A recorded native mapping can change a default font filename. Compare
        # the fully resolved behavior too, before creating another library item.
        runtime_identity = content_identity(payload, meta)
        if any(runtime_identity == x["content_identity"] for x in existing):
            row.update(status="already_installed", resolved_content_identity=runtime_identity,
                       existing_match_basis="resolved_behavior_and_frozen_resources")
            continue
        name = safe_name(row["source_name"])
        install_id = row["source_id"]
        if install_id.casefold() in occupied_ids:
            install_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"jianying-existing-preset:{install_id.lower()}:{identity}")).upper()
            name += "_" + identity[:8]
        if name.casefold() in occupied_names:
            name += "_" + identity[:8]
        if name.casefold() in occupied_names or install_id.casefold() in occupied_ids:
            raise ValueError("unresolved_stable_destination_conflict")
        occupied_names.add(name.casefold()); occupied_ids.add(install_id.casefold())
        stage = workdir / "packages" / name
        stage.mkdir(parents=True)
        # All media dependencies are frozen separately.  The native package
        # requires only its metadata, cover and full unflattened preset draft.
        shutil.copy2(cover, stage / (name + cover.suffix))
        installed_meta = copy.deepcopy(meta)
        installed_meta.update(id=install_id, name=name, cover_path=(target_root / name / (name + cover.suffix)).as_posix())
        write_json(stage / (name + ".json"), installed_meta)
        write_json(stage / "preset_draft" / "draft_content.json", payload)
        row.update(status="prepared", install_id=install_id, install_name=name, stage=str(stage), target=str(target_root / name), inventory=inventory(stage))
        resources.update(local_resources)
    for key, resource in resources.items():
        source = Path(resource["source"])
        stage = workdir / "resources" / key[:24] / ("package" if source.is_dir() else safe_name(source.name))
        stage.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, stage)
        else:
            shutil.copy2(source, stage)
        if inventory(stage) != resource["inventory"]:
            raise ValueError(f"staged_resource_differs:{source}")
        resource["stage"] = str(stage)
    store_path = target_root / STORE
    result = {"schema": "jianying-adapter.preset-package-install.v1", "source_root": str(source_root), "target_root": str(target_root), "workdir": str(workdir), "existing_presets": existing, "store_sha256_before": digest_file(store_path), "resources": list(resources.values()), "packages": rows, "invalid_packages": invalid,
              "summary": {"source_draft_count": sum(len(x) for x in groups.values()) + len(invalid), "unique_content_count": len(groups), "duplicate_source_copies_skipped": sum(len(x) - 1 for x in groups.values()), "status_counts": dict(Counter(x["status"] for x in rows)), "invalid_package_count": len(invalid), "resource_count": len(resources), "resource_bytes": sum(x["bytes"] for x in resources.values()), "prepared_package_bytes": sum(p.stat().st_size for p in (workdir / "packages").rglob("*") if p.is_file())}, "production_qualification_changed": False}
    result["summary"]["verified_runtime_path_occurrences"] = verify_dependency_targets(result)
    write_json(workdir / "import_manifest.json", result)
    return result


def bind_catalog(catalog: dict, manifests: list[dict]) -> tuple[dict, dict]:
    """Bind existing template IDs to verified installed files, without qualification.

    Slot locators are rebuilt from the installed representation because identical
    source aliases can have different local material IDs. A prepared package is
    eligible only after its actual destination has the recorded draft bytes.
    """
    from .preset_registry import _extract_slots, describe_visual_form

    result = copy.deepcopy(catalog)
    root = Path(catalog["preset_root"])
    sources = {}
    for manifest in manifests:
        target_root = Path(manifest["target_root"])
        existing = {x["content_identity"]: x for x in manifest.get("existing_presets", [])}
        for row in manifest.get("packages", []):
            destination = None
            if row.get("status") == "prepared" and row.get("target"):
                candidate_path = Path(row["target"]) / "preset_draft/draft_content.json"
                expected = row.get("inventory", {}).get("preset_draft/draft_content.json")
                if expected and candidate_path.is_file() and digest_file(candidate_path) == expected:
                    destination = candidate_path
            elif row.get("status") == "already_installed":
                identity = row.get("resolved_content_identity", row["content_identity"])
                prior = existing.get(identity)
                if prior:
                    candidate_path = target_root / prior["name"] / "preset_draft/draft_content.json"
                    if candidate_path.is_file():
                        _, metadata = package_metadata(candidate_path.parent.parent)
                        if content_identity(read_json(candidate_path), metadata) == identity:
                            destination = candidate_path
            if destination:
                for source in row.get("aliases", [row["source"]]):
                    sources[str(Path(source).resolve()).casefold()] = destination
    bound, skipped = [], []
    for candidate in result.get("candidates", []):
        original = candidate.get("original_source_path", candidate["source_path"])
        source = Path(original)
        if not source.is_absolute():
            source = root / source
        target = sources.get(str(source.resolve()).casefold())
        if target is None:
            continue
        payload = read_json(target)
        refs = [ref for ref in path_references(payload) if ref["key"] not in IGNORED_PATHS]
        missing = [ref["value"] for ref in refs if ref["value"] and not Path(ref["value"]).exists()]
        if version_hold(versions(payload)) or missing:
            skipped.append({"template_id": candidate["template_id"], "reason": "version_or_missing_runtime_resource"})
            continue
        slots = _extract_slots(payload, candidate["display_name"])
        # Preserve deliberate decorative locks and manual-style decisions only
        # when the same slot text still exists at that ordinal.
        for kind, rows in slots.items():
            old_rows = candidate.get("slots", {}).get(kind, [])
            for index, slot in enumerate(rows):
                if index < len(old_rows) and slot.get("default_text") == old_rows[index].get("default_text"):
                    for key in ("decorative_locked", "required", "style_lock_reason"):
                        if key in old_rows[index]:
                            slot[key] = old_rows[index][key]
        candidate.update(original_source_path=original, source_path=str(target),
                         source_hash=digest_file(target), slots=slots,
                         visual_features=describe_visual_form(payload, resource_base=target.parent),
                         installed_resource_binding={"verified": True, "source": str(source),
                                                     "installed": str(target), "production_qualification_changed": False})
        dependencies = candidate.setdefault("dependencies", {})
        dependencies["path_dependencies"] = [{"kind": resource_kind(ref), "original_path": ref["value"],
                                               "exists_on_current_machine": Path(ref["value"]).exists()} for ref in refs]
        dependencies["missing_path_count"] = 0
        dependencies["external_material_count"] = len(refs)
        bound.append(candidate["template_id"])
    return result, {"bound_count": len(bound), "template_ids": bound, "skipped": skipped,
                    "production_qualification_changed": False}


def apply(manifest_path: Path) -> dict:
    from .jianying_environment import collect_jianying_processes
    manifest = read_json(manifest_path)
    dependency_path_count = verify_dependency_targets(manifest)
    target = Path(manifest["target_root"]).resolve()
    workdir = manifest_path.resolve().parent
    if target.drive.upper() != "E:":
        raise ValueError("native_preset_install_requires_explicit_E_drive_target")
    if collect_jianying_processes():
        raise ValueError("close_jianying_before_preset_install")
    store_path = target / STORE
    if digest_file(store_path) != manifest["store_sha256_before"]:
        raise ValueError("native_store_changed_since_prepare")
    prepared = [x for x in manifest["packages"] if x["status"] == "prepared"]
    for item in [*manifest["resources"], *prepared]:
        if inventory(Path(item["stage"])) != item["inventory"]:
            raise ValueError(f"staging_changed:{item['stage']}")
        dest = Path(item["target"]).resolve()
        if not dest.is_relative_to(target.parent) or dest == target.parent:
            raise ValueError("destination_escaped_combination_root")
        if dest.exists() and inventory(dest) != item["inventory"]:
            raise ValueError(f"existing_destination_conflict:{dest}")
    backup = workdir / "native_store_before.json"
    if not backup.exists():
        shutil.copy2(store_path, backup)
    added = []
    for item in [*manifest["resources"], *prepared]:
        source, dest = Path(item["stage"]), Path(item["target"])
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, dest)
            else:
                shutil.copy2(source, dest)
            added.append(str(dest))
        if inventory(dest) != item["inventory"]:
            raise ValueError(f"installed_copy_differs:{dest}")
    if collect_jianying_processes() or digest_file(store_path) != manifest["store_sha256_before"]:
        raise ValueError("application_or_store_changed_before_index_merge")
    store = read_json(store_path)
    categories = {x["type"]: x["value"] for x in store["preset_virtual_store"]}
    by_id = {str(x["id"]).lower() for x in categories[0]}
    for row in prepared:
        if row["install_id"].lower() in by_id:
            raise ValueError("duplicate_native_store_id")
        categories[0].append({"copyright_type": "", "enterprise_id": "", "id": row["install_id"], "is_enterprise_edition": False, "name": row["install_name"], "save_from": 0, "subsystem_id": "", "type": 1, "user_id": ""})
        categories[1].append({"child_id": row["install_id"], "parent_id": ""})
        by_id.add(row["install_id"].lower())
    temp = target / (STORE + ".importing")
    write_json(temp, store)
    temp.replace(store_path)
    if read_json(store_path) != store:
        shutil.copy2(backup, store_path)
        raise ValueError("store_readback_failed_restored_backup")
    result = {"schema": "jianying-adapter.preset-package-install-result.v1", "installed_count": len(prepared), "native_store_count": len(categories[0]), "new_paths": added, "names": [x["install_name"] for x in prepared], "store_before_backup": str(backup), "store_sha256_after": digest_file(store_path), "all_copies_readback_equal": True, "verified_runtime_path_occurrences": dependency_path_count, "source_presets_modified": False, "production_qualification_changed": False, "native_visual_acceptance": "not_performed"}
    write_json(workdir / "import_result.json", result)
    return result
