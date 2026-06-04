"""tools/import_chapters.py — chapters.yaml → DB インポート CLI

Usage:
    python -m tools.import_chapters --story ankoku_gakuen
    python -m tools.import_chapters --story ankoku_gakuen --file path/to/chapters.yaml
    python -m tools.import_chapters --story ankoku_gakuen --db db/pocketrole.db

chapters.yaml フォーマット:
    chapters:
      - id: "ch001"
        title: "文化祭編"
        theme: "青春と友情"
        world_injection: "学校では文化祭の準備が始まっている。"
        start_condition: "turn >= 10"   # turn >= N / manual / (空) = 即起動
        beats:
          - phase: "setup"
            description: "..."
            goal: "..."
            events:
              - type: "announcement"
                desc: "..."

終了コード:
    0 — インポート成功
    1 — DB エラー
    2 — ファイルが見つからない
    3 — YAML 解析エラー
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from db.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_YAML_PARSE_ERROR = 3

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_STORIES_ROOT = Path(__file__).parent.parent / "stories"


async def import_chapters(
    story_id: str,
    chapters_file: Path,
    db_path: str | Path = "db/pocketrole.db",
) -> dict[str, int]:
    """chapters.yaml を DB にインポートする。

    Args:
        story_id: ストーリー ID（stories テーブルに存在している前提）
        chapters_file: chapters.yaml のパス
        db_path: SQLite DB ファイルパス

    Returns:
        {"chapters": N, "beats": M}

    Raises:
        FileNotFoundError: YAML ファイルが見つからない
        yaml.YAMLError: YAML 解析エラー
    """
    db = DatabaseManager(db_path, _MIGRATIONS_DIR)
    await db.initialize()

    try:
        raw = yaml.safe_load(chapters_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"YAML 解析エラー: {chapters_file}: {e}") from e

    chapters: list[dict[str, Any]] = raw.get("chapters", [])
    chapter_count = 0
    beat_count = 0

    for ch in chapters:
        chapter_data: dict[str, Any] = {
            "chapter_id": str(ch["id"]),
            "title": str(ch["title"]),
            "theme": ch.get("theme"),
            "world_injection": ch.get("world_injection"),
            "status": "pending",
            "start_condition": ch.get("start_condition"),
            "current_beat": "setup",
        }
        chapter_db_id = await db.insert_chapter(story_id, chapter_data)
        chapter_count += 1
        logger.info(
            "Inserted chapter",
            extra={
                "story_id": story_id,
                "chapter_id": ch["id"],
                "db_id": chapter_db_id,
            },
        )

        for beat in ch.get("beats", []):
            beat_data: dict[str, Any] = {
                "phase": str(beat["phase"]),
                "description": beat.get("description"),
                "goal": beat.get("goal"),
                "events_json": beat.get("events", []),
            }
            await db.insert_chapter_beat(chapter_db_id, beat_data)
            beat_count += 1

    await db.close()
    return {"chapters": chapter_count, "beats": beat_count}


def main() -> int:
    """CLI エントリーポイント。"""
    parser = argparse.ArgumentParser(
        description="chapters.yaml を SQLite DB にインポートする"
    )
    parser.add_argument("--story", required=True, help="ストーリー ID（例: ankoku_gakuen）")
    parser.add_argument(
        "--file",
        default=None,
        help="chapters.yaml のパス（省略時: stories/<story>/chapters.yaml）",
    )
    parser.add_argument(
        "--db",
        default="db/pocketrole.db",
        help="SQLite DB ファイルパス（デフォルト: db/pocketrole.db）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    story_id: str = args.story
    chapters_file = Path(args.file) if args.file else _STORIES_ROOT / story_id / "chapters.yaml"

    if not chapters_file.exists():
        print(f"ERROR: chapters.yaml が見つかりません: {chapters_file}", file=sys.stderr)
        return EXIT_FILE_NOT_FOUND

    try:
        counts = asyncio.run(import_chapters(story_id, chapters_file, args.db))
    except yaml.YAMLError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_YAML_PARSE_ERROR
    except Exception as e:
        logger.exception("インポートエラー")
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR

    print(
        f"インポート完了: chapters={counts['chapters']}, beats={counts['beats']}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
