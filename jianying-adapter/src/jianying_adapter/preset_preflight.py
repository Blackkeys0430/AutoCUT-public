from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping

from PIL import Image


OVERLAP_POLICIES = {
    "avoid_key_features",
    "intentional_overlap",
    "not_applicable",
}
INTENTIONAL_OVERLAP_SOURCES = {
    "user_current_request",
    "verified_reference",
    "accepted_template_behavior",
    "current_video_plan",
}
EDITABLE_TEXT_ROLES = frozenset({"main_emphasis", "supporting"})
NON_EDITABLE_TEXT_ROLES = frozenset({"fixed_symbol", "decorative"})
TEXT_ROLE_ALIASES = {
    # Plans written before role layering was introduced used these two
    # labels.  Keep the mapping explicit so old plans do not silently change
    # their gate semantics.
    "editable": "main_emphasis",
    "fixed_decorative": "decorative",
}
REAL_CURRENT_VIDEO_EVIDENCE_MODES = frozenset({
    "current_video_frontend",
    "current_video_render",
})


def normalize_text_role(value: Any) -> str:
    role = str(value or "").strip()
    return TEXT_ROLE_ALIASES.get(role, role)


@dataclass
class PresetPreflightReport:
    template_id: str
    current_video_ready: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.checks) and all(self.checks.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "template_id": self.template_id,
            "current_video_ready": self.current_video_ready,
            "checks": self.checks,
            "errors": self.errors,
            "warnings": self.warnings,
            "evidence": self.evidence,
        }


def records_by_id(registry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(record.get("template_id")): record
        for record in registry.get("records", [])
        if isinstance(record, Mapping) and record.get("template_id")
    }


def _phase_status(
    evidence: Mapping[str, Any],
    phase: str,
    *,
    verify_snapshot_files: bool,
    errors: list[str],
) -> bool:
    phases = evidence.get("render_phases") or {}
    value = phases.get(phase) if isinstance(phases, Mapping) else None
    if not isinstance(value, Mapping):
        errors.append(f"缺少 {phase} 阶段证据")
        return False
    snapshot = str(value.get("snapshot") or "").strip()
    if not snapshot:
        errors.append(f"{phase} 阶段缺少 snapshot")
        return False
    if verify_snapshot_files and not Path(snapshot).is_file():
        errors.append(f"{phase} 阶段 snapshot 不存在: {snapshot}")
        return False
    if verify_snapshot_files:
        try:
            with Image.open(snapshot) as image:
                image.verify()
        except Exception:
            errors.append(f"{phase} 阶段 snapshot 不是可验证图片: {snapshot}")
            return False
    evidence_mode = str(value.get("evidence_mode") or "").strip()
    if evidence_mode not in REAL_CURRENT_VIDEO_EVIDENCE_MODES:
        errors.append(
            f"{phase} 阶段 evidence_mode 必须是真实当前视频证据，当前为 {evidence_mode!r}"
        )
        return False
    if value.get("canvas_bounds") != "passed":
        errors.append(f"{phase} 阶段文字画布边界未通过")
        return False
    return True


def _distinct_phase_content(
    evidence: Mapping[str, Any],
    *,
    verify_snapshot_files: bool,
    errors: list[str],
) -> bool:
    if not verify_snapshot_files:
        return True
    phases = evidence.get("render_phases") or {}
    file_digests: list[str] = []
    pixel_digests: list[str] = []
    for phase in ("entry", "stable", "exit"):
        value = phases.get(phase) if isinstance(phases, Mapping) else None
        path = Path(str(value.get("snapshot") or "")) if isinstance(value, Mapping) else None
        if path is None or not path.is_file():
            return False
        file_digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
        try:
            with Image.open(path) as image:
                normalized = image.convert("RGB").resize((64, 64))
                pixel_digests.append(hashlib.sha256(normalized.tobytes()).hexdigest())
        except Exception:
            return False
    if (
        len(file_digests) != len(set(file_digests))
        or len(pixel_digests) != len(set(pixel_digests))
    ):
        errors.append("current_video_evidence 三阶段 snapshot 内容必须不同，禁止复制同一画面冒充")
        return False
    return True


