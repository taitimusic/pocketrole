from __future__ import annotations

from engine.config import (
    APIKeyMissingError,
    CloudProviderConfig,
    ConfigValidationError,
    Config,
    EngineConfig,
    LLMConfig,
    LLMRuntimeConfig,
    LLMRuntimeProfileConfig,
    LoggingConfig,
    LLMProviderNotConfiguredError,
    OllamaProviderConfig,
    WebPosterSettingsConfig,
)
from engine.llm_runtime import (
    ensure_story_llm_runtime_requirements,
    required_story_llm_providers,
    resolve_story_llm_config,
)


def make_config() -> Config:
    return Config(
        llm=LLMConfig(
            default_provider="ollama",
            default_model="qwen2.5:14b",
            cloud_concurrency=3,
            providers={
                "ollama": OllamaProviderConfig(
                    base_url="http://localhost:11434",
                    default_model="qwen2.5:14b",
                    timeout_sec=120,
                    max_retries=3,
                ),
                "openai": CloudProviderConfig(
                    default_model="gpt-5.4-nano",
                    timeout_sec=60,
                    max_retries=3,
                    api_key="test-openai-key",
                    api_mode="auto",
                ),
            },
        ),
        llm_runtime=LLMRuntimeConfig(
            active_profile="openai_nano",
            profiles={
                "ollama_local": LLMRuntimeProfileConfig(
                    provider="ollama",
                    model="qwen2.5:14b",
                ),
                "openai_nano": LLMRuntimeProfileConfig(
                    provider="openai",
                    model="gpt-5.4-nano",
                ),
            },
            story_overrides={"mystery_story": "ollama_local"},
        ),
        engine=EngineConfig(memory_limit=20, emotion_decay_rate=0.05, max_move_cost=1),
        web_poster=WebPosterSettingsConfig(
            batch_size=10,
            retry_interval_sec=30,
            max_consecutive_failures=3,
            pause_duration_sec=300,
        ),
        web_post_targets={},
        logging=LoggingConfig(level="INFO", log_dir="logs", rotation="daily", retention_days=7),
    )


def test_resolve_story_llm_config_prefers_story_override() -> None:
    config = make_config()

    resolved = resolve_story_llm_config(
        config,
        "mystery_story",
    )

    assert resolved.profile_name == "ollama_local"
    assert resolved.provider == "ollama"
    assert resolved.model == "qwen2.5:14b"


def test_resolve_story_llm_config_falls_back_to_active_profile() -> None:
    config = make_config()

    resolved = resolve_story_llm_config(
        config,
        "ankoku_gakuen",
    )

    assert resolved.profile_name == "openai_nano"
    assert resolved.provider == "openai"
    assert resolved.model == "gpt-5.4-nano"


def test_required_story_llm_providers_uses_resolved_profiles() -> None:
    config = make_config()

    providers = required_story_llm_providers(
        config,
        ["ankoku_gakuen", "mystery_story"],
    )

    assert providers == {"ollama", "openai"}


def test_resolve_story_llm_config_requires_runtime_profile() -> None:
    config = make_config()
    config.llm_runtime = LLMRuntimeConfig()

    try:
        resolve_story_llm_config(config, "ankoku_gakuen")
    except ConfigValidationError as exc:
        assert "ankoku_gakuen" in str(exc)
    else:
        raise AssertionError("Expected ConfigValidationError")


def test_ensure_story_llm_runtime_requirements_rejects_missing_api_key() -> None:
    config = make_config()
    config.llm.providers["openai"] = CloudProviderConfig(
        default_model="gpt-5.4-nano",
        timeout_sec=60,
        max_retries=3,
        api_key="",
        api_mode="auto",
    )

    try:
        ensure_story_llm_runtime_requirements(
            config,
            ["ankoku_gakuen"],
        )
    except APIKeyMissingError as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected APIKeyMissingError")
