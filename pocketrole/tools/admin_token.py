"""tools/admin_token.py — 管理 API token の発行・一覧・失効 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from db.db_manager import DatabaseManager


MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage PocketRole admin API tokens")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--db", required=True)
    create_parser.add_argument("--label", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--db", required=True)

    revoke_parser = subparsers.add_parser("revoke")
    revoke_parser.add_argument("--db", required=True)
    revoke_parser.add_argument("--token-id", type=int, required=True)
    return parser.parse_args(argv)


async def _run_create(db_path: str, label: str) -> None:
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    try:
        token = await db.create_admin_api_token(label)
        rows = await db.list_admin_api_tokens()
        row = rows[-1]
        print(
            json.dumps(
                {
                    "id": row["id"],
                    "label": row["label"],
                    "token_prefix": row["token_prefix"],
                    "token": token,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await db.close()


async def _run_list(db_path: str) -> None:
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    try:
        print(json.dumps({"tokens": await db.list_admin_api_tokens()}, ensure_ascii=False))
    finally:
        await db.close()


async def _run_revoke(db_path: str, token_id: int) -> None:
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    try:
        revoked = await db.revoke_admin_api_token(token_id)
        print(json.dumps({"revoked": revoked, "token_id": token_id}, ensure_ascii=False))
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "create":
        asyncio.run(_run_create(args.db, args.label))
    elif args.command == "list":
        asyncio.run(_run_list(args.db))
    else:
        asyncio.run(_run_revoke(args.db, args.token_id))


if __name__ == "__main__":
    main()
