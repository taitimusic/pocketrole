"""tests/test_archive_builder.py — 公開 archive 生成のテスト。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import json
import pytest

from db.db_manager import DatabaseManager
from tests._async_harness import async_to_sync

from admin.archive_builder import ArchiveBuilder


MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
STORY_ID = "archive_story"


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    assert manager._conn is not None
    await manager._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        (STORY_ID, "公開アーカイブテスト", "openai", "gpt-4o"),
    )
    await manager._conn.commit()
    try:
        yield manager
    finally:
        await manager.close()


async def _seed_scenes(db: DatabaseManager, count: int) -> None:
    for index in range(1, count + 1):
        arc_id = await db.insert_arc(
            STORY_ID,
            {
                "arc_type": "scene",
                "title": f"場面 {index}",
                "summary": f"要約 {index}",
                "turn_from": index,
            },
        )
        await db.insert_novel_output(
            STORY_ID,
            arc_id,
            {
                "content_type": "prose",
                "content": f"本文 {index}",
                "source_log_ids": [],
                "ordering": 1,
            },
        )
        await db.close_arc(arc_id, turn_to=index)


@async_to_sync
async def test_build_story_archive_chunks_scenes_into_pages(tmp_path: Path) -> None:
    async with _make_db() as db:
        await _seed_scenes(db, 12)
        await db.upsert_story_publication(
            STORY_ID,
            visibility="public",
            published_at="2026-03-16T12:00:00Z",
        )

        builder = ArchiveBuilder(db=db, output_root=tmp_path, page_size_scenes=10)
        result = await builder.build_story_archive(STORY_ID)

        assert result.story_id == STORY_ID
        manifest_path = tmp_path / "stories" / STORY_ID / "manifest.json"
        page1_path = tmp_path / "stories" / STORY_ID / "pages" / "0001.json"
        page2_path = tmp_path / "stories" / STORY_ID / "pages" / "0002.json"
        assert manifest_path.exists()
        assert page1_path.exists()
        assert page2_path.exists()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        page1 = json.loads(page1_path.read_text(encoding="utf-8"))
        page2 = json.loads(page2_path.read_text(encoding="utf-8"))

        assert manifest["page_count"] == 2
        assert manifest["page_size_scenes"] == 10
        assert len(page1["scenes"]) == 10
        assert len(page2["scenes"]) == 2
        assert page1["scenes"][0]["title"] == "場面 1"
        assert page2["scenes"][1]["outputs"][0]["content"] == "本文 12"
        assert (tmp_path / "index.html").exists()
        assert (tmp_path / "stories" / "index.html").exists()
        assert (tmp_path / "stories" / STORY_ID / "index.html").exists()
        assert (tmp_path / "stories" / STORY_ID / "pages" / "index.html").exists()


@async_to_sync
async def test_rebuild_catalog_includes_only_public_stories(tmp_path: Path) -> None:
    async with _make_db() as db:
        await _seed_scenes(db, 1)
        await db.upsert_story_publication(STORY_ID, visibility="draft")

        builder = ArchiveBuilder(db=db, output_root=tmp_path, page_size_scenes=10)
        await builder.build_catalog()
        catalog = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
        assert catalog["stories"] == []

        await db.upsert_story_publication(STORY_ID, visibility="public")
        await builder.build_story_archive(STORY_ID)
        await builder.build_catalog()
        catalog = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
        assert [story["story_id"] for story in catalog["stories"]] == [STORY_ID]
        assert (tmp_path / "index.html").exists()
