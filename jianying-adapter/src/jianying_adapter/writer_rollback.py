from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class WriterRollbackError(RuntimeError):
    pass


def restore_file_atomic(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".rollback", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def rollback_new_draft(
    *,
    logical_root: Path,
    draft_name: str,
    root_meta_backup: Path,
    quarantine_root: Path,
    failure: BaseException,
    physical_root: Path | None = None,
    _restore_root_meta: Callable[[Path, Path], None] = restore_file_atomic,
) -> dict[str, Any]:
    """Recoverably remove one newly-created failed Writer draft and registration.

    This is only for a name that the Writer proved absent immediately before it
    started.  It never deletes the folder: the failed draft is moved to a
    quarantine below the caller-provided backup root.
    """

    if not draft_name or Path(draft_name).name != draft_name or draft_name in {".", ".."}:
        raise WriterRollbackError("draft_name must be one exact folder name")
    logical_root = Path(os.path.abspath(logical_root))
    storage_root = logical_root
    if physical_root is not None:
        storage_root = Path(physical_root).resolve(strict=True)
        if not logical_root.is_dir() or not os.path.samefile(logical_root, storage_root):
            raise WriterRollbackError("logical and physical rollback roots must be the same directory")
    draft_path = storage_root / draft_name
    if draft_path.parent != storage_root:
        raise WriterRollbackError("draft path escaped storage root")
    root_meta_path = storage_root / "root_meta_info.json"
    if not root_meta_backup.is_file():
        raise WriterRollbackError(f"pre-write root_meta backup is missing: {root_meta_backup}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    operation_root = quarantine_root / f"{stamp}_{draft_name}"
    operation_root.mkdir(parents=True, exist_ok=False)
    quarantined_draft = operation_root / "draft"
    current_root_meta = operation_root / "root_meta_info.failed_write.json"
    if root_meta_path.is_file():
        shutil.copy2(root_meta_path, current_root_meta)

    moved = False
    root_restored = False
    try:
        if draft_path.exists():
            shutil.move(str(draft_path), str(quarantined_draft))
            moved = True
        _restore_root_meta(root_meta_backup, root_meta_path)
        root_restored = True
        return {
            "schema": "jianying-adapter.failed-writer-rollback.v1",
            "status": "rolled_back",
            "draft_name": draft_name,
            "logical_root": str(logical_root),
            "physical_root": str(storage_root),
            "failed_draft_quarantine": str(quarantined_draft) if moved else None,
            "root_meta_restored_from": str(root_meta_backup),
            "failed_root_meta_snapshot": str(current_root_meta) if current_root_meta.is_file() else None,
            "failure": f"{type(failure).__name__}: {failure}",
            "permanent_delete_performed": False,
        }
    except Exception as rollback_error:
        if root_restored and current_root_meta.is_file():
            restore_file_atomic(current_root_meta, root_meta_path)
        if moved and quarantined_draft.exists() and not draft_path.exists():
            shutil.move(str(quarantined_draft), str(draft_path))
        raise WriterRollbackError(
            f"failed Writer rollback did not complete: {type(rollback_error).__name__}: {rollback_error}"
        ) from rollback_error


__all__ = ["WriterRollbackError", "restore_file_atomic", "rollback_new_draft"]
