"""tests/test_admin_token.py — admin token CLI のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from db.db_manager import DatabaseManager
from tools import admin_token


MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


def test_admin_token_cli_create_list_revoke(tmp_path: Path, capsys: object) -> None:
    db_path = tmp_path / "admin.db"

    admin_token.main(
        [
            "create",
            "--db",
            str(db_path),
            "--label",
            "bot-agent",
        ]
    )
    created = json.loads(capsys.readouterr().out)
    assert created["label"] == "bot-agent"
    assert created["token"].startswith("ptr_")

    admin_token.main(["list", "--db", str(db_path)])
    listed = json.loads(capsys.readouterr().out)
    assert listed["tokens"][0]["label"] == "bot-agent"
    token_id = listed["tokens"][0]["id"]

    admin_token.main(["revoke", "--db", str(db_path), "--token-id", str(token_id)])
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["revoked"] is True

    admin_token.main(["list", "--db", str(db_path)])
    after_revoke = json.loads(capsys.readouterr().out)
    assert after_revoke["tokens"][0]["revoked_at"] is not None
