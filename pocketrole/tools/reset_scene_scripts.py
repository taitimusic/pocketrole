"""tools/reset_scene_scripts.py — ターン N 以降の scene_scripts を削除する CLI

「このターンから仕切り直したい」という場合に使用する。
scene_scripts のみ削除し、chat_logs はそのまま残す。

Usage:
    python -m tools.reset_scene_scripts --story ankoku_gakuen --from-turn 10
"""

import argparse
import asyncio
import sys
from pathlib import Path

from db.db_manager import DatabaseManager

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


async def reset(story_id: str, from_turn: int, db_path: str) -> int:
    db = DatabaseManager(db_path, _MIGRATIONS_DIR)
    await db.initialize()
    try:
        deleted = await db.delete_scene_scripts_from_turn(story_id, from_turn)
        return deleted
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ターン N 以降の scene_scripts を削除する"
    )
    parser.add_argument("--story", required=True, help="ストーリー ID")
    parser.add_argument(
        "--from-turn", type=int, required=True,
        dest="from_turn",
        help="このターン番号以降（含む）を削除する",
    )
    parser.add_argument("--db", default="db/pocketrole.db", help="SQLite DB ファイルパス")
    args = parser.parse_args()

    deleted = asyncio.run(reset(args.story, args.from_turn, args.db))
    print(f"削除件数: {deleted} 件 (story_id={args.story}, from_turn={args.from_turn})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