def _phase_timing_contract(
    selection: Mapping[str, Any],
    evidence: Mapping[str, Any],
    errors: list[str],
) -> bool:
    """Bind entry/stable/exit images to three ordered points inside this node."""

    start_us = selection.get("start_us")
    end_us = selection.get("end_us")
    if (
        not isinstance(start_us, int)
        or isinstance(start_us, bool)
        or not isinstance(end_us, int)
        or isinstance(end_us, bool)
        or end_us <= start_us
    ):
        errors.append("当前节点缺少可用于绑定三阶段证据的 start_us/end_us")
        return False
    phases = evidence.get("render_phases") or {}
    times: list[int] = []
    ok = True
    for phase in ("entry", "stable", "exit"):
        value = phases.get(phase) if isinstance(phases, Mapping) else None
        timeline_time_us = value.get("timeline_time_us") if isinstance(value, Mapping) else None
        if not isinstance(timeline_time_us, int) or isinstance(timeline_time_us, bool):
            errors.append(f"{phase} 阶段必须声明 timeline_time_us")
            ok = False
            continue
        times.append(timeline_time_us)
        if timeline_time_us < start_us or timeline_time_us > end_us:
            errors.append(f"{phase} 阶段 timeline_time_us 不在当前节点范围内")
            ok = False
    if len(times) == 3 and not (times[0] < times[1] < times[2]):
        errors.append("entry/stable/exit 的 timeline_time_us 必须严格递增")
        ok = False
    return ok


def declared_track_texts(track: Mapping[str, Any]) -> list[str]:
    """Return declared segment-order texts, preserving legacy repeated text."""
    if "texts" not in track:
        return [str(track.get("text") or "")]
    values = track["texts"]
    count = track.get("segment_count")
    if ("text" in track or not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not value for value in values)
            or type(count) is not int or count != len(values)):
        raise ValueError("texts 必须是与 segment_count 等长的非空字符串数组，且不能同时声明 text")
    return list(values)


