from __future__ import annotations

from pathlib import Path

import pytest

from jianying_adapter.cli import build_parser
from jianying_adapter.codec import JsonCodec, LazyJianying11DllProvider, roundtrip


def test_fake_codec_roundtrip(tmp_path: Path) -> None:
    existing = tmp_path / "roundtrip.json"
    existing.write_bytes(b"DO NOT OVERWRITE")
    value = {"id": "SYNTH-TIMELINE", "render_index_track_mode_on": {"keep": True}}
    result = roundtrip(JsonCodec(), value, tmp_path)
    assert result.equal and result.decoded == value
    assert existing.read_bytes() == b"DO NOT OVERWRITE"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["roundtrip.json"]


def test_roundtrip_cleans_temporary_file_after_failure(tmp_path: Path) -> None:
    class FailingCodec(JsonCodec):
        def write(self, path, value):
            super().write(path, value)
            raise RuntimeError("synthetic codec failure")

    with pytest.raises(RuntimeError, match="synthetic codec failure"):
        roundtrip(FailingCodec(), {"id": "SYNTH-TIMELINE"}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_real_provider_is_lazy(tmp_path: Path) -> None:
    provider = LazyJianying11DllProvider(tmp_path / "missing-writer", tmp_path / "missing-install")
    assert provider.loaded is False


def test_cli_exposes_single_apply_path() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    for command in (
        "probe",
        "audit",
        "backup",
        "apply",
        "validate-project-state",
        "validate-candidate-plan",
    ):
        assert command in help_text
    assert "dry-run-text" not in help_text
    args = parser.parse_args(
        ["apply", "draft", "plan.json", "backup", "--allowed-root", "root"]
    )
    assert args.command == "apply"
    assert args.draft == Path("draft")
    assert args.plan == Path("plan.json")
    assert args.backup == Path("backup")
