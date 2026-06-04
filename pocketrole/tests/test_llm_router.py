"""LLMRouter のテスト。"""

from __future__ import annotations

import asyncio

import pytest

from engine.config import (
    LLMConfig,
    LLMProviderNotConfiguredError,
    ModelProfileConfig,
    OllamaProviderConfig,
)
from engine.llm.base import BaseLLMClient, LLMResponse
from engine.llm.exceptions import LLMConnectionError
from engine.llm.router import LLMRouter


# ============================================================
# ヘルパー
# ============================================================


def make_ollama_config() -> LLMConfig:
    """テスト用 LLMConfig（Ollamaのみ）を生成する。"""
    return LLMConfig(
        default_provider="ollama",
        default_model="",
        cloud_concurrency=3,
        providers={
            "ollama": OllamaProviderConfig(
                base_url="http://localhost:11434",
                default_model="qwen2.5:14b",
                timeout_sec=120,
                max_retries=3,
            )
        },
    )


def make_mock_response(provider: str = "ollama") -> LLMResponse:
    """テスト用 LLMResponse を生成する。"""
    return LLMResponse(
        text="hello",
        model="test-model",
        provider=provider,
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=100,
    )


class SerialMockClient(BaseLLMClient):
    """supports_concurrent=False の直列モッククライアント。"""

    def __init__(self, response: LLMResponse | None = None) -> None:
        self._response = response or make_mock_response()
        self.last_model: str | None = None
        self.last_kwargs: dict = {}

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        **kwargs,
    ) -> LLMResponse:
        self.last_model = model
        self.last_kwargs = kwargs
        return self._response

    async def health_check(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def supports_concurrent(self) -> bool:
        return False


class ConcurrentMockClient(BaseLLMClient):
    """supports_concurrent=True の並行モッククライアント。"""

    def __init__(self, response: LLMResponse | None = None) -> None:
        self._response = response or make_mock_response(provider="mock_cloud")

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        **kwargs,
    ) -> LLMResponse:
        return self._response

    async def health_check(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["cloud-model"]

    @property
    def provider_name(self) -> str:
        return "mock_cloud"

    @property
    def supports_concurrent(self) -> bool:
        return True


# ============================================================
# プロバイダー登録テスト
# ============================================================


def test_ollama_registered_when_present() -> None:
    """Ollama 設定が存在すれば _clients に登録される。"""
    router = LLMRouter(make_ollama_config())
    assert "ollama" in router._clients
    assert router._clients["ollama"].provider_name == "ollama"


def test_ollama_not_registered_when_not_required() -> None:
    """required_providers に含まれない Ollama は登録されない。"""
    router = LLMRouter(make_ollama_config(), required_providers={"openai"})
    assert "ollama" not in router._clients


# ============================================================
# get_client テスト
# ============================================================


def test_get_client_returns_client() -> None:
    """get_client("ollama") が BaseLLMClient を返す。"""
    router = LLMRouter(make_ollama_config())
    client = router.get_client("ollama")
    assert isinstance(client, BaseLLMClient)


def test_get_client_raises_for_unknown() -> None:
    """未登録プロバイダーで LLMProviderNotConfiguredError が送出される。"""
    router = LLMRouter(make_ollama_config())
    with pytest.raises(LLMProviderNotConfiguredError):
        router.get_client("unknown")


def test_get_model_profile_returns_builtin_qwen_profile() -> None:
    """Qwen3 系には empty-final 回避用の built-in profile が適用される。"""
    router = LLMRouter(make_ollama_config())
    profile = router.get_model_profile("ollama", "qwen3.5:9b")

    assert profile.reasoning_mode == "auto"
    assert profile.max_tokens == 1000
    assert profile.recovery_max_tokens == 1400
    assert profile.recovery_reasoning_mode == "off"


def test_get_model_profile_returns_builtin_gemma_profile() -> None:
    """Gemma4:e4b には empty-final 抑制用の保守的 profile を与える。"""
    router = LLMRouter(make_ollama_config())
    profile = router.get_model_profile("ollama", "gemma4:e4b")

    assert profile.reasoning_mode == "off"
    assert profile.max_tokens == 700
    assert profile.recovery_max_tokens == 800
    assert profile.recovery_reasoning_mode == "off"


def test_get_model_profile_custom_overrides_builtin() -> None:
    """config の profile は built-in fallback より優先される。"""
    config = make_ollama_config()
    config.model_profiles = {
        "ollama": {
            "qwen3.5:9b": ModelProfileConfig(
                reasoning_mode="off",
                max_tokens=1800,
                temperature=0.2,
            )
        }
    }
    router = LLMRouter(config)
    profile = router.get_model_profile("ollama", "qwen3.5:9b")

    assert profile.reasoning_mode == "off"
    assert profile.max_tokens == 1800
    assert profile.temperature == 0.2
    assert profile.recovery_max_tokens == 1400
    assert profile.recovery_reasoning_mode == "off"


# ============================================================
# generate テスト
# ============================================================


async def test_generate_returns_llm_response() -> None:
    """generate() が LLMResponse を返す。"""
    router = LLMRouter(make_ollama_config())
    router._clients["ollama"] = SerialMockClient()
    result = await router.generate("ollama", "sys", "user", "test-model")
    assert isinstance(result, LLMResponse)
    assert result.text == "hello"


async def test_generate_passes_model() -> None:
    """model 引数がクライアントに正しく渡される。"""
    mock = SerialMockClient()
    router = LLMRouter(make_ollama_config())
    router._clients["ollama"] = mock
    await router.generate("ollama", "sys", "user", "specific-model")
    assert mock.last_model == "specific-model"


async def test_generate_passes_kwargs() -> None:
    """temperature / max_tokens が kwargs 経由でクライアントに渡される。"""
    mock = SerialMockClient()
    router = LLMRouter(make_ollama_config())
    router._clients["ollama"] = mock
    await router.generate(
        "ollama", "sys", "user", "model",
        temperature=0.5, max_tokens=200,
    )
    assert mock.last_kwargs.get("temperature") == 0.5
    assert mock.last_kwargs.get("max_tokens") == 200


async def test_generate_serial_order() -> None:
    """直列クライアントへの並行呼び出しが順番通りに実行される。"""
    execution_log: list[str] = []

    class TrackingSerial(BaseLLMClient):
        async def generate(self, system_prompt, user_prompt, model, **kwargs):
            execution_log.append("start")
            await asyncio.sleep(0)  # 制御を譲る
            execution_log.append("end")
            return make_mock_response()

        async def health_check(self) -> bool:
            return True

        async def list_models(self) -> list[str]:
            return []

        @property
        def provider_name(self) -> str:
            return "ollama"

        @property
        def supports_concurrent(self) -> bool:
            return False

    router = LLMRouter(make_ollama_config())
    router._clients["ollama"] = TrackingSerial()

    task1 = asyncio.create_task(router.generate("ollama", "s1", "u1", "m"))
    await asyncio.sleep(0)  # task1 が Queue トークンを取得するまで進める
    task2 = asyncio.create_task(router.generate("ollama", "s2", "u2", "m"))
    await asyncio.gather(task1, task2)

    assert execution_log == ["start", "end", "start", "end"]


async def test_generate_concurrent_allowed() -> None:
    """並行クライアントへの呼び出しが同時実行される。"""
    concurrent_count = 0
    max_concurrent = 0

    class TrackingConcurrent(BaseLLMClient):
        async def generate(self, system_prompt, user_prompt, model, **kwargs):
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0)
            concurrent_count -= 1
            return make_mock_response(provider="mock_cloud")

        async def health_check(self) -> bool:
            return True

        async def list_models(self) -> list[str]:
            return []

        @property
        def provider_name(self) -> str:
            return "mock_cloud"

        @property
        def supports_concurrent(self) -> bool:
            return True

    router = LLMRouter(make_ollama_config())
    router._clients["mock_cloud"] = TrackingConcurrent()

    tasks = [
        asyncio.create_task(router.generate("mock_cloud", "s", "u", "m"))
        for _ in range(3)
    ]
    await asyncio.gather(*tasks)

    # 複数タスクが同時実行された（max_concurrent > 1）ことを確認
    assert max_concurrent > 1


