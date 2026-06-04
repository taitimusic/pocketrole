"""Probe-facing tests for GrowthEngine observability."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine.config import GrowthEngineConfig
from engine.growth_engine import GrowthEngine
from tests._async_harness import async_to_sync


def _make_cfg() -> GrowthEngineConfig:
    return GrowthEngineConfig(enabled=True, rolling_window_turns=20, immediate_commit_identity_threshold=0.85)


def _make_char() -> dict[str, str]:
    return {
        "id": "char_a",
        "name_ja": "アリス",
        "current_goal": "音楽を極める",
        "current_worry": "自信がない",
        "personality_core": "慎重で理性的",
    }


@async_to_sync
async def test_growth_engine_records_skip_reason_without_evidence() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(return_value=[])
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})

    engine = GrowthEngine("test_story", db, AsyncMock(), _make_cfg(), llm_provider="ollama", llm_model="test")
    await engine.process_round(5, [_make_char()])

    assert engine.get_last_round_results()["char_a"]["skip_reason"] == "no_evidence"


@async_to_sync
async def test_growth_engine_records_parse_failed_result() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(return_value=[{"id": 1, "summary": "約束。"}])
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})

    engine = GrowthEngine("test_story", db, AsyncMock(), _make_cfg(), llm_provider="ollama", llm_model="test")
    with patch(
        "engine.growth_engine.generate_structured_json",
        AsyncMock(
            return_value=SimpleNamespace(
                parsed=None,
                used_repair=False,
                done_reason="length",
                parser_error=None,
            )
        ),
    ):
        await engine.process_round(5, [_make_char()])

    assert engine.get_last_round_results()["char_a"]["skip_reason"] == "parse_failed"


@async_to_sync
async def test_growth_engine_records_llm_empty_result() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(return_value=[{"id": 1, "summary": "約束。"}])
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})

    engine = GrowthEngine("test_story", db, AsyncMock(), _make_cfg(), llm_provider="ollama", llm_model="test")
    with patch(
        "engine.growth_engine.generate_structured_json",
        AsyncMock(
            return_value=SimpleNamespace(
                parsed=[],
                used_repair=False,
                done_reason="stop",
            )
        ),
    ):
        await engine.process_round(5, [_make_char()])

    assert engine.get_last_round_results()["char_a"]["result"] == "llm_empty"
