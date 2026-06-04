"""engine/llm/ollama_client.py のテスト。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from engine.llm.base import LLMResponse
from engine.llm.exceptions import (
    LLMConnectionError,
    LLMModelNotFoundError,
    LLMResponseError,
    LLMTimeoutError,
)
from engine.llm.ollama_client import OllamaClient


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _make_post_ctx(status: int = 200, json_data: dict | None = None) -> AsyncMock:
    """session.post() のコンテキストマネージャを返すヘルパー。"""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    mock_resp.text = AsyncMock(return_value=json.dumps(json_data or {}, ensure_ascii=False))
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_get_ctx(status: int = 200, json_data: dict | None = None) -> AsyncMock:
    """session.get() のコンテキストマネージャを返すヘルパー。"""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_session(
    *,
    post_ctx: AsyncMock | None = None,
    get_ctx: AsyncMock | None = None,
) -> AsyncMock:
    """aiohttp.ClientSession のモックを返すヘルパー。"""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    if post_ctx is not None:
        mock_session.post = MagicMock(return_value=post_ctx)
    if get_ctx is not None:
        mock_session.get = MagicMock(return_value=get_ctx)
    return mock_session


# ---------------------------------------------------------------------------
# 正常系テスト
# ---------------------------------------------------------------------------


def test_provider_name() -> None:
    client = OllamaClient()
    assert client.provider_name == "ollama"


def test_supports_concurrent() -> None:
    client = OllamaClient()
    assert client.supports_concurrent is False


async def test_generate_success() -> None:
    json_data = {
        "message": {"role": "assistant", "content": "こんにちは！"},
        "prompt_eval_count": 20,
        "eval_count": 10,
        "total_duration": 500_000_000,  # 500ms in ns
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        resp = await client.generate(
            system_prompt="あなたはキャラクターです。",
            user_prompt="挨拶してください。",
            model="qwen2.5:14b",
        )

    assert isinstance(resp, LLMResponse)
    assert resp.text == "こんにちは！"
    assert resp.model == "qwen2.5:14b"
    assert resp.provider == "ollama"
    assert resp.prompt_tokens == 20
    assert resp.completion_tokens == 10
    assert resp.latency_ms == 500


async def test_generate_latency_ms() -> None:
    """total_duration(ns) → latency_ms(ms) 変換確認。"""
    json_data = {
        "message": {"role": "assistant", "content": "OK"},
        "total_duration": 1_234_000_000,  # 1234ms
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        resp = await client.generate("s", "u", "llama3:8b")

    assert resp.latency_ms == 1234


async def test_generate_token_counts() -> None:
    """トークン数フィールドが正しく LLMResponse にマッピングされること。"""
    json_data = {
        "message": {"role": "assistant", "content": "test"},
        "prompt_eval_count": 42,
        "eval_count": 7,
        "total_duration": 100_000_000,
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        resp = await client.generate("s", "u", "llama3:8b")

    assert resp.prompt_tokens == 42
    assert resp.completion_tokens == 7


async def test_generate_none_token_counts() -> None:
    """prompt_eval_count / eval_count が欠如している場合 None になること。"""
    json_data = {
        "message": {"role": "assistant", "content": "hello"},
        "total_duration": 100_000_000,
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        resp = await client.generate("s", "u", "llama3:8b")

    assert resp.prompt_tokens is None
    assert resp.completion_tokens is None


async def test_generate_tracks_thinking_and_response_lengths(caplog: pytest.LogCaptureFixture) -> None:
    """thinking を含む応答で本文長・thinking長・done_reason が記録されること。"""
    json_data = {
        "message": {
            "role": "assistant",
            "content": "動作確認中です",
            "thinking": "internal trace",
        },
        "done_reason": "stop",
        "total_duration": 100_000_000,
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with caplog.at_level("INFO", logger="engine.llm.ollama_client"):
        with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
            client = OllamaClient()
            resp = await client.generate("system text", "user text", "qwen3.5:9b")

    assert resp.text == "動作確認中です"
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert any(
        getattr(r, "llm_response_chars", None) == len("動作確認中です")
        and getattr(r, "llm_thinking_chars", None) == len("internal trace")
        and getattr(r, "llm_done_reason", None) == "stop"
        for r in info_records
    )


async def test_generate_logs_request_tag(caplog: pytest.LogCaptureFixture) -> None:
    json_data = {
        "message": {"role": "assistant", "content": "タグ付き応答"},
        "done_reason": "stop",
        "total_duration": 100_000_000,
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with caplog.at_level("INFO", logger="engine.llm.ollama_client"):
        with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
            client = OllamaClient()
            await client.generate(
                "system text",
                "user text",
                "gemma4:e4b",
                request_tag="structured:story_memory:primary",
            )

    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert any(
        getattr(r, "llm_request_tag", None) == "structured:story_memory:primary"
        for r in info_records
    )


async def test_generate_warns_on_empty_content_with_thinking(caplog: pytest.LogCaptureFixture) -> None:
    """content が空でも thinking がある場合は空文字返却のまま WARNING を出すこと。"""
    json_data = {
        "message": {
            "role": "assistant",
            "content": "",
            "thinking": "long reasoning",
        },
        "done_reason": "stop",
        "total_duration": 100_000_000,
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with caplog.at_level("WARNING", logger="engine.llm.ollama_client"):
        with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
            client = OllamaClient()
            resp = await client.generate("sys", "user", "qwen3.5:9b")

    assert resp.text == ""
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        getattr(r, "llm_response_chars", None) == 0
        and getattr(r, "llm_thinking_chars", None) == len("long reasoning")
        and getattr(r, "llm_done_reason", None) == "stop"
        for r in warning_records
    )


async def test_generate_sets_reasoning_metadata_on_response() -> None:
    json_data = {
        "message": {
            "role": "assistant",
            "content": "最終回答です",
            "thinking": "internal reasoning",
        },
        "done_reason": "stop",
        "total_duration": 100_000_000,
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        resp = await client.generate("system", "user", "qwen3.5:9b")

    assert resp.final_text == "最終回答です"
    assert resp.reasoning_present is True
    assert resp.reasoning_chars == len("internal reasoning")
    assert resp.done_reason == "stop"
    assert resp.completion_status == "complete"


async def test_generate_passes_reasoning_mode_to_ollama_payload() -> None:
    json_data = {
        "message": {"role": "assistant", "content": "OK"},
        "done_reason": "stop",
        "total_duration": 100_000_000,
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        await client.generate("system", "user", "qwen3.5:9b", reasoning_mode="off")

    payload = mock_session.post.call_args.kwargs["json"]
    assert payload["think"] is False


async def test_generate_marks_whitespace_only_content_as_empty_final() -> None:
    json_data = {
        "message": {"role": "assistant", "content": " \n\t "},
        "done_reason": "stop",
        "total_duration": 100_000_000,
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        resp = await client.generate("system", "user", "qwen3.5:9b")

    assert resp.final_text == " \n\t "
    assert resp.completion_status == "empty_final"


# ---------------------------------------------------------------------------
# エラー系テスト
# ---------------------------------------------------------------------------


async def test_generate_connection_error() -> None:
    """ClientConnectorError → LLMConnectionError が即時 raise されること。"""
    connector_err = aiohttp.ClientConnectorError(
        connection_key=MagicMock(), os_error=OSError("refused")
    )
    post_cm = AsyncMock()
    post_cm.__aenter__ = AsyncMock(side_effect=connector_err)
    post_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=post_cm)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient(max_retries=3)
        with pytest.raises(LLMConnectionError):
            await client.generate("s", "u", "llama3:8b")


async def test_generate_timeout() -> None:
    """asyncio.TimeoutError → 全リトライ後に LLMTimeoutError が raise されること。"""

    async def _timeout_ctx_aenter(*args: object, **kwargs: object) -> None:
        raise asyncio.TimeoutError()

    post_cm = AsyncMock()
    post_cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
    post_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=post_cm)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            client = OllamaClient(max_retries=3)
            with pytest.raises(LLMTimeoutError):
                await client.generate("s", "u", "llama3:8b")


async def test_generate_model_not_found() -> None:
    """HTTP 404 → LLMModelNotFoundError が即時 raise されること。"""
    post_ctx = _make_post_ctx(status=404, json_data={})
    mock_session = _make_session(post_ctx=post_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        with pytest.raises(LLMModelNotFoundError):
            await client.generate("s", "u", "nonexistent:model")


async def test_generate_invalid_json() -> None:
    """json() 失敗 → LLMResponseError が即時 raise されること。"""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(return_value="{not-json")
    post_cm = AsyncMock()
    post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    post_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=post_cm)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        with pytest.raises(LLMResponseError):
            await client.generate("s", "u", "llama3:8b")


async def test_generate_rejects_missing_message_content() -> None:
    """message.content 欠落は LLMResponseError に正規化されること。"""
    json_data = {
        "message": {
            "role": "assistant",
            "thinking": "trace only",
        },
        "done_reason": "stop",
    }
    post_ctx = _make_post_ctx(status=200, json_data=json_data)
    mock_session = _make_session(post_ctx=post_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        with pytest.raises(LLMResponseError, match="message.content"):
            await client.generate("s", "u", "qwen3.5:9b")


async def test_generate_retries_on_timeout() -> None:
    """タイムアウト時に max_retries 回試行してから raise されること。"""
    post_cm = AsyncMock()
    post_cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
    post_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=post_cm)

    call_count = 0

    async def counting_session(*args: object, **kwargs: object) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        return mock_session

    with patch("engine.llm.ollama_client.aiohttp.ClientSession") as MockSession:
        MockSession.return_value = mock_session
        with patch("asyncio.sleep", new_callable=AsyncMock):
            client = OllamaClient(max_retries=3)
            with pytest.raises(LLMTimeoutError):
                await client.generate("s", "u", "llama3:8b")
        # max_retries=3 なので ClientSession が 3 回呼ばれること
        assert MockSession.call_count == 3


# ---------------------------------------------------------------------------
# health_check テスト
# ---------------------------------------------------------------------------


async def test_health_check_ok() -> None:
    """GET /api/tags が 200 → True を返すこと。"""
    get_ctx = _make_get_ctx(status=200, json_data={"models": []})
    mock_session = _make_session(get_ctx=get_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        result = await client.health_check()

    assert result is True


async def test_health_check_fail() -> None:
    """ClientConnectorError → False を返すこと。"""
    connector_err = aiohttp.ClientConnectorError(
        connection_key=MagicMock(), os_error=OSError("refused")
    )
    get_cm = AsyncMock()
    get_cm.__aenter__ = AsyncMock(side_effect=connector_err)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=get_cm)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        result = await client.health_check()

    assert result is False


# ---------------------------------------------------------------------------
# list_models テスト
# ---------------------------------------------------------------------------


async def test_list_models() -> None:
    """GET /api/tags → モデル名リストを正しく返すこと。"""
    get_ctx = _make_get_ctx(
        status=200,
        json_data={
            "models": [
                {"name": "qwen2.5:14b", "size": 1000},
                {"name": "llama3:8b", "size": 2000},
            ]
        },
    )
    mock_session = _make_session(get_ctx=get_ctx)

    with patch("engine.llm.ollama_client.aiohttp.ClientSession", return_value=mock_session):
        client = OllamaClient()
        models = await client.list_models()

    assert models == ["qwen2.5:14b", "llama3:8b"]
