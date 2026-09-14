"""Minimal writer for the verified native Jianying animation recipes.

This module deliberately does not know about Jianying projects, registration,
GUI automation, or the legacy preset scanner.  It only applies a recipe's
native animation enum members and native keyframes to caller-owned segments.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .animation_recipes import AnimationRecipe, AnimationReference, _ensure_vendored_import_paths


_VALID_VISUAL_STATUS = frozenset({"unverified", "validated"})
_DIRECT_PROPERTIES = {
    "position_x": "position_x",
    "position_y": "position_y",
    "scale_x": "scale_x",
    "scale_y": "scale_y",
    "uniform_scale": "uniform_scale",
    "alpha": "alpha",
}


class AnimationWriterError(ValueError):
    """Base error for deterministic recipe writing failures."""


class PaidAnimationError(AnimationWriterError):
    """Raised when a recipe or resolved native animation is VIP/paid."""


class AnimationDurationError(AnimationWriterError):
    """Raised when an animation/keyframe range exceeds its target segment."""


class UnknownAnimationPropertyError(AnimationWriterError):
    """Raised when a keyframe specification contains an unknown property."""


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnimationWriterError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result):
        raise AnimationWriterError(f"{label} 必须是有限数字")
    return result


def _duration(segment: Any, duration_us: int | None, *, default_us: int | None = None) -> int:
    segment_duration = int(getattr(segment, "duration"))
    if segment_duration < 0:
        raise AnimationDurationError("片段时长不能为负数")
    result = segment_duration if duration_us is None and default_us is None else int(
        default_us if duration_us is None else duration_us
    )
    if result < 0 or result > segment_duration:
        raise AnimationDurationError(
            f"动画/关键帧时长 {result} 超出片段时长 {segment_duration}"
        )
    return result


def _check_reference(reference: AnimationReference) -> Any:
    if reference.paid:
        raise PaidAnimationError(f"动画 {reference.enum}.{reference.member} 已标记为付费")
    member = reference.resolve()
    if reference.is_vip or bool(getattr(member.value, "is_vip", False)):
        raise PaidAnimationError(f"动画 {reference.enum}.{reference.member} 是 VIP 动画")
    return member


def validate_recipe(recipe: AnimationRecipe) -> None:
    """Validate recipe policy and resolve every native enum before writing."""

    if recipe.visual_status not in _VALID_VISUAL_STATUS:
        raise AnimationWriterError(
            f"配方 {recipe.id} 的 visual_status 不允许写入: {recipe.visual_status}"
        )
    if recipe.paid:
        raise PaidAnimationError(f"配方 {recipe.id} 已标记为付费")
    if not recipe.animations:
        raise AnimationWriterError(f"配方 {recipe.id} 没有原生动画")
    for reference in recipe.animations:
        _check_reference(reference)


def apply_animation_reference(
    segment: Any,
    reference: AnimationReference,
    *,
    duration_us: int | None = None,
    script: Any | None = None,
) -> Any:
    """Resolve one native enum and attach it through ``segment.add_animation``.

    When ``script`` is provided, also register the animation material.  This
    matters when a segment was added to the timeline before its animation was
    attached: upstream pyJianYingDraft only performs that registration inside
    ``ScriptFile.add_segment``.
    """

    allowed_duration = _duration(segment, duration_us, default_us=reference.default_duration_us)
    member = _check_reference(reference)
    if not hasattr(segment, "add_animation"):
        raise AnimationWriterError("目标片段不支持 add_animation")
    segment.add_animation(member, duration=allowed_duration)
    if script is not None:
        register_animation_material(script, segment)
    return segment


def register_animation_material(script: Any, segment: Any) -> Any:
    """Synchronize one segment's native animation container into a script."""

    animation = getattr(segment, "animations_instance", None)
    if animation is None:
        raise AnimationWriterError("目标片段尚未挂载原生动画")
    materials = getattr(script, "materials", None)
    animations = getattr(materials, "animations", None)
    if materials is None or animations is None:
        raise AnimationWriterError("目标脚本不包含可写入的动画素材表")
    if animation not in materials:
        animations.append(animation)
    return segment


def apply_recipe_animations(
    recipe: AnimationRecipe,
    targets: Any | Sequence[Any],
    *,
    duration_us: int | None = None,
    script: Any | None = None,
) -> tuple[Any, ...]:
    """Apply a recipe's refs in declared order.

    A single target receives all refs, which is required for text intro+loop.
    A sequence maps one target to each ref, which is useful for two video
    layers in a composition recipe.
    """

    validate_recipe(recipe)
    if isinstance(targets, Sequence) and not isinstance(targets, (str, bytes)):
        resolved_targets = tuple(targets)
        if len(resolved_targets) != len(recipe.animations):
            raise AnimationWriterError(
                f"配方 {recipe.id} 需要 {len(recipe.animations)} 个目标片段，"
                f"实际收到 {len(resolved_targets)} 个"
            )
    else:
        resolved_targets = tuple(targets for _ in recipe.animations)

    for reference, target in zip(recipe.animations, resolved_targets):
        apply_animation_reference(target, reference, duration_us=duration_us, script=script)
    return resolved_targets


