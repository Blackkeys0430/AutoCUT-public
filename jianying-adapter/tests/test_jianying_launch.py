from __future__ import annotations

from pathlib import Path

import pytest

from jianying_adapter.jianying_launch import (
    exact_process_identifier,
    verify_exact_window_app,
)


def test_exact_launch_contract_requires_process_identifier(tmp_path: Path) -> None:
    expected = tmp_path / "8.8.0.13328" / "JianyingPro.exe"
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"exe")
    identifier = exact_process_identifier(expected)
    assert identifier.startswith("process:")
    assert identifier.endswith("8.8.0.13328\\JianyingPro.exe")
    assert verify_exact_window_app(identifier, expected)["ok"] is True


def test_wrong_or_plain_window_app_is_rejected(tmp_path: Path) -> None:
    expected = tmp_path / "8.8.0.13328" / "JianyingPro.exe"
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"exe")
    wrong = tmp_path / "11.4.0.14403" / "JianyingPro.exe"
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(b"exe")
    with pytest.raises(ValueError, match="8.8"):
        exact_process_identifier(wrong)
    assert verify_exact_window_app(f"process:{wrong}", expected)["ok"] is False
    assert verify_exact_window_app(str(expected), expected)["ok"] is False
