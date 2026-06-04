"""StoryDirector relationship-mode ranking tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

from engine.config import StoryDirectorConfig
from engine.llm.router import LLMRouter
from engine.story_director import StoryDirector


def _build_director() -> StoryDirector:
    config = StoryDirectorConfig(
        enabled=True,
        analysis_interval_rounds=2,
        tension_escalation_rounds=4,
        max_active_tensions=4,
        max_active_interventions=2,
    )
    return StoryDirector(config, AsyncMock(), AsyncMock(spec=LLMRouter))


def test_relationship_mode_bonus_prefers_status_clash_for_irritated_respect() -> None:
    director = _build_director()
    modes = [
        {
            "mode_type": "irritated_respect",
            "char_id_from": "char_a",
            "char_id_to": "char_b",
            "intensity": 0.70,
        }
    ]
    matching = {
        "tension_type": "conflict",
        "involved_chars": ["char_a", "char_b"],
    }
    non_matching = {
        "tension_type": "secret",
        "involved_chars": ["char_a", "char_b"],
    }

    assert director._relationship_mode_bonus_for_tension(matching, modes) > director._relationship_mode_bonus_for_tension(non_matching, modes)


def test_relationship_mode_bonus_prefers_revelation_for_unsafe_confidant() -> None:
    director = _build_director()
    modes = [
        {
            "mode_type": "unsafe_confidant",
            "char_id_from": "char_a",
            "char_id_to": "char_b",
            "intensity": 0.80,
        }
    ]
    matching = {
        "intervention_type": "revelation",
        "scope": "char:char_a,char_b",
    }
    non_matching = {
        "intervention_type": "plot_twist",
        "scope": "char:char_a,char_b",
    }

    assert director._relationship_mode_bonus_for_intervention(matching, modes) > director._relationship_mode_bonus_for_intervention(non_matching, modes)


def test_relationship_mode_bonus_adds_complementary_clusters_with_cap() -> None:
    director = _build_director()
    modes = [
        {
            "mode_type": "irritated_respect",
            "char_id_from": "char_a",
            "char_id_to": "char_b",
            "intensity": 0.70,
        },
        {
            "mode_type": "cannot_ignore",
            "char_id_from": "char_a",
            "char_id_to": "char_b",
            "intensity": 0.55,
        },
    ]
    candidate = {
        "tension_type": "conflict",
        "involved_chars": ["char_a", "char_b"],
    }
    single_bonus = director._relationship_mode_bonus_for_tension(candidate, modes[:1])
    combined_bonus = director._relationship_mode_bonus_for_tension(candidate, modes)

    assert combined_bonus > single_bonus
    assert combined_bonus <= 3.0
