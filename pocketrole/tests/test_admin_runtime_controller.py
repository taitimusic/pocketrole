from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from admin.runtime_controller import HostedRuntimeController
from engine.config import ConfigValidationError


def make_db(*, story_web_receiver_url: str | None = None, story_web_auth_token: str | None = None) -> AsyncMock:
    """DB モック。get_story は web_receiver_url/auth_token を含む dict を返す。"""
    db = AsyncMock()
    db.get_story.return_value = {
        "web_receiver_url": story_web_receiver_url,
        "web_auth_token":   story_web_auth_token,
    }
    db.get_system_setting.return_value = None  # グローバルデフォルト未設定
    return db


def make_config() -> MagicMock:
    config = MagicMock()
    config.web_poster.batch_size = 10
    config.web_poster.retry_interval_sec = 30
    config.web_poster.max_consecutive_failures = 3
    config.web_poster.pause_duration_sec = 300
    config.web_post_targets = {
        "story_a": SimpleNamespace(
            enabled=True,
            receiver_url="https://a.example.com/receiver.php",
            auth_token="tok-a",
        ),
        "disabled_story": SimpleNamespace(
            enabled=False,
            receiver_url="",
            auth_token="",
        ),
    }
    return config


def make_engine():
    engine = AsyncMock()
    engine.run = AsyncMock()
    engine.stop = AsyncMock()
    return engine


class BlockingEngine:
    def __init__(self) -> None:
        self.stop = AsyncMock()
        self.started = asyncio.Event()

    async def initialize(self) -> None:
        pass

    async def run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()


class CrashingEngine:
    async def initialize(self) -> None:
        pass

    async def run(self) -> None:
        raise RuntimeError("engine boom")

    async def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_hosted_runtime_uses_story_specific_web_post_target() -> None:
    config = make_config()
    db = make_db()
    router = AsyncMock()
    engine = make_engine()
    controller = HostedRuntimeController(
        story_ids=["story_a"],
        db=db,
        router=router,
        config=config,
        engine_factory=lambda *_: engine,
    )

    poster = AsyncMock()
    poster.start = AsyncMock()
    poster.run = AsyncMock()

    with patch("admin.runtime_controller.WebPoster", return_value=poster) as poster_cls:
        await controller.start_story("story_a")

    poster_cfg = poster_cls.call_args.args[1]
    assert poster_cfg.receiver_url == "https://a.example.com/receiver.php"
    assert poster_cfg.auth_token == "tok-a"
    poster.start.assert_called_once()


@pytest.mark.asyncio
async def test_hosted_runtime_skips_web_poster_for_disabled_story() -> None:
    config = make_config()
    db = make_db()
    router = AsyncMock()
    engine = make_engine()
    controller = HostedRuntimeController(
        story_ids=["disabled_story"],
        db=db,
        router=router,
        config=config,
        engine_factory=lambda *_: engine,
    )

    with patch("admin.runtime_controller.WebPoster") as poster_cls:
        await controller.start_story("disabled_story")

    poster_cls.assert_not_called()


@pytest.mark.asyncio
async def test_hosted_runtime_skips_web_poster_when_story_target_missing() -> None:
    """DB にもグローバルにも YAML にも設定がなければ WebPoster なしでストーリーは起動する。"""
    config = make_config()
    db = make_db()
    router = AsyncMock()
    engine = make_engine()
    controller = HostedRuntimeController(
        story_ids=["missing_story"],
        db=db,
        router=router,
        config=config,
        engine_factory=lambda *_: engine,
    )

    with patch("admin.runtime_controller.WebPoster") as poster_cls:
        # ConfigValidationError ではなく正常終了し、WebPoster が生成されない
        await controller.start_story("missing_story")

    poster_cls.assert_not_called()


@pytest.mark.asyncio
async def test_hosted_runtime_stops_story_poster_on_stop() -> None:
    config = make_config()
    db = make_db()
    router = AsyncMock()
    engine = make_engine()
    controller = HostedRuntimeController(
        story_ids=["story_a"],
        db=db,
        router=router,
        config=config,
        engine_factory=lambda *_: engine,
    )

    poster = AsyncMock()
    poster.start = AsyncMock()
    poster.run = AsyncMock()
    poster.stop = AsyncMock()

    with patch("admin.runtime_controller.WebPoster", return_value=poster):
        await controller.start_story("story_a")
        await controller.stop_story("story_a")

    poster.stop.assert_called_once()


@pytest.mark.asyncio
async def test_hosted_runtime_stop_cancels_sleeping_engine_task() -> None:
    config = make_config()
    db = make_db()
    router = AsyncMock()
    engine = BlockingEngine()
    controller = HostedRuntimeController(
        story_ids=["disabled_story"],
        db=db,
        router=router,
        config=config,
        engine_factory=lambda *_: engine,
    )

    await controller.start_story("disabled_story")
    await engine.started.wait()

    await asyncio.wait_for(controller.stop_story("disabled_story"), timeout=2)

    engine.stop.assert_awaited_once()
    assert controller.list_states()["disabled_story"] == "stopped"


@pytest.mark.asyncio
async def test_hosted_runtime_logs_engine_task_exception(caplog: pytest.LogCaptureFixture) -> None:
    config = make_config()
    db = make_db()
    router = AsyncMock()
    controller = HostedRuntimeController(
        story_ids=["disabled_story"],
        db=db,
        router=router,
        config=config,
        engine_factory=lambda *_: CrashingEngine(),
    )

    with caplog.at_level(logging.ERROR, logger="admin.runtime_controller"):
        await controller.start_story("disabled_story")
        await asyncio.gather(controller._tasks["disabled_story"], return_exceptions=True)
        await asyncio.sleep(0)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Hosted runtime story task crashed story=disabled_story" in msg for msg in messages)
    assert any("engine boom" in msg for msg in messages)
