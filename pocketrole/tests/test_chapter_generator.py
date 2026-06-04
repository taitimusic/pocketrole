"""
tests/test_chapter_generator.py — ChapterGenerator 半自動 Chapter 生成テスト

in-memory SQLite + AsyncMock LLMRouter を使用。LLM は実際には呼ばない。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.db_manager import DatabaseManager
from engine.chapter_generator import ChapterGenerationError, ChapterGenerator
from engine.llm.base import LLMResponse
from tests._async_harness import async_to_sync

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_STORY_ID = "test_story"


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title) VALUES (?, ?);",
        (_STORY_ID, "テストストーリー"),
    )
    await manager._conn.commit()
    try:
        yield manager
    finally:
        await manager.close()


def _make_mock_router(json_text: str) -> MagicMock:
    """指定の JSON テキストを返す AsyncMock LLMRouter を生成する。"""
    mock_router = MagicMock()
    mock_router.generate = AsyncMock(return_value=LLMResponse(
        text=json_text,
        model="mock-model",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=50,
    ))
    return mock_router


def _valid_chapter_json() -> str:
    """テスト用の有効な chapter JSON 文字列を返す。"""
    chapter = {
        "chapter_id": "auto_12345",
        "title": "文化祭の混乱",
        "theme": "文化祭",
        "world_injection": "学校の文化祭が始まった。",
        "start_condition": "manual",
        "beats": [
            {
                "phase": "setup",
                "description": "準備フェーズ",
                "goal": "全員が参加を決める",
                "events_json": [{"type": "announce", "desc": "文化祭の参加呼びかけ"}],
            },
            {
                "phase": "complication",
                "description": "トラブル発生",
                "goal": "対立が表面化する",
                "events_json": [{"type": "conflict", "desc": "役割分担で意見衝突"}],
            },
            {
                "phase": "turning_point",
                "description": "転換点",
                "goal": "誰かが決断を下す",
                "events_json": [],
            },
            {
                "phase": "resolution",
                "description": "解決",
                "goal": "全員が納得する結末",
                "events_json": [],
            },
        ],
    }
    return json.dumps(chapter, ensure_ascii=False)


# ------------------------------------------------------------------
# unit test: extract_conflict_seeds（LLM なし）
# ------------------------------------------------------------------

def test_extract_conflict_seeds_high_tension_via_low_trust() -> None:
    """trust が低い relationship から "不信感" 系の seed が生成される。"""
    generator = ChapterGenerator(
        _STORY_ID,
        MagicMock(),  # db（呼ばれない）
        MagicMock(),  # llm_router（呼ばれない）
        "openai",
        "gpt-4o",
    )
    relationships = [
        {"char_id_from": "alice", "char_id_to": "bob", "trust": 0.2},
        {"char_id_from": "bob", "char_id_to": "charlie", "trust": 0.4},
    ]
    seeds = generator.extract_conflict_seeds(relationships)
    assert len(seeds) >= 1
    assert any("不信感" in s for s in seeds)


def test_extract_conflict_seeds_empty_relationships() -> None:
    """relationships=[] → 空リストが返る。"""
    generator = ChapterGenerator(
        _STORY_ID, MagicMock(), MagicMock(), "openai", "gpt-4o"
    )
    seeds = generator.extract_conflict_seeds([])
    assert seeds == []


def test_extract_conflict_seeds_with_tensions() -> None:
    """tensions の description が seed に含まれる。"""
    generator = ChapterGenerator(
        _STORY_ID, MagicMock(), MagicMock(), "openai", "gpt-4o"
    )
    tensions = [
        {"description": "部長と副部長の対立", "tension_type": "interpersonal_conflict"},
    ]
    seeds = generator.extract_conflict_seeds([], tensions)
    assert any("部長と副部長の対立" in s for s in seeds)


# ------------------------------------------------------------------
# test: generate_proposal — 正常系（LLM が有効な JSON を返す）
# ------------------------------------------------------------------

@async_to_sync
async def test_generate_proposal_calls_llm_and_stores() -> None:
    """AsyncMock LLM が有効な chapter JSON を返す → DB に保存され record が返る。"""
    async with _make_db() as db:
        mock_router = _make_mock_router(_valid_chapter_json())
        generator = ChapterGenerator(
            _STORY_ID, db, mock_router, "openai", "gpt-4o"
        )

        proposal = await generator.generate_proposal("文化祭", turn_number=5)

        mock_router.generate.assert_called_once()
        assert proposal["id"] is not None
        assert proposal["theme"] == "文化祭"
        assert proposal["admin_status"] == "pending"
        assert proposal["proposed_at_turn"] == 5

        # DB にも保存されていることを確認
        proposals = await db.get_chapter_proposals(_STORY_ID, status="pending")
        assert len(proposals) == 1
        assert proposals[0]["id"] == proposal["id"]


# ------------------------------------------------------------------
# test: generate_proposal — LLM が不正 JSON を返す → ChapterGenerationError
# ------------------------------------------------------------------

@async_to_sync
async def test_generate_proposal_invalid_json_raises_error() -> None:
    """LLM が JSON としてパース不能なテキストを返す → ChapterGenerationError が raise される。"""
    async with _make_db() as db:
        mock_router = _make_mock_router("これは JSON ではありません")
        generator = ChapterGenerator(
            _STORY_ID, db, mock_router, "openai", "gpt-4o"
        )

        with pytest.raises(ChapterGenerationError, match="could not be parsed"):
            await generator.generate_proposal("文化祭", turn_number=0)


# ------------------------------------------------------------------
# test: generate_proposal — beats が 3 件 → ChapterGenerationError
# ------------------------------------------------------------------

@async_to_sync
async def test_generate_proposal_missing_beats_raises_error() -> None:
    """beats が 3 件しかない JSON → ChapterGenerationError が raise される。"""
    async with _make_db() as db:
        chapter = json.loads(_valid_chapter_json())
        chapter["beats"] = chapter["beats"][:3]  # 1 件削除
        mock_router = _make_mock_router(json.dumps(chapter, ensure_ascii=False))
        generator = ChapterGenerator(
            _STORY_ID, db, mock_router, "openai", "gpt-4o"
        )

        with pytest.raises(ChapterGenerationError, match="exactly 4 phases"):
            await generator.generate_proposal("文化祭", turn_number=0)


# ------------------------------------------------------------------
# test: generate_proposal — LLM 例外 → ChapterGenerationError
# ------------------------------------------------------------------

@async_to_sync
async def test_generate_proposal_llm_failure_raises_error() -> None:
    """LLM が RuntimeError を raise → ChapterGenerationError が raise される。"""
    async with _make_db() as db:
        mock_router = MagicMock()
        mock_router.generate = AsyncMock(side_effect=RuntimeError("connection error"))
        generator = ChapterGenerator(
            _STORY_ID, db, mock_router, "openai", "gpt-4o"
        )

        with pytest.raises(ChapterGenerationError, match="LLM call failed"):
            await generator.generate_proposal("文化祭", turn_number=0)


# ------------------------------------------------------------------
# test: generate_proposal — persona を渡すと DB に保存される
# ------------------------------------------------------------------

@async_to_sync
async def test_generate_proposal_stores_persona_id() -> None:
    """persona を渡すと generated_by_persona_id が保存される。"""
    async with _make_db() as db:
        mock_router = _make_mock_router(_valid_chapter_json())
        generator = ChapterGenerator(
            _STORY_ID, db, mock_router, "openai", "gpt-4o"
        )
        persona = {
            "persona_id": "kurosawa",
            "name": "黒澤監督",
            "values_json": ["誠実さ"],
            "traits_json": ["沈黙の演出"],
        }

        proposal = await generator.generate_proposal("合宿", turn_number=10, persona=persona)

        assert proposal["generated_by_persona_id"] == "kurosawa"


# ------------------------------------------------------------------
# test: generate_proposal — max_events_per_beat が適用される
# ------------------------------------------------------------------

@async_to_sync
async def test_generate_proposal_trims_events() -> None:
    """max_events_per_beat=1 のとき、各 beat の events_json が 1 件にトリミングされる。"""
    async with _make_db() as db:
        chapter = json.loads(_valid_chapter_json())
        # setup beat に 3 件のイベントを設定
        chapter["beats"][0]["events_json"] = [
            {"type": "a", "desc": "event1"},
            {"type": "b", "desc": "event2"},
            {"type": "c", "desc": "event3"},
        ]
        mock_router = _make_mock_router(json.dumps(chapter, ensure_ascii=False))
        generator = ChapterGenerator(
            _STORY_ID, db, mock_router, "openai", "gpt-4o",
            max_events_per_beat=1,
        )

        proposal = await generator.generate_proposal("テスト", turn_number=0)

        saved_chapter = proposal["proposed_chapter_json"]
        setup_beat = next(b for b in saved_chapter["beats"] if b["phase"] == "setup")
        assert len(setup_beat["events_json"]) == 1
