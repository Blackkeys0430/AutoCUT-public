"""Read-only post-open auditor for Jianying 8.8 resource-recovery probes.

Inputs are the pre-open probe/manifest, a cache baseline, and the encrypted
live draft after Jianying has been closed.  The command decodes but never
writes the live draft.  It does not start, stop, or register Jianying.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ADAPTER = Path(__file__).resolve().parents[1]
VENDOR = ADAPTER / "vendor/pyJianYingDraft-source"
DEPS = ADAPTER / "vendor/python-deps"
for path in (DEPS, VENDOR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from pyJianYingDraft import DraftCryptoConfig, JianyingDraftCryptoCodec  # noqa: E402


REMOTE_KEYS = ("font_resource_id", "resource_id", "effect_id", "third_resource_id")
VISUAL_MATERIAL_KINDS = {
    "color_curves": "color_curve",
    "common_mask": "mask",
    "effects": "effect",
    "images": "image",
    "material_animations": "animation",
    "stickers": "sticker",
    "texts": "text_resource",
    "transitions": "transition",
    "video_effects": "video_effect",
    "videos": "video",
}
STATUS_VALUES = frozenset(
    {
        "unchanged_missing",
        "rewritten_to_existing_local",
        "already_existing_before_baseline",
        "newly_materialized_after_baseline",
    }
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be object: {path}")
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


def _identifier(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _remote_id(node: Mapping[str, Any]) -> tuple[str, str]:
    for key in REMOTE_KEYS:
        value = _identifier(node.get(key))
        if value and value != "0":
            return key, value
    return "", ""


def _remote_ids(node: Mapping[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key in REMOTE_KEYS:
        value = _identifier(node.get(key))
        if value and value != "0" and value not in seen:
            result.append((key, value))
            seen.add(value)
    return result


def _kind_from_location(location: str, fallback: str = "visual_resource") -> str:
    if ".materials.texts" in location:
        if "font_path" in location or ".fonts[" in location or ".font." in location:
            return "font"
        if "effectStyle" in location:
            return "effect"
    marker = ".materials."
    if marker in location:
        group = location.split(marker, 1)[1].split("[", 1)[0].split(".", 1)[0]
        return VISUAL_MATERIAL_KINDS.get(group, fallback)
    return fallback


def _walk_visual_nodes(value: Any, location: str, default_kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        path = value.get("font_path") if value.get("font_path") else value.get("path")
        if isinstance(path, str) and path:
            kind = _kind_from_location(location, default_kind)
            for key, resource_id in _remote_ids(value):
                rows.append(
                    {
                        "kind": "font" if key == "font_resource_id" else kind,
                        "remote_id_key": key,
                        "remote_id": resource_id,
                        "path": path,
                        "location": location,
                    }
                )
        for child_key, child in value.items():
            rows.extend(_walk_visual_nodes(child, f"{location}.{child_key}", default_kind))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            rows.extend(_walk_visual_nodes(child, f"{location}[{index}]", default_kind))
    return rows


def collect_all_visual_dependencies(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Collect every path-backed visual dependency understood by the batch builder."""

    materials = draft.get("materials") or {}
    if not isinstance(materials, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for group, kind in VISUAL_MATERIAL_KINDS.items():
        rows.extend(
            _walk_visual_nodes(materials.get(group) or [], f"$.materials.{group}", kind)
        )
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        unique.setdefault((row["kind"], row["remote_id"], row["path"]), row)
    return [unique[key] for key in sorted(unique)]


def _walk_animation_nodes(value: Any, location: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        key, resource_id = _remote_id(value)
        path = value.get("path")
        if resource_id and isinstance(path, str) and path:
            rows.append(
                {
                    "kind": "animation",
                    "remote_id_key": key,
                    "remote_id": resource_id,
                    "path": path,
                    "location": location,
                }
            )
        for child_key, child in value.items():
            rows.extend(_walk_animation_nodes(child, f"{location}.{child_key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            rows.extend(_walk_animation_nodes(child, f"{location}[{index}]"))
    return rows


def collect_visual_dependencies(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Collect runtime visual dependencies; source wrapper metadata is absent."""

    materials = draft.get("materials") or {}
    if not isinstance(materials, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(materials.get("texts") or []):
        if not isinstance(item, Mapping):
            continue
        resource_id = _identifier(item.get("font_resource_id"))
        path = item.get("font_path")
        if resource_id and resource_id != "0" and isinstance(path, str) and path:
            rows.append(
                {
                    "kind": "font",
                    "remote_id_key": "font_resource_id",
                    "remote_id": resource_id,
                    "path": path,
                    "location": f"$.materials.texts[{index}]",
                }
            )
    for index, item in enumerate(materials.get("material_animations") or []):
        rows.extend(
            _walk_animation_nodes(item, f"$.materials.material_animations[{index}]")
        )
    # A remote ID can occur more than once inside one material.  Runtime
    # closure is a resource question, so keep one deterministic row per kind/ID.
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        unique.setdefault((row["kind"], row["remote_id"]), row)
    return [unique[key] for key in sorted(unique)]


def _builder_visual_rows(template: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    report = template.get("builder_report") or {}
    if not isinstance(report, Mapping):
        return []
    result: list[Mapping[str, Any]] = []
    for inner in report.get("inner_drafts", []) or []:
        if not isinstance(inner, Mapping):
            continue
        build_report = inner.get("build_report") or {}
        if not isinstance(build_report, Mapping):
            continue
        result.extend(
            row
            for row in build_report.get("visual_rehydrations", []) or []
            if isinstance(row, Mapping)
        )
    return result


def normalize_manifest_dependency(
    template: Mapping[str, Any], dependency: Mapping[str, Any]
) -> dict[str, Any]:
    """Add typed evidence to a phase-2 pair without inventing a resource kind."""

    if dependency.get("kind") and dependency.get("remote_id_key"):
        return dict(dependency)
    remote_id = _identifier(dependency.get("remote_id"))
    path = str(dependency.get("path") or "")
    if not remote_id or not path:
        raise ValueError(f"invalid runtime visual dependency: {dependency}")

    candidates: list[dict[str, str]] = []
    for row in template.get("source_runtime_visual_dependencies", []) or []:
        if not isinstance(row, Mapping):
            continue
        if _identifier(row.get("remote_id")) == remote_id and str(row.get("path") or "") == path:
            candidates.append(
                {
                    "kind": str(row.get("kind") or "visual_resource"),
                    "remote_id_key": str(row.get("remote_id_key") or "unknown"),
                    "location": str(row.get("location") or ""),
                    "inference_source": "source_runtime_visual_dependencies",
                }
            )
    for row in _builder_visual_rows(template):
        ids = {_identifier(value) for value in row.get("remote_resource_ids", []) or []}
        if remote_id not in ids or str(row.get("original_path") or "") != path:
            continue
        location = str(row.get("location") or "")
        candidates.append(
            {
                "kind": _kind_from_location(location, str(row.get("kind") or "visual_resource")),
                "remote_id_key": "unknown",
                "location": location,
                "inference_source": "builder_visual_rehydration",
            }
        )

    kinds = {row["kind"] for row in candidates}
    kind = next(iter(kinds)) if len(kinds) == 1 else "visual_resource"
    keys = {row["remote_id_key"] for row in candidates if row["remote_id_key"] != "unknown"}
    key = next(iter(keys)) if len(keys) == 1 else "unknown"
    locations = sorted({row["location"] for row in candidates if row["location"]})
    sources = sorted({row["inference_source"] for row in candidates})
    return {
        **dict(dependency),
        "kind": kind,
        "remote_id_key": key,
        "location": locations[0] if len(locations) == 1 else None,
        "kind_inference": {
            "status": "resolved" if len(kinds) == 1 else "ambiguous",
            "candidate_kinds": sorted(kinds),
            "sources": sources,
        },
    }


def _find_after_dependency(
    before: Mapping[str, Any], after_rows: list[dict[str, Any]]
) -> Mapping[str, Any] | None:
    exact = [
        row
        for row in after_rows
        if row["kind"] == before["kind"] and row["remote_id"] == before["remote_id"]
    ]
    if exact:
        return sorted(exact, key=lambda row: (row["path"], row["location"]))[0]
    by_id = [row for row in after_rows if row["remote_id"] == before["remote_id"]]
    paths = {row["path"] for row in by_id}
    if len(paths) == 1:
        return sorted(by_id, key=lambda row: (row["kind"], row["location"]))[0]
    return None


def decode_live_draft(path: Path, install_dir: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    codec = JianyingDraftCryptoCodec(
        DraftCryptoConfig(jy_install_dir=install_dir, backup=False)
    )
    value = codec.decode(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("decoded live draft root must be object")
    return value


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def baseline_sets(baseline: Mapping[str, Any]) -> tuple[Path, set[str], set[str], bool]:
    cache_root = Path(str(baseline.get("cache_root") or ""))
    if not cache_root.is_dir():
        raise FileNotFoundError(f"cache_root: {cache_root}")

    def paths(key: str) -> set[str]:
        result: set[str] = set()
        for item in baseline.get(key, []) or []:
            raw = item.get("relative_path") if isinstance(item, Mapping) else item
            if not isinstance(raw, str) or not raw:
                continue
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = cache_root / candidate
            result.add(_norm(candidate))
        return result

    present = paths("entries")
    absent = paths("absent_entries")
    if present.intersection(absent):
        raise ValueError("cache baseline marks the same path present and absent")
    return cache_root, present, absent, baseline.get("complete_recursive_snapshot") is True


def _covered(candidate: Path, roots: set[str]) -> bool:
    normalized = _norm(candidate)
    for root in roots:
        try:
            if os.path.commonpath((normalized, root)) == root:
                return True
        except ValueError:
            continue
    return False


def _path_candidates(raw: str, cache_root: Path, kind: str) -> list[Path]:
    candidates: list[Path] = []
    raw_path = Path(raw)
    if raw_path.is_absolute():
        candidates.append(raw_path)
    normalized = raw.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    leaf = parts[-1] if parts else ""
    parent_leaf = parts[-2] if len(parts) > 1 else ""
    search_root = cache_root / "effect"
    if search_root.is_dir():
        if kind != "font" and leaf:
            candidates.extend(
                sorted(
                    (path for path in search_root.rglob(leaf) if path.is_dir()),
                    key=lambda item: str(item).casefold(),
                )
            )
        if kind == "font":
            if leaf and "�" not in leaf:
                candidates.extend(
                    sorted(
                        (path for path in search_root.rglob(leaf) if path.is_file()),
                        key=lambda item: str(item).casefold(),
                    )
                )
            if parent_leaf:
                for directory in sorted(
                    search_root.rglob(parent_leaf), key=lambda item: str(item).casefold()
                ):
                    if directory.is_dir():
                        candidates.extend(directory.glob("*.ttf"))
                        candidates.extend(directory.glob("*.otf"))
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized_candidate = _norm(candidate)
        if normalized_candidate not in seen:
            result.append(candidate)
            seen.add(normalized_candidate)
    return result


@functools.lru_cache(maxsize=None)
def resolve_local(raw: str, cache_root: Path, kind: str) -> Path | None:
    for candidate in _path_candidates(raw, cache_root, kind):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
        if candidate.is_dir() and any(
            path.is_file() and path.stat().st_size > 0 for path in candidate.rglob("*")
        ):
            return candidate
    return None


@functools.lru_cache(maxsize=None)
def inspect_local(target: Path | None) -> dict[str, Any] | None:
    if target is None:
        return None
    root = target if target.is_dir() else target.parent
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda item: item.relative_to(root).as_posix().casefold(),
    )
    rows: list[dict[str, Any]] = []
    total = 0
    nonempty = 0
    tree = hashlib.sha256()
    for path in files:
        size = path.stat().st_size
        digest = sha256_file(path)
        relative = path.relative_to(root).as_posix()
        rows.append({"relative_path": relative, "size_bytes": size, "sha256": digest})
        total += size
        nonempty += int(size > 0)
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(size).encode("ascii"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")
    return {
        "resolved_target": str(target),
        "directory": str(root),
        "recursive_file_count": len(files),
        "nonempty_file_count": nonempty,
        "total_bytes": total,
        "tree_sha256": tree.hexdigest().upper(),
        "files": rows,
    }


def classify_dependency(
    before: Mapping[str, Any],
    after: Mapping[str, Any] | None,
    *,
    cache_root: Path,
    baseline_present: set[str],
    baseline_absent: set[str],
    baseline_complete: bool = False,
) -> dict[str, Any]:
    before_path = str(before.get("path") or "")
    after_path = str((after or {}).get("path") or before_path)
    target = resolve_local(after_path, cache_root, str(before["kind"]))
    local = inspect_local(target)
    path_rewritten = after_path != before_path
    closed = bool(
        local
        and local["recursive_file_count"] > 0
        and local["recursive_file_count"] == local["nonempty_file_count"]
    )
    if not closed:
        status = "unchanged_missing"
    elif target is not None and _covered(target, baseline_present):
        status = "already_existing_before_baseline"
    elif target is not None and _covered(target, baseline_absent):
        status = "newly_materialized_after_baseline"
    elif target is not None and baseline_complete:
        status = "newly_materialized_after_baseline"
    else:
        status = "rewritten_to_existing_local"
    return {
        "kind": before["kind"],
        "remote_id_key": before["remote_id_key"],
        "remote_id": before["remote_id"],
        "before_path": before_path,
        "after_path": after_path,
        "path_rewritten": path_rewritten,
        "status": status,
        "runtime_dependency_closed": closed,
        "local_evidence": local,
        "before_location": before.get("location"),
        "after_location": (after or {}).get("location"),
    }


def audit_post_open(
    manifest: Mapping[str, Any],
    probe: Mapping[str, Any],
    live_draft: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    probe_draft = probe.get("draft")
    if not isinstance(probe_draft, Mapping):
        raise ValueError("probe is missing draft object")
    template_id = str(manifest.get("template_id") or "")
    if not template_id or str(probe.get("manifest", {}).get("template_id") or "") != template_id:
        raise ValueError("manifest/probe template_id mismatch")
    versions = (
        (live_draft.get("platform") or {}).get("app_version"),
        (live_draft.get("last_modified_platform") or {}).get("app_version"),
    )
    if versions != ("8.8.0", "8.8.0"):
        raise ValueError(f"live draft is not double 8.8.0: {versions}")
    cache_root, present, absent, complete = baseline_sets(baseline)
    before_rows = collect_visual_dependencies(probe_draft)
    after_rows = collect_visual_dependencies(live_draft)
    after_by_key = {(row["kind"], row["remote_id"]): row for row in after_rows}
    rows = [
        classify_dependency(
            before,
            after_by_key.get((before["kind"], before["remote_id"])),
            cache_root=cache_root,
            baseline_present=present,
            baseline_absent=absent,
            baseline_complete=complete,
        )
        for before in before_rows
    ]
    counts = Counter(row["status"] for row in rows)
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in sorted({row["kind"] for row in rows}):
        selected = [row for row in rows if row["kind"] == kind]
        by_kind[kind] = {
            "dependency_count": len(selected),
            "closed_count": sum(row["runtime_dependency_closed"] for row in selected),
            "fully_recovered": bool(selected)
            and all(row["runtime_dependency_closed"] for row in selected),
            "status_counts": dict(sorted(Counter(row["status"] for row in selected).items())),
        }
    fully_recovered = bool(rows) and all(row["runtime_dependency_closed"] for row in rows)
    wrapper_rows = manifest.get("source_only_project_metadata") or []
    return {
        "schema": "huoke.jianying-8.8-post-open-runtime-audit.v1",
        "template_id": template_id,
        "policy": {
            "read_only": True,
            "live_draft_written": False,
            "draft_registered": False,
            "jianying_started_or_stopped": False,
            "status_values": sorted(STATUS_VALUES),
            "partial_success_is_not_full_success": True,
        },
        "inputs": {
            "manifest_schema": manifest.get("schema"),
            "probe_source_sha256": (manifest.get("source") or {}).get("sha256"),
            "cache_baseline_schema": baseline.get("schema"),
            "cache_baseline_captured_at": baseline.get("captured_at"),
            "live_versions": {"platform": versions[0], "last_modified_platform": versions[1]},
        },
        "source_wrapper_metadata": {
            "excluded": True,
            "count": len(wrapper_rows),
            "records": wrapper_rows,
        },
        "runtime_visual_dependencies": rows,
        "summary": {
            "dependency_count": len(rows),
            "closed_count": sum(row["runtime_dependency_closed"] for row in rows),
            "unresolved_count": sum(not row["runtime_dependency_closed"] for row in rows),
            "status_counts": dict(sorted(counts.items())),
            "by_kind": by_kind,
            "fully_recovered": fully_recovered,
            "recovery_status": "fully_recovered" if fully_recovered else "partially_recovered",
        },
    }


def audit_batch_post_open(
    manifest: Mapping[str, Any],
    candidate: Mapping[str, Any],
    live_draft: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_draft = candidate.get("draft")
    if not isinstance(candidate_draft, Mapping):
        raise ValueError("batch candidate is missing draft object")
    versions = (
        (live_draft.get("platform") or {}).get("app_version"),
        (live_draft.get("last_modified_platform") or {}).get("app_version"),
    )
    if versions != ("8.8.0", "8.8.0"):
        raise ValueError(f"live draft is not double 8.8.0: {versions}")
    cache_root, present, absent, complete = baseline_sets(baseline)
    phase2 = manifest.get("schema") == "huoke.phase2-engineering-black-screen-batch-candidate.v1"
    after_rows = (
        collect_all_visual_dependencies(live_draft)
        if phase2
        else collect_visual_dependencies(live_draft)
    )
    template_reports: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for template in manifest.get("templates", []) or []:
        if not isinstance(template, Mapping):
            continue
        normalized_dependencies = [
            normalize_manifest_dependency(template, dependency)
            if phase2
            else dict(dependency)
            for dependency in template.get("runtime_visual_dependencies", []) or []
            if isinstance(dependency, Mapping)
        ]
        rows = [
            classify_dependency(
                dependency,
                _find_after_dependency(dependency, after_rows),
                cache_root=cache_root,
                baseline_present=present,
                baseline_absent=absent,
                baseline_complete=complete,
            )
            for dependency in normalized_dependencies
        ]
        all_rows.extend(rows)
        by_kind: dict[str, dict[str, Any]] = {}
        for kind in sorted({row["kind"] for row in rows}):
            selected = [row for row in rows if row["kind"] == kind]
            by_kind[kind] = {
                "dependency_count": len(selected),
                "closed_count": sum(row["runtime_dependency_closed"] for row in selected),
                "fully_recovered": bool(selected) and all(row["runtime_dependency_closed"] for row in selected),
                "status_counts": dict(sorted(Counter(row["status"] for row in selected).items())),
            }
        inventory_complete = template.get("runtime_visual_dependency_inventory_complete") is True
        fully_recovered = all(row["runtime_dependency_closed"] for row in rows) and (
            bool(rows) or inventory_complete
        )
        template_reports.append(
            {
                "template_id": template.get("template_id"),
                "start_us": template.get("start_us"),
                "end_us": template.get("end_us"),
                "runtime_visual_dependencies": rows,
                "source_wrapper_metadata": {
                    "excluded": True,
                    "records": template.get("source_wrapper_metadata") or [],
                },
                "summary": {
                    "dependency_count": len(rows),
                    "closed_count": sum(row["runtime_dependency_closed"] for row in rows),
                    "unresolved_count": sum(not row["runtime_dependency_closed"] for row in rows),
                    "by_kind": by_kind,
                    "no_runtime_visual_dependency": not rows and inventory_complete,
                    "fully_recovered": fully_recovered,
                    "recovery_status": "fully_recovered" if fully_recovered else "partially_recovered",
                },
            }
        )
    all_fully_recovered = bool(template_reports) and all(
        row["summary"]["fully_recovered"] for row in template_reports
    )
    return {
        "schema": "huoke.jianying-8.8-post-open-runtime-batch-audit.v1",
        "batch": manifest.get("batch"),
        "policy": {
            "read_only": True,
            "live_draft_written": False,
            "draft_registered": False,
            "jianying_started_or_stopped": False,
            "partial_success_is_not_full_success": True,
            "source_wrapper_metadata_excluded": True,
        },
        "inputs": {
            "manifest_schema": manifest.get("schema"),
            "cache_baseline_schema": baseline.get("schema"),
            "cache_baseline_captured_at": baseline.get("captured_at"),
            "live_versions": {"platform": versions[0], "last_modified_platform": versions[1]},
        },
        "templates": template_reports,
        "summary": {
            "template_count": len(template_reports),
            "fully_recovered_template_count": sum(row["summary"]["fully_recovered"] for row in template_reports),
            "partially_recovered_template_count": sum(not row["summary"]["fully_recovered"] for row in template_reports),
            "dependency_reference_count": len(all_rows),
            "closed_dependency_reference_count": sum(row["runtime_dependency_closed"] for row in all_rows),
            "status_counts": dict(sorted(Counter(row["status"] for row in all_rows).items())),
            "all_templates_fully_recovered": all_fully_recovered,
            "batch_status": "fully_recovered" if all_fully_recovered else "partially_recovered",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读审计剪映8.8前台打开后的视觉资源恢复结果")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--live-draft", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--cache-baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = read_json(args.manifest)
    probe = read_json(args.probe)
    baseline = read_json(args.cache_baseline)
    live = decode_live_draft(args.live_draft, args.install_dir)
    if manifest.get("schema") in {
        "huoke.phase1-resource-recovery-batch-candidate.v1",
        "huoke.phase2-engineering-black-screen-batch-candidate.v1",
    }:
        report = audit_batch_post_open(manifest, probe, live, baseline)
    else:
        report = audit_post_open(manifest, probe, live, baseline)
    write_json(args.output, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
