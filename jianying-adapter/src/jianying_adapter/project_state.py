# Copyright (c) 2026 Blackkeys0430 — AutoCUT original project code.
# Origin: https://github.com/Blackkeys0430/AutoCUT-public
# SPDX-License-Identifier: LicenseRef-AutoCUT-Personal-Use-1.0
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import copy
import json
import os
import tempfile
from typing import Any, Callable, Mapping


CANONICAL_STATUSES = (
    "planning",
    "candidate_ready",
    "written_pending_visual_qa",
    "visual_rejected",
    "completed",
)


@dataclass
class ProjectStateReport:
    ok: bool
    status: str
    canonical_status: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "canonical_status": self.canonical_status,
            "errors": self.errors,
            "warnings": self.warnings,
            "missing_artifacts": self.missing_artifacts,
        }


def _option_name(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("option", "name", "id", "key", "feature"):
            if value.get(key) is not None:
                return str(value[key]).strip().casefold()
        return ""
    return str(value).strip().casefold() if isinstance(value, str) else ""


def _authorization_entries(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return structured, current-video authorization records.

    ``confirmed`` was historically a list of free-form strings.  It remains
    readable for old projects, but those strings intentionally never become
    production authorization records.
    """
    raw = state.get("current_authorizations", state.get("authorizations"))
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def validate_current_authorizations(
    state: Mapping[str, Any],
    options: Mapping[str, Any] | None,
) -> ProjectStateReport:
    """Validate enabled optional features against current structured grants.

    A legacy state with string ``confirmed`` entries is accepted by the state
    reader, but returns an error for every enabled option.  Prohibitions win
    over grants so stale approvals cannot enable a forbidden feature.
    """
    errors: list[str] = []
    warnings: list[str] = []
    options = options if isinstance(options, Mapping) else {}
    entries = _authorization_entries(state)
    prohibited = {_option_name(item) for item in state.get("prohibited", []) if _option_name(item)}
    for name, raw in options.items():
        enabled = isinstance(raw, Mapping) and raw.get("enabled") is True
        if raw is True:
            enabled = True
        if not enabled:
            continue
        option = str(name).strip().casefold()
        if option in prohibited:
            errors.append(f"可选项 {name} 被当前 project_state.prohibited 禁止")
            continue
        plan_source = ""
        if isinstance(raw, Mapping):
            plan_source = str(
                raw.get("authorization_source") or raw.get("source") or ""
            ).strip()
        if not plan_source:
            errors.append(f"启用 {name} 时必须提供 authorization_source")
            continue
        matches = [entry for entry in entries if _option_name(entry) == option]
        valid = [
            entry for entry in matches
            if entry.get("enabled") is True
            and str(entry.get("status", "")).strip().casefold()
            in {"authorized", "confirmed", "approved", "active"}
            and str(
                entry.get("source") or entry.get("authorization_source") or ""
            ).strip() == plan_source
        ]
        if not valid:
            errors.append(
                f"可选项 {name} 的 authorization_source 未匹配当前视频有效授权记录；"
                "旧版 confirmed 字符串不能放行"
            )
    return ProjectStateReport(
        ok=not errors,
        status=str(state.get("status") or ""),
        canonical_status=normalize_status(state.get("status")),
        errors=errors,
        warnings=warnings,
    )


def validate_completion_evidence(
    state: Mapping[str, Any], *, state_path: Path | None = None
) -> ProjectStateReport:
    """Require objective structure and user visual acceptance for completion."""
    errors: list[str] = []
    evidence = state.get("completion_evidence")
    if not isinstance(evidence, Mapping):
        errors.append("completed 必须有 completion_evidence 对象")
    else:
        structure = evidence.get("structure_validation", evidence.get("structure"))
        visual = evidence.get("user_visual_qa", evidence.get("visual_qa"))
        if not isinstance(structure, Mapping) or structure.get("ok") is not True:
            errors.append("completed 缺少通过的结构验收证据")
        else:
            report_value = next((structure.get(key) for key in
                ("report_path", "report", "path", "source")
                if isinstance(structure.get(key), str) and structure[key].strip()), "")
            report_path = Path(report_value)
            if not report_path.is_absolute():
                report_path = (state_path.parent if state_path else Path.cwd()) / report_path
            try:
                report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                report = None
            if not isinstance(report, Mapping) or report.get("ok") is not True:
                errors.append("completed 的结构验收必须引用实际通过的 JSON 报告 source/report_path")
            else:
                _validate_completion_binding(state, report, "结构报告", errors)
        if not isinstance(visual, Mapping) or visual.get("accepted") is not True:
            errors.append("completed 缺少用户主观画面验收证据")
        elif not isinstance(visual.get("source"), str) or not visual["source"].strip():
            errors.append("completed 的用户主观画面验收必须有 source")
        else:
            _validate_completion_binding(state, visual, "用户视觉验收", errors)
    return ProjectStateReport(
        ok=not errors,
        status=str(state.get("status") or ""),
        canonical_status=normalize_status(state.get("status")),
        errors=errors,
    )


def _validate_completion_binding(
    state: Mapping[str, Any], record: Mapping[str, Any], label: str, errors: list[str]
) -> None:
    artifacts = state.get("artifacts") or {}
    draft_id = artifacts.get("draft_id") if isinstance(artifacts, Mapping) else None
    if record.get("project_id") != state.get("project_id"):
        errors.append(f"completed 的{label} project_id 与当前视频不符")
    if not isinstance(draft_id, str) or not draft_id.strip() or record.get("draft_id") != draft_id:
        errors.append(f"completed 的{label} draft_id 未绑定当前 Writer 草稿")


def advance_state_after_writer(
    state_path: Path,
    *,
    registration_path: Path,
    backup_path: Path,
    candidate_plan_path: Path | None = None,
    structure_report_path: Path | None = None,
    write_json: Callable[[Path, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Atomically advance state only after all Writer post-checks passed.

    The returned snapshot is suitable for restoring state if a later Writer
    step fails.  ``write_json`` is injectable for failure-path tests.
    """
    state_path = Path(state_path)
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    if not isinstance(state, dict):
        raise ValueError("project_state root must be an object")
    report = validate_project_state(
        state, state_path=state_path, require_canonical_status=True
    )
    if not report.ok:
        raise ValueError("project_state 不能推进: " + "; ".join(report.errors))
    if report.canonical_status != "candidate_ready":
        raise ValueError(f"Writer 只能从 candidate_ready 推进，当前为 {report.canonical_status}")
    registration_path = Path(registration_path)
    backup_path = Path(backup_path)
    if not registration_path.is_file() or not backup_path.exists():
        raise FileNotFoundError("registration 或 backup 不存在，不能推进 project_state")
    registration = json.loads(registration_path.read_text(encoding="utf-8-sig"))
    if not isinstance(registration, Mapping):
        raise ValueError("registration 必须是对象，不能推进 project_state")
    draft_id = str(registration.get("draft_id") or "").strip()
    live_draft = str(
        registration.get("live_draft")
        or registration.get("logical_draft")
        or ""
    ).strip()
    if not draft_id:
        raise ValueError("registration 缺少 draft_id，不能推进 project_state")
    if not live_draft:
        raise ValueError("registration 缺少 live_draft，不能推进 project_state")
    registration_backup = str(registration.get("backup") or "").strip()
    if not registration_backup:
        raise ValueError("registration 缺少 backup，不能推进 project_state")
    if Path(registration_backup).resolve() != backup_path.resolve():
        raise ValueError("registration.backup 与 Writer backup 不一致")
    if registration.get("project_id") != state.get("project_id"):
        raise ValueError("registration.project_id 与当前视频不一致")
    if (registration.get("timeline_equivalence") or {}).get("ok") is not True or registration.get("content_mirrors_deep_equal") is not True:
        raise ValueError("registration 缺少实际通过的时点与镜像写后检查")
    if structure_report_path is None or not Path(structure_report_path).is_file():
        raise FileNotFoundError(f"structure report 不存在: {structure_report_path}")
    structure = json.loads(Path(structure_report_path).read_text(encoding="utf-8-sig"))
    if not isinstance(structure, Mapping) or structure.get("ok") is not True:
        raise ValueError("structure report 未通过，不能推进 project_state")
    before = copy.deepcopy(state)
    updated = copy.deepcopy(state)
    updated["status"] = "written_pending_visual_qa"
    updated.setdefault("pending", [])
    if "用户主观画面验收" not in updated["pending"]:
        updated["pending"].append("用户主观画面验收")
    artifacts = dict(updated.get("artifacts") or {})
    artifacts.update(
        {
            "registration": str(Path(registration_path)),
            "writer_backup": str(Path(backup_path)),
            "live_draft": live_draft,
            "draft_id": draft_id,
        }
    )
    if candidate_plan_path:
        artifacts["candidate_plan"] = str(Path(candidate_plan_path))
    if structure_report_path:
        artifacts["structure_report"] = str(Path(structure_report_path))
    updated["artifacts"] = artifacts
    writer = write_json or _atomic_write_json
    writer(state_path, updated)
    return {"before": before, "after": updated, "state_path": str(state_path)}


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in CANONICAL_STATUSES:
        return status
    if any(token in status for token in ("reject", "failed", "failure", "visual_fail")):
        return "visual_rejected"
    if any(token in status for token in ("complete", "completed", "done", "finished")):
        return "completed"
    if (
        any(token in status for token in ("written", "registered", "unopened", "visual_qa", "user_qa"))
        and any(token in status for token in ("pending", "await", "waiting", "unopened", "qa"))
    ):
        return "written_pending_visual_qa"
    if any(token in status for token in ("candidate", "workspace_only", "ready_for_writer")):
        return "candidate_ready"
    return "planning"


def _string_list(state: Mapping[str, Any], key: str, errors: list[str]) -> list[str]:
    value = state.get(key)
    if not isinstance(value, list):
        errors.append(f"{key} 必须是数组")
        return []
    bad = [item for item in value if not isinstance(item, str) or not item.strip()]
    if bad:
        errors.append(f"{key} 只能包含非空字符串")
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def validate_project_state(
    state: Mapping[str, Any],
    *,
    state_path: Path | None = None,
    verify_artifacts: bool = False,
    require_canonical_status: bool = False,
) -> ProjectStateReport:
    errors: list[str] = []
    warnings: list[str] = []
    missing_artifacts: list[str] = []
    for key in ("schema", "project_id", "status"):
        if not isinstance(state.get(key), str) or not str(state.get(key)).strip():
            errors.append(f"缺少非空字符串字段: {key}")
    confirmed = _string_list(state, "confirmed", errors)
    prohibited = _string_list(state, "prohibited", errors)
    pending = _string_list(state, "pending", errors)
    if len(set(confirmed)) != len(confirmed):
        warnings.append("confirmed 存在重复规则")
    if len(set(prohibited)) != len(prohibited):
        warnings.append("prohibited 存在重复规则")
    status = str(state.get("status") or "")
    canonical = normalize_status(status)
    if status != canonical:
        message = f"状态 {status!r} 应规范为 {canonical!r}"
        if require_canonical_status:
            errors.append(message)
        else:
            warnings.append(message)
    if canonical == "completed" and pending:
        errors.append("completed 状态不能仍有 pending")
    if canonical == "completed":
        errors.extend(validate_completion_evidence(state, state_path=state_path).errors)
    from .visual_planning import required_visual_minima
    try:
        required_visual_minima(state)
    except ValueError as exc:
        errors.append(str(exc))
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, Mapping):
        errors.append("artifacts 必须是对象")
        artifacts = {}
    if verify_artifacts:
        base = state_path.parent if state_path else Path.cwd()
        for key, value in artifacts.items():
            if key == "draft_id":
                continue
            if not isinstance(value, str) or not value.strip():
                warnings.append(f"artifacts.{key} 不是可检查路径")
                continue
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = base / candidate
            if not candidate.exists():
                missing_artifacts.append(str(candidate))
        if missing_artifacts:
            errors.append(f"{len(missing_artifacts)} 个产物路径不存在")
    return ProjectStateReport(
        ok=not errors,
        status=status,
        canonical_status=canonical,
        errors=errors,
        warnings=warnings,
        missing_artifacts=missing_artifacts,
    )
