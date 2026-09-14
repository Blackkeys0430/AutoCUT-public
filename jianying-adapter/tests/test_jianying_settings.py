from pathlib import Path

import pytest

from jianying_adapter.guardrails import GuardrailError
from jianying_adapter.jianying_settings import (
    JianyingSettingError,
    ensure_real_quit_enabled,
    read_quit_behavior,
    repair_real_quit,
)


def make_user_data(tmp_path: Path, value: str = "true") -> Path:
    user_data = tmp_path / "User Data"
    config = user_data / "Config"
    config.mkdir(parents=True)
    (config / "globalSetting").write_bytes(
        f"other=true\r\nminimize_to_tray_when_app_quit={value}\r\n".encode()
    )
    return user_data


def test_repair_real_quit_is_backed_up_and_verified(tmp_path: Path) -> None:
    user_data = make_user_data(tmp_path)
    result = repair_real_quit(
        user_data,
        tmp_path / "backups",
        inspector=lambda _: (),
    )

    assert result["changed"] is True
    assert Path(str(result["backup"])).read_bytes().endswith(b"=true\r\n")
    assert read_quit_behavior(user_data).real_quit_enabled is True
    assert ensure_real_quit_enabled(user_data).minimize_to_tray is False


def test_repair_refuses_while_jianying_is_running(tmp_path: Path) -> None:
    user_data = make_user_data(tmp_path)
    with pytest.raises(GuardrailError, match="running"):
        repair_real_quit(
            user_data,
            tmp_path / "backups",
            inspector=lambda _: (1234,),
        )


def test_missing_or_duplicate_setting_fails_closed(tmp_path: Path) -> None:
    user_data = make_user_data(tmp_path)
    path = user_data / "Config" / "globalSetting"
    path.write_text("other=true\n", encoding="utf-8")
    with pytest.raises(JianyingSettingError, match="exactly one"):
        read_quit_behavior(user_data)

