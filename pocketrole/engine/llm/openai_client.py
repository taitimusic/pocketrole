"""OpenAI ChatGPT クラウド LLM クライアント実装。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from engine.llm.base import BaseLLMClient, LLMResponse
from engine.llm.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMModelNotFoundError,
    LLMQuotaExceededError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)


def _parse_retry_after(resp: aiohttp.ClientResponse) -> float | None:
    """Retry-After ヘッダーを float 秒に変換。解析失敗時は None。"""
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _build_error_detail(error_data: Any) -> str:
    if isinstance(error_data, dict):
        error = error_data.get("error")
        if isinstance(error, dict):
            parts = []
            message = error.get("message")
            if message:
                parts.append(str(message))
            error_type = error.get("type")
            if error_type:
                parts.append(f"type={error_type}")
            code = error.get("code")
            if code:
                parts.append(f"code={code}")
            if parts:
                return " | ".join(parts)
        return json.dumps(error_data, ensure_ascii=False)
    return str(error_data)


class OpenAIClient(BaseLLMClient):
    """OpenAI ChatGPT への aiohttp クライアント。

    asyncio.Semaphore で同時実行数を制限（supports_concurrent = True）。
    LLMRouter が管理する。
    """

    BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: str,
        default_model: str = "gpt-4o-mini",
        timeout_sec: int = 60,
        max_retries: int = 3,
        base_url: str | None = None,
        api_mode: str = "auto",
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._timeout_sec = timeout_sec
        self._max_retries = max_retries
        self._base_url = (base_url or self.BASE_URL).rstrip("/")
        self._api_mode = api_mode

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
        """OpenAI でテキストを生成する。

        タイムアウト時は指数バックオフで最大 max_retries 回再試行する。
        非リトライ例外（401/402/404/429/その他4xx-5xx）は即時 raise。
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        api_mode = self._resolve_api_mode(model)
        if api_mode == "responses":
            url = f"{self._base_url}/responses"
            payload = self._build_responses_payload(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_mode=reasoning_mode,
            )
        else:
            url = f"{self._base_url}/chat/completions"
            payload = self._build_chat_completions_payload(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        timeout = aiohttp.ClientTimeout(total=self._timeout_sec)

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            start = time.monotonic()
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload, headers=headers, timeout=timeout
                    ) as resp:
                        status = resp.status
                        detail = await self._read_error_detail(resp)

                        if status == 401:
                            raise LLMAuthError(f"Authentication failed: {detail}")

                        if status == 403:
                            raise LLMAuthError(f"Authorization failed: {detail}")

                        if status == 402:
                            raise LLMQuotaExceededError(f"Quota exceeded: {detail}")

                        if status == 404:
                            raise LLMModelNotFoundError(f"Model not found: {model} | {detail}")

                        if status == 429:
                            retry_after = _parse_retry_after(resp)
                            raise LLMRateLimitError(retry_after=retry_after)

                        if status >= 400:
                            raise LLMResponseError(f"API error {status}: {detail}")

                        try:
                            data = await resp.json(content_type=None)
                        except Exception as exc:
                            raise LLMResponseError(
                                f"JSON parse error: {exc}"
                            ) from exc

                        latency_ms = int((time.monotonic() - start) * 1000)
                        text, prompt_tokens, completion_tokens, done_reason = (
                            self._parse_generation_response(api_mode, data)
                        )

                        result = LLMResponse(
                            text=text,
                            model=model,
                            provider=self.provider_name,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            latency_ms=latency_ms,
                            done_reason=done_reason,
                        )
                        logger.info(
                            "LLM生成完了",
                            extra={"llm_provider": self.provider_name, "llm_model": model, "llm_latency_ms": latency_ms},
                        )
                        return result

            except (aiohttp.ClientConnectorError, aiohttp.ClientError) as exc:
                logger.warning("%s connection error: %s", self.provider_name, exc)
                raise LLMConnectionError(str(exc)) from exc

            except asyncio.TimeoutError as exc:
                wait = 2 ** attempt
                logger.warning(
                    "%s timeout (attempt %d/%d), retrying in %ds",
                    self.provider_name,
                    attempt + 1,
                    self._max_retries,
                    wait,
                )
                last_exc = LLMTimeoutError(str(exc))
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(wait)

            except (
                LLMAuthError,
                LLMRateLimitError,
                LLMQuotaExceededError,
                LLMModelNotFoundError,
                LLMResponseError,
            ):
                raise

        raise last_exc  # type: ignore[misc]

    def _resolve_api_mode(self, model: str) -> str:
        if self._api_mode != "auto":
            return self._api_mode
        if model.lower().startswith("gpt-5"):
            return "responses"
        return "chat_completions"

    @staticmethod
    def _build_chat_completions_payload(
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    @staticmethod
    def _build_responses_payload(
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
        reasoning_mode: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_prompt}],
                },
            ],
            "max_output_tokens": max_tokens,
        }
        if _supports_temperature(model):
            payload["temperature"] = temperature
        if reasoning_mode == "on":
            payload["reasoning"] = {"effort": "medium"}
        elif reasoning_mode == "off":
            payload["reasoning"] = {"effort": "minimal"}
        elif _prefer_minimal_reasoning(model):
            payload["reasoning"] = {"effort": "minimal"}
        return payload

    @staticmethod
    async def _read_error_detail(resp: aiohttp.ClientResponse) -> str:
        try:
            err_data = await resp.json(content_type=None)
        except Exception:
            text = await resp.text()
            return text or f"HTTP {resp.status}"
        return _build_error_detail(err_data)

    def _parse_generation_response(
        self,
        api_mode: str,
        data: dict[str, Any],
    ) -> tuple[str, int | None, int | None, str | None]:
        if api_mode == "responses":
            text = self._extract_responses_text(data)
            usage = data.get("usage", {})
            return (
                text,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                data.get("status"),
            )

        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return (
            text,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            data.get("choices", [{}])[0].get("finish_reason"),
        )

    @staticmethod
    def _extract_responses_text(data: dict[str, Any]) -> str:
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text:
            return output_text

        chunks: list[str] = []
        for item in data.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
        if chunks:
            return "".join(chunks)
        raise LLMResponseError("Responses API returned no output text")

    async def health_check(self) -> bool:
        """GET /models → 200 なら True、例外なら False。"""
        url = f"{self._base_url}/models"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    return resp.status == 200
        except Exception as exc:
            logger.warning("%s health_check failed: %s", self.provider_name, exc)
            return False

    async def list_models(self) -> list[str]:
        """GET /models → モデル名リストを返す。"""
        url = f"{self._base_url}/models"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:
                    raise LLMResponseError(f"JSON parse error: {exc}") from exc
                return [m["id"] for m in data.get("data", [])]

    # ------------------------------------------------------------------
    # プロパティ
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def supports_concurrent(self) -> bool:
        return True


def _supports_temperature(model: str) -> bool:
    model_lower = model.lower()
    if model_lower.startswith("gpt-5.4"):
        return True
    if model_lower.startswith("gpt-5"):
        return False
    return True


def _prefer_minimal_reasoning(model: str) -> bool:
    model_lower = model.lower()
    if model_lower.startswith("gpt-5.4"):
        return False
    return model_lower.startswith("gpt-5")
