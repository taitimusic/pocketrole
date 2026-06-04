"""tests/test_export_novel.py — tools/export_novel テスト。

in-memory DB に story + story_arc + novel_output を挿入して検証する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from db.db_manager import DatabaseManager
from tests._async_harness import async_to_sync
from tools.export_novel import (
    EXIT_OK,
    EXIT_STORY_NOT_FOUND,
    export_novel,
    format_markdown,
    format_text,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

_STORY_ID = "test_story"

_ARC_SCENE = {
    "arc_type": "scene",
    "title": "夜明け",
    "summary": "夜明けの場面。",
    "turn_from": 1,
}
_ARC_CHAPTER = {
    "arc_type": "chapter",
    "title": "第一章",
    "summary": "物語の始まり。",
    "turn_from": 1,
}
_OUTPUT = {
    "content_type": "prose",
    "content": "空が赤く染まった。",
    "ordering": 1,
}


# ============================================================
# フィクスチャ用ヘルパー
# ============================================================


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    """story + scene アーク + chapter アーク + novel_output 1件を持つ in-memory DB を返す。"""
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()

    # story 挿入
    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title, llm_provider, llm_model) VALUES (?, ?, ?, ?)",
        (_STORY_ID, "テストストーリー", "ollama", "qwen2.5:14b"),
    )
    await manager._conn.commit()

    # scene アーク挿入
    scene_arc_id = await manager.insert_arc(_STORY_ID, _ARC_SCENE)
    await manager.insert_novel_output(
        _STORY_ID,
        scene_arc_id,
        {**_OUTPUT, "source_log_ids": []},
    )
    await manager.close_arc(scene_arc_id, turn_to=2)

    # chapter アーク挿入（arc_type フィルタテスト用）
    await manager.insert_arc(_STORY_ID, _ARC_CHAPTER)

    try:
        yield manager
    finally:
        await manager.close()


# ============================================================
# 純粋関数テスト
# ============================================================


def test_format_markdown_empty() -> None:
    """arcs=[] の場合、（小説データなし）を含む。"""
    result = format_markdown([], {}, _STORY_ID)
    assert "（小説データなし）" in result


def test_format_markdown_contains_title() -> None:
    """arc.title が ## 見出しとして含まれる。"""
    arc = {"id": 1, "title": "夜明け", "summary": "場面の説明。"}
    result = format_markdown([arc], {1: []}, _STORY_ID)
    assert "## 夜明け" in result


def test_format_markdown_contains_prose() -> None:
    """output.content が本文として含まれる。"""
    arc = {"id": 1, "title": "夜明け", "summary": "場面の説明。"}
    output = {"content": "空が赤く染まった。"}
    result = format_markdown([arc], {1: [output]}, _STORY_ID)
    assert "空が赤く染まった。" in result


def test_format_text_contains_title() -> None:
    """arc.title が === 区切りで含まれる。"""
    arc = {"id": 1, "title": "夜明け", "summary": "場面の説明。"}
    result = format_text([arc], {1: []}, _STORY_ID)
    assert "=== 夜明け ===" in result


# ============================================================
# 統合テスト
# ============================================================


@async_to_sync
async def test_export_story_not_found(tmp_path: Path) -> None:
    """存在しない story_id → EXIT_STORY_NOT_FOUND。"""
    async with _make_db() as db:
        code = await export_novel("nonexistent_story", db=db)
        assert code == EXIT_STORY_NOT_FOUND


@async_to_sync
async def test_export_to_file(tmp_path: Path) -> None:
    """output=tmp_path/novel.md → ファイルが作成され EXIT_OK。"""
    async with _make_db() as db:
        out = tmp_path / "novel.md"
        code = await export_novel(_STORY_ID, fmt="markdown", output=out, db=db)
        assert code == EXIT_OK
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "夜明け" in content


@async_to_sync
async def test_export_arc_type_filter(tmp_path: Path) -> None:
    """arc_type="scene" → scene アークのみ含まれ、chapter は含まれない。"""
    async with _make_db() as db:
        out = tmp_path / "scene.md"
        code = await export_novel(
            _STORY_ID, fmt="markdown", output=out, arc_type="scene", db=db
        )
        assert code == EXIT_OK
        content = out.read_text(encoding="utf-8")
        assert "夜明け" in content
        assert "第一章" not in content
