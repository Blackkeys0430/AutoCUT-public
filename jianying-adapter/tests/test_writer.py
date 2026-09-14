from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from jianying_adapter.codec import JsonCodec
from jianying_adapter.guardrails import GuardrailError
from jianying_adapter.writer import apply_subtitle_plan, discover_content_mirrors
from conftest import add_subtitle_templates


def _prepare(draft: Path) -> dict:
    root = draft / "draft_content.json"
    value = add_subtitle_templates(json.loads(root.read_text(encoding="utf-8")))
    root.write_text(json.dumps(value), encoding="utf-8")
    timeline = draft / "Timelines" / value["id"] / "draft_content.json"
    timeline.write_text(json.dumps(copy.deepcopy(value)), encoding="utf-8")
    return value


def _plan(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "zh_template_track": "ZH_TEMPLATE",
                "en_template_track": "EN_TEMPLATE",
                "cards": [
                    {"start_us": 0, "duration_us": 1_000_000, "zh": "中文", "en": "English"},
                    {"start_us": 1_200_000, "duration_us": 800_000, "zh": "第二句", "en": "Second"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _prepare_with_history(draft: Path) -> tuple[dict, tuple[Path, ...], tuple[Path, ...]]:
    value = _prepare(draft)
    main = draft / "Timelines" / value["id"]
    live = (
        draft / "draft_content.json",
        draft / "template-2.tmp",
        main / "draft_content.json",
        main / "template-2.tmp",
    )
    for path in live[1:]:
        path.write_text(json.dumps(copy.deepcopy(value)), encoding="utf-8")

    history = (draft / "draft_content.json.bak", main / "draft_content.json.bak")
    history[0].write_bytes(b"root historical content")
    history[1].write_bytes(b"timeline historical content")
    return value, live, history


def test_apply_backs_up_once_and_synchronizes_both_mirrors(synthetic_draft: Path, tmp_path: Path) -> None:
    before = _prepare(synthetic_draft)
    backup = tmp_path / "backup"
    result = apply_subtitle_plan(
        synthetic_draft,
        _plan(tmp_path / "plan.json"),
        backup,
        JsonCodec(),
        allowed_root=tmp_path,
        inspector=lambda _name: [],
    )
    root = json.loads((synthetic_draft / "draft_content.json").read_text(encoding="utf-8"))
    mirror = json.loads((synthetic_draft / "Timelines" / root["id"] / "draft_content.json").read_text(encoding="utf-8"))
    assert result["ok"] is True and result["cards"] == 2
    assert root == mirror
    assert len([track for track in root["tracks"] if track["type"] == "text"]) == 2
    assert json.loads((backup / "draft_content.json").read_text(encoding="utf-8")) == before
    assert Path(str(backup) + ".manifest.json").is_file()


def test_apply_writes_only_four_live_paths_and_preserves_history_backups(
    synthetic_draft: Path,
    tmp_path: Path,
) -> None:
    before, live, history = _prepare_with_history(synthetic_draft)
    history_bytes = tuple(path.read_bytes() for path in history)
    backup = tmp_path / "backup"

    mirror_set = discover_content_mirrors(synthetic_draft)
    assert mirror_set.live_paths == live
    assert mirror_set.backup_paths == history
    assert not set(mirror_set.paths).intersection(history)

    result = apply_subtitle_plan(
        synthetic_draft,
        _plan(tmp_path / "plan.json"),
        backup,
        JsonCodec(),
        allowed_root=tmp_path,
        inspector=lambda _name: [],
    )

    candidate = json.loads((synthetic_draft / "draft_content.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert len(result["mirrors"]) == 4
    assert len(result["history_backups"]) == 2
    for path in live:
        assert json.loads(path.read_text(encoding="utf-8")) == candidate
    for path, expected in zip(history, history_bytes):
        assert path.read_bytes() == expected
        assert (backup / path.relative_to(synthetic_draft)).read_bytes() == expected
    assert json.loads((backup / "draft_content.json").read_text(encoding="utf-8")) == before


def test_template_tmp_mismatch_is_rejected_before_any_write(
    synthetic_draft: Path,
    tmp_path: Path,
) -> None:
    _before, live, history = _prepare_with_history(synthetic_draft)
    before_bytes = {path: path.read_bytes() for path in (*live, *history)}
    (synthetic_draft / "Timelines" / _before["id"] / "template-2.tmp").write_text(
        json.dumps({"id": "MISMATCH"}),
        encoding="utf-8",
    )
    before_bytes[live[-1]] = live[-1].read_bytes()
    backup = tmp_path / "backup"

    with pytest.raises(RuntimeError, match="deep-equal"):
        apply_subtitle_plan(
            synthetic_draft,
            _plan(tmp_path / "plan.json"),
            backup,
            JsonCodec(),
            allowed_root=tmp_path,
            inspector=lambda _name: [],
        )

    assert not backup.exists()
    for path, expected in before_bytes.items():
        assert path.read_bytes() == expected


def test_write_gates_reject_lock_process_and_out_of_scope(synthetic_draft: Path, tmp_path: Path) -> None:
    _prepare(synthetic_draft)
    plan = _plan(tmp_path / "plan.json")
    (synthetic_draft / ".locked").touch()
    with pytest.raises(GuardrailError, match="lock"):
        apply_subtitle_plan(synthetic_draft, plan, tmp_path / "b1", JsonCodec(), allowed_root=tmp_path, inspector=lambda _name: [])
    (synthetic_draft / ".locked").unlink()
    with pytest.raises(GuardrailError, match="running"):
        apply_subtitle_plan(synthetic_draft, plan, tmp_path / "b2", JsonCodec(), allowed_root=tmp_path, inspector=lambda _name: [7])
    with pytest.raises(GuardrailError, match="child"):
        apply_subtitle_plan(synthetic_draft, plan, tmp_path / "b3", JsonCodec(), allowed_root=tmp_path / "elsewhere", inspector=lambda _name: [])
    with pytest.raises(ValueError, match="outside"):
        apply_subtitle_plan(synthetic_draft, plan, synthetic_draft / "backup", JsonCodec(), allowed_root=tmp_path, inspector=lambda _name: [])


def test_second_mirror_failure_restores_original(synthetic_draft: Path, tmp_path: Path) -> None:
    before = _prepare(synthetic_draft)
    calls = 0

    def fail_second(path: Path, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic second mirror failure")
        path.write_bytes(data)

    with pytest.raises(OSError, match="second mirror"):
        apply_subtitle_plan(
            synthetic_draft,
            _plan(tmp_path / "plan.json"),
            tmp_path / "backup",
            JsonCodec(),
            allowed_root=tmp_path,
            inspector=lambda _name: [],
            replace_bytes=fail_second,
        )
    for path in (
        synthetic_draft / "draft_content.json",
        synthetic_draft / "Timelines" / before["id"] / "draft_content.json",
    ):
        assert json.loads(path.read_text(encoding="utf-8")) == before