def _slot_contract(
    record: Mapping[str, Any],
    selection: Mapping[str, Any],
    errors: list[str],
) -> tuple[list[int], list[int]]:
    content = record.get("content_contract") or {}
    slots = [str(value) for value in selection.get("slots", [])]
    actual_tracks = selection.get("actual_text_tracks")
    track_map = selection.get("actual_text_track_map")
    explicit_limits = selection.get("actual_slot_capacities")
    if actual_tracks is not None:
        if not isinstance(actual_tracks, list) or not actual_tracks:
            errors.append("actual_text_tracks 必须是非空数组")
            return [], []
        names: list[str] = []
        texts: list[str] = []
        limits: list[int] = []
        editable: list[str] = []
        main_emphasis_count = 0
        for index, track in enumerate(actual_tracks):
            if not isinstance(track, Mapping):
                errors.append(f"actual_text_tracks[{index}] 必须是对象")
                continue
            name = str(track.get("track_name") or "").strip()
            try:
                track_texts = declared_track_texts(track)
            except ValueError as error:
                errors.append(f"actual_text_tracks[{index}]: {error}")
                track_texts = []
            role = normalize_text_role(track.get("role"))
            limit = track.get("max_chars")
            if not name:
                errors.append(f"actual_text_tracks[{index}] 缺少真实 track_name")
            if role not in EDITABLE_TEXT_ROLES | NON_EDITABLE_TEXT_ROLES:
                errors.append(f"actual_text_tracks[{index}].role 无效: {role!r}")
            if role == "main_emphasis":
                main_emphasis_count += 1
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                errors.append(f"actual_text_tracks[{index}].max_chars 必须是正整数")
                limit = 0
            names.append(name)
            texts.extend(track_texts)
            limits.extend([int(limit)] * len(track_texts))
            if role in EDITABLE_TEXT_ROLES:
                editable.extend(track_texts)
        if len(names) != len(set(names)):
            errors.append("actual_text_tracks.track_name 必须唯一")
        if main_emphasis_count == 0:
            errors.append("actual_text_tracks 必须至少包含一条 main_emphasis")
        if editable != slots:
            errors.append("slots 必须与 actual_text_tracks 中 editable 文字顺序一致")
        lengths = [len(value.replace("\n", "").replace(" ", "")) for value in texts]
        if any(length > limit for length, limit in zip(lengths, limits)):
            errors.append(f"文字超过真实轨容量: lengths={lengths}, limits={limits}")
        return lengths, limits
    if track_map is not None:
        if not isinstance(track_map, Mapping) or not track_map:
            errors.append("actual_text_track_map 必须是非空对象")
            return [], []
        mapped = [str(value) for value in track_map.values()]
        if slots and slots != mapped:
            errors.append("slots 与 actual_text_track_map 的文字顺序不一致")
        slots = mapped
        limits = [int(value) for value in (explicit_limits or [])]
        if len(limits) != len(slots):
            errors.append("显式逐轨映射必须同时提供等长 actual_slot_capacities")
            return [len(value.replace("\n", "")) for value in slots], limits
    else:
        expected = int(content.get("editable_text_slot_count") or 0)
        if len(slots) != expected:
            errors.append(f"文字槽数量不匹配: 当前 {len(slots)}，预设要求 {expected}")
        limits = [int(value) for value in content.get("conservative_max_chars_per_slot") or []]
    lengths = [len(value.replace("\n", "").replace(" ", "")) for value in slots]
    if len(lengths) != len(limits):
        errors.append(f"容量记录数量不匹配: 文字 {len(lengths)}，容量 {len(limits)}")
    elif any(length > limit for length, limit in zip(lengths, limits)):
        errors.append(f"文字超过槽位容量: lengths={lengths}, limits={limits}")
    return lengths, limits


def _overlap_contract(
    selection: Mapping[str, Any],
    evidence: Mapping[str, Any],
    errors: list[str],
) -> bool:
    context = selection.get("layout_context") or {}
    policy = str(context.get("subject_overlap_policy") or "")
    if policy not in OVERLAP_POLICIES:
        errors.append(f"未知 subject_overlap_policy: {policy!r}")
        return False
    subject = evidence.get("subject_overlap") or {}
    intersects = bool(subject.get("intersects_key_features"))
    if not intersects:
        return True
    if policy == "avoid_key_features":
        errors.append("当前节点要求避让人物关键识别区域，但证据显示发生遮挡")
        return False
    if policy == "not_applicable":
        errors.append("声明人物关系不适用，但证据显示文字进入人物关键识别区域")
        return False
    design_basis = str(context.get("design_basis") or "").strip()
    authorized_by = str(context.get("authorized_by") or "").strip()
    if not design_basis or authorized_by not in INTENTIONAL_OVERLAP_SOURCES:
        errors.append("有意遮挡必须记录设计依据和有效授权来源")
        return False
    return True


def _path_is_within(path_value: Any, root: Path) -> bool:
    try:
        Path(str(path_value)).resolve(strict=False).relative_to(root.resolve())
    except (TypeError, ValueError, OSError):
        return False
    return True


