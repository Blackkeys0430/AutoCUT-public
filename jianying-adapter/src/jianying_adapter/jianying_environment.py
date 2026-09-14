from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

import psutil


Stage = Literal["before_launch", "after_launch"]
ProfileKind = Literal["production", "test"]
RegistrationExpectation = Literal["absent", "unique"]
LockExpectation = Literal["none", "target_locked"]

PROCESS_NAMES = {"jianyingpro.exe", "jianyingpro", "capcut.exe", "capcut"}
PROFILE_DIRECTORY = {"production": "8.8-production", "test": "8.8-tests"}


class JianyingEnvironmentError(RuntimeError):
    def __init__(self, report: Mapping[str, Any]) -> None:
        self.report = dict(report)
        failures = [
            str(row.get("code"))
            for row in self.report.get("checks", [])
            if isinstance(row, Mapping) and row.get("ok") is not True
        ]
        super().__init__("Jianying environment gate failed: " + ", ".join(failures))


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    name: str
    exe: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "name": self.name,
            "exe": self.exe,
            "error": self.error,
        }


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _windows_process_snapshot() -> list[tuple[int, str]]:
    """Enumerate names from Toolhelp32, without opening each process executable."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateToolhelp32Snapshot
    create.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create.restype = wintypes.HANDLE
    first, next_entry = kernel32.Process32FirstW, kernel32.Process32NextW
    for function in (first, next_entry):
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    handle = create(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if handle == ctypes.c_void_p(-1).value or handle is None:
        raise ctypes.WinError(ctypes.get_last_error())
    result: list[tuple[int, str]] = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = first(handle, ctypes.byref(entry))
        while more:
            result.append((int(entry.th32ProcessID), entry.szExeFile))
            more = next_entry(handle, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES is the only successful termination.
            raise ctypes.WinError(error)
    finally:
        close(handle)
    return result


def collect_jianying_processes() -> list[ProcessRecord]:
    """Read targets only; discovery failures propagate so the gate stays closed."""
    if sys.platform == "win32":
        result: list[ProcessRecord] = []
        # psutil.name() on Windows calls exe(), even via process_iter(['name']).
        # Toolhelp's name field avoids that lookup for unrelated system processes.
        for pid, name in _windows_process_snapshot():
            if name.casefold() not in PROCESS_NAMES:
                continue
            try:
                exe = psutil.Process(pid).exe()
                result.append(ProcessRecord(pid, name, exe or None,
                                            None if exe else "executable_path_unavailable"))
            except Exception as exc:
                result.append(ProcessRecord(pid, name, None, f"{type(exc).__name__}: {exc}"))
        return result
    return _collect_psutil_processes()


def _collect_psutil_processes() -> list[ProcessRecord]:
    """Non-Windows collection retains psutil's existing behavior."""
    result: list[ProcessRecord] = []
    # Resolving every system process executable can stall on unrelated processes.
    # Identify the application first; inspect paths only for actual targets.
    for process in psutil.process_iter(["pid", "name"]):
        name = ""
        try:
            name = str(process.info.get("name") or "")
            if name.casefold() not in PROCESS_NAMES:
                continue
            exe = process.exe()
            result.append(
                ProcessRecord(
                    pid=int(process.info.get("pid") or process.pid),
                    name=name,
                    exe=str(exe) if exe else None,
                    error=None if exe else "executable_path_unavailable",
                )
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
            if name.casefold() not in PROCESS_NAMES:
                try:
                    name = str(process.name() or "")
                except Exception:
                    name = "unknown"
            if name.casefold() not in PROCESS_NAMES:
                continue
            result.append(
                ProcessRecord(
                    pid=int(getattr(process, "pid", 0) or 0),
                    name=name,
                    exe=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        except Exception as exc:
            # An unknown inspection error is retained as a fail-closed record.
            result.append(
                ProcessRecord(
                    pid=int(getattr(process, "pid", 0) or 0),
                    name=name or "unknown",
                    exe=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return result


def _coerce_processes(rows: Iterable[ProcessRecord | Mapping[str, Any]]) -> list[ProcessRecord]:
    result: list[ProcessRecord] = []
    for row in rows:
        if isinstance(row, ProcessRecord):
            record = row
        else:
            record = ProcessRecord(
                pid=int(row.get("pid") or 0),
                name=str(row.get("name") or ""),
                exe=str(row.get("exe")) if row.get("exe") else None,
                error=str(row.get("error")) if row.get("error") else None,
            )
        if record.name.casefold() in PROCESS_NAMES:
            result.append(record)
    return result


def _registered_name(entry: Mapping[str, Any]) -> str:
    raw = str(entry.get("draft_fold_path") or "").replace("\\", "/").rstrip("/")
    return raw.rsplit("/", 1)[-1] if raw else ""


def _validate_computer_use_proof(proof: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    doctor = proof.get("node_repl_bridge_doctor")
    runtime = proof.get("runtime_observations")
    doctor_ok = (
        isinstance(doctor, Mapping)
        and doctor.get("ok") is True
        and doctor.get("mode") == "project-drift"
        and doctor.get("issues") == []
        and doctor.get("installer_preview_exit_code") == 0
        and doctor.get("installer_preview_changed") is False
        and bool(doctor.get("source_runtime_hash"))
        and doctor.get("source_runtime_hash") == doctor.get("project_runtime_hash")
        and bool(doctor.get("source_plugin_version"))
        and doctor.get("source_plugin_version") == doctor.get("project_plugin_version")
    )
    runtime_fields_ok = (
        isinstance(runtime, Mapping)
        and runtime.get("cua_get_state_called") is True
        and runtime.get("sky_window_enumeration_called") is True
        and bool(str(runtime.get("observed_at") or "").strip())
        and runtime.get("agent_attested") is True
    )
    return doctor_ok and runtime_fields_ok, {
        "doctor_declared_ok": doctor_ok,
        "cua_get_state_declared": bool(
            isinstance(runtime, Mapping) and runtime.get("cua_get_state_called") is True
        ),
        "sky_window_enumeration_declared": bool(
            isinstance(runtime, Mapping)
            and runtime.get("sky_window_enumeration_called") is True
        ),
        "python_runtime_verification": False,
        "scope": (
            "Python validates the supplied Doctor result and attestation fields only; "
            "an Agent must actually call CUA getState and enumerate SKY windows."
        ),
    }


def audit_double_8_8_reference(
    reference: Path,
    *,
    profile_kind: ProfileKind,
    profiles_root: Path,
    decode: Callable[[Path], Mapping[str, Any]],
) -> dict[str, Any]:
    """Decode and audit an explicit read-only compatibility reference."""

    reference_path = Path(reference).resolve()
    if reference_path.is_dir():
        reference_path = reference_path / "draft_content.json"
    if not reference_path.is_file():
        raise FileNotFoundError(reference_path)
    production_profile = (
        Path(profiles_root).resolve()
        / "8.8-production"
        / "com.lveditor.draft"
    )
    if profile_kind == "test" and production_profile in reference_path.parents:
        raise RuntimeError(
            "test compatibility reference must be a workspace backup, not active production"
        )
    content = decode(reference_path)
    if type(content.get("version")) is not int or content["version"] <= 0:
        raise RuntimeError("compatibility reference missing valid version")
    if not isinstance(content.get("new_version"), str) or not content["new_version"].strip():
        raise RuntimeError("compatibility reference missing valid new_version")
    versions = [
        (content.get("platform") or {}).get("app_version"),
        (content.get("last_modified_platform") or {}).get("app_version"),
    ]
    if versions != ["8.8.0", "8.8.0"]:
        raise RuntimeError(f"compatibility reference is not double-8.8: {versions}")
    return {
        "path": str(reference_path),
        "versions": versions,
        "readable": True,
        "read_only_reference": True,
        "decoded_with_jianying_codec": True,
    }


def inspect_jianying_environment(
    *,
    stage: Stage,
    profile_kind: ProfileKind,
    expected_exe: Path,
    logical_root: Path,
    physical_root: Path,
    profiles_root: Path,
    computer_use_proof: Mapping[str, Any] | None = None,
    require_computer_use_proof: bool = False,
    target_draft_name: str | None = None,
    registration_expectation: RegistrationExpectation = "absent",
    lock_expectation: LockExpectation = "none",
    process_records: Sequence[ProcessRecord | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a fail-closed, read-only environment report for Jianying 8.8."""

    if stage not in {"before_launch", "after_launch"}:
        raise ValueError(f"Unsupported stage: {stage}")
    if profile_kind not in PROFILE_DIRECTORY:
        raise ValueError(f"Unsupported profile_kind: {profile_kind}")
    if registration_expectation not in {"absent", "unique"}:
        raise ValueError(f"Unsupported registration_expectation: {registration_expectation}")
    if lock_expectation not in {"none", "target_locked"}:
        raise ValueError(f"Unsupported lock_expectation: {lock_expectation}")
    if stage == "before_launch" and lock_expectation != "none":
        raise ValueError("before_launch only permits lock_expectation=none")
    if lock_expectation == "target_locked" and not target_draft_name:
        raise ValueError("target_locked requires target_draft_name")

    checks: list[dict[str, Any]] = []

    def add(code: str, ok: bool, detail: Any) -> None:
        checks.append({"code": code, "ok": bool(ok), "detail": detail})

    expected_exe = Path(expected_exe)
    logical_root = Path(os.path.abspath(logical_root))
    physical_root = Path(os.path.abspath(physical_root))
    profiles_root = Path(os.path.abspath(profiles_root))
    expected_physical_root = (
        profiles_root / PROFILE_DIRECTORY[profile_kind] / "com.lveditor.draft"
    )
    add("expected_8_8_exe_exists", expected_exe.is_file(), str(expected_exe))
    add(
        "profile_kind_matches_physical_root",
        _path_key(physical_root) == _path_key(expected_physical_root),
        {
            "profile_kind": profile_kind,
            "expected_profile_directory": PROFILE_DIRECTORY[profile_kind],
            "profiles_root": str(profiles_root),
            "expected_physical_root": str(expected_physical_root),
            "physical_root": str(physical_root),
        },
    )

    samefile = False
    try:
        samefile = logical_root.is_dir() and physical_root.is_dir() and os.path.samefile(
            logical_root, physical_root
        )
    except OSError:
        samefile = False
    add(
        "logical_root_samefile_physical_profile",
        samefile,
        {"logical_root": str(logical_root), "physical_root": str(physical_root)},
    )

    if require_computer_use_proof:
        proof_ok, proof_detail = _validate_computer_use_proof(computer_use_proof or {})
        add("computer_use_external_proof_declared", proof_ok, proof_detail)
    else:
        add(
            "computer_use_proof_not_required_for_daily_check",
            True,
            "Daily checks do not require CUA/SKY/Doctor proof; enable it explicitly for fault diagnosis.",
        )

    try:
        processes = _coerce_processes(
            collect_jianying_processes() if process_records is None else process_records
        )
        process_discovery_error = None
    except Exception as exc:
        processes = []
        process_discovery_error = f"{type(exc).__name__}: {exc}"
    add(
        "process_discovery_succeeded",
        process_discovery_error is None,
        process_discovery_error,
    )
    if stage == "before_launch":
        add(
            "before_launch_process_count_zero",
            process_discovery_error is None and len(processes) == 0,
            [row.as_dict() for row in processes],
        )
    else:
        expected_key = _path_key(expected_exe)
        process_paths_ok = bool(processes) and all(
            row.error is None and row.exe is not None and _path_key(row.exe) == expected_key
            for row in processes
        )
        add(
            "after_launch_processes_exact_8_8_exe",
            process_discovery_error is None and process_paths_ok,
            {
                "expected_exe": str(expected_exe),
                "processes": [row.as_dict() for row in processes],
            },
        )

    lock_paths = (
        sorted(path for path in logical_root.rglob(".locked") if path.is_file())
        if logical_root.is_dir()
        else []
    )
    if lock_expectation == "none":
        locks_ok = not lock_paths
    else:
        target_root = logical_root / str(target_draft_name)
        locks_ok = bool(lock_paths) and all(target_root in path.parents for path in lock_paths)
    add(
        "lock_state_matches_stage",
        locks_ok,
        {
            "stage": stage,
            "expectation": lock_expectation,
            "locks": [str(path) for path in lock_paths],
        },
    )

    registration_detail: dict[str, Any] = {
        "expectation": registration_expectation,
        "target_draft_name": target_draft_name,
        "matches": [],
    }
    registration_ok = False
    root_meta_path = logical_root / "root_meta_info.json"
    try:
        root_meta = _read_object(root_meta_path)
        entries = root_meta.get("all_draft_store")
        if not isinstance(entries, list):
            raise ValueError("all_draft_store is not a list")
        if target_draft_name:
            matches = [
                row
                for row in entries
                if isinstance(row, dict) and _registered_name(row) == target_draft_name
            ]
            expected_lexical_path = _path_key(logical_root / target_draft_name)
            registration_detail["matches"] = [
                {
                    "draft_id": row.get("draft_id"),
                    "draft_fold_path": row.get("draft_fold_path"),
                }
                for row in matches
            ]
            registration_detail["expected_lexical_path"] = str(logical_root / target_draft_name)
            if registration_expectation == "absent":
                registration_ok = len(matches) == 0
            else:
                registration_ok = (
                    len(matches) == 1
                    and _path_key(str(matches[0].get("draft_fold_path") or ""))
                    == expected_lexical_path
                )
        else:
            registration_ok = registration_expectation == "absent"
    except Exception as exc:
        registration_detail["error"] = f"{type(exc).__name__}: {exc}"
    add("target_registration_contract", registration_ok, registration_detail)

    report = {
        "schema": "jianying-adapter.environment-gate.v1",
        "ok": all(row["ok"] for row in checks),
        "stage": stage,
        "profile_kind": profile_kind,
        "expected_exe": str(expected_exe),
        "logical_root": str(logical_root),
        "physical_root": str(physical_root),
        "profiles_root": str(profiles_root),
        "target_draft_name": target_draft_name,
        "checks": checks,
        "computer_use_boundary": {
            "python_can_prove_runtime": False,
            "proof_required_for_this_check": require_computer_use_proof,
            "actual_cua_calls_required": (
                ["cua.getState", "sky window enumeration"]
                if require_computer_use_proof
                else []
            ),
            "diagnostic_only": not require_computer_use_proof,
        },
        "mutations_performed": False,
    }
    return report


def require_jianying_environment(**kwargs: Any) -> dict[str, Any]:
    report = inspect_jianying_environment(**kwargs)
    if report.get("ok") is not True:
        raise JianyingEnvironmentError(report)
    return report
