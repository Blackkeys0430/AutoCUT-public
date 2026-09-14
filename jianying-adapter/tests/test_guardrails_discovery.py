from __future__ import annotations

from pathlib import Path

import pytest

from jianying_adapter.discovery import discover, probe_installation
from jianying_adapter.guardrails import (
    GuardrailError,
    ensure_no_overwrite,
    ensure_process_stopped,
    ensure_safe_write,
    ensure_unlocked,
)


def test_process_gate_is_injectable():
    with pytest.raises(GuardrailError, match="running"):
        ensure_process_stopped(lambda _name: [101, 202])
    ensure_process_stopped(lambda _name: [])


def test_lock_and_overwrite_gates(tmp_path: Path):
    root = tmp_path / "root"
    target = root / "SYNTH-TARGET"
    target.mkdir(parents=True)
    (target / ".locked").touch()
    with pytest.raises(GuardrailError, match="lock"):
        ensure_unlocked(target)
    (target / ".locked").unlink()
    with pytest.raises(GuardrailError, match="overwrite"):
        ensure_no_overwrite(target)
    ensure_safe_write(target, allowed_root=root, inspector=lambda _name: [], require_absent=False)


def test_discovery_version_and_dll(tmp_path: Path):
    root = tmp_path / "JianyingPro"
    old = root / "11.2.0.1"
    current = root / "11.3.0.2"
    old.mkdir(parents=True)
    current.mkdir()
    (old / "videoeditor.dll").write_bytes(b"SYNTH-OLD")
    (current / "videoeditor.dll").write_bytes(b"SYNTH-CURRENT")
    (root / "JianyingPro.exe").write_bytes(b"SYNTH-EXE")
    installation = probe_installation(root)
    assert installation.version == "11.3.0.2"
    report = discover(extra_roots=[root], registry_reader=lambda: [], local_app_data=tmp_path)
    assert report.installations[0].videoeditor_dll == current / "videoeditor.dll"
    assert report.draft_root.name == "com.lveditor.draft"

