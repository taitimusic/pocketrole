"""Ollama ローカル LLM クライアント実装。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from engine.llm.base import BaseLLMClient, LLMResponse
from engine.llm.exceptions import (
    LLMConnectionError,
    LLMModelNotFoundError,
    LLMResponseError,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)


def _preview_text(text: str, limit: int = 2048) -> str:
    """ログ用の本文 preview を返す。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


class OllamaClient(BaseLLMClient):
    """Ollama ローカル LLM への aiohttp クライアント。

    VRAM 競合防止のため supports_concurrent = False（直列処理）。
    LLMRouter が asyncio.Queue で管理する。
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_model: str = "qwen2.5:14b",
        timeout_sec: int = 120,
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout_sec = timeout_sec
        self._max_retries = max_retries

    # ------------------------------------------------------------------
    # BaseLLMClient 抽象メソッドの実装
    # ------------------------------------------------------------------

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float = 0.8,
        max_tokens: int = 300,
        reasoning_mode: str = "auto",
        request_tag: str | None = None,
    ) -> LLMResponse:
        """POST /api/chat でテキストを生成する。

        タイムアウト時は指数バックオフで最大 max_retries 回再試行する。
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if reasoning_mode == "on":
            payload["think"] = True
        elif reasoning_mode == "off":
            payload["think"] = False
        url = f"{self._base_url}/api/chat"
        timeout = aiohttp.ClientTimeout(total=self._timeout_sec)

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=timeout) as resp:
                        raw_body = await resp.text()
                        raw_preview = _preview_text(raw_body)
                        if resp.status == 404:
                            raise LLMModelNotFoundError(
                                f"Model not found: {model}"
                            )
                        try:
                            data = json.loads(raw_body)
                        except Exception as exc:
                            raise LLMResponseError(
                                f"JSON parse error: {exc}; preview={raw_preview!r}"
                            ) from exc

                        if not isinstance(data, dict):
                            raise LLMResponseError(
                                f"Unexpected Ollama response type: {type(data).__name__}; preview={raw_preview!r}"
                            )

                        message = data.get("message")
                        top_level_keys = sorted(data.keys())
                        if not isinstance(message, dict):
                            raise LLMResponseError(
                                f"Unexpected Ollama response: missing object message; keys={top_level_keys}; preview={raw_preview!r}"
                            )

                        message_keys = sorted(message.keys())
                        content = message.get("content")
                        if not isinstance(content, str):
                            raise LLMResponseError(
                                "Unexpected Ollama response: missing string message.content; "
                                f"keys={top_level_keys}; message_keys={message_keys}; preview={raw_preview!r}"
                            )

                        text = content
                        thinking = message.get("thinking")
                        thinking_text = thinking if isinstance(thinking, str) else ""
                        reasoning_present = thinking_text != ""
                        prompt_tokens: int | None = data.get("prompt_eval_count")
                        completion_tokens: int | None = data.get("eval_count")
                        total_duration_ns: int | None = data.get("total_duration")
                        done_reason = data.get("done_reason")
                        completion_status = (
                            "complete" if text.strip() != "" else "empty_final"
                        )
                        latency_ms = (
                            int(total_duration_ns // 1_000_000)
                            if total_duration_ns is not None
                            else 0
                        )
                        log_extra = {
                            "llm_provider": self.provider_name,
                            "llm_model": model,
                            "llm_latency_ms": latency_ms,
                            "llm_system_prompt_chars": len(system_prompt),
                            "llm_user_prompt_chars": len(user_prompt),
                            "llm_response_chars": len(text),
                            "llm_thinking_chars": len(thinking_text),
                            "llm_done_reason": done_reason,
                            "llm_completion_status": completion_status,
                        }
                        if request_tag:
                            log_extra["llm_request_tag"] = request_tag
                        logger.debug(
                            "Ollama raw response parsed",
                            extra={
                                **log_extra,
                                "llm_response_keys": top_level_keys,
                                "llm_message_keys": message_keys,
                                "llm_raw_preview": raw_preview,
                            },
                        )
                        if text.strip() == "":
                            logger.warning(
                                "Ollama returned empty content",
                                extra=log_extra,
                            )

                        result = LLMResponse(
                            text=text,
                            model=model,
                            provider=self.provider_name,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            latency_ms=latency_ms,
                            reasoning_present=reasoning_present,
                            reasoning_chars=len(thinking_text),
                            done_reason=done_reason if isinstance(done_reason, str) else None,
                            completion_status=completion_status,
                        )
                        logger.info(
                            "LLM生成完了",
                            extra=log_extra,
                        )
                        return result

            except aiohttp.ClientConnectorError as exc:
                logger.warning("Ollama connection error: %s", exc)
                raise LLMConnectionError(str(exc)) from exc

            except asyncio.TimeoutError as exc:
                wait = 2 ** attempt
                logger.warning(
                    "Ollama timeout (attempt %d/%d), retrying in %ds",
                    attempt + 1,
                    self._max_retries,
                    wait,
                )
                last_exc = LLMTimeoutError(str(exc))
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(wait)

            except (LLMModelNotFoundError, LLMResponseError):
                raise

        raise last_exc  # type: ignore[misc]

    async def health_check(self) -> bool:
        """GET /api/tags → 200 なら True、接続失敗なら False。"""
        url = f"{self._base_url}/api/tags"
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=timeout) as resp:
                    return resp.status == 200
        except Exception as exc:
            logger.warning("Ollama health_check failed: %s", exc)
            return False

    async def list_models(self) -> list[str]:
        """GET /api/tags → モデル名リストを返す。"""
        url = f"{self._base_url}/api/tags"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:
                    raise LLMResponseError(f"JSON parse error: {exc}") from exc
                return [m["name"] for m in data.get("models", [])]

    # ------------------------------------------------------------------
    # プロパティ
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def supports_concurrent(self) -> bool:
        return False
