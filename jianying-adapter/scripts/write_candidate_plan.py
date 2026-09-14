"""Materialize one validated CandidatePlan as a new Jianying 8.8 draft.

This is the generic production writer for ``jianying-adapter.candidate-plan.v1``.
It refuses to overwrite drafts, writes through the verified physical profile
while retaining the host logical path in registration, audits a native double-8.8 compatibility reference, backs up the
newly registered base, applies the assembled candidate once, and verifies the
live round-trip before emitting a registration record.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
ADAPTER_ROOT = SCRIPT_ROOT.parent
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(ADAPTER_ROOT / "src"))

import materialize_trial_draft as materializer  # noqa: E402
from jianying_adapter.jianying_environment import (  # noqa: E402
    JianyingEnvironmentError,
    audit_double_8_8_reference,
    collect_jianying_processes,
    inspect_jianying_environment,
)
from jianying_adapter.candidate_plan import (  # noqa: E402
    validate_actual_content,
    validate_timeline_equivalence,
    validate_writer_contract,
)
from jianying_adapter.writer_rollback import rollback_new_draft  # noqa: E402
from jianying_adapter.project_state import (  # noqa: E402
    advance_state_after_writer,
    validate_current_authorizations,
)


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def write_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _snapshot_state(state_path: Path, backup_dir: Path) -> Path:
    """Copy the exact pre-write state before any draft mutation."""
    if not state_path.is_file():
        raise FileNotFoundError(state_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    snapshot = backup_dir / "project_state.before.json"
    if snapshot.exists():
        raise FileExistsError(f"state snapshot already exists: {snapshot}")
    shutil.copy2(state_path, snapshot)
    return snapshot


def _restore_state_snapshot(snapshot: Path, state_path: Path) -> None:
    if not snapshot.is_file():
        raise FileNotFoundError(snapshot)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=state_path.name + ".", suffix=".rollback", dir=state_path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        shutil.copy2(snapshot, temporary)
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_editor_stopped() -> None:
    running = {row.name for row in collect_jianying_processes()}
    if running:
        raise RuntimeError("剪映仍在运行: " + ", ".join(sorted(running)))


def ensure_writer_gate(
    *,
    user_data: Path,
    logical_root: Path,
    physical_root: Path,
    profiles_root: Path,
    profile_kind: str,
    expected_exe: Path,
    environment_report_path: Path,
    plan: dict[str, Any],
    plan_path: Path,
    preview_test: bool = False,
) -> tuple[Path, Path, Path, str]:
    candidate = Path(str(plan.get("output") or ""))
    report = Path(str(plan.get("report") or ""))
    manifest = Path(str(plan.get("manifest") or ""))
    draft_name = str((plan.get("test_preview") or {}).get("draft_name") if preview_test else plan.get("draft_name") or "").strip()
    if not draft_name:
        raise ValueError(f"candidate plan has no draft_name: {plan_path}")
    contract = validate_writer_contract(plan, plan_path=plan_path, preview_test=preview_test, profile_kind=profile_kind)
    if contract.get("ok") is not True:
        raise RuntimeError(
            "单 Writer 门禁拒绝写入: "
            + json.dumps(contract.get("errors") or [], ensure_ascii=False)
        )
    state_value = plan.get("project_state")
    state_path = Path(str(state_value or ""))
    if not state_path.is_absolute():
        state_path = plan_path.parent / state_path
    state_path = state_path.resolve()
    state = read_object(state_path)
    authorization_report = validate_current_authorizations(
        state, plan.get("options") if isinstance(plan.get("options"), dict) else {}
    )
    if not authorization_report.ok:
        raise RuntimeError(
            "单 Writer 授权门禁拒绝写入: "
            + json.dumps(authorization_report.errors, ensure_ascii=False)
        )
    candidate = Path(contract["paths"]["candidate"])
    report = Path(contract["paths"]["report"])
    manifest = Path(contract["paths"]["manifest"])
    pre_write_environment = inspect_jianying_environment(
        stage="before_launch",
        profile_kind=profile_kind,
        expected_exe=expected_exe,
        logical_root=logical_root,
        physical_root=physical_root,
        profiles_root=profiles_root,
        computer_use_proof=None,
        require_computer_use_proof=False,
        target_draft_name=draft_name,
        registration_expectation="absent",
        lock_expectation="none",
    )
    write_object(
        environment_report_path,
        {
            "schema": "jianying-adapter.writer-environment-gates.v1",
            "ok": pre_write_environment["ok"],
            "profile_kind": profile_kind,
            "pre_write": pre_write_environment,
            "post_write": None,
        },
    )
    if pre_write_environment.get("ok") is not True:
        raise JianyingEnvironmentError(pre_write_environment)
    ensure_editor_stopped()
    if (logical_root / draft_name).exists() or (physical_root / draft_name).exists():
        raise FileExistsError(f"拒绝覆盖已有草稿: {draft_name}")
    return candidate, report, manifest, draft_name


def track_facts(content: dict[str, Any]) -> dict[str, int]:
    tracks = [track for track in content.get("tracks", []) if isinstance(track, dict)]

    def segments(name: str) -> int:
        return sum(
            len(track.get("segments", []))
            for track in tracks
            if track.get("name") == name
        )

    return {
        "total_tracks": len(tracks),
        "base_aroll_segments": segments("JY_ROUGH_CUT_VIDEO"),
        "ordinary_captions": segments("JY_ZH_SUBTITLES"),
        "preset_tracks": sum(
            1 for track in tracks if str(track.get("name", "")).startswith("JY_PRESET_")
        ),
        "preset_text_tracks": sum(
            1
            for track in tracks
            if str(track.get("name", "")).startswith("JY_PRESET_")
            and track.get("type") == "text"
        ),
        "broll_background_tracks": sum(
            1
            for track in tracks
            if str(track.get("name", "")).startswith("JY_BROLL_BACKGROUND_")
        ),
        "circle_aroll_tracks": sum(
            1 for track in tracks if track.get("name") == "JY_NATIVE_CIRCLE_AROLL"
        ),
        "action_sfx_segments": sum(
            len(track.get("segments", [])) for track in tracks
            if track.get("type") == "audio" and str(track.get("name", "")).startswith("JY_SFX_")
        ),
        "preset_audio_segments": sum(
            len(track.get("segments", [])) for track in tracks
            if track.get("type") == "audio" and str(track.get("name", "")).startswith("JY_PRESET_")
            and "_AUX_AUDIO_" in str(track.get("name", ""))
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--user-data", type=Path, required=True)
    parser.add_argument("--draft-root", type=Path, required=True)
    parser.add_argument("--physical-root", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--profile-kind", choices=("production", "test"), required=True)
    parser.add_argument("--preview-test", action="store_true", help="仅将禁生产写入的原生预览登记到独立test profile")
    parser.add_argument("--environment-report", type=Path, required=True)
    parser.add_argument("--compatibility-reference-path", type=Path,
                        default=ADAPTER_ROOT / "assets" / "compatibility" / "8.8.local.json",
                        help="独立共享8.8技术参考；仍可显式指定其他已核验参考")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--registration", type=Path, required=True)
    return parser.parse_args()


def _run(args: argparse.Namespace) -> int:

    plan_path = args.plan.resolve()
    plan = read_object(plan_path)
    if plan.get("schema") != "jianying-adapter.candidate-plan.v1":
        raise ValueError("只允许 jianying-adapter.candidate-plan.v1")

    logical_root = Path(os.path.abspath(args.draft_root))
    physical_root = args.physical_root.resolve()
    expected_exe = args.install_dir / "JianyingPro.exe"
    declared_exe = Path(str((plan.get("target") or {}).get("expected_exe") or ""))
    if not declared_exe.is_absolute():
        declared_exe = plan_path.parent / declared_exe
    if declared_exe.resolve(strict=False) != expected_exe.resolve(strict=False):
        raise RuntimeError(
            f"Writer --install-dir 与 CandidatePlan 的8.8路径不一致: {expected_exe} != {declared_exe}"
        )
    candidate_path, report_path, manifest_path, draft_name = ensure_writer_gate(
        user_data=args.user_data,
        logical_root=logical_root,
        physical_root=physical_root,
        profiles_root=args.profiles_root,
        profile_kind=args.profile_kind,
        expected_exe=expected_exe,
        environment_report_path=args.environment_report,
        plan=plan,
        plan_path=plan_path,
        preview_test=bool(getattr(args, "preview_test", False)),
    )
    state_value = plan.get("project_state")
    state_path = Path(str(state_value or ""))
    if not state_path.is_absolute():
        state_path = plan_path.parent / state_path
    state_path = state_path.resolve()
    state_snapshot = _snapshot_state(state_path, args.backup_dir)
    candidate = read_object(candidate_path)
    expected_facts = track_facts(candidate)

    codec = materializer._codec(args.install_dir)
    reference_record = audit_double_8_8_reference(
        args.compatibility_reference_path,
        profile_kind=args.profile_kind,
        profiles_root=args.profiles_root,
        decode=lambda path: materializer.load_json_object_with_codec(
            path, content_codec=codec
        )[0],
    )
    reference_path = Path(reference_record["path"])
    reference = str(reference_path)
    reference_audit = [reference_record]
    rough_cut = plan.get("rough_cut")
    if isinstance(rough_cut, dict):
        source = Path(str(rough_cut.get("path") or ""))
    else:
        source = Path(str(rough_cut or ""))
    if not source.is_file():
        raise FileNotFoundError(source)
    target = plan.get("target") or {}
    base_output = plan_path.parent / ("base_registered_for_test_preview.json" if getattr(args, "preview_test", False) else "base_registered_for_writer.json")
    operation = Namespace(
        install_dir=args.install_dir,
        user_data=args.user_data,
        draft_root=logical_root,
        physical_root=physical_root,
        name=draft_name,
        backup_dir=args.backup_dir,
        video=source,
        output=base_output,
        width=int(target.get("width") or 0),
        height=int(target.get("height") or 0),
        fps=int(target.get("fps") or 30),
        use_existing_empty_dir=False,
        compatibility_reference=None,
        compatibility_reference_path=reference_path,
        candidate=candidate_path,
        allow_candidate_rebase=True,
    )

    attempt_marker = args.backup_dir / "writer_attempt.json"
    if attempt_marker.exists():
        raise FileExistsError(f"Writer attempt marker already exists: {attempt_marker}")
    write_object(
        attempt_marker,
        {
            "schema": "jianying-adapter.writer-attempt.v1",
            "status": "create_started_after_absence_gate",
            "draft_name": draft_name,
            "logical_root": str(logical_root),
            "physical_root": str(physical_root),
            "candidate_plan": str(plan_path),
            "project_state": str(state_path),
            "project_state_before": str(state_snapshot),
            "preview_test": bool(getattr(args, "preview_test", False)),
        },
    )
    created = materializer.create_base(operation)
    applied = materializer.apply_candidate(operation)
    live = logical_root / draft_name
    physical = physical_root / draft_name
    if not live.is_dir() or not physical.is_dir() or not os.path.samefile(live, physical):
        raise RuntimeError("写入后C/E不是同一物理草稿")
    post_write_environment = inspect_jianying_environment(
        stage="before_launch",
        profile_kind=args.profile_kind,
        expected_exe=expected_exe,
        logical_root=logical_root,
        physical_root=physical_root,
        profiles_root=args.profiles_root,
        computer_use_proof=None,
        require_computer_use_proof=False,
        target_draft_name=draft_name,
        registration_expectation="unique",
        lock_expectation="none",
    )
    write_object(
        args.environment_report,
        {
            "schema": "jianying-adapter.writer-environment-gates.v1",
            "ok": post_write_environment["ok"],
            "profile_kind": args.profile_kind,
            "pre_write": read_object(args.environment_report)["pre_write"],
            "post_write": post_write_environment,
        },
    )
    if post_write_environment.get("ok") is not True:
        raise JianyingEnvironmentError(post_write_environment)

    content, _ = materializer.load_json_object_with_codec(
        live / "draft_content.json",
        content_codec=codec,
    )
    versions = {
        "platform": (content.get("platform") or {}).get("app_version"),
        "last_modified": (content.get("last_modified_platform") or {}).get("app_version"),
    }
    actual_facts = track_facts(content)
    if versions != {"platform": "8.8.0", "last_modified": "8.8.0"}:
        raise RuntimeError(f"写入后兼容头异常: {versions}")
    if actual_facts != expected_facts:
        raise RuntimeError(f"写入后轨道事实异常: {actual_facts} != {expected_facts}")
    post_write_timeline = validate_timeline_equivalence(
        plan, content, plan_path=plan_path
    )
    if post_write_timeline.get("ok") is not True:
        raise RuntimeError(
            "写入后计划与草稿时点/文字/B-roll不等价: "
            + json.dumps(post_write_timeline.get("errors") or [], ensure_ascii=False)
        )
    from jianying_adapter.final_layout import validate_final_layout
    post_write_captions = validate_actual_content(content, plan, plan_path=plan_path)
    if not post_write_captions.get('ok'):
        raise RuntimeError('写入后字幕分工/语义覆盖复核失败: '
                           + json.dumps(post_write_captions.get('errors', []), ensure_ascii=False))
    post_write_layout = validate_final_layout(content, plan, preview=args.preview_test and args.profile_kind == "test")
    if not post_write_layout.get('ok'):
        raise RuntimeError('写入后布局/平台区域复核失败: '
                           + json.dumps(post_write_layout.get('errors', []), ensure_ascii=False))

    registration = {
        "schema": "jianying-adapter.candidate-registration.v1",
        "project_id": read_object(state_path)["project_id"],
        "ok": True,
        "status": "written_unopened_pending_user_visual_qa",
        "draft_name": draft_name,
        "logical_draft": str(live),
        "physical_draft": str(physical),
        "live_draft": str(live),
        "draft_id": str(content.get("id", "")),
        "versions": versions,
        "duration_us": int(content.get("duration", 0)),
        "track_facts": actual_facts,
        "final_layout": post_write_layout,
        "actual_caption_coverage": post_write_captions,
        "timeline_equivalence": post_write_timeline,
        "compatibility_reference": reference,
        "reference_audit": reference_audit,
        "candidate_plan": str(plan_path),
        "candidate": str(candidate_path),
        "structure_report": str(report_path),
        "environment_report": str(args.environment_report),
        "profile_kind": args.profile_kind,
        "backup": applied["backup"],
        "content_mirrors_deep_equal": applied["content_mirrors_deep_equal"],
        "opened_jianying": False,
        "exported": False,
        "published": False,
        "subjective_visual_qa": "pending_user",
    }
    write_object(args.registration, registration)
    if getattr(args, "preview_test", False):
        # The preview manifest stays immutable so seal can bind its evidence.
        # This registration is an isolated viewing artifact, not production.
        registration.update({
            "status": "test_preview_written_pending_native_qa",
            "production_writer_allowed": False,
            "preview_manifest": str(manifest_path),
        })
        state = read_object(state_path)
        if state.get("status") != "planning":
            raise RuntimeError("测试预览写后必须保持 planning")
        state.setdefault("artifacts", {}).update({
            "test_preview_registration": str(args.registration),
            "registration": str(args.registration),
            "draft_id": registration["draft_id"],
            "native_test": str(physical),
            "live_draft": str(live),
            "candidate_plan": str(plan_path),
            "candidate": str(candidate_path),
            "structure_report": str(report_path),
            "writer_backup": applied["backup"],
        })
        state.setdefault("validation", {})["test_preview"] = {
            "registered": True, "draft_name": draft_name,
            "logical_draft": str(live), "physical_draft": str(physical),
            "draft_id": registration["draft_id"], "profile_kind": "test",
            "native_visual_qa": "pending", "production_writer_allowed": False,
            "backup": applied["backup"],
        }
        state.setdefault("internal_review", {})["delivery"] = {
            key: registration[key] for key in (
                "draft_id", "draft_name", "physical_draft", "logical_draft",
                "versions", "duration_us", "opened_jianying", "subjective_visual_qa"
            )
        }
        state["internal_review"]["delivery"].update(
            registered=True, structure_passed=True, native_playback=False
        )
        if "用户播放验收当前TEST" not in state.setdefault("pending", []):
            state["pending"].append("用户播放验收当前TEST")
        write_object(state_path, state)
        write_object(args.registration, registration)
        attempt = read_object(attempt_marker)
        attempt.update({"status": "completed", "draft_id": registration["draft_id"]})
        write_object(attempt_marker, attempt)
        print(json.dumps({"registration": registration}, ensure_ascii=False, indent=2))
        return 0
    manifest = read_object(manifest_path)
    manifest.update(
        {
            "status": registration["status"],
            "writer_allowed": False,
            "blockers": ["already_written_pending_user_visual_qa"],
            "registered_live_draft": True,
            "draft_name": draft_name,
            "live_draft": str(live),
            "backup": applied["backup"],
            "registration": str(args.registration),
            "compatibility_reference": reference,
        }
    )
    write_object(manifest_path, manifest)
    state_transition = advance_state_after_writer(
        state_path,
        registration_path=args.registration,
        backup_path=Path(str(applied["backup"])),
        candidate_plan_path=plan_path,
        structure_report_path=report_path,
    )
    registration["project_state_transition"] = state_transition
    write_object(args.registration, registration)
    attempt = read_object(attempt_marker)
    attempt.update({"status": "completed", "draft_id": registration["draft_id"]})
    write_object(attempt_marker, attempt)
    print(
        json.dumps(
            {"created": created, "applied": applied, "registration": registration},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    args = parse_args()
    try:
        return _run(args)
    except Exception as error:
        print(f"Writer failure before recovery: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        attempt_marker = args.backup_dir / "writer_attempt.json"
        if attempt_marker.is_file():
            attempt = read_object(attempt_marker)
            if attempt.get("status") == "create_started_after_absence_gate":
                draft_name = str(attempt.get("draft_name") or "")
                root_meta_backup = args.backup_dir / "root_meta_info.json"
                storage_root = Path(str(attempt.get("physical_root") or attempt["logical_root"]))
                live_path = storage_root / draft_name
                if not root_meta_backup.is_file() and not live_path.exists():
                    attempt.update(
                        {
                            "status": "failed_before_live_mutation",
                            "writer_failure": f"{type(error).__name__}: {error}",
                        }
                    )
                    write_object(attempt_marker, attempt)
                    raise
                try:
                    rollback = rollback_new_draft(
                        logical_root=Path(str(attempt["logical_root"])),
                        physical_root=storage_root,
                        draft_name=draft_name,
                        root_meta_backup=root_meta_backup,
                        quarantine_root=args.backup_dir / "failed_writer_quarantine",
                        failure=error,
                    )
                    state_path = Path(str(attempt.get("project_state") or ""))
                    state_snapshot = Path(str(attempt.get("project_state_before") or ""))
                    state_restore = None
                    if state_path and state_snapshot:
                        _restore_state_snapshot(state_snapshot, state_path)
                        state_restore = {"restored": True, "path": str(state_path)}
                except Exception as rollback_error:
                    attempt.update(
                        {
                            "status": "rollback_failed_manual_recovery_required",
                            "writer_failure": f"{type(error).__name__}: {error}",
                            "rollback_failure": f"{type(rollback_error).__name__}: {rollback_error}",
                        }
                    )
                    write_object(attempt_marker, attempt)
                    raise RuntimeError(
                        "Writer 失败且自动回滚失败，必须按 attempt marker 手工恢复: "
                        f"{attempt_marker}"
                    ) from error
                attempt.update({"status": "rolled_back", "rollback": rollback})
                if state_restore is not None:
                    attempt["project_state_restore"] = state_restore
                write_object(attempt_marker, attempt)
                failure_record = {
                    "schema": "jianying-adapter.candidate-registration-failure.v1",
                    "status": "writer_failed_rolled_back",
                    "draft_name": draft_name,
                    "failure": f"{type(error).__name__}: {error}",
                    "rollback": rollback,
                    "project_state_restore": state_restore,
                    "candidate_plan": str(attempt.get("candidate_plan") or ""),
                }
                write_object(args.registration, failure_record)
                # ``plan`` is local to _run; never consult it from this
                # exception handler.  The attempt marker is the durable
                # transaction record and carries the exact plan path.
                manifest_path = Path()
                candidate_plan_value = str(attempt.get("candidate_plan") or "").strip()
                if candidate_plan_value and not attempt.get("preview_test"):
                    candidate_plan_path = Path(candidate_plan_value)
                    if candidate_plan_path.is_file():
                        try:
                            failed_plan = read_object(candidate_plan_path)
                            manifest_value = str(failed_plan.get("manifest") or "").strip()
                            if manifest_value:
                                manifest_path = Path(manifest_value)
                                if not manifest_path.is_absolute():
                                    manifest_path = candidate_plan_path.parent / manifest_path
                        except (OSError, ValueError, json.JSONDecodeError):
                            manifest_path = Path()
                if manifest_path.is_file():
                    try:
                        manifest = read_object(manifest_path)
                        manifest.update({
                            "status": "writer_failed_rolled_back",
                            "writer_allowed": False,
                            "blockers": ["writer_failed_rolled_back"],
                            "registered_live_draft": False,
                            "project_state_restored": bool(state_restore),
                        })
                        write_object(manifest_path, manifest)
                    except Exception:
                        pass
                raise RuntimeError(
                    f"Writer 失败，新增草稿与首页登记已可恢复回滚: {attempt_marker}"
                ) from error
        raise


if __name__ == "__main__":
    raise SystemExit(main())
