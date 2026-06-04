"""tests/test_export_character_overlays.py — overlay export CLI のテスト。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import yaml

from db.db_manager import DatabaseManager
from tests._async_harness import async_to_sync
from tools.export_character_overlays import (
    EXIT_OK,
    EXIT_STORY_NOT_FOUND,
    export_character_overlays,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_STORY_ID = "test_story"


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()

    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title) VALUES (?, ?)",
        (_STORY_ID, "テストストーリー"),
    )
    await manager._conn.executemany(
        "INSERT INTO characters (id, story_id, name_ja) VALUES (?, ?, ?)",
        [
            ("char_a", _STORY_ID, "A"),
            ("char_b", _STORY_ID, "B"),
        ],
    )
    await manager.upsert_character_profile_overlay(
        _STORY_ID,
        "char_a",
        {
            "overlay_json": {
                "current_goal": "本音を言う",
                "mood_tag": "restless",
            },
            "version": 2,
            "last_committed_turn": 12,
        },
    )
    await manager.upsert_character_profile_overlay(
        _STORY_ID,
        "char_b",
        {
            "overlay_json": {
                "personality_core": "静かな反抗心",
                "current_worry": "誰も信じられない",
            },
            "version": 3,
            "last_committed_turn": 14,
        },
    )

    try:
        yield manager
    finally:
        await manager.close()


@async_to_sync
async def test_export_story_not_found() -> None:
    async with _make_db() as db:
        code = await export_character_overlays("missing_story", db=db)
    assert code == EXIT_STORY_NOT_FOUND


@async_to_sync
async def test_export_character_overlays_writes_registry_and_unmapped_payload(
    tmp_path: Path,
) -> None:
    out = tmp_path / "character_overlays.generated.yaml"
    async with _make_db() as db:
        code = await export_character_overlays(_STORY_ID, output=out, db=db)

    assert code == EXIT_OK
    payload = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert payload["story_id"] == _STORY_ID
    assert payload["characters"]["char_a"]["db_overlay"]["current_goal"] == "本音を言う"
    assert payload["characters"]["char_a"]["canonical_patch"]["goal"] == "本音を言う"
    assert payload["characters"]["char_a"]["unmapped_overlay"]["mood_tag"] == "restless"
    assert payload["characters"]["char_b"]["canonical_patch"]["personality"]["type"] == "静かな反抗心"
    assert payload["characters"]["char_b"]["canonical_patch"]["worry"] == "誰も信じられない"
