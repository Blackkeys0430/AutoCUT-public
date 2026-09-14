from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path
from typing import Any


class _VSFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


def windows_file_version(path: Path) -> str | None:
    """Read the PE version resource without launching the executable."""

    if os.name != "nt":
        return None
    version = ctypes.windll.version
    ignored = ctypes.c_uint32()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    value = ctypes.c_void_p()
    value_size = ctypes.c_uint32()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(value_size)):
        return None
    fixed = ctypes.cast(value, ctypes.POINTER(_VSFixedFileInfo)).contents
    parts = (
        fixed.dwFileVersionMS >> 16,
        fixed.dwFileVersionMS & 0xFFFF,
        fixed.dwFileVersionLS >> 16,
        fixed.dwFileVersionLS & 0xFFFF,
    )
    return ".".join(str(part) for part in parts)


def exact_process_identifier(expected_exe: Path) -> str:
    executable = expected_exe.resolve(strict=False)
    if executable.name.casefold() != "jianyingpro.exe":
        raise ValueError(f"预期剪映可执行文件必须名为 JianyingPro.exe: {executable}")
    if not executable.is_file():
        raise FileNotFoundError(f"剪映8.8可执行文件不存在: {executable}")
    version = windows_file_version(executable)
    if version is not None:
        if not version.startswith("8.8."):
            raise ValueError(f"剪映可执行文件版本不是8.8: {version} ({executable})")
    elif not re.search(r"(?<!\d)8\.8(?:\.0)?(?:\.\d+)?(?!\d)", str(executable)):
        # Test fixtures may not be PE files.  A real production executable has
        # a version resource; when that is unavailable, fail unless the path is
        # still explicitly versioned as 8.8.
        raise ValueError(f"无法读取版本且路径未显式绑定剪映8.8: {executable}")
    return f"process:{executable}"


def verify_exact_window_app(window_app: Any, expected_exe: Path) -> dict[str, Any]:
    expected = exact_process_identifier(expected_exe)
    actual = str(window_app or "").strip()
    ok = actual.casefold() == expected.casefold()
    return {
        "ok": ok,
        "expected_window_app": expected,
        "actual_window_app": actual,
        "error": None if ok else "剪映窗口不是精确8.8 process目标，禁止继续点击",
    }


__all__ = ["exact_process_identifier", "verify_exact_window_app", "windows_file_version"]
