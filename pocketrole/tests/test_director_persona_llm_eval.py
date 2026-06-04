"""
tests/test_director_persona_llm_eval.py — DirectorPersona LLM-assisted 満足度評価テスト

in-memory SQLite + AsyncMock LLMRouter を使用。LLM は実際には呼ばない。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from db.db_manager import DatabaseManager
from engine.director_persona import DirectorPersona, _parse_json_response
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


def _sample_persona(persona_id: str = "kurosawa") -> dict:
    return {
        "persona_id": persona_id,
        "name": f"テスト監督_{persona_id}",
        "aesthetic_json": {
            "tension_preference": 0.6,
            "character_depth": 0.7,
            "action_preference": 0.4,
            "dialogue_wit": 0.5,
            "atmosphere_weight": 0.5,
            "curiosity": 0.6,
        },
        "values_json": ["誠実さ"],
        "traits_json": ["沈黙の演出"],
        "is_active": 1,
    }


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


# ------------------------------------------------------------------
# unit test: _parse_json_response
# ------------------------------------------------------------------

def test_parse_json_response_extracts_object() -> None:
    """JSON オブジェクトが正しく抽出される。"""
    text = '{"character_depth_sat": 0.8, "dialogue_sat": 0.7}'
    result = _parse_json_response(text)
    assert result is not None
    assert result["character_depth_sat"] == 0.8
    assert result["dialogue_sat"] == 0.7


def test_parse_json_response_handles_markdown_fence() -> None:
    """```json ブロック内の JSON も抽出できる。"""
    text = '```json\n{"achieved": true, "confidence": 0.9}\n```'
    result = _parse_json_response(text)
    assert result is not None
    assert result["achieved"] is True


def test_parse_json_response_returns_none_on_invalid() -> None:
    """パース不能なテキストは None を返す。"""
    result = _parse_json_response("これは JSON ではありません")
    assert result is None


# ------------------------------------------------------------------
# test: LLM 評価が無効の場合（デフォルト）
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_eval_disabled_by_default() -> None:
    """_enable_llm_eval=False のとき LLM は呼ばれない。"""
    async with _make_db() as db:
        await db.upsert_director_persona(_STORY_ID, _sample_persona())
        dp = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await dp.initialize()

        mock_router = _make_mock_router('{"character_depth_sat": 0.9, "dialogue_sat": 0.9}')
        dp._llm_router = mock_router
        # _enable_llm_eval はデフォルト False のまま

        await dp.evaluate_satisfaction(turn_number=3)
        mock_router.generate.assert_not_called()


# ------------------------------------------------------------------
# test: LLM 評価が character_depth_sat / dialogue_sat を更新する
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_eval_updates_char_depth_and_dialogue_sat() -> None:
    """LLM が返した値で character_depth_sat と dialogue_sat が更新される。"""
    async with _make_db() as db:
        await db.upsert_director_persona(_STORY_ID, _sample_persona())
        dp = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await dp.initialize()

        mock_router = _make_mock_router(
            '{"character_depth_sat": 0.85, "dialogue_sat": 0.75, "overall_delta": 0.05, "suggestion": ""}'
        )
        dp._llm_router = mock_router
        dp._llm_provider = "openai"
        dp._llm_model = "gpt-4o"
        dp._enable_llm_eval = True
        dp._llm_eval_interval = 1  # 毎回評価

        result = await dp.evaluate_satisfaction(turn_number=3)
        mock_router.generate.assert_called_once()
        assert abs(float(result["character_depth_sat"]) - 0.85) < 0.001
        assert abs(float(result["dialogue_sat"]) - 0.75) < 0.001


# ------------------------------------------------------------------
# test: LLM suggestion が tone_hints に反映される
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_eval_suggestion_appears_in_tone_hints() -> None:
    """LLM の suggestion が get_steering_signals().tone_hints に含まれる。"""
    async with _make_db() as db:
        await db.upsert_director_persona(_STORY_ID, _sample_persona())
        dp = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await dp.initialize()

        suggestion = "主人公の葛藤をより深く掘り下げること"
        mock_router = _make_mock_router(
            f'{{"character_depth_sat": 0.7, "dialogue_sat": 0.6, '
            f'"overall_delta": 0.0, "suggestion": "{suggestion}"}}'
        )
        dp._llm_router = mock_router
        dp._llm_provider = "openai"
        dp._llm_model = "gpt-4o"
        dp._enable_llm_eval = True
        dp._llm_eval_interval = 1

        await dp.evaluate_satisfaction(turn_number=3)

        hints = dp.get_steering_signals().tone_hints
        assert suggestion in hints


# ------------------------------------------------------------------
# test: LLM 失敗時は deterministic 値を維持する
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_eval_failure_falls_back_to_deterministic() -> None:
    """LLM 例外時は deterministic 評価値をそのまま使う。"""
    async with _make_db() as db:
        await db.upsert_director_persona(_STORY_ID, _sample_persona())
        dp = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await dp.initialize()
        baseline = await dp._compute_satisfaction(turn_number=3)

        mock_router = MagicMock()
        mock_router.generate = AsyncMock(side_effect=RuntimeError("LLM connection error"))
        dp._llm_router = mock_router
        dp._llm_provider = "openai"
        dp._llm_model = "gpt-4o"
        dp._enable_llm_eval = True
        dp._llm_eval_interval = 1

        result = await dp.evaluate_satisfaction(turn_number=3)
        # 例外後もフォールバックして結果が返る（評価は完了する）
        assert result != {}
        assert abs(float(result["character_depth_sat"]) - baseline["character_depth_sat"]) < 0.001
        assert abs(float(result["dialogue_sat"]) - baseline["dialogue_sat"]) < 0.001


# ------------------------------------------------------------------
# test: llm_eval_interval による間引き
# ------------------------------------------------------------------

@async_to_sync
async def test_llm_eval_respects_interval() -> None:
    """_llm_eval_interval=3 の場合、3 回の評価ごとに 1 回だけ LLM が呼ばれる。"""
    async with _make_db() as db:
        await db.upsert_director_persona(_STORY_ID, _sample_persona())
        dp = DirectorPersona(_STORY_ID, db, evaluation_interval_rounds=1)
        await dp.initialize()

        mock_router = _make_mock_router(
            '{"character_depth_sat": 0.8, "dialogue_sat": 0.8, "overall_delta": 0.0, "suggestion": ""}'
        )
        dp._llm_router = mock_router
        dp._llm_provider = "openai"
        dp._llm_model = "gpt-4o"
        dp._enable_llm_eval = True
        dp._llm_eval_interval = 3  # 3 回に 1 回

        # 3 回評価（turn 3, 4, 5）
        for t in range(3, 6):
            await dp.evaluate_satisfaction(turn_number=t)

        # 1 回だけ LLM が呼ばれる（_llm_eval_count=3 のとき 3 % 3 == 0）
        assert mock_router.generate.call_count == 1
