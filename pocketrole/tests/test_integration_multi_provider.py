"""tests/test_integration_multi_provider.py — マルチプロバイダー統合テスト。

- 直列制御（Ollama 想定）/ 並行制御（クラウド API 想定）
- 混在稼働・エラー分離・ProcessManager 統合テスト
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from db.db_manager import DatabaseManager
from engine.config import LLMConfig
from engine.llm.base import BaseLLMClient, LLMResponse
from engine.llm.exceptions import LLMError
from engine.llm.router import LLMRouter
from engine.process_manager import ProcessManager
from engine.story_engine import StoryEngine
from tests._async_harness import async_to_sync

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

_SERIAL_STORY_ID = "serial_story"
_CLOUD_STORY_ID = "cloud_story"
_CHAR_ID = "char_a"


# ============================================================
# Mock LLM クライアント
# ============================================================


class _SerialMockClient(BaseLLMClient):
    """supports_concurrent=False — Queue(1) 直列制御パスを通る。"""

    def __init__(self) -> None:
        self.call_count: int = 0

    @property
    def provider_name(self) -> str:
        return "serial_mock"

    @property
    def supports_concurrent(self) -> bool:
        return False

    async def generate(
        self, system_prompt: str, user_prompt: str, model: str, **kwargs
    ) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(
            text="直列発言。",
            model="serial",
            provider="serial_mock",
            prompt_tokens=5,
            completion_tokens=5,
            latency_ms=1,
        )

    async def health_check(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["serial"]


class _ConcurrentMockClient(BaseLLMClient):
    """supports_concurrent=True — Semaphore 並行制御パスを通る。"""

    def __init__(self) -> None:
        self.call_count: int = 0

    @property
    def provider_name(self) -> str:
        return "cloud_mock"

    @property
    def supports_concurrent(self) -> bool:
        return True

    async def generate(
        self, system_prompt: str, user_prompt: str, model: str, **kwargs
    ) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(
            text="並行発言。",
            model="cloud",
            provider="cloud_mock",
            prompt_tokens=5,
            completion_tokens=5,
            latency_ms=1,
        )

    async def health_check(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["cloud"]


class _TrackingSerialClient(_SerialMockClient):
    """generate() の start/end を execution_log に記録して直列順序を検証する。"""

    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self._log = log

    async def generate(
        self, system_prompt: str, user_prompt: str, model: str, **kwargs
    ) -> LLMResponse:
        self._log.append("start")
        await asyncio.sleep(0)  # 他タスクに制御を譲る
        self._log.append("end")
        return await super().generate(system_prompt, user_prompt, model, **kwargs)


class _TrackingConcurrentClient(_ConcurrentMockClient):
    """同時実行数を max_concurrent に記録して Semaphore 効果を検証する。"""

    def __init__(self) -> None:
        super().__init__()
        self.active: int = 0
        self.max_concurrent: int = 0

    async def generate(
        self, system_prompt: str, user_prompt: str, model: str, **kwargs
    ) -> LLMResponse:
        self.active += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return await super().generate(system_prompt, user_prompt, model, **kwargs)


class _ErrorSerialClient(_SerialMockClient):
    """generate() が LLMError を送出するエラー版（直列）。"""

    async def generate(
        self, system_prompt: str, user_prompt: str, model: str, **kwargs
    ) -> LLMResponse:
        raise LLMError("serial error")


class _ErrorConcurrentClient(_ConcurrentMockClient):
    """generate() が LLMError を送出するエラー版（並行）。"""

    async def generate(
        self, system_prompt: str, user_prompt: str, model: str, **kwargs
    ) -> LLMResponse:
        raise LLMError("concurrent error")


# ============================================================
# ルーターファクトリー
# ============================================================


def _make_mixed_router(
    serial: _SerialMockClient | None = None,
    concurrent: _ConcurrentMockClient | None = None,
    cloud_concurrency: int = 3,
) -> LLMRouter:
    """serial_mock + cloud_mock 両方を持つ LLMRouter を生成。"""
    config = LLMConfig(
        default_provider="serial_mock",
        default_model="",
        cloud_concurrency=cloud_concurrency,
        providers={},
    )
    router = LLMRouter(config=config)
    router._clients["serial_mock"] = serial or _SerialMockClient()
    router._clients["cloud_mock"] = concurrent or _ConcurrentMockClient()
    return router


# ============================================================
# DB ヘルパー
# ============================================================

_SPEECH_JSON = json.dumps({
    "first_person": "私",
    "tone": "丁寧",
    "examples": ["「よろしくお願いします。」"],
    "never_say": [],
})
_EMOTION_JSON = json.dumps({
    "stress": 0.3,
    "motivation": 0.7,
    "loneliness": 0.2,
    "excitement": 0.5,
})
_PLACES_JSON = json.dumps(["classroom"])


async def _insert_story(db: DatabaseManager, story_id: str, provider: str) -> None:
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories
            (id, title, season_start, turn_minutes, turn_interval_sec, llm_provider, llm_model)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (story_id, f"テストストーリー_{story_id}", "2025-04-01", 30, 0, provider, provider),
    )
    await db._conn.commit()


async def _insert_place(db: DatabaseManager, story_id: str) -> None:
    assert db._conn is not None
    await db._conn.execute(
        "INSERT INTO places (id, story_id, label, zone) VALUES (?, ?, ?, ?);",
        ("classroom", story_id, "教室", "indoor"),
    )
    await db._conn.commit()


async def _insert_character(db: DatabaseManager, char_id: str, story_id: str) -> None:
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO characters
            (id, story_id, name_ja, personality_core,
             speech, emotion_default, favorite_places,
             current_goal, current_worry)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            char_id, story_id, f"キャラ_{char_id}", "テスト用性格",
            _SPEECH_JSON, _EMOTION_JSON, _PLACES_JSON,
            "テスト目標", "テスト悩み",
        ),
    )
    await db._conn.commit()


# ============================================================
# フィクスチャ
# ============================================================


@asynccontextmanager
async def _make_db_two_stories() -> AsyncIterator[DatabaseManager]:
    """2ストーリー（serial_story / cloud_story）各1キャラの in-memory DB。"""
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    for story_id, provider in [
        (_SERIAL_STORY_ID, "serial_mock"),
        (_CLOUD_STORY_ID, "cloud_mock"),
    ]:
        await _insert_story(manager, story_id, provider)
        await _insert_place(manager, story_id)
        await _insert_character(manager, _CHAR_ID, story_id)
    try:
        yield manager
    finally:
        await manager.close()


# ============================================================
# テスト 1: 直列順序検証
# ============================================================


@async_to_sync
async def test_serial_queue_enforces_ordering() -> None:
    """2並行 generate() がキューにより重複しない（start-end-start-end 順）。"""
    execution_log: list[str] = []
    serial = _TrackingSerialClient(execution_log)
    router = _make_mixed_router(serial=serial)

    t1 = asyncio.create_task(router.generate("serial_mock", "s", "u", "m"))
    await asyncio.sleep(0)  # t1 にキュートークンを取得させる
    t2 = asyncio.create_task(router.generate("serial_mock", "s", "u", "m"))
    await asyncio.gather(t1, t2)

    assert execution_log == ["start", "end", "start", "end"]


# ============================================================
# テスト 2: 並行度検証
# ============================================================


@async_to_sync
async def test_concurrent_semaphore_allows_parallel() -> None:
    """3並行 generate() がセマフォにより重複する（max_concurrent >= 2）。"""
    tracker = _TrackingConcurrentClient()
    router = _make_mixed_router(concurrent=tracker)

    tasks = [
        asyncio.create_task(router.generate("cloud_mock", "s", "u", "m"))
        for _ in range(3)
    ]
    await asyncio.gather(*tasks)

    assert tracker.max_concurrent >= 2


# ============================================================
# テスト 3: セマフォ上限=1 で直列化
# ============================================================


@async_to_sync
async def test_concurrent_semaphore_limits_to_one() -> None:
    """cloud_concurrency=1 のとき並行 generate() は直列化される。"""
    tracker = _TrackingConcurrentClient()
    router = _make_mixed_router(concurrent=tracker, cloud_concurrency=1)

    tasks = [
        asyncio.create_task(router.generate("cloud_mock", "s", "u", "m"))
        for _ in range(3)
    ]
    await asyncio.gather(*tasks)

    assert tracker.max_concurrent == 1


# ============================================================
# テスト 4: 混在稼働でデッドロックなし
# ============================================================


@async_to_sync
async def test_serial_and_concurrent_no_deadlock() -> None:
    """serial + concurrent を同時実行しても両方完了する。"""
    router = _make_mixed_router()

    tasks = [
        asyncio.create_task(router.generate("serial_mock", "s", "u", "m")),
        asyncio.create_task(router.generate("cloud_mock", "s", "u", "m")),
        asyncio.create_task(router.generate("serial_mock", "s", "u", "m")),
        asyncio.create_task(router.generate("cloud_mock", "s", "u", "m")),
    ]
    results = await asyncio.gather(*tasks)

    assert len(results) == 4
    assert all(r.text in ("直列発言。", "並行発言。") for r in results)


# ============================================================
# テスト 5: serial_mock ストーリーが serial パスを使う
# ============================================================


@async_to_sync
async def test_serial_story_engine_uses_serial_path() -> None:
    """serial_mock ストーリーの StoryEngine が serial_mock クライアントを呼ぶ。"""
    async with _make_db_two_stories() as db_two_stories:
        serial = _SerialMockClient()
        cloud = _ConcurrentMockClient()
        router = _make_mixed_router(serial=serial, concurrent=cloud)

        eng = StoryEngine(_SERIAL_STORY_ID, db_two_stories, router)
        await eng.initialize()
        await eng.run_one_turn()

        assert serial.call_count > 0
        assert cloud.call_count == 0


# ============================================================
# テスト 6: cloud_mock ストーリーが concurrent パスを使う
# ============================================================


@async_to_sync
async def test_concurrent_story_engine_uses_concurrent_path() -> None:
    """cloud_mock ストーリーの StoryEngine が cloud_mock クライアントを呼ぶ。"""
    async with _make_db_two_stories() as db_two_stories:
        serial = _SerialMockClient()
        cloud = _ConcurrentMockClient()
        router = _make_mixed_router(serial=serial, concurrent=cloud)

        eng = StoryEngine(_CLOUD_STORY_ID, db_two_stories, router)
        await eng.initialize()
        await eng.run_one_turn()

        assert cloud.call_count > 0
        assert serial.call_count == 0


# ============================================================
# テスト 7: chat_log.llm_provider が各ストーリーの llm_provider と一致
# ============================================================


@async_to_sync
async def test_each_story_records_own_provider_in_log() -> None:
    """chat_log.llm_provider が各ストーリーの llm_provider と一致する。"""
    async with _make_db_two_stories() as db_two_stories:
        router = _make_mixed_router()

        for story_id, expected_provider in [
            (_SERIAL_STORY_ID, "serial_mock"),
            (_CLOUD_STORY_ID, "cloud_mock"),
        ]:
            eng = StoryEngine(story_id, db_two_stories, router)
            await eng.initialize()
            await eng.run_one_turn()
            logs = await db_two_stories.get_unposted_logs(story_id)
            assert len(logs) == 1
            assert logs[0]["llm_provider"] == expected_provider


# ============================================================
# テスト 8: 2ストーリーを独立して1ターン実行
# ============================================================


@async_to_sync
async def test_two_stories_run_independently() -> None:
    """serial_story と cloud_story を独立して1ターン実行し、各1件のログが存在する。"""
    async with _make_db_two_stories() as db_two_stories:
        router = _make_mixed_router()

        serial_eng = StoryEngine(_SERIAL_STORY_ID, db_two_stories, router)
        cloud_eng = StoryEngine(_CLOUD_STORY_ID, db_two_stories, router)
        await serial_eng.initialize()
        await cloud_eng.initialize()
        await serial_eng.run_one_turn()
        await cloud_eng.run_one_turn()

        serial_logs = await db_two_stories.get_unposted_logs(_SERIAL_STORY_ID)
        cloud_logs = await db_two_stories.get_unposted_logs(_CLOUD_STORY_ID)
        assert len(serial_logs) == 1
        assert len(cloud_logs) == 1


# ============================================================
# テスト 9: serial エラーが concurrent に波及しない
# ============================================================


@async_to_sync
async def test_error_in_serial_does_not_affect_concurrent() -> None:
    """serial_mock でエラーが出ても cloud_mock が正常に generate() できる。"""
    error_serial = _ErrorSerialClient()
    cloud = _ConcurrentMockClient()
    router = _make_mixed_router(serial=error_serial, concurrent=cloud)

    with pytest.raises(LLMError):
        await router.generate("serial_mock", "s", "u", "m")

    result = await router.generate("cloud_mock", "s", "u", "m")
    assert result.text == "並行発言。"
    assert cloud.call_count == 1


# ============================================================
# テスト 10: concurrent エラーが serial に波及しない
# ============================================================


@async_to_sync
async def test_error_in_concurrent_does_not_affect_serial() -> None:
    """cloud_mock でエラーが出ても serial_mock が正常に generate() できる。"""
    serial = _SerialMockClient()
    error_cloud = _ErrorConcurrentClient()
    router = _make_mixed_router(serial=serial, concurrent=error_cloud)

    with pytest.raises(LLMError):
        await router.generate("cloud_mock", "s", "u", "m")

    result = await router.generate("serial_mock", "s", "u", "m")
    assert result.text == "直列発言。"
    assert serial.call_count == 1


# ============================================================
# テスト 11: ProcessManager が2ストーリーを起動する
# ============================================================


@async_to_sync
async def test_process_manager_starts_two_stories() -> None:
    """ProcessManager.start_all() 後に running_stories に2件存在する。"""
    async with _make_db_two_stories() as db_two_stories:
        router = _make_mixed_router()
        pm = ProcessManager([_SERIAL_STORY_ID, _CLOUD_STORY_ID], db_two_stories, router)

        await pm.start_all()
        await asyncio.sleep(0.05)

        assert _SERIAL_STORY_ID in pm.running_stories
        assert _CLOUD_STORY_ID in pm.running_stories

        await pm.stop_all()


# ============================================================
# テスト 12: ProcessManager が全タスクをグレースフルに停止する
# ============================================================


@async_to_sync
async def test_process_manager_stops_all_gracefully() -> None:
    """ProcessManager.stop_all() 後に all_tasks_done=True かつ例外なし。"""
    async with _make_db_two_stories() as db_two_stories:
        router = _make_mixed_router()
        pm = ProcessManager([_SERIAL_STORY_ID, _CLOUD_STORY_ID], db_two_stories, router)

        await pm.start_all()
        await asyncio.sleep(0.05)
        await pm.stop_all()

        assert pm.all_tasks_done


# ============================================================
# テスト 13: ProcessManager 稼働後、両ストーリーに chat_logs が存在する
# ============================================================


@async_to_sync
async def test_process_manager_both_stories_produce_logs() -> None:
    """ProcessManager 稼働後、両ストーリーに chat_logs が存在する。"""
    async with _make_db_two_stories() as db_two_stories:
        router = _make_mixed_router()
        pm = ProcessManager([_SERIAL_STORY_ID, _CLOUD_STORY_ID], db_two_stories, router)

        await pm.start_all()
        await asyncio.sleep(0.1)
        await pm.stop_all()

        serial_logs = await db_two_stories.get_unposted_logs(_SERIAL_STORY_ID, limit=100)
        cloud_logs = await db_two_stories.get_unposted_logs(_CLOUD_STORY_ID, limit=100)
        assert len(serial_logs) >= 1
        assert len(cloud_logs) >= 1


# ============================================================
# テスト 14: health_check_all() が全プロバイダーの結果を返す
# ============================================================


@async_to_sync
async def test_router_health_check_all_providers() -> None:
    """health_check_all() が {"serial_mock": True, "cloud_mock": True} を返す。"""
    router = _make_mixed_router()
    results = await router.health_check_all()
    assert results == {"serial_mock": True, "cloud_mock": True}
