#!/usr/bin/env python3
"""tools/export_novel.py — story_arc / novel_output を Markdown / テキスト形式で書き出す CLI

Usage:
    python -m tools.export_novel --story ankoku_gakuen --format markdown --output novel.md
    python -m tools.export_novel --story fantasy_world --arc-type scene

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

from db.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STORY_NOT_FOUND = 2

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_DEFAULT_DB = Path(__file__).parent.parent / "db" / "pocketrole.db"


# ──────────────────────────────────────────────────────────────────────────────
# 純粋フォーマット関数
# ──────────────────────────────────────────────────────────────────────────────


def format_markdown(
    arcs: list[dict],
    outputs_map: dict[int, list[dict]],
    story_id: str,
) -> str:
    """arcs + outputs_map を Markdown 形式の文字列に変換する。"""
    lines: list[str] = [f"# 小説: {story_id}", ""]
    if not arcs:
        lines.append("（小説データなし）")
        return "\n".join(lines)
    for arc in arcs:
        lines.append(f"## {arc['title']}")
        lines.append("")
        lines.append(f"> {arc['summary']}")
        lines.append("")
        for output in outputs_map.get(arc["id"], []):
            lines.append(output["content"])
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def format_text(
    arcs: list[dict],
    outputs_map: dict[int, list[dict]],
    story_id: str,
) -> str:
    """arcs + outputs_map をテキスト形式の文字列に変換する。"""
    if not arcs:
        return f"[{story_id}] （小説データなし）"
    sections: list[str] = []
    for arc in arcs:
        parts: list[str] = [f"=== {arc['title']} ===", arc["summary"], ""]
        for output in outputs_map.get(arc["id"], []):
            parts.append(output["content"])
        sections.append("\n".join(parts))
    return "\n\n".join(sections)


# ──────────────────────────────────────────────────────────────────────────────
# 非同期コア関数
# ──────────────────────────────────────────────────────────────────────────────


async def export_novel(
    story_id: str,
    fmt: str = "markdown",
    output: Path | None = None,
    db_path: str | Path = _DEFAULT_DB,
    arc_type: str | None = None,
    db: DatabaseManager | None = None,
) -> int:
    """story_arc / novel_output をファイル or stdout に出力する。

    Args:
        story_id: エクスポート対象ストーリーID
        fmt: "markdown" または "text"
        output: 出力先ファイルパス（None の場合は stdout）
        db_path: DB ファイルパス（db が None の場合に使用）
        arc_type: arc_type フィルタ（None = フィルタなし）
        db: DI 用 DatabaseManager（指定時は db_path を無視）

    Returns:
        EXIT_OK / EXIT_STORY_NOT_FOUND / EXIT_ERROR
    """
    async def _run(manager: DatabaseManager) -> int:
        story = await manager.get_story(story_id)
        if story is None:
            logger.error("ストーリーが見つかりません: story_id=%s", story_id)
            return EXIT_STORY_NOT_FOUND

        arcs = await manager.get_arcs(story_id, arc_type)

        all_outputs = await manager.get_all_novel_outputs_by_story(story_id)
        outputs_map: dict[int, list[dict]] = {
            arc["id"]: all_outputs.get(arc["id"], []) for arc in arcs
        }

        if fmt == "text":
            content = format_text(arcs, outputs_map, story_id)
        else:
            content = format_markdown(arcs, outputs_map, story_id)

        if output is None:
            print(content)
        else:
            output.write_text(content, encoding="utf-8")
            logger.info(
                "小説をエクスポートしました: path=%s, arcs=%d", output, len(arcs)
            )

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
        description="story_arc / novel_output を Markdown / テキスト形式で書き出す"
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
    parser.add_argument("--db", default=str(_DEFAULT_DB), help="SQLite DB ファイルパス")
    parser.add_argument(
        "--arc-type",
        default=None,
        dest="arc_type",
        help="arc_type フィルタ（例: scene, chapter）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    return asyncio.run(
        export_novel(
            story_id=args.story,
            fmt=args.fmt,
            output=args.output,
            db_path=args.db,
            arc_type=args.arc_type,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