async def test_generate_propagates_error() -> None:
    """クライアントの LLMConnectionError が Router を通じて伝播する。"""

    class FailingClient(BaseLLMClient):
        async def generate(self, system_prompt, user_prompt, model, **kwargs):
            raise LLMConnectionError("connection refused")

        async def health_check(self) -> bool:
            return False

        async def list_models(self) -> list[str]:
            return []

        @property
        def provider_name(self) -> str:
            return "ollama"

        @property
        def supports_concurrent(self) -> bool:
            return False

    router = LLMRouter(make_ollama_config())
    router._clients["ollama"] = FailingClient()

    with pytest.raises(LLMConnectionError):
        await router.generate("ollama", "s", "u", "m")


# ============================================================
# health_check_all テスト
# ============================================================


async def test_health_check_all_ok() -> None:
    """health_check_all() → {"ollama": True}。"""
    router = LLMRouter(make_ollama_config())
    router._clients["ollama"] = SerialMockClient()
    result = await router.health_check_all()
    assert result == {"ollama": True}


async def test_health_check_all_fail() -> None:
    """health_check() が例外を送出した場合 False になる。"""

    class BrokenClient(BaseLLMClient):
        async def generate(self, *args, **kwargs):
            return make_mock_response()

        async def health_check(self) -> bool:
            raise RuntimeError("unreachable")

        async def list_models(self) -> list[str]:
            return []

        @property
        def provider_name(self) -> str:
            return "ollama"

        @property
        def supports_concurrent(self) -> bool:
            return False

    router = LLMRouter(make_ollama_config())
    router._clients["ollama"] = BrokenClient()
    result = await router.health_check_all()
    assert result == {"ollama": False}


