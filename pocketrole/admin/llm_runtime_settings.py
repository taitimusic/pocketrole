"""Helpers for editing config/llm_runtime.local.yaml from admin settings."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

from engine.config import LLMRuntimeConfig, LLMRuntimeProfileConfig, VALID_PROVIDERS


_PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_STORY_ID_RE = re.compile(r"^[a-z0-9_]+$")


class LLMRuntimeSettingsError(RuntimeError):
    """Raised when LLM runtime settings cannot be read or written safely."""


class LLMRuntimeSettingsService:
    def __init__(self, llm_runtime_path: str | Path) -> None:
        self._path = Path(llm_runtime_path)

    async def get_payload(self) -> dict[str, Any]:
        data = self._load()
        return {
            "path": str(self._path),
            "exists": self._path.exists(),
            "active_profile": str(data.get("active_profile") or ""),
            "profiles": self._profiles_payload(data),
            "story_overrides": self._story_overrides_payload(data),
        }

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._normalize_payload(payload)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return {
            "path": str(self._path),
            "active_profile": str(data.get("active_profile") or ""),
            "profile_count": len(data.get("profiles") or {}),
            "story_override_count": len(data.get("story_overrides") or {}),
            "runtime_config": self.to_runtime_config(data),
        }

    def to_runtime_config(self, data: dict[str, Any]) -> LLMRuntimeConfig:
        profiles = {
            str(profile_name): LLMRuntimeProfileConfig(
                provider=str(profile["provider"]),
                model=str(profile["model"]),
            )
            for profile_name, profile in (data.get("profiles") or {}).items()
        }
        return LLMRuntimeConfig(
            active_profile=str(data.get("active_profile") or ""),
            profiles=profiles,
            story_overrides={
                str(story_id): str(profile_name)
                for story_id, profile_name in (data.get("story_overrides") or {}).items()
            },
        )

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"active_profile": "", "profiles": {}, "story_overrides": {}}
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise LLMRuntimeSettingsError(f"llm_runtime YAML parse error: {exc}") from exc
        if data is None:
            return {"active_profile": "", "profiles": {}, "story_overrides": {}}
        if not isinstance(data, dict):
            raise LLMRuntimeSettingsError("llm_runtime top-level must be a mapping")
        return data

    def _profiles_payload(self, data: dict[str, Any]) -> list[dict[str, str]]:
        profiles = data.get("profiles") or {}
        if not isinstance(profiles, dict):
            raise LLMRuntimeSettingsError("profiles must be a mapping")
        payload = []
        for profile_name, profile in profiles.items():
            if not isinstance(profile, dict):
                raise LLMRuntimeSettingsError(f"profile must be a mapping: {profile_name}")
            payload.append(
                {
                    "profile_name": str(profile_name),
                    "provider": str(profile.get("provider") or ""),
                    "model": str(profile.get("model") or ""),
                }
            )
        return sorted(payload, key=lambda item: item["profile_name"])

    def _story_overrides_payload(self, data: dict[str, Any]) -> dict[str, str]:
        overrides = data.get("story_overrides") or {}
        if not isinstance(overrides, dict):
            raise LLMRuntimeSettingsError("story_overrides must be a mapping")
        return {str(story_id): str(profile_name) for story_id, profile_name in overrides.items()}

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        profiles_raw = payload.get("profiles") or []
        if not isinstance(profiles_raw, list):
            raise LLMRuntimeSettingsError("profiles must be a list")

        profiles: dict[str, dict[str, str]] = {}
        for item in profiles_raw:
            if not isinstance(item, dict):
                raise LLMRuntimeSettingsError("profile item must be an object")
            profile_name = str(item.get("profile_name") or "").strip()
            provider = str(item.get("provider") or "").strip()
            model = str(item.get("model") or "").strip()
            if not _PROFILE_NAME_RE.fullmatch(profile_name):
                raise LLMRuntimeSettingsError(
                    f"profile_name must match [a-z][a-z0-9_]*: {profile_name}"
                )
            if profile_name in profiles:
                raise LLMRuntimeSettingsError(f"duplicate profile_name: {profile_name}")
            if provider not in VALID_PROVIDERS:
                raise LLMRuntimeSettingsError(f"unsupported provider: {provider}")
            if not model:
                raise LLMRuntimeSettingsError(f"model is required for profile: {profile_name}")
            profiles[profile_name] = {"provider": provider, "model": model}

        active_profile = str(payload.get("active_profile") or "").strip()
        if active_profile and active_profile not in profiles:
            raise LLMRuntimeSettingsError("active_profile must reference an existing profile")

        overrides_raw = payload.get("story_overrides") or {}
        if not isinstance(overrides_raw, dict):
            raise LLMRuntimeSettingsError("story_overrides must be a mapping")
        story_overrides: dict[str, str] = {}
        for story_id, profile_name_raw in overrides_raw.items():
            normalized_story_id = str(story_id).strip()
            profile_name = str(profile_name_raw or "").strip()
            if not normalized_story_id or not profile_name:
                continue
            if not _STORY_ID_RE.fullmatch(normalized_story_id):
                raise LLMRuntimeSettingsError(f"invalid story_id in override: {normalized_story_id}")
            if profile_name not in profiles:
                raise LLMRuntimeSettingsError(
                    f"story override references unknown profile: {profile_name}"
                )
            story_overrides[normalized_story_id] = profile_name

        return {
            "active_profile": active_profile,
            "profiles": profiles,
            "story_overrides": story_overrides,
        }
