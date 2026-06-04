"""
tests/test_story_memory.py — StoryMemoryManager のテスト

in-memory SQLite を使用。LLMRouter はモック化する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.db_manager import DatabaseManager
from engine.config import StoryMemoryConfig
from engine.llm.base import LLMResponse
from engine.story_memory import StoryMemoryManager
from tests._async_harness import async_to_sync

# ------------------------------------------------------------------
# 定数・フィクスチャ
# ------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

_STORY_ID = "test_story"
_CHAR_ID = "char_a"


def _make_llm_router(response_text: str = "") -> MagicMock:
    """LLMRouter のモックを作る。generate は AsyncMock で指定テキストを返す。"""
    router = MagicMock()
    router.generate = AsyncMock(
        return_value=LLMResponse(
            text=response_text,
            model="test_model",
            provider="ollama",
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=100,
        )
    )
    return router


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    """in-memory DB を初期化し、テスト用のストーリー・キャラクターを挿入する。"""
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()

    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title, world_rules) VALUES (?, ?, ?);",
        (_STORY_ID, "テストストーリー", "魔法が使える世界"),
    )
    await manager._conn.execute(
        "INSERT INTO characters (id, story_id, name_ja) VALUES (?, ?, ?);",
        (_CHAR_ID, _STORY_ID, "アリス"),
    )
    await manager._conn.commit()

    try:
        yield manager
    finally:
        await manager.close()


def _make_manager(
    db: DatabaseManager,
    llm_router: MagicMock,
    *,
    enabled: bool = True,
    summarize_interval_turns: int = 10,
    importance_threshold: float = 0.3,
    max_injection_count: int = 5,
) -> StoryMemoryManager:
    config = StoryMemoryConfig(
        enabled=enabled,
        summarize_interval_turns=summarize_interval_turns,
        max_injection_count=max_injection_count,
        importance_threshold=importance_threshold,
    )
    return StoryMemoryManager(
        story_id=_STORY_ID,
        db=db,
        llm_router=llm_router,
        config=config,
        llm_provider="ollama",
        llm_model="test_model",
    )


# ------------------------------------------------------------------
# テスト 1: enabled=False のとき generate は呼ばれない
# ------------------------------------------------------------------

@async_to_sync
async def test_process_round_disabled() -> None:
    """enabled=False のとき、process_round は何もせず generate を呼ばない。"""
    router = _make_llm_router('{"summary": "test"}')
    async with _make_db() as db:
        manager = _make_manager(db, router, enabled=False)
        await manager.process_round(10)

    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 2: interval 外のターンはスキップされる
# ------------------------------------------------------------------

@async_to_sync
async def test_process_round_skips_between_intervals() -> None:
    """summarize_interval_turns=10 のとき、ターン 5 では generate を呼ばない。"""
    router = _make_llm_router('{"summary": "test"}')
    async with _make_db() as db:
        manager = _make_manager(db, router, summarize_interval_turns=10)
        await manager.process_round(5)

    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 3: 正常系 — JSON 応答 → DB 保存
# ------------------------------------------------------------------

_VALID_JSON_RESPONSE = """{
  "summary": "アリスが謎の石碑を発見した。",
  "memory_type": "scene_summary",
  "involved_chars": ["char_a"],
  "importance": 0.7,
  "emotional_tone": "mysterious"
}"""


@async_to_sync
async def test_process_round_saves_memory() -> None:
    """正常な JSON 応答が DB に保存され、各フィールドが正しいことを確認する。"""
    router = _make_llm_router(_VALID_JSON_RESPONSE)
    async with _make_db() as db:
        assert db._conn is not None

        # turn_number=1 のチャットログを挿入（since_turn=0 より大きい）
        await db._conn.execute(
            """
            INSERT INTO chat_logs (
                story_id, sim_datetime, turn_number, char_id, msg_type,
                place_id, message, expression, posted_to_web
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "2024-01-01 00:00", 1, _CHAR_ID, "say",
             "plaza", "石碑を見つけた！", "surprise", 0),
        )
        await db._conn.commit()

        manager = _make_manager(db, router, summarize_interval_turns=10)
        await manager.process_round(10)

        router.generate.assert_called_once()

        # DB に保存されたメモリを確認
        cursor = await db._conn.execute(
            "SELECT * FROM story_memory WHERE story_id = ?;",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()
        assert row is not None
        data = dict(row)

        assert data["summary"] == "アリスが謎の石碑を発見した。"
        assert data["memory_type"] == "scene_summary"
        assert data["importance"] == pytest.approx(0.7)
        assert data["emotional_tone"] == "mysterious"
        assert data["trigger_turn"] == 10


# ------------------------------------------------------------------
# テスト 4: パース失敗時はデフォルト値で保存
# ------------------------------------------------------------------

@async_to_sync
async def test_process_round_parse_failure_uses_defaults() -> None:
    """非 JSON 応答のとき、memory_type=scene_summary / importance=0.5 で保存される。"""
    router = _make_llm_router("これはJSONではありません。自由記述の要約テキストです。")
    async with _make_db() as db:
        assert db._conn is not None

        await db._conn.execute(
            """
            INSERT INTO chat_logs (
                story_id, sim_datetime, turn_number, char_id, msg_type,
                place_id, message, expression, posted_to_web
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "2024-01-01 00:00", 1, _CHAR_ID, "say",
             "plaza", "何かが起きた。", "neutral", 0),
        )
        await db._conn.commit()

        manager = _make_manager(db, router, summarize_interval_turns=10)
        await manager.process_round(10)

        assert router.generate.await_count == 2

        cursor = await db._conn.execute(
            "SELECT memory_type, importance FROM story_memory WHERE story_id = ?;",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["memory_type"] == "scene_summary"
        assert row["importance"] == pytest.approx(0.5)


@async_to_sync
async def test_process_round_compacts_story_memory_prompt_for_gemma() -> None:
    router = _make_llm_router(_VALID_JSON_RESPONSE)
    long_world_rules = "魔法が使える世界。" * 80
    long_message = "見せ場と流れの相談を延々と続ける。" * 30
    long_memory = "以前の対立と回収ポイントを細かく記録した要約。" * 20

    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            "UPDATE stories SET world_rules = ? WHERE id = ?;",
            (long_world_rules, _STORY_ID),
        )
        for turn in range(1, 13):
            await db._conn.execute(
                """
                INSERT INTO chat_logs (
                    story_id, sim_datetime, turn_number, char_id, msg_type,
                    place_id, message, expression, posted_to_web
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    _STORY_ID,
                    f"2024-01-01 00:{turn:02d}",
                    turn,
                    _CHAR_ID,
                    "say",
                    "plaza",
                    long_message,
                    "neutral",
                    0,
                ),
            )
        for idx in range(4):
            await db.save_story_memory(
                _STORY_ID,
                {
                    "memory_type": "scene_summary",
                    "importance": 0.7,
                    "summary": f"{idx}:{long_memory}",
                    "involved_chars": [_CHAR_ID],
                    "emotional_tone": "tense",
                    "trigger_turn": idx + 1,
                },
            )
        await db._conn.commit()

        manager = StoryMemoryManager(
            story_id=_STORY_ID,
            db=db,
            llm_router=router,
            config=StoryMemoryConfig(enabled=True, summarize_interval_turns=10),
            llm_provider="ollama",
            llm_model="gemma4:e4b",
        )
        await manager.process_round(10)

    primary_kwargs = router.generate.await_args_list[0].kwargs
    assert primary_kwargs["request_tag"] == "structured:story_memory:primary"
    assert primary_kwargs["max_tokens"] <= 260
    assert len(primary_kwargs["user_prompt"]) <= 1000


@async_to_sync
async def test_process_round_repairs_length_truncated_json_before_saving() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            LLMResponse(
                text='{"summary":"broken"',
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
                done_reason="length",
            ),
            LLMResponse(
                text='{"summary":"修復された要約","memory_type":"scene_summary","involved_chars":["char_a"],"importance":0.8,"emotional_tone":"comic"}',
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
            ),
        ]
    )
    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO chat_logs (
                story_id, sim_datetime, turn_number, char_id, msg_type,
                place_id, message, expression, posted_to_web
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "2024-01-01 00:00", 1, _CHAR_ID, "say",
             "plaza", "修復が必要な要約。", "neutral", 0),
        )
        await db._conn.commit()

        manager = _make_manager(db, router, summarize_interval_turns=10)
        await manager.process_round(10)

        cursor = await db._conn.execute(
            "SELECT summary, importance, emotional_tone FROM story_memory WHERE story_id = ?;",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()

    assert row is not None
    assert row["summary"] == "修復された要約"
    assert row["importance"] == pytest.approx(0.8)
    assert row["emotional_tone"] == "comic"
    assert router.generate.await_count == 2


# ------------------------------------------------------------------
# テスト 5: importance < threshold の行は除外される
# ------------------------------------------------------------------

@async_to_sync
async def test_get_relevant_memories_filters_threshold() -> None:
    """importance が importance_threshold 未満の行は get_relevant_memories から除外される。"""
    async with _make_db() as db:
        router = _make_llm_router()

        # importance=0.2 のメモリ（threshold=0.3 未満 → 除外される）
        await db.save_story_memory(
            _STORY_ID,
            {
                "memory_type": "scene_summary",
                "summary": "低重要度のメモリ",
                "involved_chars": [_CHAR_ID],
                "importance": 0.2,
                "emotional_tone": None,
                "trigger_turn": 5,
            },
        )
        # importance=0.5 のメモリ（threshold=0.3 以上 → 含まれる）
        await db.save_story_memory(
            _STORY_ID,
            {
                "memory_type": "scene_summary",
                "summary": "高重要度のメモリ",
                "involved_chars": [_CHAR_ID],
                "importance": 0.5,
                "emotional_tone": "warm",
                "trigger_turn": 10,
            },
        )

        manager = _make_manager(db, router, importance_threshold=0.3)
        results = await manager.get_relevant_memories(_CHAR_ID)

    assert len(results) == 1
    assert results[0]["summary"] == "高重要度のメモリ"
    assert results[0]["importance"] == pytest.approx(0.5)


# ------------------------------------------------------------------
# テスト 6: enabled=False のとき空リストを返す
# ------------------------------------------------------------------

@async_to_sync
async def test_get_relevant_memories_disabled() -> None:
    """enabled=False のとき、get_relevant_memories は空リストを返す。"""
    async with _make_db() as db:
        await db.save_story_memory(
            _STORY_ID,
            {
                "memory_type": "scene_summary",
                "summary": "何かが起きた。",
                "involved_chars": [_CHAR_ID],
                "importance": 0.8,
                "emotional_tone": None,
                "trigger_turn": 10,
            },
        )

        router = _make_llm_router()
        manager = _make_manager(db, router, enabled=False)
        results = await manager.get_relevant_memories(_CHAR_ID)

    assert results == []
