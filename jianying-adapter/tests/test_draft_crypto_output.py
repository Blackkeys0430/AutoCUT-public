import sys
from pathlib import Path

import pytest

VENDOR = Path(__file__).parents[1] / "vendor" / "pyJianYingDraft-source"
LOCAL_DEPS = VENDOR.parent / "python-deps"
sys.path.insert(0, str(LOCAL_DEPS))
sys.path.insert(0, str(VENDOR))

from pyJianYingDraft import draft_crypto


@pytest.mark.parametrize("returncode", [0, 1])
def test_isolated_crypto_preserves_chinese_diagnostics(tmp_path, monkeypatch, returncode):
    helper = tmp_path / "isolated_helper.py"
    helper.write_text(
        'import sys\nprint("草稿写入成功")\nprint("路径校验说明", file=sys.stderr)\n'
        f'sys.exit({returncode})\n', encoding="utf-8",
    )
    monkeypatch.setattr(draft_crypto, "__file__", str(helper))
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")
    if returncode:
        with pytest.raises(draft_crypto.DraftCryptoProcessError) as caught:
            draft_crypto._run_isolated([], jy_install_dir=None, timeout=5)
        result = caught.value
    else:
        result = draft_crypto._run_isolated([], jy_install_dir=None, timeout=5)
    assert result.returncode == returncode
    assert result.stdout.strip() == "草稿写入成功"
    assert result.stderr.strip() == "路径校验说明"
