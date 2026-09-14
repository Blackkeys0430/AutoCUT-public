from __future__ import annotations

import json
from pathlib import Path

import pytest
import psutil
import jianying_adapter.jianying_environment as environment

from jianying_adapter.jianying_environment import (
    audit_double_8_8_reference,
    collect_jianying_processes,
    inspect_jianying_environment,
)


def proof() -> dict:
    return {
        "node_repl_bridge_doctor": {
            "ok": True,
            "mode": "project-drift",
            "issues": [],
            "installer_preview_exit_code": 0,
            "installer_preview_changed": False,
            "source_runtime_hash": "runtime-88",
            "project_runtime_hash": "runtime-88",
            "source_plugin_version": "26.901.22334",
            "project_plugin_version": "26.901.22334",
        },
        "runtime_observations": {
            "cua_get_state_called": True,
            "sky_window_enumeration_called": True,
            "observed_at": "2026-09-04T21:00:00+08:00",
            "agent_attested": True,
        },
    }


def profile_root(tmp_path: Path, kind: str = "8.8-tests") -> Path:
    root = tmp_path / "profiles" / kind / "com.lveditor.draft"
    root.mkdir(parents=True)
    (root / "root_meta_info.json").write_text(
        json.dumps({"all_draft_store": []}), encoding="utf-8"
    )
    return root


def expected_exe(tmp_path: Path) -> Path:
    path = tmp_path / "JianyingPro-8.8" / "JianyingPro.exe"
    path.parent.mkdir()
    path.write_bytes(b"8.8")
    return path


def failed_codes(report: dict) -> set[str]:
    return {row["code"] for row in report["checks"] if not row["ok"]}


def test_missing_expected_8_8_exe_is_rejected(tmp_path: Path) -> None:
    root = profile_root(tmp_path)
    report = inspect_jianying_environment(
        stage="before_launch",
        profile_kind="test",
        expected_exe=tmp_path / "missing" / "JianyingPro.exe",
        logical_root=root,
        physical_root=root,
        profiles_root=tmp_path / "profiles",
        computer_use_proof=proof(),
        target_draft_name="new-draft",
        registration_expectation="absent",
        process_records=[],
    )
    assert report["ok"] is False
    assert "expected_8_8_exe_exists" in failed_codes(report)


def test_after_launch_rejects_residual_11_x_process(tmp_path: Path) -> None:
    root = profile_root(tmp_path)
    exe = expected_exe(tmp_path)
    wrong = tmp_path / "JianyingPro-11.0" / "JianyingPro.exe"
    wrong.parent.mkdir()
    wrong.write_bytes(b"11")
    report = inspect_jianying_environment(
        stage="after_launch",
        profile_kind="test",
        expected_exe=exe,
        logical_root=root,
        physical_root=root,
        profiles_root=tmp_path / "profiles",
        computer_use_proof=proof(),
        target_draft_name="draft",
        registration_expectation="absent",
        process_records=[{"pid": 11, "name": "JianyingPro.exe", "exe": str(wrong)}],
    )
    assert report["ok"] is False
    assert "after_launch_processes_exact_8_8_exe" in failed_codes(report)


def test_daily_environment_check_does_not_require_computer_use_proof(tmp_path: Path) -> None:
    root = profile_root(tmp_path)
    report = inspect_jianying_environment(
        stage="before_launch",
        profile_kind="test",
        expected_exe=expected_exe(tmp_path),
        logical_root=root,
        physical_root=root,
        profiles_root=tmp_path / "profiles",
        target_draft_name="probe",
        registration_expectation="absent",
        process_records=[],
    )
    assert report["ok"] is True
    assert report["computer_use_boundary"]["proof_required_for_this_check"] is False
    assert report["computer_use_boundary"]["actual_cua_calls_required"] == []


