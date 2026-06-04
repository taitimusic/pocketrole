"""tools/init_web_post_targets.py — story ごとの Web 投稿設定ファイルを初期化する。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


DEFAULT_OUTPUT_PATH = Path("config/web_post_targets.local.yaml")


def init_web_post_targets_file(output_path: Path, stories: list[str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if output_path.exists():
        loaded = yaml.safe_load(output_path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            existing = loaded

    for story_id in stories:
        if story_id in existing and isinstance(existing[story_id], dict):
            continue
        existing[story_id] = {
            "enabled": True,
            "receiver_url": "",
            "auth_token": "",
        }

    output_path.write_text(
        yaml.safe_dump(existing, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="init_web_post_targets",
        description="story ごとの Web 投稿設定ファイルを初期化します。",
    )
    parser.add_argument("--stories", nargs="+", required=True, help="初期化対象の story_id")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="出力する local yaml のパス",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    init_web_post_targets_file(Path(args.output), args.stories)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
