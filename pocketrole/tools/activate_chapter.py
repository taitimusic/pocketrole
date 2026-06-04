"""tools.activate_chapter — pending chapter を手動で active にする CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from db.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


async def activate_chapter(
    story_id: str,
    chapter_id: str,
    *,
    turn_number: int,
    db_path: str | Path = "db/pocketrole.db",
    db: DatabaseManager | None = None,
) -> dict[str, Any]:
    """story_id + chapter_id で pending chapter を active にする。"""
    owned_db = False
    manager = db
    if manager is None:
        manager = DatabaseManager(db_path, _MIGRATIONS_DIR)
        await manager.initialize()
        owned_db = True

    try:
        active = await manager.get_active_chapter(story_id)
        if active is not None:
            raise ValueError(
                f"An active chapter already exists: {active.get('chapter_id')}"
            )

        pending = await manager.get_pending_chapters(story_id)
        chapter = next((row for row in pending if row["chapter_id"] == chapter_id), None)
        if chapter is not None:
            await manager.activate_chapter(int(chapter["id"]), opened_turn=turn_number)
            return {
                "chapter_db_id": int(chapter["id"]),
                "chapter_id": chapter["chapter_id"],
                "status": "active",
                "opened_turn": turn_number,
            }

        assert manager._conn is not None
        cursor = await manager._conn.execute(
            """
            SELECT status
            FROM story_chapters
            WHERE story_id = ? AND chapter_id = ?
            LIMIT 1;
            """,
            (story_id, chapter_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"Chapter not found: story_id={story_id}, chapter_id={chapter_id}")
        raise ValueError(f"Chapter is {row['status']}, not pending: {chapter_id}")
    finally:
        if owned_db:
            await manager.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="pending chapter を手動で active にする")
    parser.add_argument("--story", required=True, help="ストーリー ID")
    parser.add_argument("--chapter", required=True, help="chapter_id")
    parser.add_argument("--turn", required=True, type=int, help="opened_turn に使う turn 番号")
    parser.add_argument("--db", default="db/pocketrole.db", help="SQLite DB ファイルパス")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        result = asyncio.run(
            activate_chapter(
                args.story,
                args.chapter,
                turn_number=args.turn,
                db_path=args.db,
            )
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception:
        logger.exception("activate chapter failed")
        return EXIT_ERROR

    print(
        f"Chapter activated: {result['chapter_id']} "
        f"(db_id={result['chapter_db_id']}, opened_turn={result['opened_turn']})"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
