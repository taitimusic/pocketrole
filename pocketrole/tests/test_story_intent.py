from engine.story_intent import get_story_intent_profile


def test_get_story_intent_profile_returns_story_specific_and_default() -> None:
    ankoku = get_story_intent_profile("ankoku_gakuen")
    default = get_story_intent_profile("missing_story")

    assert ankoku.story_id == "ankoku_gakuen"
    assert ankoku.primary_mode == "mystery_drama"
    assert "mystery" in ankoku.preferred_tension_types
    assert "relationship_catalyst" in ankoku.preferred_intervention_types
    assert "rivalry" in ankoku.deprioritized_tension_types
    assert default.story_id == "default"
    assert default.primary_mode == "default"