def _point_values(spec: Any, *, label: str) -> list[tuple[int, float]]:
    """Normalize ``from/to`` or explicit point-list keyframe syntax."""

    if isinstance(spec, Mapping) and "points" in spec:
        points = spec["points"]
        if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
            raise AnimationWriterError(f"{label}.points 必须是列表")
        result: list[tuple[int, float]] = []
        for index, point in enumerate(points):
            if not isinstance(point, Mapping):
                raise AnimationWriterError(f"{label}.points[{index}] 必须是对象")
            raw_offset = point.get("time_offset", point.get("offset"))
            if isinstance(raw_offset, bool) or not isinstance(raw_offset, int):
                raise AnimationWriterError(f"{label}.points[{index}].time_offset 必须是整数")
            result.append((raw_offset, _number(point.get("value"), f"{label}.points[{index}].value")))
        if not result:
            raise AnimationWriterError(f"{label}.points 不能为空")
        return result

    if isinstance(spec, Mapping) and "from" in spec and "to" in spec:
        return [(0, _number(spec["from"], f"{label}.from")), (None, _number(spec["to"], f"{label}.to"))]  # type: ignore[list-item]

    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return [(0, _number(spec, label))]
    raise AnimationWriterError(f"{label} 必须包含 from/to、points 或数字")


def _set_keyframes(segment: Any, property_name: str, points: list[tuple[int, float]], duration_us: int, *, value_range: tuple[float, float] | None = None) -> None:
    _ensure_vendored_import_paths()
    from pyJianYingDraft import KeyframeProperty

    normalized: list[tuple[int, float]] = []
    for offset, value in points:
        actual_offset = duration_us if offset is None else offset
        if actual_offset < 0 or actual_offset > duration_us:
            raise AnimationDurationError(
                f"关键帧 {property_name} 时间点 {actual_offset} 超出 [0, {duration_us}]"
            )
        if value_range is not None and not value_range[0] <= value <= value_range[1]:
            raise AnimationWriterError(
                f"关键帧 {property_name} 值 {value} 超出 {value_range}"
            )
        normalized.append((actual_offset, value))
    for offset, value in normalized:
        segment.add_keyframe(getattr(KeyframeProperty, property_name), offset, value)


def _compile_position(segment: Any, spec: Any, duration_us: int) -> None:
    if not isinstance(spec, Mapping):
        raise AnimationWriterError("position 必须是对象")
    if "from" in spec or "to" in spec:
        for axis in ("x", "y"):
            if axis in spec.get("from", {}) or axis in spec.get("to", {}):
                values = {
                    "from": spec.get("from", {}).get(axis),
                    "to": spec.get("to", {}).get(axis),
                }
                if values["from"] is None or values["to"] is None:
                    raise AnimationWriterError(f"position.{axis} 的 from/to 必须成对出现")
                _set_keyframes(segment, f"position_{axis}", _point_values(values, label=f"position.{axis}"), duration_us)
        return
    for axis in ("x", "y"):
        if axis in spec:
            _set_keyframes(segment, f"position_{axis}", _point_values(spec[axis], label=f"position.{axis}"), duration_us)
    if not any(axis in spec for axis in ("x", "y")):
        raise UnknownAnimationPropertyError(f"未知 position 属性: {sorted(spec)}")


def _compile_scale(segment: Any, spec: Any, duration_us: int) -> None:
    if isinstance(spec, Mapping) and not ("from" in spec or "to" in spec or "points" in spec):
        for axis in ("x", "y"):
            if axis in spec:
                _set_keyframes(segment, f"scale_{axis}", _point_values(spec[axis], label=f"scale.{axis}"), duration_us, value_range=(0.0, 10.0))
        if not any(axis in spec for axis in ("x", "y")):
            raise UnknownAnimationPropertyError(f"未知 scale 属性: {sorted(spec)}")
        return
    points = _point_values(spec, label="scale")
    _set_keyframes(segment, "scale_x", points, duration_us, value_range=(0.0, 10.0))
    _set_keyframes(segment, "scale_y", points, duration_us, value_range=(0.0, 10.0))


def compile_native_keyframes(
    segment: Any,
    keyframes: Mapping[str, Any],
    *,
    duration_us: int | None = None,
) -> Any:
    """Compile recipe keyframe data into native ``KeyframeProperty`` values."""

    if not isinstance(keyframes, Mapping):
        raise AnimationWriterError("native_keyframes 必须是对象")
    effective_duration = _duration(segment, duration_us)
    for property_name, spec in keyframes.items():
        if property_name == "position":
            _compile_position(segment, spec, effective_duration)
        elif property_name == "scale":
            _compile_scale(segment, spec, effective_duration)
        elif property_name == "alpha":
            _set_keyframes(segment, "alpha", _point_values(spec, label="alpha"), effective_duration, value_range=(0.0, 1.0))
        elif property_name in _DIRECT_PROPERTIES:
            _set_keyframes(segment, _DIRECT_PROPERTIES[property_name], _point_values(spec, label=property_name), effective_duration)
        else:
            raise UnknownAnimationPropertyError(f"未知关键帧属性: {property_name}")
    return segment


__all__ = [
    "AnimationDurationError",
    "AnimationWriterError",
    "PaidAnimationError",
    "UnknownAnimationPropertyError",
    "apply_animation_reference",
    "apply_recipe_animations",
    "compile_native_keyframes",
    "register_animation_material",
    "validate_recipe",
]
