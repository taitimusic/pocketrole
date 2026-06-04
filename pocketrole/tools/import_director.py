"""tools/import_director.py — director.yaml → DB インポート CLI

Usage:
    python -m tools.import_director --story ankoku_gakuen
    python -m tools.import_director --story ankoku_gakuen --file path/to/director.yaml
    python -m tools.import_director --story ankoku_gakuen --db db/pocketrole.db

director.yaml フォーマット:
    default_active: kurosawa

    personas:
      kurosawa:
        name: "黒澤風監督"
        aesthetic:
          tension_preference: 0.8
          character_depth: 0.9
          action_preference: 0.5
          dialogue_wit: 0.4
          atmosphere_weight: 0.8
          curiosity: 0.5
        values:
          - "人間の尊厳"
          - "義理と人情の葛藤"
        traits:
          - "長回し好き"
          - "沈黙の演出"

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


async def import_director(
    story_id: str,
    director_file: Path,
    db_path: str | Path = "db/pocketrole.db",
) -> dict[str, int]:
    """director.yaml を DB にインポートする。

    Args:
        story_id: ストーリー ID（stories テーブルに存在している前提）
        director_file: director.yaml のパス
        db_path: SQLite DB ファイルパス

    Returns:
        {"personas": N, "default_active": persona_id_or_none}

    Raises:
        FileNotFoundError: YAML ファイルが見つからない
        yaml.YAMLError: YAML 解析エラー
    """
    db = DatabaseManager(db_path, _MIGRATIONS_DIR)
    await db.initialize()

    try:
        raw = yaml.safe_load(director_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"YAML 解析エラー: {director_file}: {e}") from e

    personas: dict[str, Any] = raw.get("personas", {})
    default_active: str | None = raw.get("default_active")
    persona_count = 0

    for persona_id, persona_def in personas.items():
        aesthetic = persona_def.get("aesthetic", {})
        values = persona_def.get("values", [])
        traits = persona_def.get("traits", [])

        persona_data: dict[str, Any] = {
            "persona_id": str(persona_id),
            "name": str(persona_def.get("name", persona_id)),
            "aesthetic_json": aesthetic,
            "values_json": values,
            "traits_json": traits,
            "is_active": 0,  # 後で default_active を設定
        }
        await db.upsert_director_persona(story_id, persona_data)
        persona_count += 1
        logger.info(
            "Upserted director persona",
            extra={
                "story_id": story_id,
                "persona_id": persona_id,
            },
        )

    # default_active の persona を active に設定
    if default_active and default_active in personas:
        await db.set_persona_active(story_id, default_active)
        logger.info(
            "Set default active persona",
            extra={"story_id": story_id, "persona_id": default_active},
        )
    elif personas:
        # default_active 未指定時は最初の persona を active に
        first_id = next(iter(personas))
        await db.set_persona_active(story_id, first_id)
        default_active = first_id
        logger.info(
            "Set first persona as active (default_active not specified)",
            extra={"story_id": story_id, "persona_id": first_id},
        )

    await db.close()
    return {"personas": persona_count, "default_active": default_active}


def main() -> int:
    """CLI エントリーポイント。"""
    parser = argparse.ArgumentParser(
        description="director.yaml を SQLite DB にインポートする"
    )
    parser.add_argument("--story", required=True, help="ストーリー ID（例: ankoku_gakuen）")
    parser.add_argument(
        "--file",
        default=None,
        help="director.yaml のパス（省略時: stories/<story>/director.yaml）",
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
    director_file = Path(args.file) if args.file else _STORIES_ROOT / story_id / "director.yaml"

    if not director_file.exists():
        print(f"ERROR: director.yaml が見つかりません: {director_file}", file=sys.stderr)
        return EXIT_FILE_NOT_FOUND

    try:
        result = asyncio.run(import_director(story_id, director_file, args.db))
    except yaml.YAMLError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_YAML_PARSE_ERROR
    except Exception as e:
        logger.exception("インポートエラー")
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR

    print(
        f"インポート完了: personas={result['personas']}, "
        f"default_active={result['default_active']}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
