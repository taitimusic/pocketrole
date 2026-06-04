"""
tests/test_director_swap.py — DirectorPersona swap ロジックのテスト

in-memory SQLite + 実 migrations を使用。LLM 不使用（deterministic のみ）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from db.db_manager import DatabaseManager
from engine.director_persona import DirectorPersona
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


def _sample_persona(persona_id: str, is_active: int = 0) -> dict:
    return {
        "persona_id": persona_id,
        "name": f"監督_{persona_id}",
        "aesthetic_json": {
            "tension_preference": 0.5,
            "character_depth": 0.5,
            "action_preference": 0.5,
            "dialogue_wit": 0.5,
            "atmosphere_weight": 0.5,
            "curiosity": 0.5,
        },
        "values_json": [],
        "traits_json": [],
        "is_active": is_active,
    }


async def _insert_personas(db: DatabaseManager, *persona_ids: str, active: str) -> None:
    """複数 persona を挿入し、active に指定した persona を active にする。"""
    for pid in persona_ids:
        is_active = 1 if pid == active else 0
        await db.upsert_director_persona(_STORY_ID, _sample_persona(pid, is_active))


# ------------------------------------------------------------------
# test: swap
# ------------------------------------------------------------------

@async_to_sync
async def test_swap_changes_active_persona() -> None:
    """swap 後に get_active_director_persona() が新 persona を返す。"""
    async with _make_db() as db:
        await _insert_personas(db, "kurosawa", "tarantino", active="kurosawa")

        manager = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await manager.initialize()

        result = await manager.swap_active_persona("tarantino", turn_number=10)
        assert result["swapped"] is True
        assert result["from"] == "kurosawa"
        assert result["to"] == "tarantino"

        active = await db.get_active_director_persona(_STORY_ID)
        assert active is not None
        assert active["persona_id"] == "tarantino"


@async_to_sync
async def test_swap_logs_recorded() -> None:
    """swap 後に director_swap_log にレコードが作成される。"""
    async with _make_db() as db:
        await _insert_personas(db, "kurosawa", "miyazaki", active="kurosawa")

        manager = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await manager.initialize()

        await manager.swap_active_persona("miyazaki", turn_number=5, reason="テスト交代")

        history = await db.get_director_swap_history(_STORY_ID)
        assert len(history) == 1
        assert history[0]["from_persona_id"] == "kurosawa"
        assert history[0]["to_persona_id"] == "miyazaki"
        assert history[0]["turn_number"] == 5


@async_to_sync
async def test_get_available_personas_returns_all() -> None:
    """upsert した全 persona が get_available_personas() に含まれる。"""
    async with _make_db() as db:
        await _insert_personas(db, "kurosawa", "tarantino", "miyazaki", active="kurosawa")

        manager = DirectorPersona(_STORY_ID, db)
        await manager.initialize()

        personas = await manager.get_available_personas()
        persona_ids = {p["persona_id"] for p in personas}
        assert persona_ids == {"kurosawa", "tarantino", "miyazaki"}


@async_to_sync
async def test_get_swap_history_returns_entries() -> None:
    """複数の swap 後に get_swap_history() が全エントリを返す。"""
    async with _make_db() as db:
        await _insert_personas(db, "kurosawa", "tarantino", "miyazaki", active="kurosawa")

        manager = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await manager.initialize()

        await manager.swap_active_persona("tarantino", turn_number=5)
        await manager.swap_active_persona("miyazaki", turn_number=15)

        history = await manager.get_swap_history()
        assert len(history) == 2


@async_to_sync
async def test_swap_to_same_persona_raises() -> None:
    """現在 active な persona への swap は ValueError を送出する。"""
    async with _make_db() as db:
        await _insert_personas(db, "kurosawa", "tarantino", active="kurosawa")

        manager = DirectorPersona(_STORY_ID, db)
        await manager.initialize()

        with pytest.raises(ValueError, match="既に active"):
            await manager.swap_active_persona("kurosawa", turn_number=10)


@async_to_sync
async def test_swap_respects_mid_chapter_guard() -> None:
    """allow_mid_chapter_swap=False かつ active chapter がある場合は swap を拒否する。"""
    async with _make_db() as db:
        await _insert_personas(db, "kurosawa", "tarantino", active="kurosawa")
        chapter_id = await db.insert_chapter(
            _STORY_ID,
            {
                "chapter_id": "ch001",
                "title": "テストチャプター",
                "theme": "テスト",
                "world_injection": "テスト世界注入。",
                "status": "pending",
                "start_condition": None,
                "current_beat": "setup",
            },
        )
        await db.activate_chapter(chapter_id, opened_turn=0)

        manager = DirectorPersona(
            _STORY_ID,
            db,
            evaluation_interval_rounds=1,
            allow_mid_chapter_swap=False,
        )
        await manager.initialize()

        with pytest.raises(ValueError, match="mid-chapter"):
            await manager.swap_active_persona("tarantino", turn_number=10)
