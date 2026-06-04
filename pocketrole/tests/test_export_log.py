"""tests/test_export_log.py — tools/export_log + db.get_chat_logs テスト。

in-memory DB に1ストーリー + 2件の chat_log（provider "openai" / "ollama"）を挿入し、
フォーマット関数・DB メソッド・export_logs 統合を検証する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from db.db_manager import DatabaseManager
from tests._async_harness import async_to_sync
from tools.export_log import (
    EXIT_OK,
    EXIT_STORY_NOT_FOUND,
    export_logs,
    format_markdown,
    format_text,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

_STORY_ID = "export_test_story"
_CHAR_ID = "char_export"
_PLACE_ID = "place_export"


# ============================================================
# フィクスチャ用ヘルパー
# ============================================================


async def _insert_story(db: DatabaseManager) -> None:
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (_STORY_ID, "テストストーリー", "openai", "gpt-4o"),
    )
    await db._conn.commit()


async def _insert_place(db: DatabaseManager) -> None:
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO places (id, story_id, label, zone)
        VALUES (?, ?, ?, ?)
        """,
        (_PLACE_ID, _STORY_ID, "テスト場所", "indoor"),
    )
    await db._conn.commit()


async def _insert_character(db: DatabaseManager) -> None:
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO characters (id, story_id, name_ja)
        VALUES (?, ?, ?)
        """,
        (_CHAR_ID, _STORY_ID, "テストキャラ"),
    )
    await db._conn.commit()


async def _insert_chat_log(
    db: DatabaseManager,
    provider: str,
    model: str,
    message: str = "テスト発言。",
) -> None:
    log: dict[str, Any] = {
        "sim_datetime": "2025-04-01T09:00:00",
        "char_id": _CHAR_ID,
        "msg_type": "monologue",
        "place_id": _PLACE_ID,
        "message": message,
        "llm_provider": provider,
        "llm_model": model,
    }
    await db.insert_chat_log(_STORY_ID, log)


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    await _insert_story(manager)
    await _insert_place(manager)
    await _insert_character(manager)
    await _insert_chat_log(manager, provider="openai", model="gpt-4o")
    await _insert_chat_log(manager, provider="ollama", model="llama3")
    try:
        yield manager
    finally:
        await manager.close()


# ============================================================
# 純粋関数テスト
# ============================================================


def _make_log(message: str = "こんにちは。", char_id: str = "char_a") -> dict[str, Any]:
    return {
        "sim_datetime": "2025-04-01T09:00:00",
        "char_id": char_id,
        "place_id": "classroom",
        "expression": "neutral",
        "message": message,
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
    }


def test_format_markdown_contains_header() -> None:
    result = format_markdown([_make_log()], "my_story")
    assert "# ストーリーログ: my_story" in result


def test_format_markdown_contains_message() -> None:
    result = format_markdown([_make_log("こんにちは。")], _STORY_ID)
    assert "こんにちは。" in result


def test_format_markdown_contains_char_id() -> None:
    result = format_markdown([_make_log(char_id="alice")], _STORY_ID)
    assert "alice" in result


def test_format_markdown_empty_logs() -> None:
    result = format_markdown([], _STORY_ID)
    assert "ログなし" in result


def test_format_text_contains_message() -> None:
    result = format_text([_make_log("テスト発言。")], _STORY_ID)
    assert "テスト発言。" in result


def test_format_text_empty_logs() -> None:
    result = format_text([], _STORY_ID)
    assert "ログなし" in result


# ============================================================
# DB メソッドテスト
# ============================================================


@async_to_sync
async def test_get_chat_logs_returns_all() -> None:
    async with _make_db() as db:
        logs = await db.get_chat_logs(_STORY_ID)
        assert len(logs) == 2


@async_to_sync
async def test_get_chat_logs_provider_filter() -> None:
    async with _make_db() as db:
        logs = await db.get_chat_logs(_STORY_ID, provider="openai")
        assert len(logs) == 1
        assert logs[0]["llm_provider"] == "openai"


@async_to_sync
async def test_get_chat_logs_model_filter() -> None:
    async with _make_db() as db:
        logs = await db.get_chat_logs(_STORY_ID, model="llama3")
        assert len(logs) == 1
        assert logs[0]["llm_model"] == "llama3"


@async_to_sync
async def test_get_chat_logs_no_match_returns_empty() -> None:
    async with _make_db() as db:
        logs = await db.get_chat_logs(_STORY_ID, provider="gemini")
        assert logs == []


# ============================================================
# 統合テスト
# ============================================================


@async_to_sync
async def test_export_logs_markdown_to_file(tmp_path: Path) -> None:
    async with _make_db() as db:
        out = tmp_path / "log.md"
        code = await export_logs(_STORY_ID, fmt="markdown", output=out, db=db)
        assert code == EXIT_OK
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "# ストーリーログ:" in content


@async_to_sync
async def test_export_logs_text_to_file(tmp_path: Path) -> None:
    async with _make_db() as db:
        out = tmp_path / "log.txt"
        code = await export_logs(_STORY_ID, fmt="text", output=out, db=db)
        assert code == EXIT_OK
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert _CHAR_ID in content


@async_to_sync
async def test_export_logs_provider_filter_in_output(tmp_path: Path) -> None:
    async with _make_db() as db:
        out = tmp_path / "openai_log.md"
        code = await export_logs(
            _STORY_ID, fmt="markdown", output=out, provider="openai", db=db
        )
        assert code == EXIT_OK
        content = out.read_text(encoding="utf-8")
        # openai ログが1件含まれる
        assert "openai" in content
        # ollama は含まれない
        assert "ollama" not in content


@async_to_sync
async def test_export_logs_story_not_found(tmp_path: Path) -> None:
    async with _make_db() as db:
        out = tmp_path / "missing.md"
        code = await export_logs("nonexistent_story", output=out, db=db)
        assert code == EXIT_STORY_NOT_FOUND
        assert not out.exists()
