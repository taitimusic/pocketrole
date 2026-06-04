"""OpenAIClient / DeepSeekClient / AnthropicClient / GeminiClient のユニットテスト（28件）。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from engine.llm.anthropic_client import AnthropicClient
from engine.llm.deepseek_client import DeepSeekClient
from engine.llm.gemini_client import GeminiClient
from engine.llm.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMModelNotFoundError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from engine.llm.openai_client import OpenAIClient

# ---------------------------------------------------------------------------
# モックヘルパー
# ---------------------------------------------------------------------------

_SUCCESS_JSON = {
    "choices": [{"message": {"content": "Hello, world!"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}

_RESPONSES_JSON = {
    "output_text": "Hello from responses!",
    "usage": {"input_tokens": 11, "output_tokens": 7},
    "status": "completed",
}

_MODELS_JSON = {
    "data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}],
}


def make_mock_response(
    status: int = 200,
    json_data: dict | None = None,
    headers: dict | None = None,
) -> AsyncMock:
    """aiohttp.ClientResponse の async context manager モック。"""
    resp = AsyncMock()
    resp.status = status
    resp.headers = headers or {}
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value="error")
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


# ---------------------------------------------------------------------------
# OpenAIClient テスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_generate_success() -> None:
    """正常生成: text / provider / model / token / latency を検証。"""
    mock_resp = make_mock_response(200, _SUCCESS_JSON)
    mock_session = make_mock_session(mock_resp)

    client = OpenAIClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await client.generate(
            system_prompt="sys",
            user_prompt="hello",
            model="gpt-4o-mini",
        )

    assert result.text == "Hello, world!"
    assert result.provider == "openai"
    assert result.model == "gpt-4o-mini"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 5
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_openai_generate_uses_responses_api_for_gpt5_auto() -> None:
    """gpt-5 系 + api_mode=auto → /responses を使う。"""
    mock_resp = make_mock_response(200, _RESPONSES_JSON)
    mock_session = make_mock_session(mock_resp)

    client = OpenAIClient(api_key="test-key", api_mode="auto")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await client.generate(
            system_prompt="sys",
            user_prompt="hello",
            model="gpt-5-mini",
        )

    assert result.text == "Hello from responses!"
    assert result.provider == "openai"
    assert result.model == "gpt-5-mini"
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 7
    called_url = mock_session.post.call_args.args[0]
    assert called_url.endswith("/responses")
    called_payload = mock_session.post.call_args.kwargs["json"]
    assert "temperature" not in called_payload
    assert called_payload["reasoning"] == {"effort": "minimal"}


@pytest.mark.asyncio
async def test_openai_generate_keeps_temperature_for_gpt54_responses() -> None:
    """gpt-5.4 系 + Responses API では temperature を残す。"""
    mock_resp = make_mock_response(200, _RESPONSES_JSON)
    mock_session = make_mock_session(mock_resp)

    client = OpenAIClient(api_key="test-key", api_mode="auto")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await client.generate(
            system_prompt="sys",
            user_prompt="hello",
            model="gpt-5.4-mini",
            temperature=0.7,
        )

    called_payload = mock_session.post.call_args.kwargs["json"]
    assert called_payload["temperature"] == 0.7
    assert "reasoning" not in called_payload


@pytest.mark.asyncio
async def test_openai_generate_rate_limit() -> None:
    """429 + Retry-After: 30 → LLMRateLimitError(retry_after=30.0)。"""
    mock_resp = make_mock_response(429, headers={"Retry-After": "30"})
    mock_session = make_mock_session(mock_resp)

    client = OpenAIClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMRateLimitError) as exc_info:
            await client.generate("sys", "hello", model="gpt-4o-mini")

    assert exc_info.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_openai_generate_rate_limit_no_header() -> None:
    """429 + ヘッダーなし → LLMRateLimitError(retry_after=None)。"""
    mock_resp = make_mock_response(429)
    mock_session = make_mock_session(mock_resp)

    client = OpenAIClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMRateLimitError) as exc_info:
            await client.generate("sys", "hello", model="gpt-4o-mini")

    assert exc_info.value.retry_after is None


@pytest.mark.asyncio
async def test_openai_generate_auth_error() -> None:
    """401 → 詳細つき LLMAuthError。"""
    mock_resp = make_mock_response(
        401,
        json_data={
            "error": {
                "message": "Incorrect API key provided",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )
    mock_session = make_mock_session(mock_resp)

    client = OpenAIClient(api_key="invalid-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMAuthError) as exc_info:
            await client.generate("sys", "hello", model="gpt-4o-mini")

    assert "Incorrect API key provided" in str(exc_info.value)
    assert "invalid_api_key" in str(exc_info.value)


@pytest.mark.asyncio
async def test_openai_generate_model_not_found() -> None:
    """404 → LLMModelNotFoundError。"""
    mock_resp = make_mock_response(404)
    mock_session = make_mock_session(mock_resp)

    client = OpenAIClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMModelNotFoundError):
            await client.generate("sys", "hello", model="gpt-unknown")


@pytest.mark.asyncio
async def test_openai_generate_api_error() -> None:
    """500 + エラーJSON → LLMResponseError。"""
    err_json = {"error": {"message": "Internal server error"}}
    mock_resp = make_mock_response(500, json_data=err_json)
    mock_session = make_mock_session(mock_resp)

    client = OpenAIClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMResponseError) as exc_info:
            await client.generate("sys", "hello", model="gpt-4o-mini")

    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_openai_generate_timeout_exhausted() -> None:
    """asyncio.TimeoutError × max_retries → LLMTimeoutError。call_count == max_retries。"""
    client = OpenAIClient(api_key="test-key", max_retries=2)

    call_count = 0

    def fake_post(*args: object, **kwargs: object) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    mock_session = AsyncMock()
    mock_session.post = fake_post
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(LLMTimeoutError):
                await client.generate("sys", "hello", model="gpt-4o-mini")

    assert call_count == 2


@pytest.mark.asyncio
async def test_openai_generate_connection_error() -> None:
    """aiohttp.ClientError → LLMConnectionError（リトライなし、call_count==1）。"""
    client = OpenAIClient(api_key="test-key", max_retries=3)

    call_count = 0

    def fake_post(*args: object, **kwargs: object) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("connection refused"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    mock_session = AsyncMock()
    mock_session.post = fake_post
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMConnectionError):
            await client.generate("sys", "hello", model="gpt-4o-mini")

    assert call_count == 1


@pytest.mark.asyncio
async def test_openai_health_check_ok() -> None:
    """GET /models 200 → True。"""
    mock_resp = make_mock_response(200, _MODELS_JSON)
    mock_session = make_mock_session(mock_resp)

    client = OpenAIClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await client.health_check()

    assert result is True


@pytest.mark.asyncio
async def test_openai_health_check_fail() -> None:
    """接続例外 → False。"""
    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=aiohttp.ClientError("unreachable"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    client = OpenAIClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await client.health_check()

    assert result is False


# ---------------------------------------------------------------------------
# DeepSeekClient テスト
# ---------------------------------------------------------------------------


def test_deepseek_provider_name() -> None:
    """provider_name == "deepseek"。"""
    client = DeepSeekClient(api_key="ds-key")
    assert client.provider_name == "deepseek"


def test_deepseek_supports_concurrent() -> None:
    """supports_concurrent is True（OpenAIClient から継承）。"""
    client = DeepSeekClient(api_key="ds-key")
    assert client.supports_concurrent is True


def test_deepseek_uses_deepseek_base_url() -> None:
    """_base_url が "api.deepseek.com" を含む。"""
    client = DeepSeekClient(api_key="ds-key")
    assert "api.deepseek.com" in client._base_url


@pytest.mark.asyncio
async def test_deepseek_generate_success() -> None:
    """DeepSeek で正常生成 → LLMResponse.provider == "deepseek"。"""
    mock_resp = make_mock_response(200, _SUCCESS_JSON)
    mock_session = make_mock_session(mock_resp)

    client = DeepSeekClient(api_key="ds-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await client.generate(
            system_prompt="sys",
            user_prompt="hello",
            model="deepseek-chat",
        )

    assert result.provider == "deepseek"
    assert result.text == "Hello, world!"
    assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# AnthropicClient テスト
# ---------------------------------------------------------------------------

_ANTHROPIC_SUCCESS_JSON = {
    "content": [{"text": "Hello from Claude!"}],
    "usage": {"input_tokens": 12, "output_tokens": 6},
}


@pytest.mark.asyncio
async def test_anthropic_generate_success() -> None:
    """正常生成: text / provider / model / token / latency を検証。"""
    mock_resp = make_mock_response(200, _ANTHROPIC_SUCCESS_JSON)
    mock_session = make_mock_session(mock_resp)

    client = AnthropicClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await client.generate(
            system_prompt="sys",
            user_prompt="hello",
            model="claude-haiku-4-5-20251001",
        )

    assert result.text == "Hello from Claude!"
    assert result.provider == "anthropic"
    assert result.model == "claude-haiku-4-5-20251001"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 6
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_anthropic_generate_rate_limit() -> None:
    """429 + Retry-After: 45 → LLMRateLimitError(retry_after=45.0)。"""
    mock_resp = make_mock_response(429, headers={"Retry-After": "45"})
    mock_session = make_mock_session(mock_resp)

    client = AnthropicClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMRateLimitError) as exc_info:
            await client.generate("sys", "hello", model="claude-haiku-4-5-20251001")

    assert exc_info.value.retry_after == 45.0


@pytest.mark.asyncio
async def test_anthropic_generate_auth_error() -> None:
    """401 → LLMAuthError。"""
    mock_resp = make_mock_response(401)
    mock_session = make_mock_session(mock_resp)

    client = AnthropicClient(api_key="invalid-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMAuthError):
            await client.generate("sys", "hello", model="claude-haiku-4-5-20251001")


@pytest.mark.asyncio
async def test_anthropic_generate_model_not_found() -> None:
    """404 → LLMModelNotFoundError。"""
    mock_resp = make_mock_response(404)
    mock_session = make_mock_session(mock_resp)

    client = AnthropicClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMModelNotFoundError):
            await client.generate("sys", "hello", model="claude-unknown")


@pytest.mark.asyncio
async def test_anthropic_generate_api_error() -> None:
    """500 → LLMResponseError。"""
    err_json = {"error": {"message": "Internal server error"}}
    mock_resp = make_mock_response(500, json_data=err_json)
    mock_session = make_mock_session(mock_resp)

    client = AnthropicClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMResponseError) as exc_info:
            await client.generate("sys", "hello", model="claude-haiku-4-5-20251001")

    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_anthropic_generate_timeout_exhausted() -> None:
    """asyncio.TimeoutError × max_retries=2 → LLMTimeoutError。call_count==2。"""
    client = AnthropicClient(api_key="test-key", max_retries=2)

    call_count = 0

    def fake_post(*args: object, **kwargs: object) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    mock_session = AsyncMock()
    mock_session.post = fake_post
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(LLMTimeoutError):
                await client.generate("sys", "hello", model="claude-haiku-4-5-20251001")

    assert call_count == 2


@pytest.mark.asyncio
async def test_anthropic_health_check_ok() -> None:
    """GET /models 200 → True。"""
    mock_resp = make_mock_response(200, {"data": []})
    mock_session = make_mock_session(mock_resp)

    client = AnthropicClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await client.health_check()

    assert result is True


# ---------------------------------------------------------------------------
# GeminiClient テスト
# ---------------------------------------------------------------------------

_GEMINI_SUCCESS_JSON = {
    "candidates": [{"content": {"parts": [{"text": "Hello from Gemini!"}]}}],
    "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 4},
}


@pytest.mark.asyncio
async def test_gemini_generate_success() -> None:
    """正常生成: text / provider / latency を検証。"""
    mock_resp = make_mock_response(200, _GEMINI_SUCCESS_JSON)
    mock_session = make_mock_session(mock_resp)

    client = GeminiClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await client.generate(
            system_prompt="sys",
            user_prompt="hello",
            model="gemini-2.0-flash",
        )

    assert result.text == "Hello from Gemini!"
    assert result.provider == "gemini"
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_gemini_generate_rate_limit() -> None:
    """429 → LLMRateLimitError。"""
    mock_resp = make_mock_response(429)
    mock_session = make_mock_session(mock_resp)

    client = GeminiClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMRateLimitError):
            await client.generate("sys", "hello", model="gemini-2.0-flash")


@pytest.mark.asyncio
async def test_gemini_generate_auth_error() -> None:
    """403 → LLMAuthError。"""
    mock_resp = make_mock_response(403)
    mock_session = make_mock_session(mock_resp)

    client = GeminiClient(api_key="bad-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMAuthError):
            await client.generate("sys", "hello", model="gemini-2.0-flash")


@pytest.mark.asyncio
async def test_gemini_generate_model_not_found() -> None:
    """404 → LLMModelNotFoundError。"""
    mock_resp = make_mock_response(404)
    mock_session = make_mock_session(mock_resp)

    client = GeminiClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMModelNotFoundError):
            await client.generate("sys", "hello", model="gemini-unknown")


@pytest.mark.asyncio
async def test_gemini_generate_api_error() -> None:
    """500 → LLMResponseError。"""
    err_json = {"error": {"message": "Internal server error"}}
    mock_resp = make_mock_response(500, json_data=err_json)
    mock_session = make_mock_session(mock_resp)

    client = GeminiClient(api_key="test-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(LLMResponseError) as exc_info:
            await client.generate("sys", "hello", model="gemini-2.0-flash")

    assert "500" in str(exc_info.value)


def test_gemini_provider_name_and_concurrent() -> None:
    """provider_name=="gemini" かつ supports_concurrent is True。"""
    client = GeminiClient(api_key="test-key")
    assert client.provider_name == "gemini"
    assert client.supports_concurrent is True


@pytest.mark.asyncio
async def test_gemini_uses_api_key_in_url() -> None:
    """session.post 呼び出し時の URL に key= が含まれる。"""
    mock_resp = make_mock_response(200, _GEMINI_SUCCESS_JSON)

    captured_kwargs: dict = {}

    def fake_post(url: str, **kwargs: object) -> AsyncMock:
        captured_kwargs["url"] = url
        captured_kwargs["params"] = kwargs.get("params", {})
        return mock_resp

    mock_session = AsyncMock()
    mock_session.post = fake_post
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    client = GeminiClient(api_key="my-gemini-key")
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await client.generate("sys", "hello", model="gemini-2.0-flash")

    assert captured_kwargs["params"].get("key") == "my-gemini-key"
