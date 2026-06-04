"""
tests/test_director_persona_data.py — Director Persona / Swap の DB データ層テスト

in-memory SQLite + 実 migrations を使用。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from db.db_manager import DatabaseManager
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


def _sample_persona(persona_id: str = "kurosawa", is_active: int = 0) -> dict:
    return {
        "persona_id": persona_id,
        "name": f"監督_{persona_id}",
        "aesthetic_json": {"tension_preference": 0.8, "character_depth": 0.9},
        "values_json": ["人間の尊厳", "義理と人情"],
        "traits_json": ["長回し好き", "沈黙の演出"],
        "is_active": is_active,
    }


# ------------------------------------------------------------------
# test: upsert and get_active
# ------------------------------------------------------------------

@async_to_sync
async def test_upsert_and_get_active_persona() -> None:
    """upsert_director_persona で登録し、get_active_director_persona で取得できる。"""
    async with _make_db() as db:
        persona = _sample_persona("kurosawa", is_active=1)
        await db.upsert_director_persona(_STORY_ID, persona)

        active = await db.get_active_director_persona(_STORY_ID)
        assert active is not None
        assert active["persona_id"] == "kurosawa"
        assert active["name"] == "監督_kurosawa"
        assert isinstance(active["aesthetic_json"], dict)
        assert active["aesthetic_json"]["tension_preference"] == 0.8
        assert isinstance(active["values_json"], list)
        assert "人間の尊厳" in active["values_json"]
        assert isinstance(active["traits_json"], list)


@async_to_sync
async def test_no_active_persona_when_none() -> None:
    """active persona がない場合 None が返る。"""
    async with _make_db() as db:
        result = await db.get_active_director_persona(_STORY_ID)
        assert result is None


# ------------------------------------------------------------------
# test: set_persona_active switches correctly
# ------------------------------------------------------------------

@async_to_sync
async def test_set_persona_active_switches() -> None:
    """set_persona_active で active が正しく切り替わる。"""
    async with _make_db() as db:
        await db.upsert_director_persona(_STORY_ID, _sample_persona("kurosawa", is_active=1))
        await db.upsert_director_persona(_STORY_ID, _sample_persona("tarantino", is_active=0))

        # 最初は kurosawa が active
        active = await db.get_active_director_persona(_STORY_ID)
        assert active is not None
        assert active["persona_id"] == "kurosawa"

        # tarantino に切り替え
        await db.set_persona_active(_STORY_ID, "tarantino")

        active = await db.get_active_director_persona(_STORY_ID)
        assert active is not None
        assert active["persona_id"] == "tarantino"

        # kurosawa は inactive になっているはず
        all_personas = await db.get_all_director_personas(_STORY_ID)
        kurosawa = next(p for p in all_personas if p["persona_id"] == "kurosawa")
        assert kurosawa["is_active"] == 0


@async_to_sync
async def test_get_all_director_personas_order() -> None:
    """get_all_director_personas は persona_id 昇順で返る。"""
    async with _make_db() as db:
        await db.upsert_director_persona(_STORY_ID, _sample_persona("miyazaki"))
        await db.upsert_director_persona(_STORY_ID, _sample_persona("kurosawa"))
        await db.upsert_director_persona(_STORY_ID, _sample_persona("tarantino"))

        personas = await db.get_all_director_personas(_STORY_ID)
        assert len(personas) == 3
        ids = [p["persona_id"] for p in personas]
        assert ids == sorted(ids)


# ------------------------------------------------------------------
# test: insert_director_satisfaction
# ------------------------------------------------------------------

@async_to_sync
async def test_insert_satisfaction() -> None:
    """insert_director_satisfaction → get_latest_director_satisfaction で取得できる。"""
    async with _make_db() as db:
        sat = {
            "persona_id": "kurosawa",
            "turn_number": 20,
            "overall": 0.65,
            "tension_sat": 0.8,
            "character_depth_sat": 0.7,
            "pacing_sat": 0.5,
            "surprise_sat": 0.6,
            "dialogue_sat": 0.4,
            "atmosphere_sat": 0.9,
            "trend": "rising",
            "details_json": {"note": "tension が良い"},
        }
        row_id = await db.insert_director_satisfaction(_STORY_ID, sat)
        assert row_id > 0

        latest = await db.get_latest_director_satisfaction(_STORY_ID, "kurosawa")
        assert latest is not None
        assert latest["turn_number"] == 20
        assert latest["overall"] == 0.65
        assert latest["trend"] == "rising"
        assert isinstance(latest["details_json"], dict)
        assert latest["details_json"]["note"] == "tension が良い"


@async_to_sync
async def test_satisfaction_history_order() -> None:
    """get_satisfaction_history は turn_number 降順で返る。"""
    async with _make_db() as db:
        for turn in [10, 20, 30]:
            await db.insert_director_satisfaction(_STORY_ID, {
                "persona_id": "kurosawa",
                "turn_number": turn,
                "overall": 0.5,
            })

        history = await db.get_satisfaction_history(_STORY_ID, "kurosawa")
        assert len(history) == 3
        assert history[0]["turn_number"] == 30
        assert history[1]["turn_number"] == 20
        assert history[2]["turn_number"] == 10


@async_to_sync
async def test_satisfaction_history_limit() -> None:
    """get_satisfaction_history の limit が効く。"""
    async with _make_db() as db:
        for turn in [10, 20, 30, 40, 50]:
            await db.insert_director_satisfaction(_STORY_ID, {
                "persona_id": "kurosawa",
                "turn_number": turn,
                "overall": 0.4,
            })

        history = await db.get_satisfaction_history(_STORY_ID, "kurosawa", limit=3)
        assert len(history) == 3
        assert history[0]["turn_number"] == 50


@async_to_sync
async def test_latest_satisfaction_none_when_empty() -> None:
    """満足度レコードがない場合 None が返る。"""
    async with _make_db() as db:
        result = await db.get_latest_director_satisfaction(_STORY_ID, "kurosawa")
        assert result is None


# ------------------------------------------------------------------
# test: director_swap_log
# ------------------------------------------------------------------

@async_to_sync
async def test_insert_swap_log() -> None:
    """insert_director_swap_log → get_director_swap_history に出る。"""
    async with _make_db() as db:
        swap = {
            "turn_number": 35,
            "from_persona_id": "kurosawa",
            "to_persona_id": "tarantino",
            "reason": "展開が暗すぎるため",
        }
        row_id = await db.insert_director_swap_log(_STORY_ID, swap)
        assert row_id > 0

        history = await db.get_director_swap_history(_STORY_ID)
        assert len(history) == 1
        assert history[0]["turn_number"] == 35
        assert history[0]["from_persona_id"] == "kurosawa"
        assert history[0]["to_persona_id"] == "tarantino"
        assert history[0]["reason"] == "展開が暗すぎるため"


@async_to_sync
async def test_swap_log_with_null_from() -> None:
    """from_persona_id が None（初回設定）のケース。"""
    async with _make_db() as db:
        swap = {
            "turn_number": 0,
            "from_persona_id": None,
            "to_persona_id": "kurosawa",
            "reason": "初回設定",
        }
        await db.insert_director_swap_log(_STORY_ID, swap)

        history = await db.get_director_swap_history(_STORY_ID)
        assert history[0]["from_persona_id"] is None


@async_to_sync
async def test_swap_history_order() -> None:
    """get_director_swap_history は turn_number 降順で返る。"""
    async with _make_db() as db:
        for turn, to_id in [(10, "tarantino"), (30, "miyazaki"), (20, "kurosawa")]:
            await db.insert_director_swap_log(_STORY_ID, {
                "turn_number": turn,
                "to_persona_id": to_id,
            })

        history = await db.get_director_swap_history(_STORY_ID)
        turns = [h["turn_number"] for h in history]
        assert turns == sorted(turns, reverse=True)
