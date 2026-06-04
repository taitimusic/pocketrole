"""Tests for engine/ambient_context.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from engine.ambient_context import AmbientContextConfig, AmbientContextManager


@pytest.mark.asyncio
async def test_resolve_turn_creates_body_and_place_factor() -> None:
    db = AsyncMock()
    db.get_active_ambient_states = AsyncMock(return_value=[])
    db.insert_ambient_state = AsyncMock(side_effect=[1, 2])

    manager = AmbientContextManager(
        story_id="test_story",
        db=db,
        llm_router=AsyncMock(),
        config=AmbientContextConfig(enabled=True),
        llm_provider="ollama",
        llm_model="qwen2.5:14b",
    )

    result = await manager.resolve_turn(
        turn_number=5,
        sim_datetime="2025-04-15T12:30",
        char_id="char_1",
        place_id="classroom",
        place_name="教室",
        same_place_char_ids=["char_2"],
        emotions={"stress": 0.4, "motivation": 0.6, "loneliness": 0.2, "excitement": 0.5},
        move_reason=None,
        anomaly=None,
    )

    assert result.body_state_texts
    assert result.ambient_texts
    assert db.insert_ambient_state.await_count == 2
