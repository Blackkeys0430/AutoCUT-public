"""Create one isolated Jianying 8.8 trial draft and apply a validated candidate.

The command deliberately has two phases so preset assembly can stay read-only:

1. ``create-base`` registers a new draft containing only the source video and
   exports its decrypted JSON to the workspace.
2. ``apply-candidate`` backs up that new draft, writes the assembled candidate,
   and lets pyJianYingDraft refresh native metadata/registration.

It never replaces an existing draft directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = REPO_ROOT / "vendor" / "pyJianYingDraft-source"
LOCAL_DEPS = REPO_ROOT / "vendor" / "python-deps"
sys.path.insert(0, str(LOCAL_DEPS))
sys.path.insert(0, str(VENDOR_ROOT))

from pyJianYingDraft import (  # noqa: E402
    DraftCryptoConfig,
    DraftFolder,
    JianyingDraftCryptoCodec,
    ScriptFile,
    TrackSpec,
    TrackType,
    VideoSegment,
    assets,
)
from pyJianYingDraft.draft_codec import (  # noqa: E402
    load_json_object_with_codec,
    write_json_object_with_codec,
)


def _codec(install_dir: Path) -> JianyingDraftCryptoCodec:
    return JianyingDraftCryptoCodec(
        DraftCryptoConfig(jy_install_dir=install_dir, backup=True)
    )


def _draft_root(user_data: Path) -> Path:
    return user_data / "Projects" / "com.lveditor.draft"


def _effective_draft_root(args: argparse.Namespace) -> Path:
    if args.draft_root is not None:
        # Keep the caller's lexical path.  Resolving here turns the C: junction
        # entry into the E: target and makes Jianying register a second identity.
        return Path(os.path.abspath(args.draft_root))
    return _draft_root(Path(os.path.abspath(args.user_data)))


def _storage_draft_root(args: argparse.Namespace) -> Path:
    logical = _effective_draft_root(args)
    physical = getattr(args, "physical_root", None)
    if physical is None:
        return logical
    physical = Path(physical).resolve(strict=True)
    if not logical.is_dir() or not os.path.samefile(logical, physical):
        raise ValueError("logical and physical draft roots must reference the same directory")
    return physical


def _open_draft_folder(args: argparse.Namespace, storage_root: Path, codec: object) -> DraftFolder:
    return DraftFolder(
        str(storage_root), content_codec=codec, user_data_path=str(args.user_data),
        registration_root_path=(str(_effective_draft_root(args))
                                if getattr(args, "physical_root", None) is not None else None),
    )


def _backup_file(source: Path, backup_dir: Path) -> None:
    if source.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup_dir / source.name)


def _without_native_render_indexes(value: object) -> object:
    """Ignore fields that pyJianYingDraft intentionally regenerates on save."""

    if isinstance(value, dict):
        return {
            key: _without_native_render_indexes(child)
            for key, child in value.items()
            if key not in {"render_index", "track_render_index"}
        }
    if isinstance(value, list):
        return [_without_native_render_indexes(child) for child in value]
    return value


def _restore_explicit_render_indexes(saved: dict[str, object], candidate: dict[str, object]) -> None:
    """Restore candidate render fields after DraftFolder.save regenerates them."""
    saved_tracks = saved.get("tracks") or []
    candidate_tracks = candidate.get("tracks") or []
    if not isinstance(saved_tracks, list) or not isinstance(candidate_tracks, list) or len(saved_tracks) != len(candidate_tracks):
        raise RuntimeError("saved draft track shape changed while preserving render indexes")
    for saved_track, candidate_track in zip(saved_tracks, candidate_tracks):
        if not isinstance(saved_track, dict) or not isinstance(candidate_track, dict):
            raise RuntimeError("saved draft track shape is invalid")
        saved_segments = saved_track.get("segments") or []
        candidate_segments = candidate_track.get("segments") or []
        if not isinstance(saved_segments, list) or not isinstance(candidate_segments, list) or len(saved_segments) != len(candidate_segments):
            raise RuntimeError("saved draft segment shape changed while preserving render indexes")
        for saved_segment, candidate_segment in zip(saved_segments, candidate_segments):
            if not isinstance(saved_segment, dict) or not isinstance(candidate_segment, dict):
                raise RuntimeError("saved draft segment shape is invalid")
            for key in ("render_index", "track_render_index"):
                if key in candidate_segment:
                    value = candidate_segment[key]
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise RuntimeError(f"candidate {key} must be an integer")
                    saved_segment[key] = value


COMPATIBILITY_HEADER_FIELDS = (
    "version",
    "new_version",
    "platform",
    "last_modified_platform",
)


def _copy_compatibility_header(
    target: dict[str, object], reference: dict[str, object]
) -> dict[str, object]:
    """Copy only the draft-reader compatibility header from a native draft."""

    result = dict(target)
    for field in COMPATIBILITY_HEADER_FIELDS:
        if field not in reference:
            raise ValueError(f"compatibility reference is missing {field!r}")
        result[field] = reference[field]
    return result


def _replace_official_audio_path(
    content: dict[str, object], *, effect_id: str, replacement: Path
) -> tuple[dict[str, object], list[str]]:
    """Replace only the cached path of one Jianying official sound effect."""

    if not replacement.is_file():
        raise FileNotFoundError(replacement)
    audios = (content.get("materials") or {}).get("audios")  # type: ignore[union-attr]
    if not isinstance(audios, list):
        raise ValueError("draft has no materials.audios list")

    updated = json.loads(json.dumps(content, ensure_ascii=False))
    updated_audios = updated["materials"]["audios"]
    old_paths: list[str] = []
    for material in updated_audios:
        if not isinstance(material, dict):
            continue
        if str(material.get("effect_id") or "") != effect_id:
            continue
        if str(material.get("source_platform") or "") != "1":
            raise ValueError(f"effect_id {effect_id} is not a Jianying official sound")
        old_paths.append(str(material.get("path") or ""))
        material["path"] = str(replacement)

    if not old_paths:
        raise ValueError(f"effect_id not found in draft: {effect_id}")
    return updated, old_paths


def create_base(args: argparse.Namespace) -> dict[str, str]:
    video = args.video.resolve()
    if not getattr(args, "compatibility_reference", None) and not getattr(args, "compatibility_reference_path", None):
        raise ValueError("compatibility_reference 或 compatibility_reference_path 至少提供一个")
    if not video.is_file():
        raise FileNotFoundError(video)
    if args.width <= 0 or args.height <= 0 or args.fps <= 0:
        raise ValueError("width, height and fps must be positive")

    draft_root = _storage_draft_root(args)
    live_draft = draft_root / args.name
    precreated_empty = args.use_existing_empty_dir
    if precreated_empty:
        try:
            existing_items = os.listdir(live_draft)
        except OSError as exc:
            raise FileNotFoundError(
                f"precreated draft directory is not accessible: {live_draft}"
            ) from exc
        if existing_items:
            raise FileExistsError(
                f"precreated draft directory is not empty: {live_draft}"
            )
    elif live_draft.exists():
        raise FileExistsError(f"refusing to replace existing draft: {live_draft}")

    _backup_file(draft_root / "root_meta_info.json", args.backup_dir)
    codec = _codec(args.install_dir)
    folder = _open_draft_folder(args, draft_root, codec)
    if precreated_empty:
        shutil.copy(
            assets.get_asset_path("DRAFT_META_TEMPLATE"),
            live_draft / "draft_meta_info.json",
        )
        script = ScriptFile(args.width, args.height, args.fps, True)
        script.save_path = str(live_draft / "draft_content.json")
        script = folder._registration.configure_script_file(
            script, args.name, is_new_draft=True
        )
    else:
        script = folder.create_draft(args.name, args.width, args.height, args.fps)
    video_track = script.append_track(TrackSpec(TrackType.video, name="原片"))
    script.add_segment(VideoSegment(str(video)), track=video_track)
    script.save()

    decoded, _ = load_json_object_with_codec(
        live_draft / "draft_content.json", content_codec=codec
    )
    explicit_reference = getattr(args, "compatibility_reference_path", None)
    reference_path = (
        Path(explicit_reference).resolve()
        if explicit_reference is not None
        else draft_root / args.compatibility_reference / "draft_content.json"
    )
    if not reference_path.is_file():
        raise FileNotFoundError(reference_path)
    reference, _ = load_json_object_with_codec(reference_path, content_codec=codec)
    decoded = _copy_compatibility_header(decoded, reference)
    write_json_object_with_codec(
        live_draft / "draft_content.json", decoded, content_codec=codec, indent=4
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(decoded, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "draft": str(live_draft),
        "base_content": str(args.output),
        "duration_us": str(decoded.get("duration", 0)),
        "draft_id": str(decoded.get("id", "")),
    }


def apply_candidate(args: argparse.Namespace) -> dict[str, str]:
    candidate = json.loads(args.candidate.read_text(encoding="utf-8-sig"))
    if not isinstance(candidate, dict):
        raise ValueError("candidate root must be a JSON object")
    if isinstance(candidate.get("draft"), dict):
        candidate = dict(candidate["draft"])

    draft_root = _storage_draft_root(args)
    live_draft = draft_root / args.name
    content_path = live_draft / "draft_content.json"
    if not content_path.is_file():
        raise FileNotFoundError(content_path)

    codec = _codec(args.install_dir)
    live_before, _ = load_json_object_with_codec(content_path, content_codec=codec)
    if candidate.get("id") != live_before.get("id"):
        if not args.allow_candidate_rebase:
            raise ValueError("candidate draft id differs from the registered base draft")
        candidate["id"] = live_before.get("id")
    candidate = _copy_compatibility_header(candidate, live_before)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_target = args.backup_dir / f"{args.name}_{timestamp}"
    shutil.copytree(live_draft, backup_target)

    write_json_object_with_codec(content_path, candidate, content_codec=codec, indent=4)
    mirror = live_draft / "template-2.tmp"
    if mirror.exists():
        write_json_object_with_codec(mirror, candidate, content_codec=codec, indent=4)

    folder = _open_draft_folder(args, draft_root, codec)
    folder.load_template(args.name).save()
    roundtrip = codec.decode(content_path.read_bytes())
    if _without_native_render_indexes(roundtrip) != _without_native_render_indexes(candidate):
        raise RuntimeError("live draft round-trip differs beyond native render-index normalization")
    _restore_explicit_render_indexes(roundtrip, candidate)
    expected_after_restore = json.loads(json.dumps(roundtrip))
    write_json_object_with_codec(content_path, roundtrip, content_codec=codec, indent=4)
    roundtrip = codec.decode(content_path.read_bytes())
    if roundtrip != expected_after_restore:
        raise RuntimeError("live draft differs from expected candidate after render-index restoration")
    # DraftFolder.save() may add native render indexes to the primary content
    # after the pre-save mirror was written.  Mirror the normalized primary
    # object only after that save so Jianying never sees two content variants.
    if mirror.exists():
        write_json_object_with_codec(mirror, roundtrip, content_codec=codec, indent=4)
        mirror_roundtrip = codec.decode(mirror.read_bytes())
        if mirror_roundtrip != roundtrip:
            raise RuntimeError("template-2 mirror differs from normalized live draft")
    return {
        "draft": str(live_draft),
        "backup": str(backup_target),
        "tracks": str(len(roundtrip.get("tracks", []))),
        "duration_us": str(roundtrip.get("duration", 0)),
        "content_mirrors_deep_equal": str(not mirror.exists() or codec.decode(mirror.read_bytes()) == roundtrip).lower(),
    }


def repair_compatibility(args: argparse.Namespace) -> dict[str, str]:
    draft_root = _effective_draft_root(args)
    live_draft = draft_root / args.name
    reference_path = draft_root / args.reference_draft / "draft_content.json"
    content_path = live_draft / "draft_content.json"
    if not content_path.is_file():
        raise FileNotFoundError(content_path)
    if not reference_path.is_file():
        raise FileNotFoundError(reference_path)

    codec = _codec(args.install_dir)
    current, _ = load_json_object_with_codec(content_path, content_codec=codec)
    reference, _ = load_json_object_with_codec(reference_path, content_codec=codec)
    repaired = _copy_compatibility_header(current, reference)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_target = args.backup_dir / f"{args.name}_compat_{timestamp}"
    shutil.copytree(live_draft, backup_target)
    write_json_object_with_codec(content_path, repaired, content_codec=codec, indent=4)
    mirror = live_draft / "template-2.tmp"
    if mirror.exists():
        write_json_object_with_codec(mirror, repaired, content_codec=codec, indent=4)

    roundtrip = codec.decode(content_path.read_bytes())
    changed = [
        field for field in COMPATIBILITY_HEADER_FIELDS
        if current.get(field) != roundtrip.get(field)
    ]
    non_header_before = {
        key: value for key, value in current.items()
        if key not in COMPATIBILITY_HEADER_FIELDS
    }
    non_header_after = {
        key: value for key, value in roundtrip.items()
        if key not in COMPATIBILITY_HEADER_FIELDS
    }
    if non_header_before != non_header_after:
        raise RuntimeError("compatibility repair changed non-header draft content")
    return {
        "draft": str(live_draft),
        "reference": str(reference_path.parent),
        "backup": str(backup_target),
        "changed_fields": ",".join(changed),
        "new_version": str(roundtrip.get("new_version", "")),
        "app_version": str(
            (roundtrip.get("last_modified_platform") or {}).get("app_version", "")
        ),
    }


def repair_audio_path(args: argparse.Namespace) -> dict[str, object]:
    draft_root = _effective_draft_root(args)
    live_draft = draft_root / args.name
    content_path = live_draft / "draft_content.json"
    if not content_path.is_file():
        raise FileNotFoundError(content_path)

    replacement = args.replacement.resolve()
    codec = _codec(args.install_dir)
    current, _ = load_json_object_with_codec(content_path, content_codec=codec)
    repaired, old_paths = _replace_official_audio_path(
        current,
        effect_id=args.effect_id,
        replacement=replacement,
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_target = args.backup_dir / f"{args.name}_audio_{timestamp}"
    shutil.copytree(live_draft, backup_target)

    write_json_object_with_codec(content_path, repaired, content_codec=codec, indent=4)
    mirror = live_draft / "template-2.tmp"
    if mirror.exists():
        write_json_object_with_codec(mirror, repaired, content_codec=codec, indent=4)

    roundtrip = codec.decode(content_path.read_bytes())
    verified, _ = _replace_official_audio_path(
        current,
        effect_id=args.effect_id,
        replacement=replacement,
    )
    if roundtrip != verified:
        raise RuntimeError("audio path repair changed fields other than the requested path")
    if mirror.exists() and codec.decode(mirror.read_bytes()) != roundtrip:
        raise RuntimeError("template-2 mirror differs after audio path repair")
    return {
        "draft": str(live_draft),
        "backup": str(backup_target),
        "effect_id": args.effect_id,
        "old_paths": old_paths,
        "replacement": str(replacement),
        "content_mirrors_deep_equal": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--user-data", type=Path, required=True)
    parser.add_argument("--draft-root", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-base")
    create.add_argument("--video", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--width", type=int, required=True)
    create.add_argument("--height", type=int, required=True)
    create.add_argument("--fps", type=int, default=30)
    create.add_argument(
        "--use-existing-empty-dir",
        action="store_true",
        help="仅使用由 Windows 原生命令预创建的空目录；目录非空时仍拒绝覆盖",
    )
    create.add_argument(
        "--compatibility-reference",
        required=False,
        help="能被当前剪映版本打开的原生草稿目录名",
    )
    create.add_argument(
        "--compatibility-reference-path",
        type=Path,
        help="兼容参考 draft_content.json 的显式路径；用于实时参考目录已迁移但已有双8.8备份的情况",
    )

    apply = subparsers.add_parser("apply-candidate")
    apply.add_argument("--candidate", type=Path, required=True)
    apply.add_argument(
        "--allow-candidate-rebase",
        action="store_true",
        help="将工作区候选的草稿根 ID 对齐到刚注册的新草稿，并继承其兼容头",
    )
    repair = subparsers.add_parser("repair-compatibility")
    repair.add_argument("--reference-draft", required=True)
    audio_repair = subparsers.add_parser("repair-audio-path")
    audio_repair.add_argument("--effect-id", required=True)
    audio_repair.add_argument("--replacement", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "create-base":
        result = create_base(args)
    elif args.command == "apply-candidate":
        result = apply_candidate(args)
    elif args.command == "repair-compatibility":
        result = repair_compatibility(args)
    else:
        result = repair_audio_path(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
