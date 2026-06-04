"""Runtime LLM profile resolution for story execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from engine.config import (
    APIKeyMissingError,
    CloudProviderConfig,
    Config,
    ConfigValidationError,
    LLMProviderNotConfiguredError,
    OllamaProviderConfig,
    _CLOUD_API_KEY_MAP,
)


@dataclass(frozen=True)
class ResolvedStoryLLMConfig:
    story_id: str
    provider: str
    model: str
    profile_name: str | None = None


def resolve_story_llm_config(
    config: Config,
    story_id: str,
) -> ResolvedStoryLLMConfig:
    profile_name = config.llm_runtime.story_overrides.get(story_id)
    if profile_name:
        profile = config.llm_runtime.profiles[profile_name]
        return ResolvedStoryLLMConfig(
            story_id=story_id,
            provider=profile.provider,
            model=profile.model,
            profile_name=profile_name,
        )

    active_profile = config.llm_runtime.active_profile
    if active_profile:
        profile = config.llm_runtime.profiles[active_profile]
        return ResolvedStoryLLMConfig(
            story_id=story_id,
            provider=profile.provider,
            model=profile.model,
            profile_name=active_profile,
        )

    raise ConfigValidationError(
        f"story '{story_id}' has no runtime LLM profile. "
        "Set active_profile or story_overrides in config/llm_runtime.local.yaml."
    )


def resolve_story_llm_configs(
    config: Config,
    story_ids: Iterable[str],
) -> dict[str, ResolvedStoryLLMConfig]:
    return {
        story_id: resolve_story_llm_config(config, story_id)
        for story_id in story_ids
    }


def required_story_llm_providers(
    config: Config,
    story_ids: Iterable[str],
) -> set[str]:
    resolved = resolve_story_llm_configs(config, story_ids)
    return {item.provider for item in resolved.values()}


def ensure_story_llm_runtime_requirements(
    config: Config,
    story_ids: Iterable[str],
) -> dict[str, ResolvedStoryLLMConfig]:
    resolved = resolve_story_llm_configs(config, story_ids)
    for story_id, item in resolved.items():
        provider_cfg = config.llm.providers.get(item.provider)
        if isinstance(provider_cfg, OllamaProviderConfig):
            continue
        if isinstance(provider_cfg, CloudProviderConfig):
            if not provider_cfg.api_key:
                api_key_name = _CLOUD_API_KEY_MAP.get(item.provider, "API key")
                raise APIKeyMissingError(
                    f"story '{story_id}' resolved to provider '{item.provider}', "
                    f"but {api_key_name} is not configured."
                )
            continue
        raise LLMProviderNotConfiguredError(
            f"story '{story_id}' resolved to provider '{item.provider}', "
            "but no matching provider config was loaded."
        )
    return resolved
