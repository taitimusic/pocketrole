"""tools/inspect_scene_script.py — 生成された演技指導ノートを整形表示する CLI。

使い方:
  python -m tools.inspect_scene_script <story_id> [--latest N]

例:
  python -m tools.inspect_scene_script ankoku_gakuen --latest 3
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db_manager import DatabaseManager

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_SECTION_RE = re.compile(r"(〔[^〕]+〕)")


def _colorize(text: str) -> str:
    """〔〕セクションヘッダーを ANSI 太字で強調する。"""
    if not sys.stdout.isatty():
        return text
    return _SECTION_RE.sub(r"\033[1m\1\033[0m", text)


async def _run(story_id: str, latest: int) -> None:
    db = DatabaseManager("db/pocketrole.db", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    scripts = await db.get_recent_scene_scripts(story_id, limit=latest)
    if not scripts:
        print(f"[{story_id}] シーンスクリプトがまだありません。")
        return

    for i, s in enumerate(reversed(scripts)):
        turn = s.get("turn_number", "?")
        persona = s.get("director_persona_id") or "（ペルソナ未設定）"
        chapter = s.get("chapter_id") or "—"
        fmt = s.get("format_mode") or "—"
        print(f"\n{'='*60}")
        print(f"  ターン {turn} | 監督: {persona} | 章: {chapter} | モード: {fmt}")
        print(f"{'='*60}")
        text = s.get("script_text", "（空）")
        print(_colorize(text))

    await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="演技指導ノートを整形表示する")
    parser.add_argument("story_id", help="ストーリー ID（例: ankoku_gakuen）")
    parser.add_argument("--latest", type=int, default=5, help="最新 N 件を表示（デフォルト: 5）")
    args = parser.parse_args()
    asyncio.run(_run(args.story_id, args.latest))


if __name__ == "__main__":
    main()