def _current_video_binding_contract(
    selection: Mapping[str, Any],
    evidence: Mapping[str, Any],
    expected: Mapping[str, Any],
    errors: list[str],
) -> bool:
    """Bind visual evidence to this project/node, not a reusable old sample."""

    ok = True
    project_id = str(expected.get("project_id") or "")
    if str(evidence.get("project_id") or "") != project_id:
        errors.append("current_video_evidence.project_id 未绑定当前项目")
        ok = False
    roughcut_sha = str(expected.get("roughcut_sha256") or "").upper()
    if str(evidence.get("roughcut_sha256") or "").upper() != roughcut_sha:
        errors.append("current_video_evidence.roughcut_sha256 未绑定当前粗剪")
        ok = False
    template_id = str(selection.get("template_id") or "")
    if str(evidence.get("template_id") or "") != template_id:
        errors.append(f"current_video_evidence.template_id 必须是当前预设: {template_id}")
        ok = False

    expected_texts = [str(item) for item in expected.get("final_texts") or []]
    actual_texts = evidence.get("final_texts")
    if not isinstance(actual_texts, list) or [str(item) for item in actual_texts] != expected_texts:
        errors.append("current_video_evidence.final_texts 与当前节点最终文字不一致")
        ok = False
    position = evidence.get("position")
    position_ok = isinstance(position, Mapping) and all(
        isinstance(position.get(axis), (int, float)) and not isinstance(position.get(axis), bool)
        for axis in ("x", "y")
    )
    if not position_ok:
        errors.append("current_video_evidence.position 必须包含当前节点 x/y")
        ok = False
    group_scale = evidence.get("group_scale")
    if not isinstance(group_scale, (int, float)) or isinstance(group_scale, bool) or group_scale <= 0:
        errors.append("current_video_evidence.group_scale 必须是正数")
        ok = False

    expected_position = expected.get("final_position")
    expected_scale = expected.get("final_group_scale")
    expected_position_ok = isinstance(expected_position, Mapping) and all(
        isinstance(expected_position.get(axis), (int, float))
        and not isinstance(expected_position.get(axis), bool)
        for axis in ("x", "y")
    )
    if not expected_position_ok:
        errors.append("当前节点计划缺少 final_position x/y")
        ok = False
    elif position_ok and any(
        not math.isclose(float(position[axis]), float(expected_position[axis]), rel_tol=1e-6, abs_tol=1e-6)
        for axis in ("x", "y")
    ):
        errors.append("current_video_evidence.position 与计划 final_position 不一致")
        ok = False
    if (
        not isinstance(expected_scale, (int, float))
        or isinstance(expected_scale, bool)
        or expected_scale <= 0
    ):
        errors.append("当前节点计划缺少正数 final_group_scale")
        ok = False
    elif isinstance(group_scale, (int, float)) and not isinstance(group_scale, bool) and not math.isclose(
        float(group_scale), float(expected_scale), rel_tol=1e-6, abs_tol=1e-6
    ):
        errors.append("current_video_evidence.group_scale 与计划 final_group_scale 不一致")
        ok = False

    project_root = expected.get("project_root")
    if not isinstance(project_root, (str, Path)):
        errors.append("当前视频证据缺少 project_root 绑定")
        return False
    snapshots: list[str] = []
    for phase, value in (evidence.get("render_phases") or {}).items():
        if isinstance(value, Mapping) and not _path_is_within(value.get("snapshot"), Path(project_root)):
            errors.append(f"{phase} 阶段 snapshot 不属于当前项目目录")
            ok = False
        if isinstance(value, Mapping):
            snapshot = str(value.get("snapshot") or "")
            if snapshot:
                snapshots.append(snapshot)
    if len(snapshots) != len(set(snapshots)):
        errors.append("current_video_evidence 三阶段 snapshot 必须使用不同文件")
        ok = False
    for key in ("current_text_bounds_evidence", "source_snapshot"):
        value = evidence.get(key)
        if value and not _path_is_within(value, Path(project_root)):
            errors.append(f"current_video_evidence.{key} 不属于当前项目目录")
            ok = False
    return ok


