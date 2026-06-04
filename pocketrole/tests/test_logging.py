"""tests/test_logging.py — 構造化ロギングのテスト（14件）。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.config import LoggingConfig
from engine.llm.anthropic_client import AnthropicClient
from engine.llm.base import LLMResponse
from engine.llm.gemini_client import GeminiClient
from engine.llm.ollama_client import OllamaClient
from engine.llm.openai_client import OpenAIClient
from engine.main import JsonFormatter, setup_logging
from engine.story_engine import StoryEngine


# ============================================================
# ヘルパー
# ============================================================


def make_record(msg: str = "test", level: int = logging.INFO, **extra_attrs: object) -> logging.LogRecord:
    """テスト用 LogRecord を生成する。"""
    record = logging.LogRecord(
        name="test.module",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, val in extra_attrs.items():
        setattr(record, key, val)
    return record


@pytest.fixture()
def restore_root_logger() -> object:
    """テスト後にルートロガーを元の状態に戻す。"""
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    for h in root.handlers:
        h.close()
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.setLevel(original_level)


def make_mock_response(status: int = 200, json_data: dict | None = None) -> AsyncMock:
    """aiohttp.ClientResponse の async context manager モック。"""
    resp = AsyncMock()
    resp.status = status
    resp.headers = {}
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=json.dumps(json_data or {}, ensure_ascii=False))
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def make_mock_session(mock_resp: AsyncMock) -> AsyncMock:
    """aiohttp.ClientSession の async context manager モック。"""
    session = AsyncMock()
    session.post = MagicMock(return_value=mock_resp)
    session.get = MagicMock(return_value=mock_resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ============================================================
# StoryEngine テスト用ヘルパー（test_story_engine.py パターン再利用）
# ============================================================

_PLACE_ROW: dict = {
    "id": "music_room",
    "label": "音楽室",
    "zone": "indoor",
    "atmosphere": None,
    "who_gathers": None,
    "events_likely": None,
    "access_note": None,
    "adjacent_places": None,
}


def _make_story(**overrides: object) -> dict:
    defaults: dict = {
        "id": "test_story",
        "llm_provider": "ollama",
        "llm_model": "qwen2.5:14b",
        "turn_minutes": 30,
        "turn_interval_sec": 0,
        "season_start": "2025-04-01",
        "last_sim_time": None,
    }
    defaults.update(overrides)
    return defaults


def _make_character(**overrides: object) -> dict:
    speech_json = json.dumps({
        "first_person": "俺",
        "tone": "短く話す",
        "examples": ["「別に。」"],
        "never_say": [],
    })
    emotion_json = json.dumps({"stress": 0.3, "motivation": 0.7, "loneliness": 0.2, "excitement": 0.5})
    places_json = json.dumps(["music_room"])
    defaults: dict = {
        "id": "char_1",
        "name_ja": "テストキャラ",
        "personality_core": "冷静で論理的",
        "current_goal": "音楽を極める",
        "current_worry": "時間が足りない",
        "secret": None,
        "secret_reveal_condition": None,
        "speech": speech_json,
        "emotion_default": emotion_json,
        "favorite_places": places_json,
    }
    defaults.update(overrides)
    return defaults


def _make_state(**overrides: object) -> dict:
    defaults: dict = {
        "current_place": "music_room",
        "previous_place": None,
        "current_expression": "neutral",
        "stress": 0.3,
        "motivation": 0.7,
        "loneliness": 0.2,
        "excitement": 0.5,
    }
    defaults.update(overrides)
    return defaults


def _make_cm(rows: list) -> AsyncMock:
    """_conn.execute() 用の async context manager モックを生成する。"""
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=rows)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cursor)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_mock_db(story: dict | None = None, characters: list | None = None) -> AsyncMock:
    db = AsyncMock()
    db.get_story = AsyncMock(return_value=story or _make_story())
    db.get_characters = AsyncMock(return_value=characters or [_make_character()])
    db.get_latest_character_state = AsyncMock(return_value=_make_state())
    db.insert_chat_log = AsyncMock(return_value=1)
    db.insert_character_state = AsyncMock()
    db.update_last_sim_time = AsyncMock()
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db._conn = MagicMock()
    db._conn.execute = MagicMock(side_effect=[
        _make_cm([_PLACE_ROW]),  # 1: places
        _make_cm([]),            # 2: time_schedules
        _make_cm([]),            # 3: anomaly_rules
        _make_cm([]),            # 4: relationships
    ])
    return db


def _make_llm_response(**overrides: object) -> LLMResponse:
    defaults: dict = {
        "text": "「今日も練習だ。」",
        "model": "qwen2.5:14b",
        "provider": "ollama",
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "latency_ms": 500,
        "reasoning_chars": 0,
        "done_reason": None,
        "completion_status": "complete",
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)


# ============================================================
# テスト 1-4: JsonFormatter 純粋テスト
# ============================================================


def test_json_formatter_output_is_valid_json() -> None:
    """出力が JSON としてパース可能。"""
    formatter = JsonFormatter()
    record = make_record("hello")
    output = formatter.format(record)
    data = json.loads(output)
    assert isinstance(data, dict)


def test_json_formatter_base_fields() -> None:
    """ts / level / module / msg が全て存在する。"""
    formatter = JsonFormatter()
    record = make_record("base test")
    data = json.loads(formatter.format(record))
    assert "ts" in data
    assert "level" in data
    assert "module" in data
    assert "msg" in data


def test_json_formatter_timestamp_is_timezone_aware_utc() -> None:
    """ts は UTC offset 付きの aware datetime として出力される。"""
    formatter = JsonFormatter()
    record = make_record("tz test")
    data = json.loads(formatter.format(record))
    timestamp = datetime.fromisoformat(data["ts"])
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() == timedelta(0)


def test_json_formatter_extra_fields_included() -> None:
    """llm_provider / llm_model / llm_latency_ms が extra 経由で出力に含まれる。"""
    formatter = JsonFormatter()
    record = make_record("llm log", llm_provider="openai", llm_model="gpt-4o-mini", llm_latency_ms=150)
    data = json.loads(formatter.format(record))
    assert data["llm_provider"] == "openai"
    assert data["llm_model"] == "gpt-4o-mini"
    assert data["llm_latency_ms"] == 150


def test_json_formatter_llm_diagnostics_included() -> None:
    """LLM 診断用 extra フィールドが JSON 出力に含まれる。"""
    formatter = JsonFormatter()
    record = make_record(
        "llm diag",
        llm_provider="ollama",
        llm_model="qwen3.5:9b",
        llm_latency_ms=150,
        llm_system_prompt_chars=120,
        llm_user_prompt_chars=48,
        llm_response_chars=0,
        llm_thinking_chars=256,
        llm_done_reason="stop",
        llm_completion_status="empty_final",
        llm_recovery_attempted=True,
    )
    data = json.loads(formatter.format(record))
    assert data["llm_system_prompt_chars"] == 120
    assert data["llm_user_prompt_chars"] == 48
    assert data["llm_response_chars"] == 0
    assert data["llm_thinking_chars"] == 256
    assert data["llm_done_reason"] == "stop"
    assert data["llm_completion_status"] == "empty_final"
    assert data["llm_recovery_attempted"] is True


def test_json_formatter_includes_request_tag_and_structured_policy_name() -> None:
    formatter = JsonFormatter()
    record = make_record(
        "llm diag",
        llm_request_tag="structured:story_memory:primary",
        structured_policy_name="story_memory",
    )
    data = json.loads(formatter.format(record))
    assert data["llm_request_tag"] == "structured:story_memory:primary"
    assert data["structured_policy_name"] == "story_memory"


def test_json_formatter_none_fields_excluded() -> None:
    """extra に含まれない optional フィールドは出力に含まれない。"""
    formatter = JsonFormatter()
    record = make_record("no extra")
    data = json.loads(formatter.format(record))
    assert "story_id" not in data
    assert "char_id" not in data
    assert "llm_provider" not in data


# ============================================================
# テスト 5-8: setup_logging テスト
# ============================================================


def test_setup_logging_creates_stream_handler(tmp_path: object, restore_root_logger: object) -> None:
    """root logger に StreamHandler が追加される。"""
    config = LoggingConfig(level="INFO", log_dir=str(tmp_path), rotation="midnight", retention_days=7)
    setup_logging(config)
    root = logging.getLogger()
    stream_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, TimedRotatingFileHandler)
    ]
    assert len(stream_handlers) >= 1


def test_setup_logging_creates_file_handler(tmp_path: object, restore_root_logger: object) -> None:
    """TimedRotatingFileHandler が追加される。"""
    config = LoggingConfig(level="INFO", log_dir=str(tmp_path), rotation="midnight", retention_days=7)
    setup_logging(config)
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) >= 1


def test_setup_logging_level_from_config(tmp_path: object, restore_root_logger: object) -> None:
    """config.level が root logger のレベルに設定される。"""
    config = LoggingConfig(level="WARNING", log_dir=str(tmp_path), rotation="midnight", retention_days=7)
    setup_logging(config)
    assert logging.getLogger().level == logging.WARNING


def test_setup_logging_level_override(tmp_path: object, restore_root_logger: object) -> None:
    """log_level_override が config.level より優先される。"""
    config = LoggingConfig(level="INFO", log_dir=str(tmp_path), rotation="midnight", retention_days=7)
    setup_logging(config, log_level_override="DEBUG")
    assert logging.getLogger().level == logging.DEBUG


# ============================================================
# テスト 9-12: クライアント構造化ログテスト
# ============================================================


@pytest.mark.asyncio
async def test_openai_client_generate_logs_structured_extra(caplog: object) -> None:
    """OpenAIClient.generate() が llm_provider / llm_model / llm_latency_ms を持つ INFO レコードを出力する。"""
    json_data = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    mock_resp = make_mock_response(200, json_data)
    mock_session = make_mock_session(mock_resp)
    client = OpenAIClient(api_key="test-key")

    with caplog.at_level(logging.INFO, logger="engine.llm.openai_client"):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await client.generate(system_prompt="sys", user_prompt="hi", model="gpt-4o-mini")

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        getattr(r, "llm_provider", None) == "openai"
        and getattr(r, "llm_model", None) == "gpt-4o-mini"
        and getattr(r, "llm_latency_ms", None) is not None
        for r in info_records
    )


@pytest.mark.asyncio
async def test_anthropic_client_generate_logs_structured_extra(caplog: object) -> None:
    """AnthropicClient.generate() が llm_provider / llm_model / llm_latency_ms を持つ INFO レコードを出力する。"""
    json_data = {
        "content": [{"text": "hello"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    mock_resp = make_mock_response(200, json_data)
    mock_session = make_mock_session(mock_resp)
    client = AnthropicClient(api_key="test-key")

    with caplog.at_level(logging.INFO, logger="engine.llm.anthropic_client"):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await client.generate(system_prompt="sys", user_prompt="hi", model="claude-haiku-4-5-20251001")

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        getattr(r, "llm_provider", None) == "anthropic"
        and getattr(r, "llm_model", None) is not None
        and getattr(r, "llm_latency_ms", None) is not None
        for r in info_records
    )


@pytest.mark.asyncio
async def test_gemini_client_generate_logs_structured_extra(caplog: object) -> None:
    """GeminiClient.generate() が llm_provider / llm_model / llm_latency_ms を持つ INFO レコードを出力する。"""
    json_data = {
        "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }
    mock_resp = make_mock_response(200, json_data)
    mock_session = make_mock_session(mock_resp)
    client = GeminiClient(api_key="test-key")

    with caplog.at_level(logging.INFO, logger="engine.llm.gemini_client"):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await client.generate(system_prompt="sys", user_prompt="hi", model="gemini-2.0-flash")

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        getattr(r, "llm_provider", None) == "gemini"
        and getattr(r, "llm_model", None) is not None
        and getattr(r, "llm_latency_ms", None) is not None
        for r in info_records
    )


@pytest.mark.asyncio
async def test_ollama_client_generate_logs_structured_extra(caplog: object) -> None:
    """OllamaClient.generate() が llm_provider / llm_model / llm_latency_ms を持つ INFO レコードを出力する。"""
    json_data = {
        "message": {"content": "hello"},
        "prompt_eval_count": 10,
        "eval_count": 5,
        "total_duration": 500_000_000,  # 500ms in ns
    }
    mock_resp = make_mock_response(200, json_data)
    mock_session = make_mock_session(mock_resp)
    client = OllamaClient()

    with caplog.at_level(logging.INFO, logger="engine.llm.ollama_client"):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await client.generate(system_prompt="sys", user_prompt="hi", model="qwen2.5:14b")

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        getattr(r, "llm_provider", None) == "ollama"
        and getattr(r, "llm_model", None) is not None
        and getattr(r, "llm_latency_ms", None) is not None
        and getattr(r, "llm_response_chars", None) == len("hello")
        for r in info_records
    )


# ============================================================
# テスト 13-14: StoryEngine ログテスト
# ============================================================


@pytest.mark.asyncio
async def test_story_engine_logs_llm_completion(caplog: object) -> None:
    """run_one_turn() 後のログに story_id / char_id / llm_provider が含まれる。"""
    db = _make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=_make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    with caplog.at_level(logging.INFO, logger="engine.story_engine"):
        await engine.run_one_turn()

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        getattr(r, "story_id", None) == "test_story"
        and getattr(r, "char_id", None) == "char_1"
        and getattr(r, "llm_provider", None) == "ollama"
        and getattr(r, "llm_response_chars", None) == len("「今日も練習だ。」")
        for r in info_records
    )


@pytest.mark.asyncio
async def test_story_engine_warns_on_empty_llm_response(caplog: object) -> None:
    """run_one_turn() で空本文なら insert 前に WARNING が出る。"""
    db = _make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=_make_llm_response(text=""))
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    with caplog.at_level(logging.WARNING, logger="engine.story_engine"):
        await engine.run_one_turn()

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        getattr(r, "story_id", None) == "test_story"
        and getattr(r, "char_id", None) == "char_1"
        and getattr(r, "llm_response_chars", None) == 0
        for r in warning_records
    )


@pytest.mark.asyncio
async def test_story_engine_logs_move_extra(caplog: object) -> None:
    """移動発生時のログに story_id / char_id が extra フィールドとして含まれる。"""
    place_row_with_adj = dict(_PLACE_ROW)
    place_row_with_adj["adjacent_places"] = '{"hallway": 1}'
    hallway_row = {
        "id": "hallway",
        "label": "廊下",
        "zone": "indoor",
        "atmosphere": None,
        "who_gathers": None,
        "events_likely": None,
        "access_note": None,
        "adjacent_places": '{"music_room": 1}',
    }

    db = AsyncMock()
    db.get_story = AsyncMock(return_value=_make_story())
    db.get_characters = AsyncMock(return_value=[_make_character()])
    db.get_latest_character_state = AsyncMock(return_value=_make_state())
    db.insert_chat_log = AsyncMock(return_value=1)
    db.insert_character_state = AsyncMock()
    db.update_last_sim_time = AsyncMock()
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db._conn = MagicMock()
    db._conn.execute = MagicMock(side_effect=[
        _make_cm([place_row_with_adj, hallway_row]),  # 1: places
        _make_cm([]),                                  # 2: time_schedules
        _make_cm([]),                                  # 3: anomaly_rules
        _make_cm([]),                                  # 4: relationships
    ])

    router = AsyncMock()
    router.generate = AsyncMock(return_value=_make_llm_response(text="「移動する。」"))
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    with caplog.at_level(logging.DEBUG, logger="engine.story_engine"):
        with patch.object(engine._move_engine, "should_move", return_value=True):
            with patch.object(engine._move_engine, "decide_destination", return_value="hallway"):
                await engine.run_one_turn()

    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        getattr(r, "story_id", None) == "test_story"
        and getattr(r, "char_id", None) == "char_1"
        for r in debug_records
    )
