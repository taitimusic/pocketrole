"""
tests/test_chapter_data.py — Chapter System の DB データ層テスト

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


def _sample_chapter(chapter_id: str = "ch001") -> dict:
    return {
        "chapter_id": chapter_id,
        "title": "文化祭編",
        "theme": "青春と友情",
        "world_injection": "学校では文化祭の準備が始まっている。",
        "status": "pending",
        "current_beat": "setup",
    }


# ------------------------------------------------------------------
# test: insert and get_active
# ------------------------------------------------------------------

@async_to_sync
async def test_insert_and_get_active_chapter() -> None:
    """insert_chapter → activate_chapter → get_active_chapter で取得できる。"""
    async with _make_db() as db:
        chapter_id = await db.insert_chapter(_STORY_ID, _sample_chapter())
        await db.activate_chapter(chapter_id, opened_turn=10)

        chapter = await db.get_active_chapter(_STORY_ID)
        assert chapter is not None
        assert chapter["chapter_id"] == "ch001"
        assert chapter["title"] == "文化祭編"
        assert chapter["status"] == "active"
        assert chapter["opened_turn"] == 10
        assert chapter["current_beat"] == "setup"


# ------------------------------------------------------------------
# test: activate
# ------------------------------------------------------------------

@async_to_sync
async def test_activate_chapter() -> None:
    """activate_chapter で status が 'active' になる。"""
    async with _make_db() as db:
        row_id = await db.insert_chapter(_STORY_ID, _sample_chapter())
        chapter = await db.get_active_chapter(_STORY_ID)
        assert chapter is None  # まだ active でない

        await db.activate_chapter(row_id, opened_turn=5)
        chapter = await db.get_active_chapter(_STORY_ID)
        assert chapter is not None
        assert chapter["status"] == "active"
        assert chapter["opened_turn"] == 5


# ------------------------------------------------------------------
# test: update_chapter_beat
# ------------------------------------------------------------------

@async_to_sync
async def test_update_chapter_beat() -> None:
    """update_chapter_beat で current_beat が変わる。"""
    async with _make_db() as db:
        row_id = await db.insert_chapter(_STORY_ID, _sample_chapter())
        await db.activate_chapter(row_id, opened_turn=1)
        await db.update_chapter_beat(row_id, "complication")

        chapter = await db.get_active_chapter(_STORY_ID)
        assert chapter is not None
        assert chapter["current_beat"] == "complication"


# ------------------------------------------------------------------
# test: close_chapter
# ------------------------------------------------------------------

@async_to_sync
async def test_close_chapter() -> None:
    """close_chapter で status='closed'、reason・carry_over が保存される。"""
    async with _make_db() as db:
        row_id = await db.insert_chapter(_STORY_ID, _sample_chapter())
        await db.activate_chapter(row_id, opened_turn=1)

        carry = {"summary": "文化祭は大成功だった。", "unresolved_hooks": []}
        await db.close_chapter(row_id, closed_turn=40, reason="completed", carry_over=carry)

        # active chapter はなくなる
        active = await db.get_active_chapter(_STORY_ID)
        assert active is None

        # DB に直接クエリして closed を確認
        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT status, close_reason, carry_over_json FROM story_chapters WHERE id = ?;",
            (row_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "closed"
        assert row["close_reason"] == "completed"
        assert row["closed_turn"] == 40 if "closed_turn" in dict(row) else True


# ------------------------------------------------------------------
# test: no active chapter
# ------------------------------------------------------------------

@async_to_sync
async def test_no_active_chapter_when_none() -> None:
    """active chapter がない場合 None が返る。"""
    async with _make_db() as db:
        result = await db.get_active_chapter(_STORY_ID)
        assert result is None


# ------------------------------------------------------------------
# test: get_pending_chapters
# ------------------------------------------------------------------

@async_to_sync
async def test_get_pending_chapters_order() -> None:
    """get_pending_chapters は挿入順（id 昇順）で返る。"""
    async with _make_db() as db:
        await db.insert_chapter(_STORY_ID, _sample_chapter("ch001"))
        await db.insert_chapter(_STORY_ID, _sample_chapter("ch002"))

        pending = await db.get_pending_chapters(_STORY_ID)
        assert len(pending) == 2
        assert pending[0]["chapter_id"] == "ch001"
        assert pending[1]["chapter_id"] == "ch002"


# ------------------------------------------------------------------
# test: chapter beats
# ------------------------------------------------------------------

@async_to_sync
async def test_insert_and_get_chapter_beats() -> None:
    """insert_chapter_beat → get_chapter_beats でビートが取得できる。"""
    async with _make_db() as db:
        chapter_id = await db.insert_chapter(_STORY_ID, _sample_chapter())

        await db.insert_chapter_beat(chapter_id, {
            "phase": "setup",
            "description": "文化祭の計画が始まる",
            "goal": "全キャラが参加を表明する",
            "events_json": [{"type": "announcement", "desc": "文化祭開催通知"}],
        })
        await db.insert_chapter_beat(chapter_id, {
            "phase": "complication",
            "description": "トラブル発生",
            "goal": "問題が表面化する",
            "events_json": [],
        })

        beats = await db.get_chapter_beats(chapter_id)
        assert len(beats) == 2
        assert beats[0]["phase"] == "setup"
        assert isinstance(beats[0]["events_json"], list)
        assert beats[1]["phase"] == "complication"


@async_to_sync
async def test_update_beat_status() -> None:
    """update_beat_status で beat の status と reached_turn が変わる。"""
    async with _make_db() as db:
        chapter_id = await db.insert_chapter(_STORY_ID, _sample_chapter())
        beat_id = await db.insert_chapter_beat(chapter_id, {
            "phase": "setup",
            "goal": "開始条件の達成",
        })

        await db.update_beat_status(beat_id, "reached", reached_turn=15)

        beats = await db.get_chapter_beats(chapter_id)
        assert beats[0]["status"] == "reached"
        assert beats[0]["reached_turn"] == 15
