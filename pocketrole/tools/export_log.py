#!/usr/bin/env python3
"""tools/export_log.py — chat_logs を Markdown / テキスト形式でファイル出力する CLI

Usage:
    python -m tools.export_log --story ankoku_gakuen --format markdown --output log.md
    python -m tools.export_log --story fantasy_world --provider openai --output log.md
    python -m tools.export_log --story ankoku_gakuen                     # stdout 出力

終了コード:
    0 — 成功
    1 — DB エラー / 予期しない例外
    2 — ストーリーが見つからない
"""

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
EXIT_STORY_NOT_FOUND = 2

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


# ──────────────────────────────────────────────────────────────────────────────
# 純粋フォーマット関数
# ──────────────────────────────────────────────────────────────────────────────


def format_markdown(logs: list[dict[str, Any]], story_id: str) -> str:
    """logs を Markdown 形式の文字列に変換する。"""
    lines: list[str] = [f"# ストーリーログ: {story_id}", ""]
    if not logs:
        lines.append("（ログなし）")
        return "\n".join(lines)
    for log in logs:
        lines.append("---")
        lines.append(f"**{log.get('sim_datetime', '')}** — {log.get('char_id', '')} @ {log.get('place_id', '')}")
        expression = log.get("expression", "")
        if expression:
            lines.append(f"表情: {expression}")
        provider = log.get("llm_provider", "")
        model = log.get("llm_model", "")
        if provider or model:
            lines.append(f"LLM: {provider}/{model}")
        lines.append("")
        lines.append(log.get("message", ""))
        lines.append("")
    return "\n".join(lines)


def format_text(logs: list[dict[str, Any]], story_id: str) -> str:
    """logs をテキスト形式の文字列に変換する（1ログ = 1行）。"""
    if not logs:
        return f"[{story_id}] （ログなし）"
    lines: list[str] = []
    for log in logs:
        sim_dt = log.get("sim_datetime", "")
        char_id = log.get("char_id", "")
        place_id = log.get("place_id", "")
        message = log.get("message", "")
        lines.append(f"[{sim_dt}] {char_id} ({place_id}): {message}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 非同期コア関数
# ──────────────────────────────────────────────────────────────────────────────


async def export_logs(
    story_id: str,
    fmt: str = "markdown",
    output: Path | None = None,
    db_path: str | Path = "db/pocketrole.db",
    provider: str | None = None,
    model: str | None = None,
    db: DatabaseManager | None = None,
) -> int:
    """chat_logs をファイル or stdout に出力する。

    Args:
        story_id: エクスポート対象ストーリーID
        fmt: "markdown" または "text"
        output: 出力先ファイルパス（None の場合は stdout）
        db_path: DB ファイルパス（db が None の場合に使用）
        provider: llm_provider フィルタ（None = フィルタなし）
        model: llm_model フィルタ（None = フィルタなし）
        db: DI 用 DatabaseManager（指定時は db_path を無視）

    Returns:
        EXIT_OK / EXIT_STORY_NOT_FOUND / EXIT_ERROR
    """
    async def _run(manager: DatabaseManager) -> int:
        story = await manager.get_story(story_id)
        if story is None:
            logger.error("ストーリーが見つかりません: story_id=%s", story_id)
            return EXIT_STORY_NOT_FOUND

        logs = await manager.get_chat_logs(story_id, provider, model)

        if fmt == "text":
            content = format_text(logs, story_id)
        else:
            content = format_markdown(logs, story_id)

        if output is None:
            print(content)
        else:
            output.write_text(content, encoding="utf-8")
            logger.info("ログをエクスポートしました: path=%s, rows=%d", output, len(logs))

        return EXIT_OK

    try:
        if db is not None:
            return await _run(db)
        async with DatabaseManager(db_path, _MIGRATIONS_DIR) as manager:
            return await _run(manager)
    except Exception:
        logger.exception("エクスポートエラー")
        return EXIT_ERROR


# ──────────────────────────────────────────────────────────────────────────────
# CLI エントリーポイント
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    """CLIエントリーポイント。"""
    parser = argparse.ArgumentParser(
        description="chat_logs を Markdown / テキスト形式でファイル出力する"
    )
    parser.add_argument("--story", required=True, help="ストーリーID")
    parser.add_argument(
        "--format",
        choices=["markdown", "text"],
        default="markdown",
        dest="fmt",
        help="出力フォーマット（デフォルト: markdown）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="出力先ファイルパス（省略時は stdout）",
    )
    parser.add_argument("--provider", default=None, help="llm_provider フィルタ")
    parser.add_argument("--model", default=None, help="llm_model フィルタ")
    parser.add_argument("--db", default="db/pocketrole.db", help="SQLite DB ファイルパス")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    return asyncio.run(
        export_logs(
            story_id=args.story,
            fmt=args.fmt,
            output=args.output,
            db_path=args.db,
            provider=args.provider,
            model=args.model,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
