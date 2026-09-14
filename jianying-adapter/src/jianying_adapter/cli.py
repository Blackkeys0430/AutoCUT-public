# Copyright (c) 2026 Blackkeys0430 — AutoCUT original project code.
# Origin: https://github.com/Blackkeys0430/AutoCUT-public
# SPDX-License-Identifier: LicenseRef-AutoCUT-Personal-Use-1.0
from __future__ import annotations

import argparse
from . import __version__
from .identity import AUTHOR, PROJECT_URL, ORIGIN_ID, ORIGIN_REFERENCE_COMMIT
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .backup import create_complete_backup
from .candidate_plan import (
    CandidateAssembler,
    get_shared_operation_registry,
    read_json as read_candidate_json,
    validate_candidate_plan,
)
from .shared_operations import shared_final_validator
from .semantic_evidence.content_gate import validate_content_gate
from .codec import DraftCodec, JsonCodec, LazyJianying11DllProvider
from .guardrails import ensure_process_stopped, ensure_unlocked
from .hashing import object_sha256, tree_fingerprint
from .media_ledger import DEFAULT_POLICY, load_json, validate_media_ledger
from .project_state import validate_project_state
from .jianying_launch import exact_process_identifier, verify_exact_window_app
from .timeline_view import render_timeline_view
from .transcript import pack_transcript_file
from .writer import apply_subtitle_plan
from .validate import (
    validate_content_mirrors,
    validate_media_exists,
    validate_reference_graph,
    validate_subtitle_track_order,
    validate_timeline_directories,
)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _codec(args: argparse.Namespace) -> DraftCodec:
    if args.codec == "json":
        return JsonCodec()
    if not args.writer_source or not args.install_dir:
        raise ValueError("--codec jianying11 requires --writer-source and --install-dir")
    return LazyJianying11DllProvider(args.writer_source, args.install_dir).load()


def _read_pair(draft: Path, codec: DraftCodec) -> tuple[dict[str, Any], dict[str, Any], dict[str, bool]]:
    content = codec.read(draft / "draft_content.json")
    meta = codec.read(draft / "draft_meta_info.json")
    return content.value, meta.value, {"content_encoded": content.encoded, "meta_encoded": meta.encoded}


def command_probe(args: argparse.Namespace) -> int:
    draft = args.draft.resolve()
    codec = _codec(args)
    content, meta, flags = _read_pair(draft, codec)
    tracks = content.get("tracks") or []
    materials = content.get("materials") or {}
    _print(
        {
            "operation": "probe",
            "read_only": True,
            "draft": str(draft),
            **flags,
            "draft_id": meta.get("draft_id"),
            "project_name": meta.get("draft_name"),
            "timeline_content_id": content.get("id"),
            "track_count": len(tracks),
            "material_counts": {
                key: len(value) for key, value in sorted(materials.items()) if isinstance(value, list)
            },
            "content_object_sha256": object_sha256(content),
            "meta_object_sha256": object_sha256(meta),
            "tree": tree_fingerprint(draft).to_dict(),
        }
    )
    return 0


def _timeline_dirs(draft: Path) -> list[str]:
    timelines = draft / "Timelines"
    if not timelines.is_dir():
        return []
    return sorted(item.name for item in timelines.iterdir() if item.is_dir())


def command_audit(args: argparse.Namespace) -> int:
    draft = args.draft.resolve()
    codec = _codec(args)
    content, meta, flags = _read_pair(draft, codec)
    project = json.loads((draft / "Timelines" / "project.json").read_text(encoding="utf-8-sig"))
    layout = json.loads((draft / "timeline_layout.json").read_text(encoding="utf-8-sig"))
    graph = validate_reference_graph(
        content=content,
        meta=meta,
        project=project,
        timeline_layout=layout,
        timeline_directory_ids=_timeline_dirs(draft),
    )
    mirrors = [draft / "draft_content.json"]
    main_id = project.get("main_timeline_id")
    if main_id:
        mirrors.append(draft / "Timelines" / str(main_id) / "draft_content.json")
    mirror_report = validate_content_mirrors(content, mirrors, codec)
    timeline_report = validate_timeline_directories(draft, project)
    media = validate_media_exists(content, meta)
    subtitle_order = validate_subtitle_track_order(content)
    graph.merge(mirror_report)
    graph.merge(timeline_report)
    graph.merge(media)
    graph.merge(subtitle_order)
    payload = {
        "operation": "audit",
        "read_only": True,
        "draft": str(draft),
        **flags,
        **graph.to_dict(),
    }
    _print(payload)
    return 0 if graph.ok else 1


