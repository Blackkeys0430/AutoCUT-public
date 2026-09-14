from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol


class GuardrailError(RuntimeError):
    pass


class ProcessInspector(Protocol):
    def __call__(self, image_name: str) -> Iterable[int]: ...


def windows_tasklist_inspector(image_name: str) -> tuple[int, ...]:
    if os.name != "nt":
        return ()
    completed = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        if image_name.casefold() not in line.casefold():
            continue
        parts = [part.strip().strip('"') for part in line.split(",")]
        if len(parts) > 1 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return tuple(pids)


@dataclass(frozen=True)
class GuardStatus:
    process_ids: tuple[int, ...]
    locked: bool
    target: str
    target_exists: bool

    @property
    def safe(self) -> bool:
        return not self.process_ids and not self.locked


def process_ids(
    inspector: ProcessInspector = windows_tasklist_inspector,
    image_name: str = "JianyingPro.exe",
) -> tuple[int, ...]:
    return tuple(int(pid) for pid in inspector(image_name))


def lock_path(target: str | Path) -> Path:
    return Path(target) / ".locked"


def inspect_target(
    target: str | Path,
    inspector: ProcessInspector = windows_tasklist_inspector,
) -> GuardStatus:
    path = Path(target).resolve()
    return GuardStatus(
        process_ids=process_ids(inspector),
        locked=lock_path(path).exists(),
        target=str(path),
        target_exists=path.exists(),
    )


def ensure_process_stopped(inspector: ProcessInspector = windows_tasklist_inspector) -> None:
    pids = process_ids(inspector)
    if pids:
        raise GuardrailError(f"JianyingPro.exe is running: {pids}")


def ensure_unlocked(target: str | Path) -> None:
    marker = lock_path(target)
    if marker.exists():
        raise GuardrailError(f"Target contains lock marker: {marker}")


def ensure_scoped_target(target: str | Path, allowed_root: str | Path) -> Path:
    resolved = Path(target).resolve()
    root = Path(allowed_root).resolve()
    if resolved == root or root not in resolved.parents:
        raise GuardrailError(f"Target must be a child of allowed root: {root}")
    return resolved


def ensure_no_overwrite(target: str | Path) -> None:
    if Path(target).exists():
        raise GuardrailError(f"Refusing overwrite of existing target: {Path(target).resolve()}")


def ensure_safe_write(
    target: str | Path,
    *,
    allowed_root: str | Path,
    inspector: ProcessInspector = windows_tasklist_inspector,
    require_absent: bool = False,
) -> Path:
    ensure_process_stopped(inspector)
    path = ensure_scoped_target(target, allowed_root)
    ensure_unlocked(path)
    if require_absent:
        ensure_no_overwrite(path)
    return path

