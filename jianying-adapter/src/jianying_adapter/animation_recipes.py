"""Small, native Jianying 8.8 animation recipe catalog.

The catalog describes how the adapter may compose five common talking-head
events.  It references only free animation metadata vendored with
``pyJianYingDraft``; layout composition is expressed as caller-applied native
keyframes, not rendered overlay media.

This module is deliberately declarative.  It does not open Jianying, mutate a
draft, scan the preset library, or download animation resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import importlib
from pathlib import Path
import sys
from typing import Any, Mapping


_VENDORED_ROOT = "vendor/pyJianYingDraft-source"
_VENDORED_DEPS = "vendor/python-deps"


@dataclass(frozen=True)
class AnimationReference:
    """A reference to one member of a vendored pyJianYingDraft enum."""

    enum: str
    member: str
    animation_type: str
    default_duration_us: int
    paid: bool = False

    def resolve(self) -> Any:
        """Resolve this reference to the vendored enum member."""

        enum_class = _metadata_enum(self.enum)
        try:
            return getattr(enum_class, self.member)
        except AttributeError as exc:
            raise LookupError(f"未知剪映动画：{self.enum}.{self.member}") from exc

    @property
    def metadata(self) -> Any:
        """Return the ``AnimationMeta`` value behind the enum member."""

        return self.resolve().value

    @property
    def is_vip(self) -> bool:
        """Expose the vendored metadata flag for callers and tests."""

        return bool(self.metadata.is_vip)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enum": self.enum,
            "member": self.member,
            "animation_type": self.animation_type,
            "default_duration_us": self.default_duration_us,
            "paid": self.paid,
        }


@dataclass(frozen=True)
class AnimationRecipe:
    """Declarative recipe consumed by a later draft writer."""

    id: str
    semantic_event: str
    steps: tuple[Mapping[str, Any], ...]
    animations: tuple[AnimationReference, ...]
    composition: Mapping[str, Any]
    sfx_role: str | None
    paid: bool
    compatibility_baseline: str
    visual_status: str

    @property
    def animation_refs(self) -> tuple[AnimationReference, ...]:
        """Compatibility-friendly name for callers that call them refs."""

        return self.animations

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "semantic_event": self.semantic_event,
            "steps": [dict(step) for step in self.steps],
            "animations": [animation.as_dict() for animation in self.animations],
            "composition": dict(self.composition),
            "sfx_role": self.sfx_role,
            "paid": self.paid,
            "compatibility_baseline": self.compatibility_baseline,
            "visual_status": self.visual_status,
        }


def _ensure_vendored_import_paths() -> None:
    """Make the repository's vendored pyJianYingDraft importable locally."""

    project_root = Path(__file__).resolve().parents[2]
    for relative in (_VENDORED_ROOT, _VENDORED_DEPS):
        path = str(project_root / relative)
        if path not in sys.path:
            sys.path.insert(0, path)


@lru_cache(maxsize=None)
def _metadata_enum(enum_name: str) -> type:
    _ensure_vendored_import_paths()
    enum_modules = {
        "TextIntro": "pyJianYingDraft.metadata.text_intro",
        "TextLoopAnim": "pyJianYingDraft.metadata.text_loop",
        "GroupAnimationType": "pyJianYingDraft.metadata.video_group_animation",
    }
    module_name = enum_modules.get(enum_name)
    if module_name is None:
        raise LookupError(f"不支持的剪映动画枚举：{enum_name}")
    module = importlib.import_module(module_name)
    return getattr(module, enum_name)


def _animation(
    enum: str,
    member: str,
    animation_type: str,
    default_duration_us: int = 500_000,
) -> AnimationReference:
    return AnimationReference(
        enum=enum,
        member=member,
        animation_type=animation_type,
        default_duration_us=default_duration_us,
        paid=False,
    )


