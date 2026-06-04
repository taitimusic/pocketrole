"""tools/dump_scene_scripts.py — story の scene_scripts を一覧表示する CLI

Usage:
    python -m tools.dump_scene_scripts --story ankoku_gakuen
    python -m tools.dump_scene_scripts --story ankoku_gakuen --limit 20
"""

import argparse
import asyncio
import sys
from pathlib import Path

from db.db_manager import DatabaseManager

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


async def dump(story_id: str, db_path: str, limit: int) -> None:
    db = DatabaseManager(db_path, _MIGRATIONS_DIR)
    await db.initialize()
    try:
        scripts = await db.get_recent_scene_scripts(story_id, limit=limit)
        if not scripts:
            print(f"scene_scripts が見つかりません: story_id={story_id}")
            return
        print(f"story_id={story_id}  最新 {len(scripts)} 件（新しい順）\n")
        for s in scripts:
            turn = s.get("turn_number", "?")
            persona = s.get("director_persona_id") or "-"
            chapter = s.get("chapter_id") or "-"
            beat = s.get("beat_phase") or "-"
            text = str(s.get("script_text", ""))
            print(f"turn={turn}  persona={persona}  chapter={chapter}  beat={beat}")
            print(f"  {text[:120]}{'…' if len(text) > 120 else ''}")
            print()
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="scene_scripts 一覧を表示する")
    parser.add_argument("--story", required=True, help="ストーリー ID")
    parser.add_argument("--db", default="db/pocketrole.db", help="SQLite DB ファイルパス")
    parser.add_argument("--limit", type=int, default=30, help="最大表示件数（デフォルト: 30）")
    args = parser.parse_args()
    asyncio.run(dump(args.story, args.db, args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
