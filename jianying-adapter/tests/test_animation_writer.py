from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

VENDOR = Path(__file__).resolve().parents[1] / "vendor"
sys.path.insert(0, str(VENDOR / "pyJianYingDraft-source"))
sys.path.insert(0, str(VENDOR / "python-deps"))

from pyJianYingDraft import (  # noqa: E402
    KeyframeProperty,
    ScriptFile,
    TextSegment,
    TextStyle,
    TrackSpec,
    TrackType,
    VideoMaterial,
    VideoSegment,
    trange,
)

from jianying_adapter.animation_recipes import AnimationReference, get_animation_recipe  # noqa: E402
from jianying_adapter.animation_writer import (  # noqa: E402
    AnimationDurationError,
    AnimationWriterError,
    PaidAnimationError,
    UnknownAnimationPropertyError,
    apply_animation_reference,
    apply_recipe_animations,
    compile_native_keyframes,
    register_animation_material,
)


def _text(duration: int = 3_000_000) -> TextSegment:
    return TextSegment("测试", trange(0, duration), style=TextStyle())


def _video(tmp_path: Path, duration: int = 3_000_000) -> VideoSegment:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"placeholder")
    material = object.__new__(VideoMaterial)
    material.material_id = "test-material"
    material.path = str(source)
    material.duration = duration
    material.width = 1920
    material.height = 1080
    material.material_name = source.name
    material.material_type = "video"
    material.local_material_id = ""
    material.crop_settings = object.__new__(type("Crop", (), {}))
    material.beauty_face_auto_preset_infos = []
    material.beauty_face_preset_infos = []
    material.beauty_body_preset_id = ""
    material.beauty_body_auto_preset = None
    material.beauty_face_auto_preset = {"name": "", "preset_id": "", "rate_map": "", "scene": ""}
    material.is_unified_beauty_mode = False
    material.check_flag = 63487
    # VideoSegment only needs material fields above for this unit-level writer test.
    return VideoSegment(material, trange(0, duration), volume=1.0)


def _animation_ref(enum: str, member: str, animation_type: str = "in") -> AnimationReference:
    return AnimationReference(enum, member, animation_type, 500_000)


def test_text_intro_then_loop_preserves_declared_order() -> None:
    recipe = get_animation_recipe("conclusion_impact")
    assert recipe is not None
    segment = _text()
    apply_recipe_animations(recipe, segment, duration_us=500_000)
    animations = segment.animations_instance.animations
    assert [item.animation_type for item in animations] == ["in", "loop"]
    assert [item.name for item in animations] == ["弹入", "跳动"]


def test_video_keyframes_compile_position_scale_and_alpha(tmp_path: Path) -> None:
    segment = _video(tmp_path)
    compile_native_keyframes(
        segment,
        {
            "position": {"from": {"x": 0.0, "y": 0.0}, "to": {"x": -0.28, "y": 0.1}},
            "scale": {"from": 1.0, "to": 0.72},
            "alpha": {"from": 0.0, "to": 1.0},
        },
        duration_us=3_000_000,
    )
    exported = {item.keyframe_property.value: item for item in segment.common_keyframes}
    assert len(exported[KeyframeProperty.position_x.value].keyframes) == 2
    assert len(exported[KeyframeProperty.position_y.value].keyframes) == 2
    assert len(exported[KeyframeProperty.scale_x.value].keyframes) == 2
    assert len(exported[KeyframeProperty.scale_y.value].keyframes) == 2
    assert len(exported[KeyframeProperty.alpha.value].keyframes) == 2


def test_paid_animation_is_rejected_even_if_reference_flag_is_false() -> None:
    paid = _animation_ref("TextIntro", "弹入")
    object.__setattr__(paid, "paid", True)
    with pytest.raises(PaidAnimationError):
        apply_animation_reference(_text(), paid)


def test_duration_boundary_is_allowed_but_overflow_is_explicit() -> None:
    reference = _animation_ref("TextIntro", "弹入")
    segment = _text(1_000_000)
    apply_animation_reference(segment, reference, duration_us=1_000_000)
    with pytest.raises(AnimationDurationError):
        apply_animation_reference(_text(1_000_000), reference, duration_us=1_000_001)
    with pytest.raises(AnimationDurationError):
        compile_native_keyframes(segment, {"alpha": {"from": 0.0, "to": 1.0}}, duration_us=1_000_001)


def test_unknown_property_and_invalid_status_are_explicit() -> None:
    with pytest.raises(UnknownAnimationPropertyError):
        compile_native_keyframes(_text(), {"blur": {"from": 0.0, "to": 1.0}})
    recipe = get_animation_recipe("emphasis_text_pop")
    assert recipe is not None
    recipe = replace(recipe, visual_status="validated_but_not_allowed")
    with pytest.raises(AnimationWriterError):
        apply_recipe_animations(recipe, _text())


def test_animation_material_can_be_registered_after_segment_was_added(tmp_path: Path) -> None:
    script = ScriptFile(1920, 1080, 30, True)
    track = script.append_track(TrackSpec(TrackType.video, "video"))
    segment = _video(tmp_path)
    script.add_segment(segment, track=track)
    assert script.materials.animations == []

    recipe = get_animation_recipe("cumulative_items")
    assert recipe is not None
    apply_recipe_animations(recipe, segment, duration_us=500_000, script=script)

    assert script.materials.animations == [segment.animations_instance]
    register_animation_material(script, segment)
    assert script.materials.animations == [segment.animations_instance]
