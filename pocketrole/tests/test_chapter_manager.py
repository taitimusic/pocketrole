"""
tests/test_chapter_manager.py — ChapterManager ロジックのテスト

in-memory SQLite + 実 migrations を使用。LLM 不使用（deterministic のみ）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from db.db_manager import DatabaseManager
from engine.chapter_manager import ChapterManager
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


def _sample_chapter(
    chapter_id: str = "ch001",
    world_injection: str = "テストの世界注入テキスト。",
    start_condition: str | None = None,
) -> dict:
    return {
        "chapter_id": chapter_id,
        "title": "テストチャプター",
        "theme": "テスト",
        "world_injection": world_injection,
        "status": "pending",
        "start_condition": start_condition,
        "current_beat": "setup",
    }


def _sample_beat(phase: str, events: list | None = None) -> dict:
    return {
        "phase": phase,
        "description": f"{phase} フェーズ",
        "goal": f"{phase} の目標",
        "events_json": events or [],
    }


async def _insert_full_chapter(db: DatabaseManager, **kwargs) -> int:
    """4 beat 付きのチャプターを insert して chapter_db_id を返す。"""
    ch_id = await db.insert_chapter(_STORY_ID, _sample_chapter(**kwargs))
    for phase in ("setup", "complication", "turning_point", "resolution"):
        await db.insert_chapter_beat(ch_id, _sample_beat(phase))
    return ch_id


# ------------------------------------------------------------------
# test: start_condition の評価
# ------------------------------------------------------------------

@async_to_sync
async def test_pending_chapter_activates_at_turn_threshold() -> None:
    """turn >= 10 の条件で turn=10 に activate する。"""
    async with _make_db() as db:
        await _insert_full_chapter(db, start_condition="turn >= 10")
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=30)
        await manager.initialize()

        result = await manager.process_round(turn_number=10)
        assert result["activated_chapter"] is not None
        assert result["activated_chapter"]["chapter_id"] == "ch001"

        chapter = await db.get_active_chapter(_STORY_ID)
        assert chapter is not None
        assert chapter["status"] == "active"
        assert chapter["opened_turn"] == 10


@async_to_sync
async def test_no_activation_before_threshold() -> None:
    """turn=9 では threshold に達していないので activate しない。"""
    async with _make_db() as db:
        await _insert_full_chapter(db, start_condition="turn >= 10")
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=30)
        await manager.initialize()

        result = await manager.process_round(turn_number=9)
        assert result["activated_chapter"] is None
        assert await db.get_active_chapter(_STORY_ID) is None


@async_to_sync
async def test_manual_condition_does_not_auto_activate() -> None:
    """start_condition="manual" の場合は自動起動しない。"""
    async with _make_db() as db:
        await _insert_full_chapter(db, start_condition="manual")
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=30)
        await manager.initialize()

        result = await manager.process_round(turn_number=100)
        assert result["activated_chapter"] is None
        assert await db.get_active_chapter(_STORY_ID) is None


@async_to_sync
async def test_empty_condition_activates_immediately() -> None:
    """start_condition が None（空）の場合は turn=0 で即起動する。"""
    async with _make_db() as db:
        await _insert_full_chapter(db, start_condition=None)
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=30)
        await manager.initialize()

        result = await manager.process_round(turn_number=0)
        assert result["activated_chapter"] is not None
        assert await db.get_active_chapter(_STORY_ID) is not None


# ------------------------------------------------------------------
# test: beat タイムアウト進行
# ------------------------------------------------------------------

@async_to_sync
async def test_beat_advances_on_timeout() -> None:
    """beat_timeout=2 ターン後に setup → complication に進む。"""
    async with _make_db() as db:
        await _insert_full_chapter(db, start_condition=None)
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=2)
        await manager.initialize()

        # Turn 0: activate（setup 開始）
        r0 = await manager.process_round(turn_number=0)
        assert r0["activated_chapter"] is not None
        assert r0["beat_advanced"] is False

        # Turn 1: まだタイムアウトしていない (1 - 0 = 1 < 2)
        r1 = await manager.process_round(turn_number=1)
        assert r1["beat_advanced"] is False

        # Turn 2: タイムアウト (2 - 0 = 2 >= 2) → setup → complication
        r2 = await manager.process_round(turn_number=2)
        assert r2["beat_advanced"] is True

        chapter = await db.get_active_chapter(_STORY_ID)
        assert chapter is not None
        assert chapter["current_beat"] == "complication"


@async_to_sync
async def test_chapter_closes_after_resolution_beat() -> None:
    """resolution beat タイムアウトで chapter が close する。"""
    async with _make_db() as db:
        await _insert_full_chapter(db, start_condition=None)
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=2)
        await manager.initialize()

        # Turn 0: activate（setup 開始）
        await manager.process_round(turn_number=0)
        # Turn 2: setup → complication
        await manager.process_round(turn_number=2)
        # Turn 4: complication → turning_point
        await manager.process_round(turn_number=4)
        # Turn 6: turning_point → resolution
        await manager.process_round(turn_number=6)
        # Turn 8: resolution → close
        result = await manager.process_round(turn_number=8)

        assert result["closed_chapter"] is not None

        # active chapter はなくなる
        chapter = await db.get_active_chapter(_STORY_ID)
        assert chapter is None

        # DB 上で status = 'closed' を確認
        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT status, close_reason FROM story_chapters WHERE story_id = ?;",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "closed"
        assert row["close_reason"] == "completed"


# ------------------------------------------------------------------
# test: world_injection
# ------------------------------------------------------------------

@async_to_sync
async def test_world_injection_returns_text_when_active() -> None:
    """active chapter の world_injection テキストが返る。"""
    async with _make_db() as db:
        await _insert_full_chapter(
            db,
            world_injection="文化祭の準備が始まっている。",
            start_condition=None,
        )
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=30)
        await manager.initialize()
        await manager.process_round(turn_number=0)  # activate

        injection = await manager.get_current_world_injection()
        assert injection == "文化祭の準備が始まっている。"


@async_to_sync
async def test_world_injection_returns_none_when_no_chapter() -> None:
    """active chapter がない場合は None が返る。"""
    async with _make_db() as db:
        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=30)
        await manager.initialize()

        injection = await manager.get_current_world_injection()
        assert injection is None


# ------------------------------------------------------------------
# test: auto_event_injection
# ------------------------------------------------------------------

@async_to_sync
async def test_auto_event_injection_creates_interventions() -> None:
    """beat 起動時に persona 選択された event が director_interventions に変換される。"""
    async with _make_db() as db:
        ch_id = await db.insert_chapter(_STORY_ID, _sample_chapter(start_condition=None))
        # setup beat にイベントを 2 件設定
        await db.insert_chapter_beat(ch_id, {
            "phase": "setup",
            "description": "文化祭の準備",
            "goal": "参加表明",
            "events_json": [
                {"type": "announcement", "desc": "文化祭の告知ポスターが貼られた。"},
                {"type": "meeting", "desc": "生徒会長が全員に呼びかけた。"},
            ],
        })
        for phase in ("complication", "turning_point", "resolution"):
            await db.insert_chapter_beat(ch_id, _sample_beat(phase))

        manager = ChapterManager(
            _STORY_ID, db, beat_timeout_turns=30, auto_event_injection=True
        )
        await manager.initialize()

        result = await manager.process_round(turn_number=0)  # activate + inject

        # Phase 3: 候補群から最大 1 件だけ注入される
        assert len(result["injected_interventions"]) == 1

        # DB に実際に interventions が存在するか確認
        interventions = await db.get_active_interventions(_STORY_ID, turn_number=0)
        assert len(interventions) == 1
        descs = {iv["prompt_injection"] for iv in interventions}
        assert "文化祭の告知ポスターが貼られた。" in descs


@async_to_sync
async def test_beat_advances_when_injected_event_is_acknowledged() -> None:
    """timeout 前でも injected intervention が acknowledged なら beat が進行する。"""
    async with _make_db() as db:
        ch_id = await db.insert_chapter(_STORY_ID, _sample_chapter(start_condition=None))
        await db.insert_chapter_beat(
            ch_id,
            _sample_beat(
                "setup",
                events=[{"type": "announcement", "desc": "文化祭の告知ポスターが貼られた。"}],
            ),
        )
        for phase in ("complication", "turning_point", "resolution"):
            await db.insert_chapter_beat(ch_id, _sample_beat(phase))

        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=10, auto_event_injection=True)
        await manager.initialize()

        activate_result = await manager.process_round(turn_number=0)
        intervention_id = activate_result["injected_interventions"][0]
        await db.update_intervention(intervention_id, {"status": "acknowledged"})

        advance_result = await manager.process_round(turn_number=1)
        assert advance_result["beat_advanced"] is True

        chapter = await db.get_active_chapter(_STORY_ID)
        assert chapter is not None
        assert chapter["current_beat"] == "complication"


@async_to_sync
async def test_beat_advances_when_goal_matching_hook_resolves() -> None:
    """goal と一致する resolved hook があれば timeout 前でも beat が進行する。"""
    async with _make_db() as db:
        ch_id = await db.insert_chapter(_STORY_ID, _sample_chapter(start_condition=None))
        await db.insert_chapter_beat(
            ch_id,
            {
                "phase": "setup",
                "description": "文化祭の準備",
                "goal": "参加表明",
                "events_json": [],
            },
        )
        for phase in ("complication", "turning_point", "resolution"):
            await db.insert_chapter_beat(ch_id, _sample_beat(phase))

        manager = ChapterManager(_STORY_ID, db, beat_timeout_turns=10, auto_event_injection=True)
        await manager.initialize()
        await manager.process_round(turn_number=0)

        hook_id = await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "chapter_goal",
                "status": "open",
                "title": "参加表明",
                "description": "参加表明が必要な状況",
                "priority": 0.8,
            },
        )
        await db.resolve_story_hook(
            hook_id,
            resolution_log_id=None,
            resolved_turn=1,
            summary="参加表明が済んだ",
        )

        advance_result = await manager.process_round(turn_number=1)
        assert advance_result["beat_advanced"] is True

        chapter = await db.get_active_chapter(_STORY_ID)
        assert chapter is not None
        assert chapter["current_beat"] == "complication"
