"""LLMRouter — プロバイダー振り分け・キュー/セマフォ管理。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from engine.config import (
    CloudProviderConfig,
    LLMConfig,
    LLMProviderNotConfiguredError,
    ModelProfileConfig,
    OllamaProviderConfig,
)
from engine.llm.anthropic_client import AnthropicClient
from engine.llm.base import BaseLLMClient, LLMResponse
from engine.llm.deepseek_client import DeepSeekClient
from engine.llm.gemini_client import GeminiClient
from engine.llm.ollama_client import OllamaClient
from engine.llm.openai_client import OpenAIClient

logger = logging.getLogger(__name__)

# クラウドプロバイダー名 → (クライアントクラス, 環境変数名)
_CLOUD_CLIENT_MAP: dict[str, tuple[type, str]] = {
    "openai":    (OpenAIClient,    "OPENAI_API_KEY"),
    "anthropic": (AnthropicClient, "ANTHROPIC_API_KEY"),
    "gemini":    (GeminiClient,    "GEMINI_API_KEY"),
    "deepseek":  (DeepSeekClient,  "DEEPSEEK_API_KEY"),
}


class LLMRouter:
    """LLMクライアントを管理し、適切なキュー制御でルーティングする。

    - Ollama（supports_concurrent=False）: asyncio.Queue(maxsize=1) で直列化
    - クラウドAPI（supports_concurrent=True）: asyncio.Semaphore で並行制限
    """

    def __init__(
        self,
        config: LLMConfig,
        required_providers: set[str] | None = None,
    ) -> None:
        self._config = config
        self._required_providers = set(required_providers) if required_providers else None
        self._clients: dict[str, BaseLLMClient] = {}
        # Ollama（直列）用: maxsize=1 の Queue に 1 トークンを事前投入
        self._local_queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._local_queue.put_nowait(None)
        # クラウド（並行）用: Semaphore
        self._cloud_semaphore: asyncio.Semaphore = asyncio.Semaphore(config.cloud_concurrency)
        self._register_clients()

    def _register_clients(self) -> None:
        """config.providers から必要なクライアントを登録する。"""
        required = self._required_providers
        ollama_cfg = self._config.providers.get("ollama")
        if (
            isinstance(ollama_cfg, OllamaProviderConfig)
            and (required is None or "ollama" in required)
        ):
            self._clients["ollama"] = OllamaClient(
                base_url=ollama_cfg.base_url,
                default_model=ollama_cfg.default_model,
                timeout_sec=ollama_cfg.timeout_sec,
                max_retries=ollama_cfg.max_retries,
            )
            logger.info("Registered LLM provider: ollama")

        # クラウドプロバイダー登録
        for provider_name, (client_class, env_key) in _CLOUD_CLIENT_MAP.items():
            if required is not None and provider_name not in required:
                continue
            cloud_cfg = self._config.providers.get(provider_name)
            if not isinstance(cloud_cfg, CloudProviderConfig):
                continue
            kwargs: dict[str, Any] = {
                "api_key": cloud_cfg.api_key,
                "default_model": cloud_cfg.default_model,
                "timeout_sec": cloud_cfg.timeout_sec,
                "max_retries": cloud_cfg.max_retries,
            }
            if provider_name == "openai":
                kwargs["api_mode"] = cloud_cfg.api_mode or "auto"
            self._clients[provider_name] = client_class(**kwargs)
            logger.info("Registered LLM provider: %s", provider_name)

    def get_client(self, provider: str) -> BaseLLMClient:
        """登録済みクライアントを返す。未登録なら LLMProviderNotConfiguredError。"""
        if provider not in self._clients:
            raise LLMProviderNotConfiguredError(
                f"Provider '{provider}' is not registered or not enabled."
            )
        return self._clients[provider]

    def get_model_profile(self, provider: str, model: str) -> ModelProfileConfig:
        """provider/model に対する生成 profile を返す。

        優先順位:
        1. config.yaml の llm.model_profiles
        2. コード側の安全な built-in fallback
        3. 何もなければ共通デフォルト
        """
        profile = ModelProfileConfig()
        built_in = self._built_in_profile(provider, model)
        profile = self._merge_profile(profile, built_in)
        provider_profiles = self._config.model_profiles.get(provider, {})
        custom = provider_profiles.get(model)
        if custom is not None:
            profile = self._merge_profile(profile, custom)
        return profile

    @staticmethod
    def _merge_profile(
        base: ModelProfileConfig,
        override: ModelProfileConfig | None,
    ) -> ModelProfileConfig:
        if override is None:
            return base
        return ModelProfileConfig(
            reasoning_mode=override.reasoning_mode or base.reasoning_mode,
            max_tokens=override.max_tokens if override.max_tokens is not None else base.max_tokens,
            recovery_max_tokens=(
                override.recovery_max_tokens
                if override.recovery_max_tokens is not None
                else base.recovery_max_tokens
            ),
            temperature=override.temperature if override.temperature is not None else base.temperature,
            recovery_temperature=(
                override.recovery_temperature
                if override.recovery_temperature is not None
                else base.recovery_temperature
            ),
            recovery_reasoning_mode=(
                override.recovery_reasoning_mode
                if override.recovery_reasoning_mode is not None
                else base.recovery_reasoning_mode
            ),
        )

    @staticmethod
    def _built_in_profile(provider: str, model: str) -> ModelProfileConfig | None:
        model_lower = model.lower()
        if provider == "ollama" and model_lower.startswith("qwen3"):
            # Qwen3.x は thinking 系の前提が強いため、保守的な既定値を与える。
            return ModelProfileConfig(
                reasoning_mode="auto",
                max_tokens=1000,
                recovery_max_tokens=1400,
                recovery_reasoning_mode="off",
            )
        if provider == "ollama" and model_lower.startswith("gemma4"):
            # Gemma4:e4b は empty_final が出やすいため、thinking を抑えて短めに返させる。
            return ModelProfileConfig(
                reasoning_mode="off",
                max_tokens=700,
                recovery_max_tokens=800,
                recovery_reasoning_mode="off",
            )
        return None

    async def generate(
        self,
        provider: str,
        system_prompt: str,
        user_prompt: str,
        model: str,
        **kwargs: Any,
    ) -> LLMResponse:
        """キュー/セマフォ経由でプロバイダーにルーティングする。

        例外はクライアントから伝播させるだけ（Router では変換しない）。
        """
        client = self.get_client(provider)

        if not client.supports_concurrent:
            # 直列: Queue トークンを取得して実行し、完了後に返却
            token = await self._local_queue.get()
            try:
                result = await client.generate(system_prompt, user_prompt, model, **kwargs)
            finally:
                self._local_queue.put_nowait(token)
            return result
        else:
            # 並行: Semaphore で同時実行数を制限
            async with self._cloud_semaphore:
                return await client.generate(system_prompt, user_prompt, model, **kwargs)

    async def health_check_all(self) -> dict[str, bool]:
        """登録済み全プロバイダーのヘルスチェック結果を返す。"""
        results: dict[str, bool] = {}
        for name, client in self._clients.items():
            try:
                results[name] = await client.health_check()
            except Exception:
                results[name] = False
        return results

    async def start(self) -> None:
        """ルーター起動（Phase 1 はログのみ）。"""
        logger.info("LLMRouter started. providers=%s", list(self._clients.keys()))

    async def stop(self) -> None:
        """ルーター停止（Phase 1 はログのみ）。"""
        logger.info("LLMRouter stopped.")
