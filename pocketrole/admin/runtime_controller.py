"""Hosted runtime control primitives."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from db.db_manager import DatabaseManager
from engine.config import Config, LLMRuntimeConfig
from engine.llm.router import LLMRouter
from engine.llm_runtime import ResolvedStoryLLMConfig, resolve_story_llm_configs
from engine.story_engine import StoryEngine
from engine.web_poster import WebPoster


logger = logging.getLogger(__name__)
_STOP_GRACE_TIMEOUT_SEC = 1.0


class HostedRuntimeController:
    def __init__(
        self,
        story_ids: list[str],
        db: DatabaseManager,
        router: LLMRouter,
        config: Config,
        resolved_llm_configs: dict[str, ResolvedStoryLLMConfig] | None = None,
        engine_factory: Callable[[str, DatabaseManager, LLMRouter], StoryEngine] | None = None,
    ) -> None:
        self._story_ids = list(story_ids)
        self._db = db
        self._router = router
        self._config = config
        self._resolved_llm_configs = dict(resolved_llm_configs or {})
        self._engine_factory = engine_factory or self._make_engine
        self._engines: dict[str, StoryEngine] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._web_posters: dict[str, WebPoster] = {}
        self._web_poster_tasks: dict[str, asyncio.Task[None]] = {}

    async def initialize(self) -> None:
        return None

    def register_story(self, story_id: str) -> None:
        if story_id not in self._story_ids:
            self._story_ids.append(story_id)

    def list_states(self) -> dict[str, str]:
        states = {}
        for story_id in self._story_ids:
            task = self._tasks.get(story_id)
            states[story_id] = "running" if task is not None and not task.done() else "stopped"
        return states

    def apply_llm_runtime_config(self, llm_runtime: LLMRuntimeConfig) -> None:
        self._config.llm_runtime = llm_runtime
        self._resolved_llm_configs = resolve_story_llm_configs(self._config, self._story_ids)

    async def set_story_state(self, story_id: str, desired_state: str) -> dict[str, str]:
        if desired_state == "running":
            await self.start_story(story_id)
        elif desired_state == "stopped":
            await self.stop_story(story_id)
        else:
            raise ValueError(f"unsupported desired_state: {desired_state}")
        return {"story_id": story_id, "desired_state": desired_state}

    async def restart_story(self, story_id: str) -> dict[str, str]:
        await self.stop_story(story_id)
        await self.start_story(story_id)
        return {"story_id": story_id, "desired_state": "running"}

    async def start_story(self, story_id: str) -> None:
        task = self._tasks.get(story_id)
        if task is not None and not task.done():
            return
        await self._ensure_web_poster(story_id)
        engine = self._engine_factory(story_id, self._db, self._router)
        self._engines[story_id] = engine
        await engine.initialize()  # 失敗時は例外が呼び出し元に伝播し UI でエラー表示される
        task = asyncio.create_task(engine.run(), name=f"story-{story_id}")
        task.add_done_callback(
            lambda completed_task, sid=story_id: self._on_story_task_done(sid, completed_task)
        )
        self._tasks[story_id] = task
        logger.info("HostedRuntimeController started story=%s", story_id)

    async def stop_story(self, story_id: str) -> None:
        engine = self._engines.get(story_id)
        task = self._tasks.get(story_id)
        if engine is not None:
            await engine.stop()
        if task is not None:
            await self._finish_or_cancel_task(task)
        self._engines.pop(story_id, None)
        self._tasks.pop(story_id, None)
        poster = self._web_posters.pop(story_id, None)
        poster_task = self._web_poster_tasks.pop(story_id, None)
        if poster is not None:
            await poster.stop()
        if poster_task is not None:
            await self._finish_or_cancel_task(poster_task)
        logger.info("HostedRuntimeController stopped story=%s", story_id)

    async def _finish_or_cancel_task(self, task: asyncio.Task[Any]) -> None:
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_STOP_GRACE_TIMEOUT_SEC)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _on_story_task_done(self, story_id: str, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Hosted runtime story task crashed story=%s: %s",
                story_id,
                exc,
                exc_info=exc,
            )

    def get_chapter_generator(self, story_id: str) -> Any | None:
        """実行中 StoryEngine の ChapterGenerator を返す。未起動 or 無効なら None。"""
        engine = self._engines.get(story_id)
        if engine is None:
            return None
        return getattr(engine, "_chapter_generator", None)

    async def reload_director_persona(self, story_id: str) -> bool:
        """実行中 StoryEngine の active director persona を即時再読み込みする。"""
        engine = self._engines.get(story_id)
        if engine is None:
            return False
        director_persona = getattr(engine, "_director_persona", None)
        if director_persona is None:
            return False
        await director_persona.reload_active_persona()
        return True

    def _make_engine(self, story_id: str, db: DatabaseManager, router: LLMRouter) -> StoryEngine:
        return StoryEngine(
            story_id,
            db,
            router,
            web_poster=self._web_posters.get(story_id),
            config=self._config,
            resolved_llm_config=self._resolved_llm_configs.get(story_id),
        )

    async def _ensure_web_poster(self, story_id: str) -> None:
        if story_id in self._web_posters:
            return

        from engine.web_post_targets import resolve_story_web_poster_config_with_db
        poster_config = await resolve_story_web_poster_config_with_db(
            self._config, story_id, self._db
        )
        if poster_config is None:
            logger.info("Hosted runtime web posting disabled for story=%s", story_id)
            return

        poster = WebPoster(story_id, poster_config, self._db)
        await poster.start()
        self._web_posters[story_id] = poster
        self._web_poster_tasks[story_id] = asyncio.create_task(
            poster.run(),
            name=f"poster-{story_id}",
        )
