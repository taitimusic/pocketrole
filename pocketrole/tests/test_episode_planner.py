"""Tests for engine/episode_planner.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

from engine.episode_planner import EpisodePlanner
from tests._async_harness import async_to_sync


def _make_pattern(**overrides: object) -> dict[str, object]:
    pattern = {
        "id": 5,
        "pattern_type": "misunderstanding",
        "status": "active",
        "title": "食い違いが残る",
        "description": "ちょっとした誤解が残っている。",
        "involved_chars": ["char_a", "char_b"],
        "source_hook_id": 7,
        "source_tension_id": None,
        "source_scene_id": 12,
    }
    pattern.update(overrides)
    return pattern


@async_to_sync
async def test_process_round_opens_episode_from_active_pattern() -> None:
    db = AsyncMock()
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_active_interaction_patterns = AsyncMock(return_value=[_make_pattern()])
    db.get_open_story_hooks = AsyncMock(
        return_value=[
            {"id": 7, "title": "誤解", "description": "すれ違いが残る。"},
        ]
    )
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.insert_story_episode = AsyncMock(return_value=41)

    planner = EpisodePlanner("test_story", db)

    await planner.process_round(8)

    db.insert_story_episode.assert_awaited_once()
    payload = db.insert_story_episode.await_args.args[1]
    assert payload["episode_type"] == "misunderstanding"
    assert payload["focus_char_ids"] == ["char_a", "char_b"]
    assert payload["carry_over_hook_ids"] == [7]


@async_to_sync
async def test_process_round_closes_stale_episode() -> None:
    db = AsyncMock()
    db.get_active_story_episode = AsyncMock(
        return_value={
            "id": 41,
            "episode_type": "misunderstanding",
            "focus_char_ids": ["char_a", "char_b"],
            "carry_over_hook_ids": [7],
            "opened_turn": 1,
            "last_progress_turn": 2,
            "active_pattern_id": 5,
            "active_pattern_type": "misunderstanding",
        }
    )
    db.get_active_interaction_patterns = AsyncMock(return_value=[])
    db.get_open_story_hooks = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.close_story_episode = AsyncMock()

    planner = EpisodePlanner(
        "test_story",
        db,
        min_turns_per_episode=4,
        max_turns_per_episode=12,
        stale_rounds_before_close=2,
    )

    await planner.process_round(8)

    db.close_story_episode.assert_awaited_once()
    assert db.close_story_episode.await_args.args[0] == 41


@async_to_sync
async def test_process_round_prefers_pressure_matched_pattern_when_opening_episode() -> None:
    db = AsyncMock()
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_active_interaction_patterns = AsyncMock(
        return_value=[
            _make_pattern(id=5, pattern_type="misunderstanding", involved_chars=["char_c", "char_d"]),
            _make_pattern(id=6, pattern_type="status_clash", involved_chars=["char_a", "char_b"]),
        ]
    )
    db.get_open_story_hooks = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(
        return_value=[
            {
                "pressure_type": "status_flashpoint",
                "focus_char_ids": ["char_a", "char_b"],
                "focus_place_id": None,
                "score": 0.8,
            }
        ]
    )
    db.insert_story_episode = AsyncMock(return_value=44)

    planner = EpisodePlanner("test_story", db)

    await planner.process_round(8)

    payload = db.insert_story_episode.await_args.args[1]
    assert payload["episode_type"] == "status_clash"
    assert payload["focus_char_ids"] == ["char_a", "char_b"]


@async_to_sync
async def test_process_round_uses_pressure_aware_goal_when_showoff_flashpoint_is_high() -> None:
    db = AsyncMock()
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_active_interaction_patterns = AsyncMock(
        return_value=[
            _make_pattern(id=6, pattern_type="status_clash", involved_chars=["char_a", "char_b"]),
        ]
    )
    db.get_open_story_hooks = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(
        return_value=[
            {
                "pressure_type": "showoff_flashpoint",
                "focus_char_ids": ["char_a", "char_b"],
                "focus_place_id": "music_room",
                "score": 0.86,
            }
        ]
    )
    db.insert_story_episode = AsyncMock(return_value=45)

    planner = EpisodePlanner("test_story", db)

    await planner.process_round(8)

    payload = db.insert_story_episode.await_args.args[1]
    assert payload["goal"] == "ここで誰かが本気を見せる瞬間になる"


@async_to_sync
async def test_process_round_prefers_theme_matched_pattern_over_pressure_bonus() -> None:
    db = AsyncMock()
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_active_interaction_patterns = AsyncMock(
        return_value=[
            _make_pattern(
                id=5,
                pattern_type="misunderstanding",
                title="食い違い",
                description="ささいな誤解が続いている。",
                involved_chars=["char_c", "char_d"],
            ),
            _make_pattern(
                id=6,
                pattern_type="status_clash",
                title="文化祭の張り合い",
                description="文化祭の見せ場を巡って競い合っている。",
                involved_chars=["char_a", "char_b"],
            ),
        ]
    )
    db.get_open_story_hooks = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(
        return_value=[
            {
                "pressure_type": "status_flashpoint",
                "focus_char_ids": ["char_c", "char_d"],
                "focus_place_id": None,
                "score": 0.95,
            }
        ]
    )
    db.insert_story_episode = AsyncMock(return_value=46)

    planner = EpisodePlanner("test_story", db)

    await planner.process_round(
        8,
        active_chapter={"id": 9, "theme": "文化祭", "chapter_id": "festival_arc"},
    )

    payload = db.insert_story_episode.await_args.args[1]
    assert payload["episode_type"] == "status_clash"
    assert payload["focus_char_ids"] == ["char_a", "char_b"]


@async_to_sync
async def test_process_round_prefers_theme_matched_hook_when_no_pattern_matches() -> None:
    db = AsyncMock()
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_active_interaction_patterns = AsyncMock(return_value=[])
    db.get_open_story_hooks = AsyncMock(
        return_value=[
            {
                "id": 7,
                "hook_type": "question",
                "title": "秘密の噂",
                "description": "秘密が漏れたかもしれない。",
                "owner_char_id": "char_a",
                "target_char_id": "char_b",
                "source_place_id": None,
                "priority": 0.3,
            },
            {
                "id": 8,
                "hook_type": "conflict",
                "title": "文化祭の出し物が衝突する",
                "description": "文化祭の企画を巡って意見が割れている。",
                "owner_char_id": "char_c",
                "target_char_id": "char_d",
                "source_place_id": None,
                "priority": 0.2,
            },
        ]
    )
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    db.insert_story_episode = AsyncMock(return_value=47)

    planner = EpisodePlanner("test_story", db)

    await planner.process_round(
        8,
        active_chapter={"id": 9, "theme": "文化祭", "chapter_id": "festival_arc"},
    )

    payload = db.insert_story_episode.await_args.args[1]
    assert payload["focus_char_ids"] == ["char_c", "char_d"]
    assert payload["carry_over_hook_ids"] == [8]
