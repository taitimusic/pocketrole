"""
tests/test_chapter_director_integration.py — Chapter × Director Persona 統合テスト

in-memory SQLite + 実 migrations を使用。LLM 不使用（deterministic のみ）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from db.db_manager import DatabaseManager
from engine.chapter_manager import ChapterManager, _select_intervention_type
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


def _sample_persona(
    persona_id: str = "kurosawa",
    tension_pref: float = 0.5,
    character_depth: float = 0.5,
    curiosity: float = 0.5,
    atmosphere_weight: float = 0.5,
    action_pref: float = 0.5,
) -> dict:
    return {
        "persona_id": persona_id,
        "name": f"テスト監督_{persona_id}",
        "aesthetic_json": {
            "tension_preference": tension_pref,
            "character_depth": character_depth,
            "action_preference": action_pref,
            "dialogue_wit": 0.4,
            "atmosphere_weight": atmosphere_weight,
            "curiosity": curiosity,
        },
        "values_json": [],
        "traits_json": [],
        "is_active": 1,
    }


def _sample_chapter_with_events() -> dict:
    return {
        "chapter_id": "ch001",
        "title": "テストチャプター",
        "theme": "テスト",
        "world_injection": "テスト世界注入。",
        "status": "pending",
        "start_condition": None,  # 即起動
        "current_beat": "setup",
    }


def _sample_beat(phase: str, events: list | None = None) -> dict:
    return {
        "phase": phase,
        "description": f"{phase} フェーズ",
        "goal": f"{phase} の目標",
        "events_json": events or [],
    }


async def _setup_chapter_with_persona(
    db: DatabaseManager,
    tension_pref: float = 0.5,
    curiosity: float = 0.5,
    character_depth: float = 0.5,
    atmosphere_weight: float = 0.5,
    action_pref: float = 0.5,
    events: list | None = None,
) -> tuple[ChapterManager, DirectorPersona]:
    """persona を挿入・active に設定し、chapter + setup beat（events_json あり）を挿入する。"""
    # persona 挿入
    await db.upsert_director_persona(
        _STORY_ID,
        _sample_persona(
            tension_pref=tension_pref,
            curiosity=curiosity,
            character_depth=character_depth,
            atmosphere_weight=atmosphere_weight,
            action_pref=action_pref,
        ),
    )

    # chapter + beats 挿入
    ch_id = await db.insert_chapter(_STORY_ID, _sample_chapter_with_events())
    setup_events = events if events is not None else [{"type": "test_event", "desc": "テストイベントの説明文"}]
    await db.insert_chapter_beat(ch_id, _sample_beat("setup", events=setup_events))
    for phase in ("complication", "turning_point", "resolution"):
        await db.insert_chapter_beat(ch_id, _sample_beat(phase))

    # DirectorPersona を初期化
    director_persona = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
    await director_persona.initialize()

    # ChapterManager に director_persona を渡す
    chapter_manager = ChapterManager(
        _STORY_ID,
        db,
        beat_timeout_turns=30,
        director_persona=director_persona,
    )
    await chapter_manager.initialize()

    return chapter_manager, director_persona


# ------------------------------------------------------------------
# test: _select_intervention_type ユニットテスト
# ------------------------------------------------------------------

def test_select_intervention_type_empty_returns_plot_twist() -> None:
    """weights が空の場合は 'plot_twist' を返す。"""
    result = _select_intervention_type({})
    assert result == "plot_twist"


def test_select_intervention_type_invalid_keys_returns_plot_twist() -> None:
    """VALID_INTERVENTION_TYPES に含まれないキーのみの場合は 'plot_twist' を返す。"""
    result = _select_intervention_type({"relationship_shift": 2.0, "environmental": 1.8})
    assert result == "plot_twist"


def test_select_intervention_type_picks_max_weight() -> None:
    """最大 weight を持つ valid キーを選ぶ。"""
    result = _select_intervention_type({
        "plot_twist": 1.2,
        "crisis": 1.8,
        "mood": 1.0,
    })
    assert result == "crisis"


# ------------------------------------------------------------------
# test: director なしの場合は "plot_twist"
# ------------------------------------------------------------------

@async_to_sync
async def test_beat_event_uses_default_type_without_director() -> None:
    """director_persona なし → intervention_type が 'plot_twist' になる。"""
    async with _make_db() as db:
        ch_id = await db.insert_chapter(_STORY_ID, _sample_chapter_with_events())
        await db.insert_chapter_beat(
            ch_id,
            _sample_beat("setup", events=[{"type": "event", "desc": "テストイベント"}]),
        )
        for phase in ("complication", "turning_point", "resolution"):
            await db.insert_chapter_beat(ch_id, _sample_beat(phase))

        # director_persona なしで ChapterManager を構築
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=30, director_persona=None)
        await manager.initialize()

        result = await manager.process_round(turn_number=0)
        assert result["injected_interventions"]

        # DB に記録された intervention_type を確認
        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT intervention_type FROM director_interventions WHERE story_id = ?",
            (_STORY_ID,),
        )
        rows = await cursor.fetchall()
        assert rows
        for row in rows:
            assert row["intervention_type"] == "plot_twist"


# ------------------------------------------------------------------
# test: high tension_pref → crisis
# ------------------------------------------------------------------

@async_to_sync
async def test_beat_event_uses_crisis_when_high_tension_pref() -> None:
    """tension_pref=1.0 → crisis の weight が最高 → intervention_type='crisis'。"""
    async with _make_db() as db:
        manager, _ = await _setup_chapter_with_persona(
            db,
            tension_pref=1.0,  # crisis = _w(1.0) = 2.0 が最大
            curiosity=0.1,
            character_depth=0.1,
            atmosphere_weight=0.1,
            action_pref=0.1,
        )

        result = await manager.process_round(turn_number=0)
        assert result["injected_interventions"]

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT intervention_type FROM director_interventions WHERE story_id = ?",
            (_STORY_ID,),
        )
        rows = await cursor.fetchall()
        assert rows
        for row in rows:
            assert row["intervention_type"] == "crisis"


# ------------------------------------------------------------------
# test: high character_depth → relationship_catalyst
# ------------------------------------------------------------------

@async_to_sync
async def test_beat_event_uses_relationship_catalyst_when_high_depth() -> None:
    """character_depth=1.0 → relationship_catalyst の weight が最高。"""
    async with _make_db() as db:
        manager, _ = await _setup_chapter_with_persona(
            db,
            tension_pref=0.1,
            curiosity=0.1,
            character_depth=1.0,  # relationship_catalyst = _w(1.0) = 2.0 が最大
            atmosphere_weight=0.1,
            action_pref=0.1,
        )

        result = await manager.process_round(turn_number=0)
        assert result["injected_interventions"]

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT intervention_type FROM director_interventions WHERE story_id = ?",
            (_STORY_ID,),
        )
        rows = await cursor.fetchall()
        assert rows
        for row in rows:
            assert row["intervention_type"] == "relationship_catalyst"


# ------------------------------------------------------------------
# test: high curiosity → plot_twist
# ------------------------------------------------------------------

@async_to_sync
async def test_beat_event_uses_plot_twist_when_high_curiosity() -> None:
    """curiosity=1.0 → plot_twist の weight が最高。"""
    async with _make_db() as db:
        manager, _ = await _setup_chapter_with_persona(
            db,
            tension_pref=0.1,
            curiosity=1.0,  # plot_twist = _w(1.0) = 2.0 が最大
            character_depth=0.1,
            atmosphere_weight=0.1,
            action_pref=0.1,
        )

        result = await manager.process_round(turn_number=0)
        assert result["injected_interventions"]

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT intervention_type FROM director_interventions WHERE story_id = ?",
            (_STORY_ID,),
        )
        rows = await cursor.fetchall()
        assert rows
        for row in rows:
            assert row["intervention_type"] == "plot_twist"


# ------------------------------------------------------------------
# test: 同一 beat の候補 events から persona に応じて 1 件選ばれる
# ------------------------------------------------------------------

@async_to_sync
async def test_beat_events_select_single_event_per_beat() -> None:
    """同一 beat の候補 events は最大 1 件だけ注入される。"""
    async with _make_db() as db:
        multi_events = [
            {"type": "event_a", "desc": "イベントAの説明"},
            {"type": "event_b", "desc": "イベントBの説明"},
            {"type": "event_c", "desc": "イベントCの説明"},
        ]
        manager, _ = await _setup_chapter_with_persona(
            db,
            tension_pref=1.0,  # crisis が最大
            curiosity=0.1,
            character_depth=0.1,
            atmosphere_weight=0.1,
            action_pref=0.1,
            events=multi_events,
        )

        result = await manager.process_round(turn_number=0)
        assert len(result["injected_interventions"]) == 1

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT intervention_type FROM director_interventions WHERE story_id = ? ORDER BY id",
            (_STORY_ID,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        types = {row["intervention_type"] for row in rows}
        assert len(types) == 1
        assert "crisis" in types


@async_to_sync
async def test_beat_event_flavor_prefers_persona_matched_candidate() -> None:
    """preferred_event_flavors に一致する候補が優先される。"""
    async with _make_db() as db:
        events = [
            {"type": "quiet", "desc": "静かな導入", "flavor": "mood"},
            {"type": "clash", "desc": "激しい衝突", "flavor": "conflict"},
        ]
        manager, director = await _setup_chapter_with_persona(
            db,
            tension_pref=1.0,
            curiosity=0.1,
            character_depth=0.1,
            atmosphere_weight=0.1,
            action_pref=0.1,
            events=events,
        )

        assert "conflict" in director.get_steering_signals().preferred_event_flavors

        result = await manager.process_round(turn_number=0)
        assert len(result["injected_interventions"]) == 1

        interventions = await db.get_active_interventions(_STORY_ID, turn_number=0)
        assert len(interventions) == 1
        assert interventions[0]["description"] == "激しい衝突"
