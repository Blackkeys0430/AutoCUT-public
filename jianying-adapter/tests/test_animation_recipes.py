from __future__ import annotations

from jianying_adapter.animation_recipes import (
    get_animation_recipe,
    list_animation_recipes,
    select_animation_recipe,
)


EXPECTED_IDS = {
    "emphasis_text_pop",
    "person_shrink_broll_expand",
    "split_screen_entrance",
    "cumulative_items",
    "conclusion_impact",
}


def test_five_recipe_categories_are_registered() -> None:
    recipes = list_animation_recipes()
    assert {recipe.id for recipe in recipes} == EXPECTED_IDS
    assert len(recipes) == 5
    assert all(recipe.compatibility_baseline == "jianying_8.8" for recipe in recipes)
    assert all(recipe.visual_status == "unverified" for recipe in recipes)


def test_all_registered_animations_exist_and_are_free() -> None:
    for recipe in list_animation_recipes():
        assert recipe.paid is False
        assert recipe.animations
        for animation in recipe.animations:
            member = animation.resolve()
            metadata = member.value
            assert animation.default_duration_us == metadata.duration
            assert metadata.is_vip is False
            assert animation.paid is False


def test_recipe_fields_include_ordered_steps_composition_and_optional_sfx() -> None:
    for recipe in list_animation_recipes():
        assert [step["order"] for step in recipe.steps] == list(range(1, len(recipe.steps) + 1))
        assert recipe.composition
        assert recipe.sfx_role is None or isinstance(recipe.sfx_role, str)

    combined = get_animation_recipe("person_shrink_broll_expand")
    assert combined is not None
    assert "native_keyframes" in combined.composition
    assert combined.composition["full_screen_allowed"] is False
    assert combined.composition["native_keyframes"]["b_roll"]["scale"]["to"] < 1.0
    emphasis = get_animation_recipe("emphasis_text_pop")
    assert emphasis is not None
    assert emphasis.composition["normal_caption_policy"] == "replace_in_event_window"
    conclusion = get_animation_recipe("conclusion_impact")
    assert conclusion is not None
    assert conclusion.sfx_role == "impact"
    assert conclusion.composition["normal_caption_policy"] == "replace_in_event_window"
    assert all(animation.member != "放大震动" for animation in conclusion.animations)


def test_unknown_semantic_event_returns_none() -> None:
    assert select_animation_recipe("not-a-real-event") is None
    assert select_animation_recipe("  conclusion ").id == "conclusion_impact"
