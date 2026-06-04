"""engine/process_manager.py — 複数ストーリーの asyncio.Task 管理クラス。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from db.db_manager import DatabaseManager
from engine.llm.router import LLMRouter
from engine.story_engine import StoryEngine

logger = logging.getLogger(__name__)


class ProcessManager:
    """複数ストーリーの asyncio.Task 管理クラス。

    - 各ストーリーに StoryEngine を 1 インスタンス生成し asyncio.Task で起動
    - 全ストーリーは LLMRouter / DatabaseManager を共有
    - stop_all() でグレースフルに全タスクを停止
    """

    def __init__(
        self,
        story_ids: list[str],
        db: DatabaseManager,
        router: LLMRouter,
        engine_factory: Callable[[str, DatabaseManager, LLMRouter], StoryEngine] | None = None,
    ) -> None:
        self._story_ids = story_ids
        self._db = db
        self._router = router
        self._engine_factory: Callable[..., StoryEngine] = engine_factory or StoryEngine
        self._engines: dict[str, StoryEngine] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start_all(self) -> None:
        """全ストーリーエンジンを asyncio.Task として起動する。
        すでに起動済みの story_id はスキップする。
        """
        for story_id in self._story_ids:
            if story_id in self._tasks:
                continue
            engine = self._engine_factory(story_id, self._db, self._router)
            self._engines[story_id] = engine
            task = asyncio.create_task(engine.run(), name=f"story-{story_id}")
            task.add_done_callback(self._on_task_done)
            self._tasks[story_id] = task
            logger.info("Started story engine: %s", story_id)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """タスク完了コールバック。クラッシュを error ログに記録する。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Story task crashed: %s", exc, exc_info=exc)

    async def stop_all(self) -> None:
        """全エンジンに stop() を要求し、全タスクの完了を待つ。"""
        for engine in self._engines.values():
            await engine.stop()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        logger.info("All story engines stopped.")

    async def wait_all(self) -> None:
        """全 story task の通常終了を待つ。

        いずれかの story task が例外終了した場合は、残りの task を停止してから
        元の例外を呼び出し元へ伝播する。CLI entrypoint はこれを exit 1 に変換する。
        """
        if not self._tasks:
            return
        try:
            await asyncio.gather(*self._tasks.values())
        except Exception:
            await self.stop_all()
            raise

    @property
    def running_stories(self) -> list[str]:
        """実行中（未完了）のストーリー ID リストを返す。"""
        return [sid for sid, task in self._tasks.items() if not task.done()]

    @property
    def all_tasks_done(self) -> bool:
        """登録済み全タスクが完了しているかを返す。"""
        return all(task.done() for task in self._tasks.values())