_RECIPES: tuple[AnimationRecipe, ...] = (
    AnimationRecipe(
        id="emphasis_text_pop",
        semantic_event="keyword_emphasis",
        steps=(
            {"order": 1, "track": "text_overlay", "action": "add_short_emphasis_text"},
            {"order": 2, "track": "text_overlay", "action": "apply_native_intro"},
            {"order": 3, "track": "text_overlay", "action": "remove_at_event_end"},
        ),
        animations=(_animation("TextIntro", "弹入", "in"),),
        composition={
            "coordinate_system": "normalized_center_origin",
            "position": {"x": 0.0, "y": -0.12},
            "scale": 1.0,
            "normal_caption_policy": "replace_in_event_window",
            "safe_zone": "caller_validates_against_subtitles",
        },
        sfx_role=None,
        paid=False,
        compatibility_baseline="jianying_8.8",
        visual_status="unverified",
    ),
    AnimationRecipe(
        id="person_shrink_broll_expand",
        semantic_event="product_reveal",
        steps=(
            {"order": 1, "track": "a_roll", "action": "keep_person_as_base"},
            {"order": 2, "track": "a_roll", "action": "apply_native_keyframes_person_shrink_left"},
            {"order": 3, "track": "b_roll", "action": "insert_caller_selected_media"},
            {"order": 4, "track": "b_roll", "action": "apply_native_keyframes_broll_expand_right"},
        ),
        animations=(
            _animation("GroupAnimationType", "向左缩小", "group"),
            _animation("GroupAnimationType", "放大弹动", "group"),
        ),
        composition={
            "coordinate_system": "normalized_center_origin",
            "native_keyframes": {
                "person": {
                    "position": {"from": {"x": 0.0, "y": 0.0}, "to": {"x": -0.28, "y": 0.0}},
                    "scale": {"from": 1.0, "to": 0.72},
                },
                "b_roll": {
                    "position": {"from": {"x": 0.40, "y": 0.0}, "to": {"x": 0.28, "y": 0.0}},
                    "scale": {"from": 0.56, "to": 0.72},
                },
                "duration_source": "caller_event_window",
            },
            "background": "preserve_a_roll",
            "full_screen_allowed": False,
            "safe_zone": "caller_validates_against_subtitles",
        },
        sfx_role="whoosh",
        paid=False,
        compatibility_baseline="jianying_8.8",
        visual_status="unverified",
    ),
    AnimationRecipe(
        id="split_screen_entrance",
        semantic_event="contrast_split",
        steps=(
            {"order": 1, "track": "left_media", "action": "place_caller_media_left"},
            {"order": 2, "track": "right_media", "action": "place_caller_media_right"},
            {"order": 3, "track": "both_media", "action": "apply_native_group_entrance"},
        ),
        animations=(_animation("GroupAnimationType", "左右分割", "group"),),
        composition={
            "coordinate_system": "normalized_center_origin",
            "panels": {
                "left": {"position": {"x": -0.25, "y": 0.0}, "scale": 0.56},
                "right": {"position": {"x": 0.25, "y": 0.0}, "scale": 0.56},
            },
            "caller_supplies_media": True,
            "full_screen_allowed": False,
            "safe_zone": "caller_validates_against_subtitles",
        },
        sfx_role="swipe",
        paid=False,
        compatibility_baseline="jianying_8.8",
        visual_status="unverified",
    ),
    AnimationRecipe(
        id="cumulative_items",
        semantic_event="parallel_list",
        steps=(
            {"order": 1, "track": "item_overlay_1", "action": "insert_item_at_phrase_start"},
            {"order": 2, "track": "item_overlay_n", "action": "repeat_for_each_detected_item"},
            {"order": 3, "track": "item_overlay_n", "action": "retain_previous_items"},
        ),
        animations=(_animation("GroupAnimationType", "弹入旋转", "group"),),
        composition={
            "coordinate_system": "normalized_center_origin",
            "item_slots": "caller_supplies_positions_and_media",
            "default_slot_positions": (
                {"x": -0.42, "y": -0.18},
                {"x": 0.42, "y": -0.18},
                {"x": -0.42, "y": 0.18},
                {"x": 0.42, "y": 0.18},
            ),
            "uniform_item_scale": 0.24,
            "remove_previous_items": False,
            "full_screen_allowed": False,
            "safe_zone": "caller_validates_against_subtitles",
        },
        sfx_role="pop",
        paid=False,
        compatibility_baseline="jianying_8.8",
        visual_status="unverified",
    ),
    AnimationRecipe(
        id="conclusion_impact",
        semantic_event="conclusion",
        steps=(
            {"order": 1, "track": "text_overlay", "action": "add_short_conclusion_text"},
            {"order": 2, "track": "text_overlay", "action": "apply_free_native_intro_and_loop"},
            {"order": 3, "track": "sfx", "action": "bind_role_to_animation_start"},
        ),
        animations=(
            _animation("TextIntro", "弹入", "in"),
            _animation("TextLoopAnim", "跳动", "loop"),
        ),
        composition={
            "coordinate_system": "normalized_center_origin",
            "position": {"x": 0.0, "y": -0.16},
            "scale": 1.08,
            "keyframes": {"scale": {"from": 0.94, "to": 1.08}},
            "normal_caption_policy": "replace_in_event_window",
            "safe_zone": "caller_validates_against_subtitles",
        },
        sfx_role="impact",
        paid=False,
        compatibility_baseline="jianying_8.8",
        visual_status="unverified",
    ),
)


def list_animation_recipes() -> tuple[AnimationRecipe, ...]:
    """Return the five registered recipes in stable order."""

    return _RECIPES


def get_animation_recipe(recipe_id: str) -> AnimationRecipe | None:
    """Return a recipe by stable id, or ``None`` when it is unknown."""

    return next((recipe for recipe in _RECIPES if recipe.id == recipe_id), None)


def select_animation_recipe(semantic_event: str) -> AnimationRecipe | None:
    """Select a recipe by semantic event, or ``None`` for an unknown event."""

    if not isinstance(semantic_event, str):
        return None
    event = semantic_event.strip().lower()
    return next(
        (
            recipe
            for recipe in _RECIPES
            if recipe.semantic_event.lower() == event or recipe.id.lower() == event
        ),
        None,
    )


__all__ = [
    "AnimationReference",
    "AnimationRecipe",
    "get_animation_recipe",
    "list_animation_recipes",
    "select_animation_recipe",
]
