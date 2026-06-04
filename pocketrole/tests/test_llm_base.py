"""engine/llm/exceptions.py と engine/llm/base.py のテスト。"""

from __future__ import annotations

import pytest

from engine.llm.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMError,
    LLMModelNotFoundError,
    LLMQuotaExceededError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from engine.llm.base import BaseLLMClient, LLMResponse


# ---------------------------------------------------------------------------
# テスト用 MockLLMClient
# ---------------------------------------------------------------------------


class MockLLMClient(BaseLLMClient):
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float = 0.8,
        max_tokens: int = 300,
    ) -> LLMResponse:
        return LLMResponse(
            text="test response",
            model=model,
            provider="mock",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=50,
        )

    async def health_check(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["mock-model"]

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def supports_concurrent(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# 例外クラス階層テスト
# ---------------------------------------------------------------------------


def test_llm_error_is_exception() -> None:
    assert issubclass(LLMError, Exception)


def test_connection_error_is_llm_error() -> None:
    assert issubclass(LLMConnectionError, LLMError)


def test_timeout_error_is_llm_error() -> None:
    assert issubclass(LLMTimeoutError, LLMError)


def test_auth_error_is_llm_error() -> None:
    assert issubclass(LLMAuthError, LLMError)


def test_model_not_found_is_llm_error() -> None:
    assert issubclass(LLMModelNotFoundError, LLMError)


def test_response_error_is_llm_error() -> None:
    assert issubclass(LLMResponseError, LLMError)


def test_quota_exceeded_is_llm_error() -> None:
    assert issubclass(LLMQuotaExceededError, LLMError)


# ---------------------------------------------------------------------------
# LLMRateLimitError テスト
# ---------------------------------------------------------------------------


def test_rate_limit_no_retry() -> None:
    err = LLMRateLimitError(None)
    assert err.retry_after is None
    assert str(err) == "Rate limited"


def test_rate_limit_with_retry() -> None:
    err = LLMRateLimitError(30.0)
    assert err.retry_after == 30.0
    assert "30.0" in str(err)


# ---------------------------------------------------------------------------
# LLMResponse テスト
# ---------------------------------------------------------------------------


def test_llm_response_fields() -> None:
    resp = LLMResponse(
        text="hello",
        model="llama3",
        provider="ollama",
        prompt_tokens=20,
        completion_tokens=10,
        latency_ms=123,
    )
    assert resp.text == "hello"
    assert resp.model == "llama3"
    assert resp.provider == "ollama"
    assert resp.prompt_tokens == 20
    assert resp.completion_tokens == 10
    assert resp.latency_ms == 123
    assert resp.final_text == "hello"
    assert resp.reasoning_present is False
    assert resp.reasoning_chars == 0
    assert resp.completion_status == "complete"


def test_llm_response_reasoning_metadata() -> None:
    resp = LLMResponse(
        text="final answer",
        model="qwen3.5:9b",
        provider="ollama",
        prompt_tokens=30,
        completion_tokens=40,
        latency_ms=200,
        reasoning_present=True,
        reasoning_chars=512,
        done_reason="stop",
        completion_status="complete",
    )
    assert resp.final_text == "final answer"
    assert resp.reasoning_present is True
    assert resp.reasoning_chars == 512
    assert resp.done_reason == "stop"
    assert resp.completion_status == "complete"


def test_llm_response_none_tokens() -> None:
    resp = LLMResponse(
        text="hi",
        model="gemma",
        provider="ollama",
        prompt_tokens=None,
        completion_tokens=None,
        latency_ms=50,
    )
    assert resp.prompt_tokens is None
    assert resp.completion_tokens is None


# ---------------------------------------------------------------------------
# BaseLLMClient テスト
# ---------------------------------------------------------------------------


def test_base_client_not_instantiable() -> None:
    with pytest.raises(TypeError):
        BaseLLMClient()  # type: ignore[abstract]


def test_mock_client_provider_name() -> None:
    client = MockLLMClient()
    assert client.provider_name == "mock"


async def test_mock_client_generate() -> None:
    client = MockLLMClient()
    resp = await client.generate(
        system_prompt="You are a helpful assistant.",
        user_prompt="Hello",
        model="mock-model",
    )
    assert isinstance(resp, LLMResponse)
    assert resp.text == "test response"
    assert resp.provider == "mock"
    assert resp.model == "mock-model"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5
    assert resp.latency_ms == 50
