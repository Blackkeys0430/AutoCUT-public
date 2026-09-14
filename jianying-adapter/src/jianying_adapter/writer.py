# Copyright (c) 2026 Blackkeys0430 — AutoCUT original project code.
# Origin: https://github.com/Blackkeys0430/AutoCUT-public
# SPDX-License-Identifier: LicenseRef-AutoCUT-Personal-Use-1.0
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .backup import create_complete_backup
from .codec import DraftCodec
from .guardrails import ProcessInspector, ensure_safe_write, windows_tasklist_inspector
from .hashing import object_sha256, tree_fingerprint
from .subtitles import build_bilingual_subtitles, load_subtitle_plan


ReplaceBytes = Callable[[Path, bytes], None]


@dataclass(frozen=True)
class ContentMirrors:
    main_timeline_id: str
    # Backwards-compatible name: ``paths`` now means live content paths only.
    paths: tuple[Path, ...]
    history_paths: tuple[Path, ...] = ()

    @property
    def live_paths(self) -> tuple[Path, ...]:
        """Content files that are allowed to be compared and written."""
        return self.paths

    @property
    def backup_paths(self) -> tuple[Path, ...]:
        """Historical ``.bak`` files, which are never written by the writer."""
        return self.history_paths


def discover_content_mirrors(draft: str | Path) -> ContentMirrors:
    root = Path(draft).resolve()
    project_path = root / "Timelines" / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8-sig"))
    main_id = str(project.get("main_timeline_id") or "")
    if not main_id:
        raise ValueError("Timelines/project.json has no main_timeline_id")
    main_root = root / "Timelines" / main_id
    required = (root / "draft_content.json", main_root / "draft_content.json")
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(f"required content mirror is missing: {path}")
    live_candidates = (
        required[0],
        root / "template-2.tmp",
        required[1],
        main_root / "template-2.tmp",
    )
    history_candidates = (
        root / "draft_content.json.bak",
        main_root / "draft_content.json.bak",
    )
    return ContentMirrors(
        main_id,
        tuple(path for path in live_candidates if path.is_file()),
        tuple(path for path in history_candidates if path.is_file()),
    )


def _atomic_replace(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".jianying_adapter_tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_mirrors(draft: Path, backup: Path, mirrors: tuple[Path, ...], codec: DraftCodec, before: dict[str, Any]) -> None:
    for mirror in mirrors:
        relative = mirror.relative_to(draft)
        source = backup / relative
        if not source.is_file():
            raise RuntimeError(f"backup mirror is missing during recovery: {source}")
        _atomic_replace(mirror, source.read_bytes())
    for mirror in mirrors:
        if codec.read(mirror).value != before:
            raise RuntimeError(f"restored mirror does not match original: {mirror}")


def apply_subtitle_plan(
    draft: str | Path,
    plan_path: str | Path,
    backup: str | Path,
    codec: DraftCodec,
    *,
    allowed_root: str | Path,
    inspector: ProcessInspector = windows_tasklist_inspector,
    replace_bytes: ReplaceBytes = _atomic_replace,
) -> dict[str, Any]:
    draft_path = ensure_safe_write(draft, allowed_root=allowed_root, inspector=inspector)
    backup_path = Path(backup).resolve()
    if backup_path == draft_path or draft_path in backup_path.parents:
        raise ValueError("backup must be outside the draft directory")
    mirror_set = discover_content_mirrors(draft_path)
    mirrors = mirror_set.live_paths
    decoded = [codec.read(path) for path in mirrors]
    before = decoded[0].value
    if str(before.get("id") or "") != mirror_set.main_timeline_id:
        raise RuntimeError("draft content id does not match project main_timeline_id")
    if any(item.value != before for item in decoded[1:]):
        raise RuntimeError("content mirrors are not deep-equal before apply")
    source_tree = tree_fingerprint(draft_path)
    plan = load_subtitle_plan(plan_path)
    patch = build_bilingual_subtitles(before, plan)
    candidate = patch.content
    create_complete_backup(draft_path, backup_path)
    if tree_fingerprint(draft_path) != source_tree:
        raise RuntimeError("draft changed while backup was being created")

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jianying_adapter_apply_", dir=backup_path.parent) as temporary:
        encoded_path = Path(temporary) / "draft_content.json"
        codec.write(encoded_path, candidate)
        if codec.read(encoded_path).value != candidate:
            raise RuntimeError("candidate codec roundtrip failed")
        encoded = encoded_path.read_bytes()

    mutated = False
    try:
        if tree_fingerprint(draft_path) != source_tree:
            raise RuntimeError("draft changed after backup and before apply")
        ensure_safe_write(draft_path, allowed_root=allowed_root, inspector=inspector)
        mutated = True
        for mirror in mirrors:
            replace_bytes(mirror, encoded)
        for mirror in mirrors:
            if codec.read(mirror).value != candidate:
                raise RuntimeError(f"content mirror readback failed: {mirror}")
    except Exception as exc:
        if mutated:
            try:
                _restore_mirrors(draft_path, backup_path, mirrors, codec, before)
            except Exception as restore_error:
                raise RuntimeError(f"apply failed and automatic restore also failed: {restore_error}") from exc
        raise

    return {
        "ok": True,
        "draft": str(draft_path),
        "backup": str(backup_path),
        "mirrors": [str(path.relative_to(draft_path)).replace("\\", "/") for path in mirrors],
        "history_backups": [
            str(path.relative_to(draft_path)).replace("\\", "/")
            for path in mirror_set.backup_paths
        ],
        "before_object_sha256": object_sha256(before),
        "after_object_sha256": object_sha256(candidate),
        "cards": patch.card_count,
        "restored": False,
    }


__all__ = ["ContentMirrors", "apply_subtitle_plan", "discover_content_mirrors"]
