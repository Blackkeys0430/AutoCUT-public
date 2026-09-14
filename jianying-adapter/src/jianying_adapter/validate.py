from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .codec import DraftCodec
from .hashing import object_sha256


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.issues

    def add(self, code: str, message: str, path: str | None = None) -> None:
        self.issues.append(ValidationIssue(code, message, path))

    def merge(self, other: "ValidationReport") -> None:
        self.issues.extend(other.issues)
        self.facts.update(other.facts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [asdict(item) for item in self.issues],
            "facts": self.facts,
        }


def validate_reference_graph(
    *,
    content: dict[str, Any],
    meta: dict[str, Any],
    project: dict[str, Any],
    timeline_layout: dict[str, Any],
    timeline_directory_ids: Iterable[str],
    expected_draft_id: str | None = None,
) -> ValidationReport:
    report = ValidationReport()
    timeline_id = content.get("id")
    main_id = project.get("main_timeline_id")
    project_timeline_ids = [item.get("id") for item in project.get("timelines") or []]
    layout_ids = [
        timeline_id_value
        for dock in timeline_layout.get("dockItems") or []
        for timeline_id_value in dock.get("timelineIds") or []
    ]
    directories = sorted(set(timeline_directory_ids))
    checks = {
        "content_matches_main": timeline_id == main_id,
        "main_in_project_timelines": main_id in project_timeline_ids,
        "main_is_active": timeline_layout.get("activeTimeline") == main_id,
        "main_in_layout": main_id in layout_ids,
        "main_directory_exists": main_id in directories,
        "no_unreferenced_timeline_directory": set(directories) == set(project_timeline_ids),
    }
    if expected_draft_id is not None:
        checks["draft_id_matches"] = meta.get("draft_id") == expected_draft_id
    for name, passed in checks.items():
        if not passed:
            report.add("reference_graph_invalid", f"Reference graph check failed: {name}", name)
    if not project.get("id"):
        report.add("missing_project_id", "project.json has no project id", "project.id")
    if not meta.get("draft_id"):
        report.add("missing_draft_id", "draft meta has no draft id", "meta.draft_id")
    report.facts.update(
        {
            "draft_id": meta.get("draft_id"),
            "project_id": project.get("id"),
            "timeline_content_id": timeline_id,
            "timeline_directories": directories,
            "checks": checks,
        }
    )
    return report


def validate_content_mirrors(
    expected: dict[str, Any],
    mirrors: Sequence[str | Path],
    codec: DraftCodec,
) -> ValidationReport:
    report = ValidationReport()
    expected_hash = object_sha256(expected)
    mirror_facts: list[dict[str, Any]] = []
    for mirror in mirrors:
        path = Path(mirror)
        if not path.is_file():
            report.add("mirror_missing", "Content mirror does not exist", str(path))
            continue
        try:
            decoded = codec.read(path)
        except Exception as exc:  # audit boundary: preserve exact failure in report
            report.add("mirror_decode_failed", f"{type(exc).__name__}: {exc}", str(path))
            continue
        value_hash = object_sha256(decoded.value)
        mirror_facts.append({"path": str(path), "object_sha256": value_hash, "encoded": decoded.encoded})
        if value_hash != expected_hash or decoded.value != expected:
            report.add("mirror_mismatch", "Content mirror differs from expected object", str(path))
    report.facts["content_object_sha256"] = expected_hash
    report.facts["mirrors"] = mirror_facts
    return report


def validate_timeline_directories(
    draft_root: str | Path,
    project: dict[str, Any],
) -> ValidationReport:
    """Verify that every declared timeline has a non-empty on-disk content mirror."""
    report = ValidationReport()
    timelines_root = Path(draft_root) / "Timelines"
    declared_ids = [str(item.get("id")) for item in project.get("timelines") or [] if item.get("id")]
    actual_ids = sorted(item.name for item in timelines_root.iterdir() if item.is_dir()) if timelines_root.is_dir() else []
    for timeline_id in declared_ids:
        directory = timelines_root / timeline_id
        mirror = directory / "draft_content.json"
        if not directory.is_dir():
            report.add("timeline_directory_missing", "Declared timeline directory is missing", str(directory))
        elif not mirror.is_file() or mirror.stat().st_size == 0:
            report.add("timeline_content_missing", "Timeline content mirror is missing or empty", str(mirror))
    for timeline_id in sorted(set(actual_ids) - set(declared_ids)):
        report.add(
            "timeline_directory_unreferenced",
            "Timeline directory is not declared by project.json",
            str(timelines_root / timeline_id),
        )
    report.facts["declared_timeline_ids"] = declared_ids
    report.facts["actual_timeline_directory_ids"] = actual_ids
    return report


MEDIA_KEYS = {"path", "file_path", "filepath", "file_Path"}

_SUBTITLE_NAME_MARKERS = ("subtitle", "caption", "字幕", "ordinary_zh", "ordinary_en", "jy_zh", "jy_en")
_VISUAL_TRACK_TYPES = {"video", "image", "sticker", "compound", "adjustment", "filter"}


def _track_has_segments(track: dict[str, Any]) -> bool:
    segments = track.get("segments")
    return isinstance(segments, list) and bool(segments)


def _is_subtitle_track(track: dict[str, Any]) -> bool:
    if str(track.get("type", "")).casefold() != "text" or not _track_has_segments(track):
        return False
    name = str(track.get("name", "")).casefold()
    return any(marker in name for marker in _SUBTITLE_NAME_MARKERS)


def validate_subtitle_track_order(content: dict[str, Any]) -> ValidationReport:
    """Ensure populated named subtitle tracks are above populated visual tracks.

    Jianying's draft convention is that later entries in ``tracks`` render on
    top. Empty visual tracks are intentionally ignored; unnamed/other text
    tracks may be packaging text and are not treated as ordinary subtitles.
    """

    report = ValidationReport()
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        return report
    subtitle_indices = [index for index, track in enumerate(tracks) if isinstance(track, dict) and _is_subtitle_track(track)]
    visual_indices = [
        index
        for index, track in enumerate(tracks)
        if isinstance(track, dict)
        and _track_has_segments(track)
        and str(track.get("type", "")).casefold() in _VISUAL_TRACK_TYPES
    ]
    report.facts["subtitle_track_indices"] = subtitle_indices
    report.facts["populated_visual_track_indices"] = visual_indices
    if subtitle_indices and visual_indices:
        highest_visual = max(visual_indices)
        for subtitle_index in subtitle_indices:
            if subtitle_index < highest_visual:
                track = tracks[subtitle_index]
                report.add(
                    "subtitle_track_below_visual",
                    "Populated subtitle track must be above all populated visual tracks",
                    f"tracks[{subtitle_index}].name={track.get('name', '')}",
                )
    return report


def _media_paths(value: Any, prefix: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if key in MEDIA_KEYS and isinstance(item, str) and item and not item.startswith("./"):
                yield path, item
            else:
                yield from _media_paths(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _media_paths(item, f"{prefix}[{index}]")


def validate_media_exists(
    *documents: dict[str, Any],
    exists: Callable[[str], bool] | None = None,
) -> ValidationReport:
    exists_fn = exists or (lambda value: Path(value).is_file())
    report = ValidationReport()
    seen: set[str] = set()
    missing: list[str] = []
    for document in documents:
        for path, value in _media_paths(document):
            if value in seen:
                continue
            seen.add(value)
            if not exists_fn(value):
                missing.append(value)
                report.add("media_missing", f"Referenced media is missing: {value}", path)
    report.facts["referenced_media_count"] = len(seen)
    report.facts["missing_media"] = missing
    return report