def command_backup(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    destination = args.destination.resolve()
    ensure_process_stopped()
    ensure_unlocked(source)
    result = create_complete_backup(source, destination)
    _print(
        {
            "operation": "backup",
            "source": result.source,
            "backup": result.backup,
            "manifest": result.manifest_path,
            "restore_plan": result.restore_plan_path,
            "source_fingerprint": result.source_fingerprint.to_dict(),
            "backup_fingerprint": result.backup_fingerprint.to_dict(),
            "source_deleted": False,
        }
    )
    return 0


def command_apply(args: argparse.Namespace) -> int:
    result = apply_subtitle_plan(
        args.draft,
        args.plan,
        args.backup,
        _codec(args),
        allowed_root=args.allowed_root,
    )
    _print(result)
    return 0


def command_pack_transcript(args: argparse.Namespace) -> int:
    output = pack_transcript_file(args.input, args.output, args.silence_threshold)
    _print({"operation": "pack-transcript", "input": str(args.input.resolve()), "output": str(output.resolve()), "silence_threshold": args.silence_threshold})
    return 0


def command_timeline_view(args: argparse.Namespace) -> int:
    output = render_timeline_view(
        args.video,
        args.start,
        args.end,
        args.output,
        transcript=args.transcript,
        n_frames=args.n_frames,
        ffmpeg=args.ffmpeg,
    )
    _print({"operation": "timeline-view", "video": str(args.video.resolve()), "start": args.start, "end": args.end, "output": str(output.resolve()), "n_frames": args.n_frames})
    return 0


def command_audit_media_ledger(args: argparse.Namespace) -> int:
    ledger_path = args.ledger.resolve()
    policy_path = args.policy.resolve()
    report = validate_media_ledger(
        load_json(ledger_path),
        policy=load_json(policy_path),
        verify_files=not args.skip_file_check,
    )
    _print(
        {
            "operation": "audit-media-ledger",
            "read_only": True,
            "ledger": str(ledger_path),
            "policy": str(policy_path),
            **report.to_dict(),
        }
    )
    return 0 if report.ok else 1


def command_material(args: argparse.Namespace) -> int:
    from .material_library import generation_brief, material_handoff, material_frame, process_material, register_material, search_materials
    output = args.output.resolve() if args.output else None
    if output:
        from pathlib import PureWindowsPath
        if PureWindowsPath(str(output)).drive.casefold() == "c:":
            raise ValueError("素材系统输出不可写入 C 盘")
        if output.exists():
            raise FileExistsError(f"输出文件已存在，请使用新文件名: {output}")
    plan_path = args.plan.resolve() if getattr(args, 'plan', None) else None
    if plan_path and (not output or output.parent != plan_path.parent):
        raise ValueError('material-use --plan 必须 --output 到原计划同目录的新文件，保留相对路径含义')
    request = load_json(args.request)
    config = load_json(args.config)
    if args.material_action == "search":
        result = search_materials(request, config)
    elif args.material_action == "generation-brief":
        result = generation_brief(request)
    elif args.material_action == "register":
        result = register_material(request, load_json(args.details), config)
    elif args.material_action == "process":
        result = process_material(request, load_json(args.details), config)
    elif args.material_action == 'frame':
        result = material_frame(request, load_json(args.details), config)
    else:
        result = material_handoff(args.request.resolve(), load_json(args.details), config)
        if plan_path:
            from .material_library import attach_material_handoff
            result = attach_material_handoff(load_json(plan_path), plan_path, result)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    _print({'operation': 'material-use', 'plan_output': str(output), 'writer_allowed': False}
           if plan_path else result)
    return 0


def command_preset_select(args: argparse.Namespace) -> int:
    from .preset_registry import preset_request_from_plan, select_template
    request = preset_request_from_plan(load_json(args.plan), args.event)
    if args.top_k is not None:
        request['top_k'] = args.top_k
    result = select_template(load_json(args.registry), request)
    _save_creative_output(args.output, result)
    _print(result)
    return 0


def command_creative_batch(args: argparse.Namespace) -> int:
    from .creative_batch import run_batch
    result = run_batch(args.plan, args.matrix, args.output, resume=args.resume)
    _print(result)
    return 0 if result['ok'] else 1


def _save_creative_output(output, value, *, plan=None):
    from pathlib import PureWindowsPath
    if output is None:
        return
    output = output.resolve()
    if PureWindowsPath(str(output)).drive.casefold() == 'c:':
        raise ValueError('创作交接文件不可写入 C 盘')
    if plan and output.parent != plan.resolve().parent:
        raise ValueError('新计划须保存在原计划同目录，以保留相对路径含义')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def command_preset_use(args: argparse.Namespace) -> int:
    from .preset_registry import attach_preset_selection
    result = attach_preset_selection(load_json(args.plan), load_json(args.query), load_json(args.decision))
    _save_creative_output(args.output, result, plan=args.plan)
    _print({'plan_output': str(args.output.resolve()), 'event_id': load_json(args.decision)['event_id'],
            'next_action': '用新计划继续下一事件的 preset-select；按选择记录绑定预设操作后统一装配'})
    return 0


def command_creative_review(args: argparse.Namespace) -> int:
    from .attention_review import review_attention, storyboard
    from .creative_plan import validate_creative_plan
    from .material_library import plan_material_requests, validate_plan_materials
    from .preset_registry import validate_preset_selections
    from .visual_planning import summarize_visual_sequence, validate_visual_execution
    plan = load_json(args.plan)
    if args.storyboard:
        from pathlib import PureWindowsPath
        if PureWindowsPath(str(args.storyboard.resolve())).drive.casefold() == 'c:':
            raise ValueError('分镜摘要不可写入C盘')
        args.storyboard.parent.mkdir(parents=True, exist_ok=True)
        with args.storyboard.open('x', encoding='utf-8') as handle:
            handle.write(storyboard(plan))
    result = {'creative_plan': validate_creative_plan(plan, plan_path=args.plan, require=True),
              'attention_review': review_attention(plan),
              'preset_choices': validate_preset_selections(plan, require_choices=plan.get('creative_brief') is not None),
              'material_requests': plan_material_requests(plan, args.plan),
              'scope': ('当前设计与实际装配事实；不替代内容门禁、原生播放或用户判断' if args.draft
                        else '规划缺口清单；允许未绑定需求，ok不代表装配或写入资格')}
    if args.draft:
        draft = load_json(args.draft)
        result['material_delivery'] = validate_plan_materials(plan, args.plan, draft)
        result['visual_execution'] = validate_visual_execution(plan, draft, plan_path=args.plan)
        result['sequence_summary'] = summarize_visual_sequence(plan, draft)
        result['attention_review'] = result['sequence_summary']['attention_review']
    result['ok'] = all(row.get('ok', True) for row in result.values() if isinstance(row, dict))
    _save_creative_output(args.output, result)
    _print(result)
    # Planning inspection lists unfinished work; only draft review is pass/fail.
    # CandidateAssembler and Writer still validate all required delivery bindings.
    return 0 if not args.draft or result['ok'] else 1


def command_material_requests(args: argparse.Namespace) -> int:
    from .material_library import plan_material_requests
    report = plan_material_requests(load_json(args.plan), args.plan.resolve())
    _print(report)
    return 0 if report['ok'] else 1


def command_validate_project_state(args: argparse.Namespace) -> int:
    path = args.state.resolve()
    report = validate_project_state(
        read_candidate_json(path),
        state_path=path,
        verify_artifacts=args.verify_artifacts,
        require_canonical_status=not args.allow_legacy_status,
    )
    _print({"operation": "validate-project-state", "state": str(path), **report.to_dict()})
    return 0 if report.ok else 1


def command_validate_candidate_plan(args: argparse.Namespace) -> int:
    path = args.plan.resolve()
    report = validate_candidate_plan(
        read_candidate_json(path),
        plan_path=path,
        verify_snapshot_files=not args.skip_snapshot_file_check,
    )
    _print({"operation": "validate-candidate-plan", "plan": str(path), **report.to_dict()})
    return 0 if report.ok else 1


def command_candidate_preview(args: argparse.Namespace) -> int:
    assembler = CandidateAssembler(
        args.plan.resolve(), get_shared_operation_registry(),
        final_validator=shared_final_validator,
    )
    _print({"operation": "candidate-preview", "read_only_writer": True, **assembler.preview()})
    return 0


def command_candidate_seal(args: argparse.Namespace) -> int:
    assembler = CandidateAssembler(
        args.plan.resolve(), get_shared_operation_registry(),
        final_validator=shared_final_validator,
    )
    _print({"operation": "candidate-seal", "writer_allowed": True, **assembler.seal()})
    return 0


def command_validate_content_gate(args: argparse.Namespace) -> int:
    """Read a content-gate JSON payload and validate its real files."""
    path = args.payload.resolve()
    payload = read_candidate_json(path)
    semantic = payload.get("semantic_evidence")
    if isinstance(semantic, dict):
        for key in ("rough_cut_path",):
            if semantic.get(key):
                candidate = Path(str(semantic[key]))
                if not candidate.is_absolute():
                    semantic[key] = str((path.parent / candidate).resolve())
        if isinstance(semantic.get("evidence_paths"), list):
            semantic["evidence_paths"] = [
                str((path.parent / Path(str(item))).resolve()) if not Path(str(item)).is_absolute() else str(item)
                for item in semantic["evidence_paths"]
            ]
    report = validate_content_gate(payload)
    _print({"operation": "validate-content-gate", "payload": str(path), **report})
    return 0 if report.get("ok") else 1


def command_jianying_8_8_launch_contract(args: argparse.Namespace) -> int:
    expected_exe = (args.install_dir / "JianyingPro.exe").resolve(strict=False)
    if args.window_app is None:
        payload = {
            "operation": "jianying-8-8-launch-contract",
            "ok": True,
            "launch_target": exact_process_identifier(expected_exe),
            "optional_diagnostic": "首次配置或目标窗口不明确时，可用 --window-app 核对；日常复用已确认入口，以实际操作结果验收",
        }
    else:
        payload = {
            "operation": "jianying-8-8-launch-contract",
            **verify_exact_window_app(args.window_app, expected_exe),
        }
    _print(payload)
    return 0 if payload["ok"] else 1


def _codec_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codec", choices=("json", "jianying11"), default="json")
    parser.add_argument("--writer-source", type=Path)
    parser.add_argument("--install-dir", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jianying-adapter",
        description="Probe, audit, back up, and apply bilingual subtitle plans to Jianying drafts.",
        epilog="AutoCUT by Blackkeys0430 | https://github.com/Blackkeys0430/AutoCUT-public",
    )
    parser.add_argument(
        '--version', action='version',
        version=(f'AutoCUT {__version__} | {AUTHOR}\n'
                 f'Origin: {PROJECT_URL}\n'
                 f'Origin ID: {ORIGIN_ID}\n'
                 f'Origin reference (not build revision): {ORIGIN_REFERENCE_COMMIT}'),
    )
    parser.add_argument('--timing-log', type=Path, help='Append command timing to a non-C .jsonl file')
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="Read content/meta and print a structural summary")
    probe.add_argument("draft", type=Path)
    _codec_options(probe)
    probe.set_defaults(handler=command_probe)

    audit = sub.add_parser("audit", help="Audit IDs, timeline mirrors, and referenced media")
    audit.add_argument("draft", type=Path)
    _codec_options(audit)
    audit.set_defaults(handler=command_audit)

    backup = sub.add_parser("backup", help="Create a complete copy, manifest, and restore plan")
    backup.add_argument("source", type=Path)
    backup.add_argument("destination", type=Path)
    backup.set_defaults(handler=command_backup)

    apply = sub.add_parser("apply", help="Back up once and apply one bilingual subtitle plan")
    apply.add_argument("draft", type=Path)
    apply.add_argument("plan", type=Path)
    apply.add_argument("backup", type=Path)
    apply.add_argument("--allowed-root", type=Path, required=True)
    _codec_options(apply)
    apply.set_defaults(handler=command_apply)

    pack = sub.add_parser("pack-transcript", help="Pack word-timed Whisper JSON into timestamped Markdown")
    pack.add_argument("input", type=Path)
    pack.add_argument("-o", "--output", type=Path, required=True)
    pack.add_argument("--silence-threshold", type=float, default=0.5)
    pack.set_defaults(handler=command_pack_transcript)

    timeline = sub.add_parser("timeline-view", help="Render an on-demand filmstrip and waveform for one interval")
    timeline.add_argument("video", type=Path)
    timeline.add_argument("start", type=float)
    timeline.add_argument("end", type=float)
    timeline.add_argument("-o", "--output", type=Path, required=True)
    timeline.add_argument("--transcript", type=Path)
    timeline.add_argument("--n-frames", type=int, default=8)
    timeline.add_argument("--ffmpeg", default="ffmpeg")
    timeline.set_defaults(handler=command_timeline_view)

    media_ledger = sub.add_parser(
        "audit-media-ledger",
        help="Validate B-roll/audio provenance, license status, and frozen local files",
    )
    media_ledger.add_argument("ledger", type=Path)
    media_ledger.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    media_ledger.add_argument("--skip-file-check", action="store_true")
    media_ledger.set_defaults(handler=command_audit_media_ledger)

    from .material_library import DEFAULT_CONFIG
    for action in ("search", "generation-brief", "register", "process", 'frame', "use"):
        material = sub.add_parser(f"material-{action}", help=f"Shared material library: {action}")
        material.add_argument("request", type=Path)
        if action in {"register", "process", 'frame', "use"}:
            material.add_argument("details", type=Path, help="Selection or current-use JSON")
        if action == 'use':
            material.add_argument('--plan', type=Path, help='Attach to the current CandidatePlan; requires --output to a new file beside it')
        material.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        material.add_argument("-o", "--output", type=Path, help="Save JSON to a new non-C file")
        material.set_defaults(handler=command_material, material_action=action)

    material_requests = sub.add_parser('material-requests', help='Read pending visual demands from the same CandidatePlan')
    material_requests.add_argument('plan', type=Path)
    material_requests.set_defaults(handler=command_material_requests)

    preset_select = sub.add_parser('preset-select', help='Compare same-purpose presets using the current event and whole-film context')
    preset_select.add_argument('plan', type=Path)
    preset_select.add_argument('--event', required=True)
    preset_select.add_argument('--registry', type=Path,
                               default=Path(__file__).resolve().parents[2] / 'preset_catalog' / 'preset_usage_registry_v1.json')
    preset_select.add_argument('--top-k', type=int)
    preset_select.add_argument('-o', '--output', type=Path, help='Save the exact query to a new non-C file')
    preset_select.set_defaults(handler=command_preset_select)

    preset_use = sub.add_parser('preset-use', help='Persist a compared choice before querying the next event')
    preset_use.add_argument('plan', type=Path)
    preset_use.add_argument('query', type=Path)
    preset_use.add_argument('decision', type=Path)
    preset_use.add_argument('-o', '--output', type=Path, required=True)
    preset_use.set_defaults(handler=command_preset_use)

    batch = sub.add_parser('creative-batch', help='Batch existing handoffs into one current CandidatePlan with per-item resume')
    batch.add_argument('plan', type=Path)
    batch.add_argument('matrix', type=Path)
    batch.add_argument('-o', '--output', type=Path, required=True)
    batch.add_argument('--resume', action='store_true')
    batch.set_defaults(handler=command_creative_batch)

    creative_review = sub.add_parser('creative-review', help='Inspect observable needs and actual whole-film expression in the same pipeline')
    creative_review.add_argument('plan', type=Path)
    creative_review.add_argument('--draft', type=Path, help='Assembled readable native draft JSON')
    creative_review.add_argument('--storyboard', type=Path, help='Write a Markdown view of the current design; no approval gate')
    creative_review.add_argument('-o', '--output', type=Path)
    creative_review.set_defaults(handler=command_creative_review)

    project_state = sub.add_parser(
        "validate-project-state",
        help="Validate one video's confirmed/prohibited/pending/artifacts contract",
    )
    project_state.add_argument("state", type=Path)
    project_state.add_argument("--verify-artifacts", action="store_true")
    project_state.add_argument("--allow-legacy-status", action="store_true")
    project_state.set_defaults(handler=command_validate_project_state)

    candidate_plan = sub.add_parser(
        "validate-candidate-plan",
        help="Fail closed on state, semantic evidence, and selected-preset evidence",
    )
    candidate_plan.add_argument("plan", type=Path)
    candidate_plan.add_argument("--skip-snapshot-file-check", action="store_true")
    candidate_plan.set_defaults(handler=command_validate_candidate_plan)

    candidate_preview = sub.add_parser(
        "candidate-preview",
        help="Build a clean workspace preview; never authorizes the single Writer",
    )
    candidate_preview.add_argument("plan", type=Path)
    candidate_preview.set_defaults(handler=command_candidate_preview)

    candidate_seal = sub.add_parser(
        "candidate-seal",
        help="Seal a preview with real current-video evidence and authorize one Writer",
    )
    candidate_seal.add_argument("plan", type=Path)
    candidate_seal.set_defaults(handler=command_candidate_seal)

    content_gate = sub.add_parser(
        "validate-content-gate",
        help="Validate two real semantic evidence files and final subtitle coverage",
    )
    content_gate.add_argument("payload", type=Path)
    content_gate.set_defaults(handler=command_validate_content_gate)

    launch_contract = sub.add_parser(
        "jianying-8-8-launch-contract",
        help="Emit the only allowed CUA process target and verify the returned window app",
    )
    launch_contract.add_argument("--install-dir", type=Path, required=True)
    launch_contract.add_argument("--window-app")
    launch_contract.set_defaults(handler=command_jianying_8_8_launch_contract)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from datetime import datetime, timezone
    from time import perf_counter

    parser = build_parser()
    args = parser.parse_args(argv)
    timing = None
    exit_code = 2
    try:
        if args.timing_log is not None:
            log_path = args.timing_log.resolve()
            if log_path.drive.lower() == 'c:' or log_path.suffix.lower() != '.jsonl':
                raise ValueError('timing-log 必须为非C盘 .jsonl 文件')
            log_path.parent.mkdir(parents=True, exist_ok=True)
            timing = log_path.open('a', encoding='utf-8')
        started_at = datetime.now(timezone.utc).isoformat()
        started = perf_counter()
        exit_code = int(args.handler(args))
    except Exception as exc:
        _print({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if timing is not None:
            try:
                with timing:
                    timing.write(json.dumps({'command': args.command, 'started_at': started_at,
                                             'elapsed_seconds': round(perf_counter() - started, 6),
                                             'exit_code': exit_code}, ensure_ascii=False) + '\n')
            except OSError as exc:
                # A diagnostic failure must not invite rerunning a completed Writer/action.
                print(f'耗时记录写入失败，命令退出码保持 {exit_code}: {exc}', file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