def preflight_selection(
    selection: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    target_canvas: tuple[int, int],
    verify_snapshot_files: bool = True,
    expected_binding: Mapping[str, Any] | None = None,
    require_current_video_evidence: bool = True,
) -> PresetPreflightReport:
    template_id = str(selection.get("template_id") or "")
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}
    record = records_by_id(registry).get(template_id)
    checks["registered"] = record is not None
    if record is None:
        errors.append(f"预设未进入统一使用注册表: {template_id}")
        return PresetPreflightReport(template_id, False, errors, warnings, checks)
    gate = str((record.get("technical_gate") or {}).get("production_gate") or "")
    checks["not_rejected"] = gate != "disabled_visual_rejected"
    if not checks["not_rejected"]:
        errors.append("预设已被视觉淘汰")
    expected_category = str(record.get("primary_category") or "")
    supplied_category = str(selection.get("category") or "")
    checks["semantic_category"] = supplied_category == expected_category
    if not checks["semantic_category"]:
        errors.append(f"语义类别不匹配: {supplied_category!r} != {expected_category!r}")
    lengths, limits = _slot_contract(record, selection, errors)
    checks["declared_text_limit"] = len(lengths) == len(limits) and all(
        length <= limit for length, limit in zip(lengths, limits)
    )
    evidence = selection.get("current_video_evidence") or {}
    if require_current_video_evidence:
        checks["declared_current_video_ready"] = evidence.get("current_video_ready") is True
        if not checks["declared_current_video_ready"]:
            errors.append("current_video_evidence.current_video_ready 必须显式为 true")
        if expected_binding is not None:
            checks["current_video_binding"] = _current_video_binding_contract(
                selection, evidence, expected_binding, errors
            )
        canvas = evidence.get("canvas") or {}
        checks["canvas"] = (
            int(canvas.get("width") or 0), int(canvas.get("height") or 0)
        ) == target_canvas
        if not checks["canvas"]:
            errors.append(f"当前视频画布证据不匹配: {canvas} != {target_canvas}")
        checks["resources"] = evidence.get("resources") == "passed"
        if not checks["resources"]:
            errors.append("当前视频资源完整性未通过")
        for phase in ("entry", "stable", "exit"):
            checks[f"phase_{phase}"] = _phase_status(
                evidence,
                phase,
                verify_snapshot_files=verify_snapshot_files,
                errors=errors,
            )
        checks["phase_timing"] = _phase_timing_contract(selection, evidence, errors)
        checks["distinct_phase_content"] = _distinct_phase_content(
            evidence,
            verify_snapshot_files=verify_snapshot_files,
            errors=errors,
        )
        stable = (evidence.get("render_phases") or {}).get("stable") or {}
        checks["caption_collision"] = stable.get("caption_collision") in {"clear", "not_applicable"}
        if not checks["caption_collision"]:
            errors.append("稳定阅读阶段与普通字幕的关系未解决")
        checks["subject_overlap_context"] = _overlap_contract(selection, evidence, errors)
    else:
        # Preview validates the immutable template/slot contract, while
        # explicitly recording that runtime visual evidence is deferred to
        # the seal phase.  It must never be interpreted as writer approval.
        checks["current_video_evidence_deferred"] = True
    native_min = float((record.get("timing_contract") or {}).get("recommended_minimum_node_seconds") or 0.0)
    duration_us = int(selection.get("node_duration_us") or 0)
    extension = str(selection.get("hold_policy") or "")
    checks["timing"] = duration_us >= int(native_min * 1_000_000) or extension == "extend_final_state_only"
    if not checks["timing"]:
        errors.append(f"节点停留不足: {duration_us / 1_000_000:.3f}s < {native_min:.3f}s")
    if gate != "guarded_production_eligible":
        warnings.append(f"预设全局状态为 {gate}；本次只能凭当前视频证据一次性放行")
    return PresetPreflightReport(
        template_id=template_id,
        current_video_ready=require_current_video_evidence and not errors and all(checks.values()),
        errors=errors,
        warnings=warnings,
        checks=checks,
        evidence={
            "category": expected_category,
            "production_gate": gate,
            "slot_lengths": lengths,
            "slot_limits": limits,
            "capacity_scope": "declared_character_limit_only; final glyph geometry must be measured from the built draft",
            "actual_text_tracks": selection.get("actual_text_tracks") or [],
            "layout_context": selection.get("layout_context") or {},
            "current_video_evidence": evidence,
        },
    )