def test_duplicate_e_registration_is_rejected(tmp_path: Path, monkeypatch) -> None:
    logical = tmp_path / "C-entry" / "com.lveditor.draft"
    physical = tmp_path / "profiles" / "8.8-tests" / "com.lveditor.draft"
    logical.mkdir(parents=True)
    physical.mkdir(parents=True)
    draft_name = "probe"
    entries = [
        {"draft_id": "C", "draft_fold_path": str(logical / draft_name)},
        {"draft_id": "E", "draft_fold_path": str(physical / draft_name)},
    ]
    (logical / "root_meta_info.json").write_text(
        json.dumps({"all_draft_store": entries}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "jianying_adapter.jianying_environment.os.path.samefile",
        lambda left, right: {Path(left), Path(right)} == {logical, physical},
    )
    report = inspect_jianying_environment(
        stage="before_launch",
        profile_kind="test",
        expected_exe=expected_exe(tmp_path),
        logical_root=logical,
        physical_root=physical,
        profiles_root=tmp_path / "profiles",
        computer_use_proof=proof(),
        target_draft_name=draft_name,
        registration_expectation="unique",
        process_records=[],
    )
    assert report["ok"] is False
    assert "target_registration_contract" in failed_codes(report)


def test_test_writer_fails_closed_when_c_root_is_production_profile(tmp_path: Path) -> None:
    production = profile_root(tmp_path, "8.8-production")
    report = inspect_jianying_environment(
        stage="before_launch",
        profile_kind="test",
        expected_exe=expected_exe(tmp_path),
        logical_root=production,
        physical_root=production,
        profiles_root=tmp_path / "profiles",
        computer_use_proof=proof(),
        target_draft_name="probe",
        registration_expectation="absent",
        process_records=[],
    )
    assert report["ok"] is False
    assert "profile_kind_matches_physical_root" in failed_codes(report)


def test_missing_actual_cua_attestation_is_rejected(tmp_path: Path) -> None:
    root = profile_root(tmp_path)
    bad_proof = proof()
    bad_proof["runtime_observations"]["sky_window_enumeration_called"] = False
    report = inspect_jianying_environment(
        stage="before_launch",
        profile_kind="test",
        expected_exe=expected_exe(tmp_path),
        logical_root=root,
        physical_root=root,
        profiles_root=tmp_path / "profiles",
        computer_use_proof=bad_proof,
        require_computer_use_proof=True,
        target_draft_name="probe",
        registration_expectation="absent",
        process_records=[],
    )
    assert report["ok"] is False
    assert report["computer_use_boundary"]["python_can_prove_runtime"] is False
    assert "computer_use_external_proof_declared" in failed_codes(report)


def test_profile_root_must_match_explicit_profiles_root_not_just_folder_name(
    tmp_path: Path,
) -> None:
    lookalike = tmp_path / "other" / "8.8-tests" / "com.lveditor.draft"
    lookalike.mkdir(parents=True)
    (lookalike / "root_meta_info.json").write_text(
        json.dumps({"all_draft_store": []}), encoding="utf-8"
    )
    report = inspect_jianying_environment(
        stage="before_launch",
        profile_kind="test",
        expected_exe=expected_exe(tmp_path),
        logical_root=lookalike,
        physical_root=lookalike,
        profiles_root=tmp_path / "profiles",
        computer_use_proof=proof(),
        target_draft_name="probe",
        registration_expectation="absent",
        process_records=[],
    )
    assert report["ok"] is False
    assert "profile_kind_matches_physical_root" in failed_codes(report)


def test_psutil_process_collection_preserves_unicode_exe_path(monkeypatch) -> None:
    class FakeProcess:
        pid = 88
        info = {
            "pid": 88,
            "name": "JianyingPro.exe",
            "exe": r"D:\剪映专业版\8.8\JianyingPro.exe",
        }

        def exe(self):
            return self.info["exe"]

    monkeypatch.setattr(
        "jianying_adapter.jianying_environment.psutil.process_iter",
        lambda _fields: [FakeProcess()],
    )
    rows = environment._collect_psutil_processes()
    assert len(rows) == 1
    assert rows[0].exe == r"D:\剪映专业版\8.8\JianyingPro.exe"


@pytest.mark.parametrize("failure", [psutil.AccessDenied(88), psutil.NoSuchProcess(88), RuntimeError("path lookup failed")])
def test_only_target_exe_is_read_and_lookup_failure_blocks_environment(
    tmp_path: Path, monkeypatch, failure: Exception,
) -> None:
    calls = []

    class FakeProcess:
        def __init__(self, pid, name):
            self.pid = pid
            self.info = {"pid": pid, "name": name}

        def exe(self):
            calls.append(self.pid)
            if self.pid != 88:
                raise AssertionError("unrelated executable must never be inspected")
            raise failure

        def name(self):
            raise AssertionError("identified target name must not be re-read")

    def processes(fields):
        assert fields == ["pid", "name"]
        return [FakeProcess(1, "unrelated.exe"), FakeProcess(88, "JianyingPro.exe")]

    monkeypatch.setattr("jianying_adapter.jianying_environment.psutil.process_iter", processes)
    rows = environment._collect_psutil_processes()
    assert calls == [88]
    assert len(rows) == 1
    assert rows[0].name == "JianyingPro.exe"
    assert rows[0].exe is None
    assert rows[0].error.startswith(type(failure).__name__)
    root = profile_root(tmp_path)
    report = inspect_jianying_environment(
        stage="after_launch", profile_kind="test", expected_exe=expected_exe(tmp_path),
        logical_root=root, physical_root=root, profiles_root=tmp_path / "profiles",
        target_draft_name="probe", registration_expectation="absent", process_records=rows,
    )
    assert report["ok"] is False
    assert "after_launch_processes_exact_8_8_exe" in failed_codes(report)


def test_windows_snapshot_never_uses_psutil_name_or_unrelated_exe(monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "win32")
    monkeypatch.setattr(environment, "_windows_process_snapshot", lambda: [
        (4, "System"), (7, "unrelated.exe"), (88, "JianyingPro.exe"), (89, "CapCut.EXE")])
    monkeypatch.setattr(psutil, "process_iter", lambda *a: pytest.fail("name() reads unrelated exe"))
    calls = []

    class Target:
        def __init__(self, pid):
            calls.append(pid)
            assert pid in (88, 89)

        def exe(self):
            return r"D:\剪映专业版\8.8\JianyingPro.exe"

    monkeypatch.setattr(psutil, "Process", Target)
    rows = collect_jianying_processes()
    assert calls == [88, 89]
    assert rows[0].exe == r"D:\剪映专业版\8.8\JianyingPro.exe"
    assert all(row.error is None for row in rows)


@pytest.mark.parametrize("failure", [psutil.AccessDenied(88), psutil.NoSuchProcess(88), RuntimeError("lookup failed")])
def test_windows_target_lookup_failure_is_retained(monkeypatch, failure):
    monkeypatch.setattr(environment.sys, "platform", "win32")
    monkeypatch.setattr(environment, "_windows_process_snapshot", lambda: [(88, "JianyingPro.exe")])

    def inaccessible(pid):
        raise failure

    monkeypatch.setattr(psutil, "Process", inaccessible)
    rows = collect_jianying_processes()
    assert len(rows) == 1
    assert rows[0].exe is None
    assert rows[0].error.startswith(type(failure).__name__)


def test_snapshot_discovery_failure_blocks_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(environment.sys, "platform", "win32")

    def failed_snapshot():
        raise OSError("snapshot denied")

    monkeypatch.setattr(environment, "_windows_process_snapshot", failed_snapshot)
    root = profile_root(tmp_path)
    report = inspect_jianying_environment(
        stage="before_launch", profile_kind="test", expected_exe=expected_exe(tmp_path),
        logical_root=root, physical_root=root, profiles_root=tmp_path / "profiles")
    assert {"process_discovery_succeeded", "before_launch_process_count_zero"} <= failed_codes(report)


@pytest.mark.skipif(environment.sys.platform != "win32", reason="Windows Toolhelp API")
@pytest.mark.parametrize("finish_error", [18, 5])
def test_toolhelp_snapshot_closes_handle_and_rejects_partial_discovery(monkeypatch, finish_error):
    import ctypes
    from types import SimpleNamespace

    class Function:
        def __init__(self, call):
            self.call = call

        def __call__(self, *args):
            return self.call(*args)

    closed = []

    def first(handle, pointer):
        pointer._obj.th32ProcessID = 88
        pointer._obj.szExeFile = "JianyingPro.exe"
        return True

    def end(handle, pointer):
        ctypes.set_last_error(finish_error)
        return False

    api = SimpleNamespace(CreateToolhelp32Snapshot=Function(lambda *a: 123),
                          Process32FirstW=Function(first), Process32NextW=Function(end),
                          CloseHandle=Function(lambda handle: closed.append(handle) or True))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: api)
    if finish_error == 18:
        assert environment._windows_process_snapshot() == [(88, "JianyingPro.exe")]
    else:
        with pytest.raises(OSError):
            environment._windows_process_snapshot()
    assert closed == [123]


