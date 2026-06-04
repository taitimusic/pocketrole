"""tests/test_process_manager.py — ProcessManager のユニットテスト（14 件）。"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from engine.process_manager import ProcessManager


# ---------------------------------------------------------------------------
# MockStoryEngine ヘルパー
# ---------------------------------------------------------------------------

class MockStoryEngine:
    """テスト用 StoryEngine モック。"""

    def __init__(
        self,
        story_id: str,
        db: object,
        router: object,
        *,
        fail: bool = False,
    ) -> None:
        self.story_id = story_id
        self.stop_called = False
        self._stop_event = asyncio.Event()
        self._fail = fail

    async def run(self) -> None:
        if self._fail:
            raise RuntimeError(f"engine {self.story_id} crashed")
        await self._stop_event.wait()

    async def stop(self) -> None:
        self.stop_called = True
        self._stop_event.set()


def make_process_manager(
    story_ids: list[str],
    fail_ids: set[str] | None = None,
) -> ProcessManager:
    """ProcessManager を MockStoryEngine で DI して生成するヘルパー。"""
    if fail_ids is None:
        fail_ids = set()

    db = MagicMock()
    router = MagicMock()

    def factory(story_id: str, db_: object, router_: object) -> MockStoryEngine:
        return MockStoryEngine(story_id, db_, router_, fail=story_id in fail_ids)

    return ProcessManager(story_ids=story_ids, db=db, router=router, engine_factory=factory)


# ---------------------------------------------------------------------------
# テスト
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_all_creates_engines() -> None:
    """start_all 後、全 story_id に engine が登録される。"""
    pm = make_process_manager(["s1", "s2", "s3"])
    await pm.start_all()
    assert set(pm._engines.keys()) == {"s1", "s2", "s3"}
    await pm.stop_all()


@pytest.mark.asyncio
async def test_start_all_creates_tasks() -> None:
    """start_all 後、全 story_id の task が _tasks に登録される。"""
    pm = make_process_manager(["s1", "s2"])
    await pm.start_all()
    assert set(pm._tasks.keys()) == {"s1", "s2"}
    await pm.stop_all()


@pytest.mark.asyncio
async def test_running_stories_after_start() -> None:
    """`running_stories` が起動中の story_id 全てを返す。"""
    pm = make_process_manager(["s1", "s2", "s3"])
    await pm.start_all()
    # タスクはまだ実行中のはず
    assert set(pm.running_stories) == {"s1", "s2", "s3"}
    await pm.stop_all()


@pytest.mark.asyncio
async def test_stop_all_calls_engine_stop() -> None:
    """stop_all 後、各エンジンの stop() が呼ばれる。"""
    pm = make_process_manager(["s1", "s2"])
    await pm.start_all()
    await pm.stop_all()
    for engine in pm._engines.values():
        assert engine.stop_called  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stop_all_waits_for_tasks() -> None:
    """stop_all 後、全タスクが完了状態になる。"""
    pm = make_process_manager(["s1", "s2"])
    await pm.start_all()
    await pm.stop_all()
    for task in pm._tasks.values():
        assert task.done()


@pytest.mark.asyncio
async def test_all_tasks_done_after_stop() -> None:
    """stop_all 後、`all_tasks_done == True`。"""
    pm = make_process_manager(["s1", "s2"])
    await pm.start_all()
    await pm.stop_all()
    assert pm.all_tasks_done is True


@pytest.mark.asyncio
async def test_multiple_stories_run_concurrently() -> None:
    """複数エンジンが同時実行される（カウンタで確認）。"""
    started: list[str] = []
    stop_events: dict[str, asyncio.Event] = {}

    class CountingEngine:
        def __init__(self, story_id: str, db: object, router: object) -> None:
            self.story_id = story_id
            self._event = asyncio.Event()
            stop_events[story_id] = self._event

        async def run(self) -> None:
            started.append(self.story_id)
            await self._event.wait()

        async def stop(self) -> None:
            self._event.set()

    db = MagicMock()
    router = MagicMock()
    pm = ProcessManager(
        story_ids=["a", "b", "c"],
        db=db,
        router=router,
        engine_factory=CountingEngine,  # type: ignore[arg-type]
    )
    await pm.start_all()
    # yield control so tasks can start
    await asyncio.sleep(0)
    assert set(started) == {"a", "b", "c"}
    await pm.stop_all()


@pytest.mark.asyncio
async def test_empty_story_ids() -> None:
    """story_ids=[] でも start_all/stop_all がエラーなし。"""
    pm = make_process_manager([])
    await pm.start_all()
    await pm.stop_all()
    assert pm._tasks == {}
    assert pm._engines == {}


@pytest.mark.asyncio
async def test_start_all_idempotent() -> None:
    """2 回 start_all を呼んでも task が重複しない。"""
    pm = make_process_manager(["s1", "s2"])
    await pm.start_all()
    first_tasks = dict(pm._tasks)
    await pm.start_all()  # 2 回目
    assert pm._tasks == first_tasks
    await pm.stop_all()


@pytest.mark.asyncio
async def test_stop_all_before_start() -> None:
    """start_all 前に stop_all を呼んでもエラーなし。"""
    pm = make_process_manager(["s1"])
    await pm.stop_all()  # start_all なしで呼ぶ
    assert pm.all_tasks_done is True


@pytest.mark.asyncio
async def test_running_stories_empty_after_stop() -> None:
    """stop_all 後、`running_stories == []`。"""
    pm = make_process_manager(["s1", "s2"])
    await pm.start_all()
    await pm.stop_all()
    assert pm.running_stories == []


@pytest.mark.asyncio
async def test_crashed_engine_logged_as_error(caplog: pytest.LogCaptureFixture) -> None:
    """`fail=True` エンジンのクラッシュが error ログに記録される。"""
    pm = make_process_manager(["crash"], fail_ids={"crash"})
    with caplog.at_level(logging.ERROR, logger="engine.process_manager"):
        await pm.start_all()
        # クラッシュするエンジンには stop() が届かなくてもよい
        await asyncio.gather(*pm._tasks.values(), return_exceptions=True)
    assert any("crashed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_crash_does_not_stop_other_stories() -> None:
    """1 エンジンがクラッシュしても他エンジンは継続実行される。"""
    pm = make_process_manager(["ok1", "ok2", "crash"], fail_ids={"crash"})
    await pm.start_all()
    # crash タスクが完了するまで待つ
    await asyncio.gather(pm._tasks["crash"], return_exceptions=True)
    # ok エンジンはまだ実行中のはず
    assert "ok1" in pm.running_stories
    assert "ok2" in pm.running_stories
    await pm.stop_all()


@pytest.mark.asyncio
async def test_wait_all_propagates_crash_and_stops_remaining_stories() -> None:
    """通常待機中の story task クラッシュは呼び出し元へ伝播し、残りを停止する。"""
    pm = make_process_manager(["ok", "crash"], fail_ids={"crash"})
    await pm.start_all()

    with pytest.raises(RuntimeError, match="engine crash crashed"):
        await pm.wait_all()

    assert pm.all_tasks_done is True
    assert pm._engines["ok"].stop_called is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_engine_count_matches_story_ids() -> None:
    """3 story_ids → 3 エンジン / 3 タスク。"""
    pm = make_process_manager(["x", "y", "z"])
    await pm.start_all()
    assert len(pm._engines) == 3
    assert len(pm._tasks) == 3
    await pm.stop_all()
