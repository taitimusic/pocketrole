"""
tests/test_character_evolution.py — CharacterEvolutionManager のテスト

in-memory SQLite を使用。LLMRouter はモック化する。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from db.db_manager import DatabaseManager
from engine.character_evolution import CharacterEvolutionManager, _validated_memory_id
from engine.config import CharacterEvolutionConfig
from engine.llm.base import LLMResponse
from tests._async_harness import async_to_sync

# ------------------------------------------------------------------
# 定数・ヘルパー
# ------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_STORY_ID = "test_story"
_CHAR_ID = "char_a"


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    """in-memory DB を初期化し、テスト用ストーリー・キャラクターを挿入する。"""
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


def _make_cfg(**overrides: object) -> CharacterEvolutionConfig:
    """デフォルト設定を返す。overrides でフィールドを上書き可能。"""
    defaults: dict[str, object] = dict(
        enabled=True,
        check_interval_rounds=3,
        max_changes_per_check=2,
    )
    defaults.update(overrides)
    return CharacterEvolutionConfig(**defaults)  # type: ignore[arg-type]


def _make_char(char_id: str = _CHAR_ID) -> dict:
    """テスト用キャラクター dict を返す。"""
    return {
        "id": char_id,
        "current_goal": "探索",
        "current_worry": "孤独",
        "personality_core": "勇敢",
        "name_ja": "アリス" if char_id == _CHAR_ID else char_id,
    }


def _make_llm_router(response_text: str = "") -> MagicMock:
    """LLMRouter のモックを返す。generate は AsyncMock で指定テキストを返す。"""
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


# ------------------------------------------------------------------
# テスト 1: enabled=False のとき generate は呼ばれない
# ------------------------------------------------------------------


@async_to_sync
async def test_disabled_skips_processing() -> None:
    """enabled=False のとき、process_round は何もせず generate を呼ばない。"""
    router = _make_llm_router()
    cfg = _make_cfg(enabled=False)
    async with _make_db() as db:
        manager = CharacterEvolutionManager(_STORY_ID, db, router, cfg, llm_provider="ollama", llm_model="test_model")
        await manager.process_round(3, [_make_char()])

    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 2: interval 外のターンはスキップされる
# ------------------------------------------------------------------


@async_to_sync
async def test_interval_skips_processing() -> None:
    """turn=1, interval=3 のとき、1 % 3 != 0 → generate は呼ばれない。"""
    router = _make_llm_router()
    cfg = _make_cfg(check_interval_rounds=3)
    async with _make_db() as db:
        manager = CharacterEvolutionManager(_STORY_ID, db, router, cfg, llm_provider="ollama", llm_model="test_model")
        await manager.process_round(1, [_make_char()])

    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 3: story_memory が空のとき generate は呼ばれない
# ------------------------------------------------------------------


@async_to_sync
async def test_no_memories_skips_character() -> None:
    """story_memory が空のとき、generate は呼ばれない。"""
    router = _make_llm_router()
    cfg = _make_cfg(check_interval_rounds=3)
    async with _make_db() as db:
        manager = CharacterEvolutionManager(_STORY_ID, db, router, cfg, llm_provider="ollama", llm_model="test_model")
        await manager.process_round(3, [_make_char()])

    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 4: 有効 JSON → DB に evolution 1件保存
# ------------------------------------------------------------------

_VALID_EVOLUTION_JSON = json.dumps([
    {
        "field": "current_goal",
        "new_value": "世界を救う",
        "reason": "決意した",
    }
])


@async_to_sync
async def test_evolution_saved_to_db() -> None:
    """有効な JSON 応答が DB に保存され、get_evolution_overlay で取得できる。"""
    router = _make_llm_router(_VALID_EVOLUTION_JSON)
    cfg = _make_cfg(check_interval_rounds=3)
    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO story_memory
                (story_id, memory_type, summary, involved_chars, trigger_turn, importance)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "scene_summary", "アリスが試練を乗り越えた。", json.dumps([_CHAR_ID]), 1, 0.8),
        )
        await db._conn.commit()

        manager = CharacterEvolutionManager(_STORY_ID, db, router, cfg, llm_provider="ollama", llm_model="test_model")
        await manager.process_round(3, [_make_char()])

        overlay = await manager.get_evolution_overlay(_CHAR_ID)

    assert overlay["current_goal"] == "世界を救う"


# ------------------------------------------------------------------
# テスト 5: 無効 JSON → 例外なし・evolution 空
# ------------------------------------------------------------------


@async_to_sync
async def test_parse_failure_no_crash() -> None:
    """無効な JSON 応答でも例外を起こさず、evolution は空のまま。"""
    router = _make_llm_router("invalid json")
    cfg = _make_cfg(check_interval_rounds=3)
    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO story_memory
                (story_id, memory_type, summary, involved_chars, trigger_turn, importance)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "scene_summary", "謎の出来事があった。", json.dumps([_CHAR_ID]), 1, 0.8),
        )
        await db._conn.commit()

        manager = CharacterEvolutionManager(_STORY_ID, db, router, cfg, llm_provider="ollama", llm_model="test_model")
        # 例外が発生しないことを確認
        await manager.process_round(3, [_make_char()])

        overlay = await manager.get_evolution_overlay(_CHAR_ID)

    assert overlay == {}


# ------------------------------------------------------------------
# テスト 6: max_changes_per_check が適用される
# ------------------------------------------------------------------

_THREE_CHANGES_JSON = json.dumps([
    {"field": "current_goal",     "new_value": "新目標", "reason": "r1"},
    {"field": "current_worry",    "new_value": "新悩み", "reason": "r2"},
    {"field": "personality_core", "new_value": "新性格", "reason": "r3"},
])


@async_to_sync
async def test_max_changes_per_check_respected() -> None:
    """LLM が 3件返しても max_changes_per_check=2 なら 2件のみ保存される。"""
    router = _make_llm_router(_THREE_CHANGES_JSON)
    cfg = _make_cfg(check_interval_rounds=3, max_changes_per_check=2)
    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO story_memory
                (story_id, memory_type, summary, involved_chars, trigger_turn, importance)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "scene_summary", "大きな出来事があった。", json.dumps([_CHAR_ID]), 1, 0.8),
        )
        await db._conn.commit()

        manager = CharacterEvolutionManager(_STORY_ID, db, router, cfg, llm_provider="ollama", llm_model="test_model")
        await manager.process_round(3, [_make_char()])

        overlay = await db.get_latest_evolution_overlay(_STORY_ID, _CHAR_ID)

    assert len(overlay) == 2


# ------------------------------------------------------------------
# テスト 7: get_evolution_overlay が最新値を返す
# ------------------------------------------------------------------


@async_to_sync
async def test_get_evolution_overlay_returns_latest() -> None:
    """同 field で 2件挿入した場合、get_evolution_overlay は最新 turn の値を返す。"""
    async with _make_db() as db:
        assert db._conn is not None
        # turn=1: current_goal = "古い目標"
        await db._conn.execute(
            """
            INSERT INTO character_evolution
                (char_id, story_id, turn_number, field, new_value, reason)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_CHAR_ID, _STORY_ID, 1, "current_goal", "古い目標", "最初の変化"),
        )
        # turn=5: current_goal = "新しい目標"
        await db._conn.execute(
            """
            INSERT INTO character_evolution
                (char_id, story_id, turn_number, field, new_value, reason)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_CHAR_ID, _STORY_ID, 5, "current_goal", "新しい目標", "後の変化"),
        )
        await db._conn.commit()

        router = _make_llm_router()
        cfg = _make_cfg()
        manager = CharacterEvolutionManager(_STORY_ID, db, router, cfg, llm_provider="ollama", llm_model="test_model")
        overlay = await manager.get_evolution_overlay(_CHAR_ID)

    assert overlay["current_goal"] == "新しい目標"


# ------------------------------------------------------------------
# テスト 8: 複数キャラで generate が 2回呼ばれる
# ------------------------------------------------------------------


@async_to_sync
async def test_multiple_characters_processed() -> None:
    """chars=2 のとき、generate が 2回呼ばれる。"""
    router = _make_llm_router(_VALID_EVOLUTION_JSON)
    cfg = _make_cfg(check_interval_rounds=3)
    async with _make_db() as db:
        assert db._conn is not None
        # char_b も characters テーブルに挿入
        await db._conn.execute(
            "INSERT INTO characters (id, story_id, name_ja) VALUES (?, ?, ?);",
            ("char_b", _STORY_ID, "ボブ"),
        )
        # char_a の story_memory
        await db._conn.execute(
            """
            INSERT INTO story_memory
                (story_id, memory_type, summary, involved_chars, trigger_turn, importance)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "scene_summary", "アリスの出来事。", json.dumps([_CHAR_ID]), 1, 0.8),
        )
        # char_b の story_memory
        await db._conn.execute(
            """
            INSERT INTO story_memory
                (story_id, memory_type, summary, involved_chars, trigger_turn, importance)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "scene_summary", "ボブの出来事。", json.dumps(["char_b"]), 2, 0.8),
        )
        await db._conn.commit()

        chars = [_make_char("char_a"), _make_char("char_b")]
        manager = CharacterEvolutionManager(_STORY_ID, db, router, cfg, llm_provider="ollama", llm_model="test_model")
        await manager.process_round(3, chars)

    assert router.generate.call_count == 2


# ------------------------------------------------------------------
# テスト 9: _validated_memory_id — 幻覚 ID は None になる
# ------------------------------------------------------------------


def test_validated_memory_id_returns_none_for_hallucinated_id() -> None:
    """valid_ids に含まれない ID は None を返す。"""
    assert _validated_memory_id(999, {1, 2, 3}) is None
    assert _validated_memory_id("abc", {1, 2, 3}) is None
    assert _validated_memory_id(None, {1, 2, 3}) is None


def test_validated_memory_id_returns_valid_id() -> None:
    """valid_ids に含まれる ID はそのまま返す。"""
    assert _validated_memory_id(2, {1, 2, 3}) == 2
    assert _validated_memory_id("3", {1, 2, 3}) == 3


def test_validated_memory_id_none_valid_ids_allows_any() -> None:
    """valid_ids=None のとき、変換可能な値はそのまま返す。"""
    assert _validated_memory_id(42, None) == 42
    assert _validated_memory_id(None, None) is None
