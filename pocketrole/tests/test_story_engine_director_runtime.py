"""StoryEngine の director runtime 連携テスト。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from engine.story_engine import StoryEngine
from tests.test_story_engine import make_mock_db


@pytest.mark.asyncio
async def test_post_round_hooks_reload_director_before_chapter_processing() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    events: list[str] = []

    async def _record_reload() -> bool:
        events.append("reload")
        return True

    async def _record_chapter(turn_number: int) -> dict[str, object]:
        events.append(f"chapter:{turn_number}")
        return {}

    async def _record_evaluate(turn_number: int) -> dict[str, object]:
        events.append(f"evaluate:{turn_number}")
        return {}

    director = AsyncMock()
    director.reload_active_persona = AsyncMock(side_effect=_record_reload)
    director.evaluate_satisfaction = AsyncMock(side_effect=_record_evaluate)
    engine._director_persona = director

    chapter_manager = AsyncMock()
    chapter_manager.process_round = AsyncMock(side_effect=_record_chapter)
    engine._chapter_manager = chapter_manager
    engine._story_director = None

    await engine._run_post_round_hooks(
        False,
        current_places={"char_1": "music_room"},
    )

    assert events[:3] == ["reload", f"chapter:{engine.world_clock.turn_number}", f"evaluate:{engine.world_clock.turn_number}"]
