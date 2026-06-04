#!/usr/bin/env python3
"""tools/smoke_engine_start.py — 指定 story を /tmp コピー DB で数ターンだけ実行する CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from db.db_manager import DatabaseManager
from engine.config import load_config
from engine.llm.router import LLMRouter
from engine.llm_runtime import (
    ensure_story_llm_runtime_requirements,
    required_story_llm_providers,
)
from engine.story_engine import StoryEngine

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """コマンドライン引数を解析する。"""
    parser = argparse.ArgumentParser(
        description="指定 story を /tmp の DB コピーで少数ターン実行する"
    )
    parser.add_argument("--story", required=True, help="対象ストーリーID")
    parser.add_argument(
        "--db",
        default="db/pocketrole.db",
        help="SQLite DB ファイルパス（デフォルト: db/pocketrole.db）",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=1,
        help="実行ターン数（デフォルト: 1）",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="設定ファイルパス（デフォルト: config.yaml）",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help=".env パス（デフォルト: .env）",
    )
    parser.add_argument(
        "--llm-runtime",
        default="config/llm_runtime.local.yaml",
        dest="llm_runtime",
        help="runtime LLM 設定ファイル（デフォルト: config/llm_runtime.local.yaml）",
    )
    return parser.parse_args(argv)


def _copy_db_to_tmp(db_path: str | Path, story_id: str) -> Path:
    """作業用 DB を /tmp に複製して返す。"""
    source = Path(db_path)
    suffix = f"{story_id}-{uuid.uuid4().hex}.db"
    work_db = Path(tempfile.gettempdir()) / f"pocketrole-smoke-{suffix}"
    shutil.copy2(source, work_db)
    return work_db


async def _collect_summary(db: DatabaseManager, story_id: str) -> dict[str, Any]:
    """chat_logs から smoke 実行結果の要約を返す。"""
    assert db._conn is not None

    count_cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM chat_logs WHERE story_id = ?",
        (story_id,),
    )
    count_row = await count_cursor.fetchone()
    chat_log_count = int(count_row[0]) if count_row is not None else 0

    last_cursor = await db._conn.execute(
        """
        SELECT char_id, turn_number, LENGTH(COALESCE(message, ''))
        FROM chat_logs
        WHERE story_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (story_id,),
    )
    last_row = await last_cursor.fetchone()

    return {
        "chat_log_count": chat_log_count,
        "last_char_id": last_row[0] if last_row is not None else None,
        "last_turn_number": last_row[1] if last_row is not None else None,
        "last_message_len": last_row[2] if last_row is not None else None,
    }


async def run_smoke(
    story_id: str,
    db_path: str | Path = "db/pocketrole.db",
    *,
    turns: int = 1,
    config_path: str = "config.yaml",
    env_path: str = ".env",
    llm_runtime_path: str = "config/llm_runtime.local.yaml",
) -> dict[str, Any]:
    """指定 story を /tmp コピー DB で数ターンだけ実行し、要約を返す。"""
    if turns < 1:
        raise ValueError("turns must be >= 1")

    cfg = load_config(config_path, env_path, llm_runtime_path=llm_runtime_path)
    work_db = _copy_db_to_tmp(db_path, story_id)

    async with DatabaseManager(work_db, _MIGRATIONS_DIR) as db:
        story = await db.get_story(story_id)
        if story is None:
            raise ValueError(f"Story not found: {story_id}")
        resolved_llm_configs = ensure_story_llm_runtime_requirements(cfg, [story_id])
        required_providers = required_story_llm_providers(cfg, [story_id])

        router = LLMRouter(cfg.llm, required_providers=required_providers)
        await router.start()
        try:
            engine = StoryEngine(
                story_id,
                db,
                router,
                config=cfg,
                resolved_llm_config=resolved_llm_configs.get(story_id),
            )
            await engine.initialize()
            for _ in range(turns):
                await engine.run_one_turn()
            summary = await _collect_summary(db, story_id)
        finally:
            await router.stop()

    summary["story_id"] = story_id
    summary["turns_requested"] = turns
    summary["work_db_path"] = str(work_db)
    return summary


def main() -> int:
    """CLI エントリーポイント。"""
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        summary = asyncio.run(
            run_smoke(
                args.story,
                args.db,
                turns=args.turns,
                config_path=args.config,
                env_path=args.env,
                llm_runtime_path=args.llm_runtime,
            )
        )
    except Exception:
        logger.exception("smoke engine start failed")
        return EXIT_ERROR

    print(json.dumps(summary, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