@pytest.mark.parametrize("field,value", [("version", None), ("version", True),
                                         ("new_version", ""), ("new_version", None)])
def test_reference_requires_complete_header(tmp_path, field, value):
    reference = tmp_path / "reference.json"
    reference.write_text("{}", encoding="utf-8")
    header = {"version": 360000, "new_version": "141.0.0",
              "platform": {"app_version": "8.8.0"},
              "last_modified_platform": {"app_version": "8.8.0"}}
    header[field] = value
    with pytest.raises(RuntimeError, match=field):
        audit_double_8_8_reference(reference, profile_kind="test", profiles_root=tmp_path,
                                  decode=lambda _: header)


def test_test_profile_accepts_workspace_backup_double_8_8_reference(tmp_path: Path) -> None:
    reference = tmp_path / "workspace-backup" / "draft_content.json"
    reference.parent.mkdir()
    reference.write_text("encrypted-placeholder", encoding="utf-8")
    decoded = {
        "version": 360000,
        "new_version": "141.0.0",
        "platform": {"app_version": "8.8.0"},
        "last_modified_platform": {"app_version": "8.8.0"},
    }
    audit = audit_double_8_8_reference(
        reference,
        profile_kind="test",
        profiles_root=tmp_path / "profiles",
        decode=lambda _path: decoded,
    )
    assert audit["versions"] == ["8.8.0", "8.8.0"]
    assert audit["decoded_with_jianying_codec"] is True


def test_test_profile_rejects_active_production_compatibility_reference(
    tmp_path: Path,
) -> None:
    reference = (
        tmp_path
        / "profiles"
        / "8.8-production"
        / "com.lveditor.draft"
        / "production-draft"
        / "draft_content.json"
    )
    reference.parent.mkdir(parents=True)
    reference.write_text("encrypted-placeholder", encoding="utf-8")
    with pytest.raises(RuntimeError, match="workspace backup"):
        audit_double_8_8_reference(
            reference,
            profile_kind="test",
            profiles_root=tmp_path / "profiles",
            decode=lambda _path: {},
        )
