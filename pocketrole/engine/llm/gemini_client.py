"""Google Gemini クラウド LLM クライアント実装。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from engine.llm.base import BaseLLMClient, LLMResponse
from engine.llm.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMModelNotFoundError,
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


class GeminiClient(BaseLLMClient):
    """Google Gemini への aiohttp クライアント。

    APIキーは URL クエリパラメータで渡す（Authorization ヘッダー不使用）。
    asyncio.Semaphore で同時実行数を制限（supports_concurrent = True）。
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: str,
        default_model: str = "gemini-2.0-flash",
        timeout_sec: int = 60,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._timeout_sec = timeout_sec
        self._max_retries = max_retries
        self._base_url = self.BASE_URL.rstrip("/")

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
        """POST /models/{model}:generateContent でテキストを生成する。

        APIキーは URL クエリパラメータ key= で渡す。
        タイムアウト時は指数バックオフで最大 max_retries 回再試行する。
        非リトライ例外（401/403/404/429/その他4xx-5xx）は即時 raise。
        """
        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        headers = {"Content-Type": "application/json"}
        params = {"key": self._api_key}
        url = f"{self._base_url}/models/{model}:generateContent"
        timeout = aiohttp.ClientTimeout(total=self._timeout_sec)

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            start = time.monotonic()
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload, headers=headers, params=params, timeout=timeout
                    ) as resp:
                        status = resp.status

                        if status in (401, 403):
                            raise LLMAuthError("Authentication failed")

                        if status == 404:
                            raise LLMModelNotFoundError(f"Model not found: {model}")

                        if status == 429:
                            retry_after = _parse_retry_after(resp)
                            raise LLMRateLimitError(retry_after=retry_after)

                        if status >= 400:
                            try:
                                err_data = await resp.json(content_type=None)
                                msg = (
                                    err_data.get("error", {}).get("message", "")
                                    or str(err_data)
                                )
                            except Exception:
                                msg = await resp.text()
                            raise LLMResponseError(f"API error {status}: {msg}")

                        try:
                            data = await resp.json(content_type=None)
                        except Exception as exc:
                            raise LLMResponseError(
                                f"JSON parse error: {exc}"
                            ) from exc

                        latency_ms = int((time.monotonic() - start) * 1000)
                        text: str = data["candidates"][0]["content"]["parts"][0]["text"]
                        usage = data.get("usageMetadata", {})
                        prompt_tokens: int | None = usage.get("promptTokenCount")
                        completion_tokens: int | None = usage.get("candidatesTokenCount")

                        result = LLMResponse(
                            text=text,
                            model=model,
                            provider=self.provider_name,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            latency_ms=latency_ms,
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
                LLMModelNotFoundError,
                LLMResponseError,
            ):
                raise

        raise last_exc  # type: ignore[misc]

    async def health_check(self) -> bool:
        """GET /models?key={api_key} → 200 なら True、例外なら False。"""
        url = f"{self._base_url}/models"
        params = {"key": self._api_key}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=timeout) as resp:
                    return resp.status == 200
        except Exception as exc:
            logger.warning("%s health_check failed: %s", self.provider_name, exc)
            return False

    async def list_models(self) -> list[str]:
        """GET /models?key={api_key} → モデル名リストを返す。

        "models/" プレフィックスを除去して返す。
        """
        url = f"{self._base_url}/models"
        params = {"key": self._api_key}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=timeout) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:
                    raise LLMResponseError(f"JSON parse error: {exc}") from exc
                return [m["name"].split("/")[-1] for m in data.get("models", [])]

    # ------------------------------------------------------------------
    # プロパティ
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def supports_concurrent(self) -> bool:
        return True
