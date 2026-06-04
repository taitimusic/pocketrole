from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config import StoryWebPostTargetConfig
from engine.web_post_targets import resolve_story_web_poster_config_with_db


def _make_config() -> MagicMock:
    config = MagicMock()
    config.web_poster.batch_size = 10
    config.web_poster.retry_interval_sec = 30
    config.web_poster.max_consecutive_failures = 3
    config.web_poster.pause_duration_sec = 300
    config.web_post_targets = {
        "story_a": StoryWebPostTargetConfig(
            enabled=True,
            receiver_url="https://yaml.example.com/receiver.php",
            auth_token="yaml-token",
        )
    }
    return config


def _make_db(
    *,
    story_url: str | None = None,
    story_token: str | None = None,
    default_url: str | None = None,
    default_token: str | None = None,
) -> AsyncMock:
    db = AsyncMock()
    db.get_story.return_value = {
        "web_receiver_url": story_url,
        "web_auth_token": story_token,
    }

    async def get_system_setting(key: str) -> str | None:
        if key == "default_web_receiver_url":
            return default_url
        if key == "default_web_auth_token":
            return default_token
        return None

    db.get_system_setting.side_effect = get_system_setting
    return db


@pytest.mark.asyncio
async def test_db_story_web_post_target_takes_precedence_over_yaml() -> None:
    config = _make_config()
    db = _make_db(
        story_url="https://db.example.com/receiver.php",
        story_token="db-token",
    )

    resolved = await resolve_story_web_poster_config_with_db(config, "story_a", db)

    assert resolved is not None
    assert resolved.receiver_url == "https://db.example.com/receiver.php"
    assert resolved.auth_token == "db-token"


@pytest.mark.asyncio
async def test_default_web_post_target_takes_precedence_over_yaml() -> None:
    config = _make_config()
    db = _make_db(
        default_url="https://default.example.com/receiver.php",
        default_token="default-token",
    )

    resolved = await resolve_story_web_poster_config_with_db(config, "story_a", db)

    assert resolved is not None
    assert resolved.receiver_url == "https://default.example.com/receiver.php"
    assert resolved.auth_token == "default-token"


@pytest.mark.asyncio
async def test_web_post_target_resolver_rejects_private_db_url() -> None:
    config = _make_config()
    db = _make_db(
        story_url="http://127.0.0.1/receiver.php",
        story_token="db-token",
    )

    resolved = await resolve_story_web_poster_config_with_db(config, "story_a", db)

    assert resolved is None


@pytest.mark.asyncio
async def test_web_post_target_resolver_returns_none_when_unconfigured() -> None:
    config = _make_config()
    config.web_post_targets = {}
    db = _make_db()

    resolved = await resolve_story_web_poster_config_with_db(config, "story_missing", db)

    assert resolved is None