# ============================================================
# ライフサイクルテスト
# ============================================================


async def test_start_runs() -> None:
    """start() が正常終了する。"""
    router = LLMRouter(make_ollama_config())
    await router.start()  # 例外が発生しないことを確認


async def test_stop_runs() -> None:
    """stop() が正常終了する。"""
    router = LLMRouter(make_ollama_config())
    await router.stop()  # 例外が発生しないことを確認


# ============================================================
# クラウドプロバイダー登録テスト（#3-5 / #3-17）
# ============================================================


def make_cloud_config(
    provider: str = "openai",
) -> LLMConfig:
    """テスト用 LLMConfig（クラウドプロバイダー）を生成する。"""
    from engine.config import CloudProviderConfig
    return LLMConfig(
        default_provider="ollama",
        default_model="",
        cloud_concurrency=3,
        providers={
            "ollama": OllamaProviderConfig(
                base_url="http://localhost:11434",
                default_model="qwen2.5:14b",
                timeout_sec=120,
                max_retries=3,
            ),
            provider: CloudProviderConfig(
                default_model="test-model",
                timeout_sec=60,
                max_retries=3,
                api_key="test-key",
                api_mode="auto" if provider == "openai" else None,
            ),
        },
    )


def test_openai_registered_when_present() -> None:
    """OpenAI 設定が存在すれば _clients に登録される。"""
    router = LLMRouter(make_cloud_config(provider="openai"))
    assert "openai" in router._clients
    assert router._clients["openai"].provider_name == "openai"


def test_cloud_not_registered_when_not_required() -> None:
    """required_providers に含まれないクラウド設定は登録されない。"""
    router = LLMRouter(make_cloud_config(provider="openai"), required_providers={"ollama"})
    assert "openai" not in router._clients


def test_anthropic_registered() -> None:
    """Anthropic enabled=True → "anthropic" が _clients に登録される。"""
    router = LLMRouter(make_cloud_config(provider="anthropic"))
    assert "anthropic" in router._clients
    assert router._clients["anthropic"].provider_name == "anthropic"


def test_gemini_registered() -> None:
    """Gemini enabled=True → "gemini" が _clients に登録される。"""
    router = LLMRouter(make_cloud_config(provider="gemini"))
    assert "gemini" in router._clients
    assert router._clients["gemini"].provider_name == "gemini"


def test_deepseek_registered() -> None:
    """DeepSeek enabled=True → "deepseek" が _clients に登録される。"""
    router = LLMRouter(make_cloud_config(provider="deepseek"))
    assert "deepseek" in router._clients
    assert router._clients["deepseek"].provider_name == "deepseek"


def test_all_cloud_providers_registered() -> None:
    """4プロバイダー設定があれば全て _clients に登録される。"""
    from engine.config import CloudProviderConfig

    config = LLMConfig(
        default_provider="ollama",
        default_model="",
        cloud_concurrency=3,
        providers={
            "ollama": OllamaProviderConfig(
                base_url="http://localhost:11434",
                default_model="qwen2.5:14b",
                timeout_sec=120,
                max_retries=3,
            ),
            "openai": CloudProviderConfig(default_model="gpt-4o", timeout_sec=60, max_retries=3, api_key="test-key", api_mode="auto"),
            "anthropic": CloudProviderConfig(default_model="claude-3-5-sonnet-20241022", timeout_sec=60, max_retries=3, api_key="test-key"),
            "gemini": CloudProviderConfig(default_model="gemini-2.0-flash", timeout_sec=60, max_retries=3, api_key="test-key"),
            "deepseek": CloudProviderConfig(default_model="deepseek-chat", timeout_sec=60, max_retries=3, api_key="test-key"),
        },
    )
    router = LLMRouter(config)
    for provider in ("openai", "anthropic", "gemini", "deepseek"):
        assert provider in router._clients, f"{provider} not registered"


async def test_generate_with_cloud_provider_via_semaphore() -> None:
    """クラウドクライアントに差し替えた後、generate() が LLMResponse を返す。"""
    router = LLMRouter(make_cloud_config(provider="openai"))
    router._clients["openai"] = ConcurrentMockClient(make_mock_response(provider="mock_cloud"))
    result = await router.generate("openai", "sys", "user", "test-model")
    assert isinstance(result, LLMResponse)
    assert result.text == "hello"
