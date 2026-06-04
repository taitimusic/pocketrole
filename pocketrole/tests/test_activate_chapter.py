"""tools.activate_chapter のテスト。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from db.db_manager import DatabaseManager
from tests._async_harness import async_to_sync
from tools.activate_chapter import activate_chapter

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
STORY_ID = "test_story"


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title) VALUES (?, ?);",
        (STORY_ID, "テストストーリー"),
    )
    await manager._conn.commit()
    try:
        yield manager
    finally:
        await manager.close()


async def _insert_chapter(
    db: DatabaseManager,
    chapter_id: str,
    *,
    status: str = "pending",
    start_condition: str | None = "manual",
) -> int:
    row_id = await db.insert_chapter(
        STORY_ID,
        {
            "chapter_id": chapter_id,
            "title": f"{chapter_id} title",
            "theme": "test",
            "world_injection": "test injection",
            "status": "pending",
            "start_condition": start_condition,
            "current_beat": "setup",
        },
    )
    if status == "active":
        await db.activate_chapter(row_id, opened_turn=5)
    elif status == "closed":
        await db.close_chapter(row_id, closed_turn=8, reason="completed", carry_over={})
    return row_id


@async_to_sync
async def test_activate_pending_manual_chapter_by_chapter_id() -> None:
    async with _make_db() as db:
        await _insert_chapter(db, "after_school_showcase")

        result = await activate_chapter(
            STORY_ID,
            "after_school_showcase",
            turn_number=108,
            db_path=":memory:",
            db=db,
        )

        assert result["chapter_id"] == "after_school_showcase"
        assert result["status"] == "active"
        assert result["opened_turn"] == 108
        active = await db.get_active_chapter(STORY_ID)
        assert active is not None
        assert active["chapter_id"] == "after_school_showcase"


@async_to_sync
async def test_activate_chapter_rejects_when_active_exists() -> None:
    async with _make_db() as db:
        await _insert_chapter(db, "current_arc", status="active", start_condition=None)
        await _insert_chapter(db, "next_arc")

        with pytest.raises(ValueError, match="active chapter already exists"):
            await activate_chapter(
                STORY_ID,
                "next_arc",
                turn_number=108,
                db_path=":memory:",
                db=db,
            )


@async_to_sync
async def test_activate_chapter_rejects_when_not_found() -> None:
    async with _make_db() as db:
        with pytest.raises(ValueError, match="Chapter not found"):
            await activate_chapter(
                STORY_ID,
                "missing_arc",
                turn_number=108,
                db_path=":memory:",
                db=db,
            )


@async_to_sync
async def test_activate_chapter_rejects_when_not_pending() -> None:
    async with _make_db() as db:
        await _insert_chapter(db, "finished_arc", status="closed")

        with pytest.raises(ValueError, match="not pending"):
            await activate_chapter(
                STORY_ID,
                "finished_arc",
                turn_number=108,
                db_path=":memory:",
                db=db,
            )
