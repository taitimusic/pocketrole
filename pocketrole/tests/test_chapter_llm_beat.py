"""
tests/test_chapter_llm_beat.py — ChapterManager LLM-assisted beat 進行判定テスト

in-memory SQLite + AsyncMock LLMRouter を使用。LLM は実際には呼ばない。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from db.db_manager import DatabaseManager
from engine.chapter_manager import ChapterManager
from engine.llm.base import LLMResponse
from tests._async_harness import async_to_sync

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_STORY_ID = "test_story"


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title) VALUES (?, ?);",
        (_STORY_ID, "テストストーリー"),
    )
    await manager._conn.commit()
    try:
        yield manager
    finally:
        await manager.close()


def _make_mock_router(json_text: str) -> MagicMock:
    """指定の JSON テキストを返す AsyncMock LLMRouter を生成する。"""
    mock_router = MagicMock()
    mock_router.generate = AsyncMock(return_value=LLMResponse(
        text=json_text,
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=50,
    ))
    return mock_router


async def _insert_chapter_with_goal(db: DatabaseManager) -> int:
    """goal を持つ setup beat つきのチャプターを挿入して chapter_db_id を返す。"""
    ch_id = await db.insert_chapter(_STORY_ID, {
        "chapter_id": "ch001",
        "title": "テストチャプター",
        "theme": "テスト",
        "world_injection": "テスト世界注入。",
        "status": "pending",
        "start_condition": None,  # 即起動
        "current_beat": "setup",
    })
    await db.insert_chapter_beat(ch_id, {
        "phase": "setup",
        "description": "序章の説明",
        "goal": "主人公が葛藤を認識する",
        "events_json": [],
    })
    for phase in ("complication", "turning_point", "resolution"):
        await db.insert_chapter_beat(ch_id, {
            "phase": phase,
            "description": f"{phase} の説明",
            "goal": f"{phase} のゴール",
            "events_json": [],
        })
    return ch_id


# ------------------------------------------------------------------
# test: LLM 判定が無効の場合（デフォルト）
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_beat_disabled_by_default() -> None:
    """_enable_llm_beat_judgment=False のとき LLM は呼ばれない。"""
    async with _make_db() as db:
        await _insert_chapter_with_goal(db)
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=20)
        await manager.initialize()

        mock_router = _make_mock_router('{"achieved": true, "confidence": 0.9, "reason": "達成"}')
        manager._llm_router = mock_router
        # _enable_llm_beat_judgment はデフォルト False のまま

        # activate → turn 7（timeout 前）
        await manager.process_round(turn_number=0)  # activate
        result = await manager.process_round(turn_number=7)

        mock_router.generate.assert_not_called()
        # timeout していないので beat は進まない
        assert result["beat_advanced"] is False


# ------------------------------------------------------------------
# test: LLM が achieved=True, confidence>=0.7 → timeout 前に beat が進む
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_beat_advances_before_timeout_when_achieved() -> None:
    """LLM が achieved=True, confidence=0.8 を返すと timeout 前に beat が進む。"""
    async with _make_db() as db:
        await _insert_chapter_with_goal(db)
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=20)
        await manager.initialize()

        mock_router = _make_mock_router(
            '{"achieved": true, "confidence": 0.8, "reason": "ゴール達成"}'
        )
        manager._llm_router = mock_router
        manager._llm_provider = "openai"
        manager._llm_model = "gpt-4o"
        manager._enable_llm_beat_judgment = True
        manager._llm_beat_min_elapsed = 5

        # turn 0: activate（setup 開始）
        r0 = await manager.process_round(turn_number=0)
        assert r0["activated_chapter"] is not None

        # turn 7: timeout=20 の前だが elapsed=7 >= min_elapsed=5 → LLM 判定
        r7 = await manager.process_round(turn_number=7)
        mock_router.generate.assert_called_once()
        assert r7["beat_advanced"] is True


# ------------------------------------------------------------------
# test: LLM が achieved=False → beat は進まない
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_beat_does_not_advance_when_not_achieved() -> None:
    """LLM が achieved=False を返すと beat は進まない（timeout 待ち）。"""
    async with _make_db() as db:
        await _insert_chapter_with_goal(db)
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=20)
        await manager.initialize()

        mock_router = _make_mock_router(
            '{"achieved": false, "confidence": 0.3, "reason": "まだ達成されていない"}'
        )
        manager._llm_router = mock_router
        manager._llm_provider = "openai"
        manager._llm_model = "gpt-4o"
        manager._enable_llm_beat_judgment = True
        manager._llm_beat_min_elapsed = 5

        # turn 0: activate
        await manager.process_round(turn_number=0)

        # turn 7: LLM は呼ばれるが achieved=False なので beat は進まない
        r7 = await manager.process_round(turn_number=7)
        mock_router.generate.assert_called_once()
        assert r7["beat_advanced"] is False


# ------------------------------------------------------------------
# test: LLM が高 confidence でも achieved=False なら beat は進まない
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_beat_requires_both_achieved_and_confidence() -> None:
    """confidence >= 0.7 でも achieved=False なら beat は進まない。"""
    async with _make_db() as db:
        await _insert_chapter_with_goal(db)
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=20)
        await manager.initialize()

        mock_router = _make_mock_router(
            '{"achieved": false, "confidence": 0.95, "reason": "確信はあるが未達"}'
        )
        manager._llm_router = mock_router
        manager._llm_provider = "openai"
        manager._llm_model = "gpt-4o"
        manager._enable_llm_beat_judgment = True
        manager._llm_beat_min_elapsed = 5

        await manager.process_round(turn_number=0)
        r7 = await manager.process_round(turn_number=7)
        assert r7["beat_advanced"] is False


# ------------------------------------------------------------------
# test: LLM 失敗時は beat が進まない（timeout フォールバック）
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_beat_failure_falls_back_to_timeout() -> None:
    """LLM 例外時は beat は進まない（timeout フォールバック）。"""
    async with _make_db() as db:
        await _insert_chapter_with_goal(db)
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=20)
        await manager.initialize()

        mock_router = MagicMock()
        mock_router.generate = AsyncMock(side_effect=RuntimeError("connection error"))
        manager._llm_router = mock_router
        manager._llm_provider = "openai"
        manager._llm_model = "gpt-4o"
        manager._enable_llm_beat_judgment = True
        manager._llm_beat_min_elapsed = 5

        await manager.process_round(turn_number=0)

        # turn 7: LLM 例外 → 進まない
        r7 = await manager.process_round(turn_number=7)
        assert r7["beat_advanced"] is False

        # timeout 後（turn 20）は普通に進む
        r20 = await manager.process_round(turn_number=20)
        assert r20["beat_advanced"] is True


# ------------------------------------------------------------------
# test: min_elapsed 前は LLM を呼ばない
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_beat_not_called_before_min_elapsed() -> None:
    """elapsed < min_elapsed の turn では LLM は呼ばれない。"""
    async with _make_db() as db:
        await _insert_chapter_with_goal(db)
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=20)
        await manager.initialize()

        mock_router = _make_mock_router(
            '{"achieved": true, "confidence": 0.9, "reason": "達成"}'
        )
        manager._llm_router = mock_router
        manager._llm_provider = "openai"
        manager._llm_model = "gpt-4o"
        manager._enable_llm_beat_judgment = True
        manager._llm_beat_min_elapsed = 5

        await manager.process_round(turn_number=0)  # activate

        # turn 3: elapsed=3 < min_elapsed=5 → LLM 呼ばれない
        r3 = await manager.process_round(turn_number=3)
        mock_router.generate.assert_not_called()
        assert r3["beat_advanced"] is False
