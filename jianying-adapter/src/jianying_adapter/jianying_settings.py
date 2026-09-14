from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .guardrails import GuardrailError, ensure_process_stopped


QUIT_SETTING = b"minimize_to_tray_when_app_quit"


class JianyingSettingError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuitBehaviorStatus:
    config_path: str
    minimize_to_tray: bool

    @property
    def real_quit_enabled(self) -> bool:
        """Legacy name: only reports that the known close-to-tray flag is disabled.

        Jianying 8.8 can still keep a hidden process tree alive when this is true.
        Callers must use the process/lock guardrails as the authoritative gate.
        """
        return not self.minimize_to_tray

    @property
    def known_close_to_tray_setting_disabled(self) -> bool:
        return not self.minimize_to_tray


def global_setting_path(user_data: str | Path) -> Path:
    return Path(user_data) / "Config" / "globalSetting"


def _setting_lines(payload: bytes) -> list[bytes]:
    return [
        line.strip()
        for line in payload.splitlines()
        if line.strip().startswith(QUIT_SETTING + b"=")
    ]


def read_quit_behavior(user_data: str | Path) -> QuitBehaviorStatus:
    path = global_setting_path(user_data)
    if not path.is_file():
        raise FileNotFoundError(path)
    lines = _setting_lines(path.read_bytes())
    if len(lines) != 1:
        raise JianyingSettingError(
            f"Expected exactly one {QUIT_SETTING.decode()} entry in {path}, found {len(lines)}"
        )
    value = lines[0].split(b"=", 1)[1].strip().lower()
    if value not in {b"true", b"false"}:
        raise JianyingSettingError(f"Unsupported quit setting value in {path}: {value!r}")
    return QuitBehaviorStatus(str(path.resolve()), minimize_to_tray=value == b"true")


def ensure_real_quit_enabled(user_data: str | Path) -> QuitBehaviorStatus:
    """Check the known tray preference; this does not prove Jianying fully exited."""
    status = read_quit_behavior(user_data)
    if not status.real_quit_enabled:
        raise JianyingSettingError(
            "剪映已知的“关闭窗口时最小化到托盘”设置仍开启；拒绝进入 Writer，"
            "请先运行 repair_jianying_quit_behavior.py。"
        )
    return status


def repair_real_quit(
    user_data: str | Path,
    backup_dir: str | Path,
    *,
    inspector: Callable[[str], Iterable[int]] | None = None,
) -> dict[str, object]:
    if inspector is None:
        ensure_process_stopped()
    else:
        ensure_process_stopped(inspector)

    status = read_quit_behavior(user_data)
    path = Path(status.config_path)
    original = path.read_bytes()
    if status.real_quit_enabled:
        return {
            "changed": False,
            "config": str(path),
            "backup": None,
            "minimize_to_tray_when_app_quit": False,
            "known_close_to_tray_setting_disabled": True,
            "full_process_exit_proven": False,
        }

    old = QUIT_SETTING + b"=true"
    new = QUIT_SETTING + b"=false"
    if original.count(old) != 1:
        raise JianyingSettingError(f"Refusing ambiguous replacement in {path}")

    backup_root = Path(backup_dir)
    backup_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(original).hexdigest()[:12]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_root / f"globalSetting.{stamp}.{digest}.bak"
    backup.write_bytes(original)

    updated = original.replace(old, new, 1)
    temporary = path.with_name(path.name + ".quit-fix.tmp")
    try:
        temporary.write_bytes(updated)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()

    verified = read_quit_behavior(user_data)
    if not verified.real_quit_enabled:
        raise JianyingSettingError(f"Quit behavior verification failed: {path}")
    return {
        "changed": True,
        "config": str(path),
        "backup": str(backup.resolve()),
        "minimize_to_tray_when_app_quit": False,
        "known_close_to_tray_setting_disabled": True,
        "full_process_exit_proven": False,
    }
