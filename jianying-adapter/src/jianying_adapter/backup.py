from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .hashing import TreeFingerprint, tree_fingerprint


def _write_json_exclusive(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


@dataclass(frozen=True)
class RestorePlan:
    source_backup: str
    restore_target: str
    strategy: str = "copy-overwrite-existing-files"
    delete_extraneous: bool = False
    requires_process_stopped: bool = True
    requires_unlocked: bool = True


@dataclass(frozen=True)
class BackupResult:
    source: str
    backup: str
    manifest_path: str
    restore_plan_path: str
    source_fingerprint: TreeFingerprint
    backup_fingerprint: TreeFingerprint


def create_complete_backup(
    source: str | Path,
    backup: str | Path,
) -> BackupResult:
    source_path = Path(source).resolve()
    backup_path = Path(backup).resolve()
    manifest_path = backup_path.parent / f"{backup_path.name}.manifest.json"
    restore_path = backup_path.parent / f"{backup_path.name}.restore-plan.json"
    if not source_path.is_dir():
        raise NotADirectoryError(source_path)
    for output_path in (backup_path, manifest_path, restore_path):
        if output_path.exists():
            raise FileExistsError(f"Refusing overwrite of backup output: {output_path}")
    source_hash = tree_fingerprint(source_path)
    shutil.copytree(source_path, backup_path)
    backup_hash = tree_fingerprint(backup_path)
    if source_hash != backup_hash:
        raise RuntimeError("Backup tree differs from source tree")

    manifest: dict[str, Any] = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": str(source_path),
        "backup": str(backup_path),
        "fingerprint": source_hash.to_dict(),
    }
    plan = RestorePlan(str(backup_path), str(source_path))
    _write_json_exclusive(manifest_path, manifest)
    _write_json_exclusive(restore_path, asdict(plan))
    return BackupResult(
        source=str(source_path),
        backup=str(backup_path),
        manifest_path=str(manifest_path),
        restore_plan_path=str(restore_path),
        source_fingerprint=source_hash,
        backup_fingerprint=backup_hash,
    )
