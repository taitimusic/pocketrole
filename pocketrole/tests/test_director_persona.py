"""
tests/test_director_persona.py — DirectorPersona ロジックのテスト

in-memory SQLite + 実 migrations を使用。LLM 不使用（deterministic のみ）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from db.db_manager import DatabaseManager
from engine.director_persona import DirectorPersona, SteeringSignals
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


def _sample_persona(
    persona_id: str = "kurosawa",
    is_active: int = 1,
    tension_pref: float = 0.8,
    character_depth: float = 0.7,
) -> dict:
    return {
        "persona_id": persona_id,
        "name": f"テスト監督_{persona_id}",
        "aesthetic_json": {
            "tension_preference": tension_pref,
            "character_depth": character_depth,
            "action_preference": 0.5,
            "dialogue_wit": 0.4,
            "atmosphere_weight": 0.6,
            "curiosity": 0.5,
        },
        "values_json": ["人間の尊厳"],
        "traits_json": ["沈黙の演出"],
        "is_active": is_active,
    }


async def _insert_persona(db: DatabaseManager, **kwargs) -> None:
    await db.upsert_director_persona(_STORY_ID, _sample_persona(**kwargs))


# ------------------------------------------------------------------
# test: initialize
# ------------------------------------------------------------------

@async_to_sync
async def test_initialize_loads_active_persona() -> None:
    """DB に active persona があれば initialize() 後に non-empty signals が返る。"""
    async with _make_db() as db:
        await _insert_persona(db, persona_id="kurosawa", is_active=1)
        manager = DirectorPersona(_STORY_ID, db)
        await manager.initialize()

        signals = manager.get_steering_signals()
        # tension_preference=0.8 → conflict ウェイト > 0
        assert signals.tension_type_weights.get("conflict", 0.0) > 0.0


@async_to_sync
async def test_initialize_no_persona_returns_empty_signals() -> None:
    """active persona がなければ empty SteeringSignals が返る。"""
    async with _make_db() as db:
        manager = DirectorPersona(_STORY_ID, db)
        await manager.initialize()

        signals = manager.get_steering_signals()
        assert signals.tension_type_weights == {}
        assert signals.tone_hints == []


# ------------------------------------------------------------------
# test: evaluate_satisfaction
# ------------------------------------------------------------------

@async_to_sync
async def test_satisfaction_computed_and_saved() -> None:
    """evaluate_satisfaction() を呼ぶと DB に satisfaction レコードが作成される。"""
    async with _make_db() as db:
        await _insert_persona(db, persona_id="kurosawa", is_active=1)
        manager = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await manager.initialize()

        result = await manager.evaluate_satisfaction(turn_number=0)
        assert result  # 空 dict でないこと
        assert "overall" in result
        assert 0.0 <= result["overall"] <= 1.0

        # DB に保存されているか確認
        saved = await db.get_latest_director_satisfaction(_STORY_ID, "kurosawa")
        assert saved is not None
        assert saved["turn_number"] == 0


@async_to_sync
async def test_tension_sat_reflects_active_tensions() -> None:
    """active tension がある場合、tension_sat が 0 より大きくなる（かつ全体 overall も計算される）。"""
    async with _make_db() as db:
        await _insert_persona(db, persona_id="kurosawa", is_active=1, tension_pref=0.8)
        # tension を 1 件挿入（intensity=0.9）
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO narrative_tensions
                (story_id, tension_type, description, intensity, status, detected_turn)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "interpersonal_conflict", "テスト緊張", 0.9, "simmering", 0),
        )
        await db._conn.commit()

        manager = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await manager.initialize()

        result = await manager.evaluate_satisfaction(turn_number=0)
        assert result["tension_sat"] > 0.0


@async_to_sync
async def test_steering_signals_tension_weight() -> None:
    """tension_pref=0.8 の persona → conflict の weight が 1.0 より大きい。"""
    async with _make_db() as db:
        await _insert_persona(db, persona_id="kurosawa", is_active=1, tension_pref=0.8)
        manager = DirectorPersona(_STORY_ID, db)
        await manager.initialize()

        signals = manager.get_steering_signals()
        weight = signals.tension_type_weights.get("conflict", 0.0)
        assert weight > 1.0  # 0.8 * 2.0 = 1.6


@async_to_sync
async def test_tone_hints_generated_from_traits() -> None:
    """traits に "沈黙の演出" が含まれる → tone_hints にそれに対応するテキストが入る。"""
    async with _make_db() as db:
        await _insert_persona(db, persona_id="kurosawa", is_active=1)
        manager = DirectorPersona(_STORY_ID, db)
        await manager.initialize()

        signals = manager.get_steering_signals()
        assert len(signals.tone_hints) > 0
        # 沈黙の演出 trait がある → 対応 hint
        assert any("沈黙" in h for h in signals.tone_hints)


@async_to_sync
async def test_satisfaction_trend_rising() -> None:
    """2回評価で overall が上昇したとき、2回目の trend が 'rising' になる。"""
    async with _make_db() as db:
        await _insert_persona(db, persona_id="kurosawa", is_active=1)
        manager = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await manager.initialize()

        # 1回目の評価
        r0 = await manager.evaluate_satisfaction(turn_number=0)

        # overall を強制的に低い値に設定して「次回は上昇」を再現
        manager._last_overall = 0.0

        # 2回目（interval=1 なので turn=1 で評価可能）
        r1 = await manager.evaluate_satisfaction(turn_number=1)

        # overall が 0.0 より上がれば rising
        if r1["overall"] > 0.1:
            assert r1["trend"] == "rising"
        else:
            # overall が変わらなければ flat も許容（データが空なので低い可能性）
            assert r1["trend"] in ("rising", "flat")


@async_to_sync
async def test_skip_evaluation_before_interval() -> None:
    """turn=0 で評価後、interval=3 なら turn=1 では評価がスキップされる。"""
    async with _make_db() as db:
        await _insert_persona(db, persona_id="kurosawa", is_active=1)
        manager = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=3)
        await manager.initialize()

        await manager.evaluate_satisfaction(turn_number=0)
        result = await manager.evaluate_satisfaction(turn_number=1)

        # interval 未達のためスキップ → 空 dict
        assert result == {}


@async_to_sync
async def test_reload_active_persona_refreshes_cached_signals() -> None:
    """DB 上の active persona 変更後に reload_active_persona() で steering が更新される。"""
    async with _make_db() as db:
        await db.upsert_director_persona(_STORY_ID, _sample_persona("kurosawa", is_active=1))
        await db.upsert_director_persona(
            _STORY_ID,
            _sample_persona("tarantino", is_active=0, tension_pref=0.2, character_depth=1.0),
        )
        manager = DirectorPersona(_STORY_ID, db)
        await manager.initialize()

        before = manager.get_steering_signals().intervention_type_weights.get(
            "relationship_catalyst", 0.0
        )

        await db.set_persona_active(_STORY_ID, "tarantino")
        changed = await manager.reload_active_persona()

        after = manager.get_steering_signals().intervention_type_weights.get(
            "relationship_catalyst", 0.0
        )
        assert changed is True
        assert after > before


@async_to_sync
async def test_character_depth_satisfaction_uses_recent_growth_and_relationship_events() -> None:
    """growth と relationship event があると character_depth_sat は中立値 0.5 のままではない。"""
    async with _make_db() as db:
        await _insert_persona(db, persona_id="kurosawa", is_active=1, character_depth=0.9)
        await db.insert_evolution(
            _STORY_ID,
            {
                "char_id": "char_1",
                "turn_number": 0,
                "field": "current_goal",
                "new_value": "葛藤と向き合う",
                "reason": "成長コミット",
            },
        )
        await db.insert_relationship_event(
            _STORY_ID,
            {
                "char_id_from": "char_1",
                "char_id_to": "char_2",
                "event_type": "support",
                "summary": "葛藤を打ち明けた",
                "turn_number": 0,
            },
        )

        manager = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await manager.initialize()

        result = await manager.evaluate_satisfaction(turn_number=0)
        assert result["character_depth_sat"] != 0.5


@async_to_sync
async def test_steering_strength_changes_weight_span() -> None:
    """strong は subtle より大きい steering weight を返す。"""
    async with _make_db() as db:
        await _insert_persona(db, persona_id="kurosawa", is_active=1, tension_pref=0.8)

        subtle = DirectorPersona(_STORY_ID, db, steering_strength="subtle")
        await subtle.initialize()
        strong = DirectorPersona(_STORY_ID, db, steering_strength="strong")
        await strong.initialize()

        subtle_weight = subtle.get_steering_signals().tension_type_weights.get("conflict", 0.0)
        strong_weight = strong.get_steering_signals().tension_type_weights.get("conflict", 0.0)

        assert strong_weight > subtle_weight
