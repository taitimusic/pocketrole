#!/usr/bin/env python3
"""tools/export_character_overlays.py — character_profile_overlays を YAML 化する CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from db.db_manager import DatabaseManager
from tools.character_overlay_utils import build_canonical_patch

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STORY_NOT_FOUND = 2

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_DEFAULT_DB = Path(__file__).parent.parent / "db" / "pocketrole.db"


def _default_output_path(story_id: str) -> Path:
    return Path(__file__).parent.parent / "stories" / story_id / "character_overlays.generated.yaml"


async def export_character_overlays(
    story_id: str,
    *,
    output: Path | None = None,
    db_path: str | Path = _DEFAULT_DB,
    db: DatabaseManager | None = None,
) -> int:
    """story 単位の character_profile_overlays を YAML へ書き出す。"""

    async def _run(manager: DatabaseManager) -> int:
        story = await manager.get_story(story_id)
        if story is None:
            logger.error("ストーリーが見つかりません: story_id=%s", story_id)
            return EXIT_STORY_NOT_FOUND

        assert manager._conn is not None
        cursor = await manager._conn.execute(
            """
            SELECT char_id, overlay_json, version, last_committed_turn, source_evolution_id
            FROM character_profile_overlays
            WHERE story_id = ?
            ORDER BY char_id ASC
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()

        characters: dict[str, dict[str, object]] = {}
        for row in rows:
            db_overlay = json.loads(row["overlay_json"]) if row["overlay_json"] else {}
            canonical_patch, unmapped_overlay = build_canonical_patch(db_overlay)
            characters[str(row["char_id"])] = {
                "db_overlay": db_overlay,
                "canonical_patch": canonical_patch,
                "unmapped_overlay": unmapped_overlay,
                "version": int(row["version"]),
                "last_committed_turn": row["last_committed_turn"],
                "source_evolution_id": row["source_evolution_id"],
            }

        payload = {
            "story_id": story_id,
            "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
            "source_db": str(db_path),
            "characters": characters,
        }
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        if output is None:
            print(rendered, end="")
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        return EXIT_OK

    try:
        if db is not None:
            return await _run(db)
        async with DatabaseManager(db_path, _MIGRATIONS_DIR) as manager:
            return await _run(manager)
    except Exception:
        logger.exception("overlay export failed")
        return EXIT_ERROR


def main() -> int:
    parser = argparse.ArgumentParser(description="character_profile_overlays を YAML に書き出す")
    parser.add_argument("--story", required=True, help="ストーリーID")
    parser.add_argument("--db", default=str(_DEFAULT_DB), help="SQLite DB ファイルパス")
    parser.add_argument("--output", type=Path, default=None, help="出力先 YAML パス")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    output = args.output or _default_output_path(args.story)
    return asyncio.run(
        export_character_overlays(
            story_id=args.story,
            output=output,
            db_path=args.db,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
