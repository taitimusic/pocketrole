"""tools/switch_director.py — 監督ペルソナ切り替え CLI

Usage:
    python -m tools.switch_director --story ankoku_gakuen --persona tarantino
    python -m tools.switch_director --story ankoku_gakuen --persona tarantino \\
        --reason "展開が暗すぎるため明るい監督に交代" --turn 42

終了コード:
    0 — swap 成功
    1 — エラー（persona が存在しない、同じ persona への swap、cooldown 中など）
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from db.db_manager import DatabaseManager
from engine.config import load_config
from engine.director_persona import DirectorPersona

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


async def switch_director(
    story_id: str,
    new_persona_id: str,
    turn_number: int = 0,
    reason: str = "",
    db_path: str | Path = "db/pocketrole.db",
    config_path: str = "config.yaml",
) -> dict[str, Any]:
    """監督ペルソナを切り替える。

    Args:
        story_id: ストーリー ID
        new_persona_id: 新しい persona ID
        turn_number: 現在の turn 番号
        reason: 交代理由（任意）
        db_path: SQLite DB ファイルパス

    Returns:
        swap 結果 dict

    Raises:
        ValueError: persona が存在しない、同じ persona への swap
    """
    db = DatabaseManager(db_path, _MIGRATIONS_DIR)
    await db.initialize()

    # persona の存在確認
    all_personas = await db.get_all_director_personas(story_id)
    persona_ids = {p["persona_id"] for p in all_personas}
    if new_persona_id not in persona_ids:
        raise ValueError(
            f"persona_id {new_persona_id!r} が story {story_id!r} に存在しません。"
            f"利用可能: {sorted(persona_ids)}"
        )

    config = load_config(config_path=config_path)
    director = DirectorPersona(
        story_id,
        db,
        evaluation_interval_rounds=config.director_persona.evaluation_interval_rounds,
        steering_strength=config.director_persona.steering_strength,
        satisfaction_decay=config.director_persona.satisfaction_decay,
        allow_mid_chapter_swap=config.director_persona.allow_mid_chapter_swap,
        swap_cooldown_turns=config.director_persona.swap_cooldown_turns,
    )
    await director.initialize()

    result = await director.swap_active_persona(new_persona_id, turn_number, reason)
    await db.close()
    return result


def main() -> int:
    """CLI エントリーポイント。"""
    parser = argparse.ArgumentParser(
        description="監督ペルソナを切り替える"
    )
    parser.add_argument("--story", required=True, help="ストーリー ID（例: ankoku_gakuen）")
    parser.add_argument("--persona", required=True, help="新しい persona ID")
    parser.add_argument("--reason", default="", help="交代理由（任意）")
    parser.add_argument("--turn", type=int, default=0, help="現在の turn 番号（デフォルト: 0）")
    parser.add_argument(
        "--db",
        default="db/pocketrole.db",
        help="SQLite DB ファイルパス（デフォルト: db/pocketrole.db）",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="設定ファイルパス（デフォルト: config.yaml）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        result = asyncio.run(
            switch_director(
                args.story,
                args.persona,
                args.turn,
                args.reason,
                args.db,
                args.config,
            )
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as e:
        logger.exception("swap エラー")
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR

    from_id = result.get("from") or "(なし)"
    to_id = result.get("to", args.persona)
    print(f"監督交代: {from_id} → {to_id}")
    if args.reason:
        print(f"理由: {args.reason}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
