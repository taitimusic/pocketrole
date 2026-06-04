"""Tests for tools/runtime_probe_story_events.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.runtime_probe_story_events import parse_args, run_probe


def test_parse_args_defaults() -> None:
    """probe CLI のデフォルト引数を解釈できる。"""
    ns = parse_args(["--story", "ankoku_gakuen", "--goal", "round_end", "--max-character-turns", "8"])
    assert ns.story == "ankoku_gakuen"
    assert ns.db == "db/pocketrole.db"
    assert ns.goal == "round_end"
    assert ns.max_character_turns == 8


@pytest.mark.asyncio
async def test_run_probe_stops_after_first_round_for_round_end(tmp_path: Path) -> None:
    """round_end goal は 1 round 分だけ進めて stop する。"""
    source_db = tmp_path / "source.db"
    source_db.write_text("seed", encoding="utf-8")

    config = MagicMock()
    config.llm = MagicMock()

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)
    fake_db.get_story = AsyncMock(return_value={"id": "ankoku_gakuen"})

    fake_router = AsyncMock()
    fake_router.start = AsyncMock()
    fake_router.stop = AsyncMock()

    fake_engine = AsyncMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._run_single_round = AsyncMock(
        return_value={
            "start_turn_number": 10,
            "end_turn_number": 11,
            "planned_turns": 3,
            "processed_closed_scene_ids": [],
            "growth_results": {},
        }
    )

    with (
        patch("tools.runtime_probe_story_events.load_config", return_value=config),
        patch("tools.runtime_probe_story_events.DatabaseManager", return_value=fake_db) as db_cls,
        patch(
            "tools.runtime_probe_story_events.ensure_story_llm_runtime_requirements",
            return_value={"ankoku_gakuen": MagicMock()},
        ),
        patch(
            "tools.runtime_probe_story_events.required_story_llm_providers",
            return_value=set(),
        ),
        patch("tools.runtime_probe_story_events.LLMRouter", return_value=fake_router),
        patch(
            "tools.runtime_probe_story_events.StoryEngine",
            return_value=fake_engine,
        ),
        patch(
            "tools.runtime_probe_story_events._collect_probe_counts",
            side_effect=[
                {
                    "last_turn_number": 10,
                    "closed_scene_ids": [],
                    "scene_arc_count": 0,
                    "episode_arc_count": 0,
                    "novel_output_count": 0,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                },
                {
                    "last_turn_number": 11,
                    "closed_scene_ids": [],
                    "scene_arc_count": 0,
                    "episode_arc_count": 1,
                    "novel_output_count": 0,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 1,
                    "active_relationship_mode_count": 1,
                    "active_canon_bit_count": 1,
                    "canon_reignition_total": 1,
                    "canon_triggered_hook_count": 1,
                    "active_dramatic_pressure_count": 1,
                    "episode_count": 1,
                    "dominant_pattern_types": ["status_clash"],
                    "dominant_relationship_modes": ["irritated_respect"],
                    "dominant_canon_motifs": ["irritated_respect"],
                    "dominant_pressure_types": ["status_flashpoint"],
                    "active_episode_id": 9,
                    "active_episode_type": "status_clash",
                    "active_episode_goal": "張り合いを進める",
                    "max_pressure_score": 0.82,
                },
            ],
        ),
    ):
        result = await run_probe(
            "ankoku_gakuen",
            source_db,
            goal="round_end",
            max_character_turns=8,
        )

    work_db_path = Path(db_cls.call_args.args[0])
    assert work_db_path != source_db
    assert result["goal"] == "round_end"
    assert result["goal_reached"] is True
    assert result["rounds_advanced"] == 1
    assert result["character_turns_advanced"] == 3
    assert result["start_turn_number"] == 10
    assert result["end_turn_number"] == 11
    assert result["pattern_delta"] == 1
    assert result["active_pattern_count"] == 1
    assert result["dominant_pattern_types"] == ["status_clash"]
    assert result["relationship_mode_delta"] == 1
    assert result["active_relationship_mode_count"] == 1
    assert result["dominant_relationship_modes"] == ["irritated_respect"]
    assert result["canon_delta"] == 1
    assert result["active_canon_bit_count"] == 1
    assert result["canon_reignitions_delta"] == 1
    assert result["canon_triggered_hook_count"] == 1
    assert result["dominant_canon_motifs"] == ["irritated_respect"]
    assert result["pressure_delta"] == 1
    assert result["active_dramatic_pressure_count"] == 1
    assert result["dominant_pressure_types"] == ["status_flashpoint"]
    assert result["max_pressure_score"] == pytest.approx(0.82)
    assert result["episode_delta"] == 1
    assert result["active_episode_id"] == 9
    assert result["active_episode_type"] == "status_clash"
    assert result["active_episode_goal"] == "張り合いを進める"
    assert result["work_db_path"] == str(work_db_path)


@pytest.mark.asyncio
async def test_run_probe_waits_for_scene_close_goal(tmp_path: Path) -> None:
    """scene_close goal は close と artifact handoff が揃うまで round を進める。"""
    source_db = tmp_path / "source.db"
    source_db.write_text("seed", encoding="utf-8")

    config = MagicMock()
    config.llm = MagicMock()

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)
    fake_db.get_story = AsyncMock(return_value={"id": "ankoku_gakuen"})

    fake_router = AsyncMock()
    fake_engine = AsyncMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._run_single_round = AsyncMock(
        side_effect=[
            {
                "start_turn_number": 10,
                "end_turn_number": 11,
                "planned_turns": 3,
                "processed_closed_scene_ids": [],
                "growth_results": {},
            },
            {
                "start_turn_number": 11,
                "end_turn_number": 12,
                "planned_turns": 2,
                "processed_closed_scene_ids": [25],
                "growth_results": {},
            },
        ]
    )

    with (
        patch("tools.runtime_probe_story_events.load_config", return_value=config),
        patch("tools.runtime_probe_story_events.DatabaseManager", return_value=fake_db),
        patch(
            "tools.runtime_probe_story_events.ensure_story_llm_runtime_requirements",
            return_value={"ankoku_gakuen": MagicMock()},
        ),
        patch(
            "tools.runtime_probe_story_events.required_story_llm_providers",
            return_value=set(),
        ),
        patch("tools.runtime_probe_story_events.LLMRouter", return_value=fake_router),
        patch("tools.runtime_probe_story_events.StoryEngine", return_value=fake_engine),
        patch(
            "tools.runtime_probe_story_events._collect_probe_counts",
            side_effect=[
                {
                    "last_turn_number": 10,
                    "closed_scene_ids": [],
                    "scene_arc_count": 2,
                    "episode_arc_count": 0,
                    "novel_output_count": 2,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                },
                {
                    "last_turn_number": 12,
                    "closed_scene_ids": [25],
                    "scene_arc_count": 3,
                    "episode_arc_count": 0,
                    "novel_output_count": 3,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                },
            ],
        ),
    ):
        result = await run_probe(
            "ankoku_gakuen",
            source_db,
            goal="scene_close",
            max_character_turns=8,
        )

    assert result["goal_reached"] is True
    assert result["scene_close_goal_stage"] == "complete"
    assert result["new_closed_scene_ids"] == [25]
    assert result["artifact_ready_scene_ids"] == [25]
    assert result["artifact_missing_scene_ids"] == []
    assert result["scene_arc_delta"] == 1
    assert result["novel_output_delta"] == 1
    assert result["rounds_advanced"] == 2


@pytest.mark.asyncio
async def test_run_probe_reports_closed_only_when_artifacts_are_missing(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source_db.write_text("seed", encoding="utf-8")

    config = MagicMock()
    config.llm = MagicMock()

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)
    fake_db.get_story = AsyncMock(return_value={"id": "ankoku_gakuen"})

    fake_router = AsyncMock()
    fake_engine = AsyncMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._run_single_round = AsyncMock(
        return_value={
            "start_turn_number": 20,
            "end_turn_number": 21,
            "planned_turns": 2,
            "processed_closed_scene_ids": [41],
            "growth_results": {},
        }
    )

    with (
        patch("tools.runtime_probe_story_events.load_config", return_value=config),
        patch("tools.runtime_probe_story_events.DatabaseManager", return_value=fake_db),
        patch(
            "tools.runtime_probe_story_events.ensure_story_llm_runtime_requirements",
            return_value={"ankoku_gakuen": MagicMock()},
        ),
        patch(
            "tools.runtime_probe_story_events.required_story_llm_providers",
            return_value=set(),
        ),
        patch("tools.runtime_probe_story_events.LLMRouter", return_value=fake_router),
        patch("tools.runtime_probe_story_events.StoryEngine", return_value=fake_engine),
        patch(
            "tools.runtime_probe_story_events._collect_probe_counts",
            side_effect=[
                {
                    "last_turn_number": 20,
                    "closed_scene_ids": [],
                    "scene_arc_count": 1,
                    "novel_output_count": 1,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                    "scene_arc_scene_ids": [5],
                    "novel_output_scene_ids": [5],
                },
                {
                    "last_turn_number": 21,
                    "closed_scene_ids": [41],
                    "scene_arc_count": 1,
                    "novel_output_count": 1,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                    "scene_arc_scene_ids": [5],
                    "novel_output_scene_ids": [5],
                },
            ],
        ),
    ):
        result = await run_probe(
            "ankoku_gakuen",
            source_db,
            goal="scene_close",
            max_character_turns=2,
        )

    assert result["goal_reached"] is False
    assert result["scene_close_goal_stage"] == "closed_only"
    assert result["artifact_ready_scene_ids"] == []
    assert result["artifact_missing_scene_ids"] == [41]


@pytest.mark.asyncio
async def test_run_probe_continues_until_scene_close_backlog_recovers(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source_db.write_text("seed", encoding="utf-8")

    config = MagicMock()
    config.llm = MagicMock()

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)
    fake_db.get_story = AsyncMock(return_value={"id": "ankoku_gakuen"})

    fake_router = AsyncMock()
    fake_engine = AsyncMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._run_single_round = AsyncMock(
        side_effect=[
            {
                "start_turn_number": 30,
                "end_turn_number": 31,
                "planned_turns": 2,
                "processed_closed_scene_ids": [41],
                "growth_results": {},
            },
            {
                "start_turn_number": 31,
                "end_turn_number": 32,
                "planned_turns": 2,
                "processed_closed_scene_ids": [],
                "growth_results": {},
            },
        ]
    )

    with (
        patch("tools.runtime_probe_story_events.load_config", return_value=config),
        patch("tools.runtime_probe_story_events.DatabaseManager", return_value=fake_db),
        patch(
            "tools.runtime_probe_story_events.ensure_story_llm_runtime_requirements",
            return_value={"ankoku_gakuen": MagicMock()},
        ),
        patch(
            "tools.runtime_probe_story_events.required_story_llm_providers",
            return_value=set(),
        ),
        patch("tools.runtime_probe_story_events.LLMRouter", return_value=fake_router),
        patch("tools.runtime_probe_story_events.StoryEngine", return_value=fake_engine),
        patch(
            "tools.runtime_probe_story_events._collect_probe_counts",
            side_effect=[
                {
                    "last_turn_number": 30,
                    "closed_scene_ids": [],
                    "scene_arc_count": 2,
                    "episode_arc_count": 0,
                    "novel_output_count": 2,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                    "scene_arc_scene_ids": [],
                    "novel_output_scene_ids": [],
                },
                {
                    "last_turn_number": 31,
                    "closed_scene_ids": [41],
                    "scene_arc_count": 2,
                    "episode_arc_count": 0,
                    "novel_output_count": 2,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                    "scene_arc_scene_ids": [],
                    "novel_output_scene_ids": [],
                },
                {
                    "last_turn_number": 32,
                    "closed_scene_ids": [41],
                    "scene_arc_count": 3,
                    "episode_arc_count": 0,
                    "novel_output_count": 3,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                    "scene_arc_scene_ids": [41],
                    "novel_output_scene_ids": [41],
                },
            ],
        ),
    ):
        result = await run_probe(
            "ankoku_gakuen",
            source_db,
            goal="scene_close",
            max_character_turns=4,
        )

    assert result["goal_reached"] is True
    assert result["scene_close_goal_stage"] == "complete"
    assert result["rounds_advanced"] == 2
    assert result["backlog_recovered_scene_ids"] == [41]
    assert result["closed_only_count"] == 1
    assert result["arc_created_count"] == 0
    assert result["complete_count"] == 1


@pytest.mark.asyncio
async def test_run_probe_reports_backlog_recovery_without_new_close(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source_db.write_text("seed", encoding="utf-8")

    config = MagicMock()
    config.llm = MagicMock()

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)
    fake_db.get_story = AsyncMock(return_value={"id": "ankoku_gakuen"})

    fake_router = AsyncMock()
    fake_engine = AsyncMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._run_single_round = AsyncMock(
        return_value={
            "start_turn_number": 30,
            "end_turn_number": 31,
            "planned_turns": 2,
            "processed_closed_scene_ids": [],
            "growth_results": {},
        }
    )

    with (
        patch("tools.runtime_probe_story_events.load_config", return_value=config),
        patch("tools.runtime_probe_story_events.ensure_story_llm_runtime_requirements", return_value={}),
        patch("tools.runtime_probe_story_events.required_story_llm_providers", return_value=[]),
        patch("tools.runtime_probe_story_events.DatabaseManager", return_value=fake_db),
        patch("tools.runtime_probe_story_events.LLMRouter", return_value=fake_router),
        patch("tools.runtime_probe_story_events.StoryEngine", return_value=fake_engine),
        patch(
            "tools.runtime_probe_story_events._collect_probe_counts",
            side_effect=[
                {
                    "scene_arc_count": 0,
                    "episode_arc_count": 0,
                    "novel_output_count": 0,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_writeback_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_canon_profile_overlay_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "last_turn_number": 30,
                    "closed_scene_ids": [41],
                    "scene_arc_scene_ids": [],
                    "novel_output_scene_ids": [],
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                },
                {
                    "scene_arc_count": 1,
                    "episode_arc_count": 0,
                    "novel_output_count": 1,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_writeback_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_canon_profile_overlay_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "last_turn_number": 31,
                    "closed_scene_ids": [41],
                    "scene_arc_scene_ids": [41],
                    "novel_output_scene_ids": [41],
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                },
            ],
        ),
    ):
        result = await run_probe(
            "ankoku_gakuen",
            source_db,
            goal="scene_close",
            max_character_turns=1,
        )

    assert result["goal_reached"] is False
    assert result["scene_close_goal_stage"] == "no_close"
    assert result["backlog_recovered_scene_ids"] == [41]
    assert result["complete_count"] == 1


@pytest.mark.asyncio
async def test_run_probe_returns_growth_evidence_ids(tmp_path: Path) -> None:
    """growth goal は evidence-bearing char が出た round で stop する。"""
    source_db = tmp_path / "source.db"
    source_db.write_text("seed", encoding="utf-8")

    config = MagicMock()
    config.llm = MagicMock()

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)
    fake_db.get_story = AsyncMock(return_value={"id": "ankoku_gakuen"})

    fake_router = AsyncMock()
    fake_engine = AsyncMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._run_single_round = AsyncMock(
        return_value={
            "start_turn_number": 10,
            "end_turn_number": 11,
            "planned_turns": 4,
            "processed_closed_scene_ids": [],
            "growth_results": {
                "char_a": {"evidence_count": 2, "result": "llm_empty"},
                "char_b": {"evidence_count": 0, "skip_reason": "no_evidence"},
            },
        }
    )

    with (
        patch("tools.runtime_probe_story_events.load_config", return_value=config),
        patch("tools.runtime_probe_story_events.DatabaseManager", return_value=fake_db),
        patch(
            "tools.runtime_probe_story_events.ensure_story_llm_runtime_requirements",
            return_value={"ankoku_gakuen": MagicMock()},
        ),
        patch(
            "tools.runtime_probe_story_events.required_story_llm_providers",
            return_value=set(),
        ),
        patch("tools.runtime_probe_story_events.LLMRouter", return_value=fake_router),
        patch("tools.runtime_probe_story_events.StoryEngine", return_value=fake_engine),
        patch(
            "tools.runtime_probe_story_events._collect_probe_counts",
            side_effect=[
                {
                    "last_turn_number": 10,
                    "closed_scene_ids": [],
                    "scene_arc_count": 0,
                    "episode_arc_count": 0,
                    "novel_output_count": 0,
                    "growth_candidate_count": 1,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                },
                {
                    "last_turn_number": 11,
                    "closed_scene_ids": [],
                    "scene_arc_count": 0,
                    "episode_arc_count": 0,
                    "novel_output_count": 0,
                    "growth_candidate_count": 1,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                },
            ],
        ),
    ):
        result = await run_probe(
            "ankoku_gakuen",
            source_db,
            goal="growth",
            max_character_turns=8,
        )

    assert result["goal_reached"] is True
    assert result["evidence_bearing_char_ids"] == ["char_a"]


@pytest.mark.asyncio
async def test_run_probe_returns_goal_not_reached_when_cap_exhausted(tmp_path: Path) -> None:
    """goal 未達でも cap 到達時は正常終了して summary を返す。"""
    source_db = tmp_path / "source.db"
    source_db.write_text("seed", encoding="utf-8")

    config = MagicMock()
    config.llm = MagicMock()

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)
    fake_db.get_story = AsyncMock(return_value={"id": "ankoku_gakuen"})

    fake_router = AsyncMock()
    fake_engine = AsyncMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._run_single_round = AsyncMock(
        side_effect=[
            {
                "start_turn_number": 10,
                "end_turn_number": 11,
                "planned_turns": 3,
                "processed_closed_scene_ids": [],
                "growth_results": {},
            },
            {
                "start_turn_number": 11,
                "end_turn_number": 12,
                "planned_turns": 3,
                "processed_closed_scene_ids": [],
                "growth_results": {},
            },
        ]
    )

    with (
        patch("tools.runtime_probe_story_events.load_config", return_value=config),
        patch("tools.runtime_probe_story_events.DatabaseManager", return_value=fake_db),
        patch(
            "tools.runtime_probe_story_events.ensure_story_llm_runtime_requirements",
            return_value={"ankoku_gakuen": MagicMock()},
        ),
        patch(
            "tools.runtime_probe_story_events.required_story_llm_providers",
            return_value=set(),
        ),
        patch("tools.runtime_probe_story_events.LLMRouter", return_value=fake_router),
        patch("tools.runtime_probe_story_events.StoryEngine", return_value=fake_engine),
        patch(
            "tools.runtime_probe_story_events._collect_probe_counts",
            side_effect=[
                {
                    "last_turn_number": 10,
                    "closed_scene_ids": [],
                    "scene_arc_count": 0,
                    "episode_arc_count": 0,
                    "novel_output_count": 0,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                },
                {
                    "last_turn_number": 12,
                    "closed_scene_ids": [],
                    "scene_arc_count": 0,
                    "episode_arc_count": 0,
                    "novel_output_count": 0,
                    "growth_candidate_count": 0,
                    "growth_commit_count": 0,
                    "active_pattern_count": 0,
                    "active_relationship_mode_count": 0,
                    "active_canon_bit_count": 0,
                    "canon_reignition_total": 0,
                    "canon_triggered_hook_count": 0,
                    "active_dramatic_pressure_count": 0,
                    "episode_count": 0,
                    "dominant_pattern_types": [],
                    "dominant_relationship_modes": [],
                    "dominant_canon_motifs": [],
                    "dominant_pressure_types": [],
                    "active_episode_id": None,
                    "active_episode_type": None,
                    "active_episode_goal": None,
                    "max_pressure_score": 0.0,
                },
            ],
        ),
    ):
        result = await run_probe(
            "ankoku_gakuen",
            source_db,
            goal="scene_close",
            max_character_turns=5,
        )

    assert result["goal_reached"] is False
    assert result["character_turns_advanced"] == 6
