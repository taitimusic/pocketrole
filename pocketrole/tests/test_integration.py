"""tests/test_integration.py — エンドツーエンド統合テスト。

in-memory DB + _MockLLMClient + 全モジュール統合で、
1ターンフローがエンドツーエンドで動作することを検証する。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from db.db_manager import DatabaseManager
from engine.config import LLMConfig
from engine.llm.base import BaseLLMClient, LLMResponse
from engine.llm.exceptions import LLMError
from engine.llm.router import LLMRouter
from engine.story_engine import StoryEngine
from tests._async_harness import async_to_sync

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

_STORY_ID = "integ_story"
_CHAR_ID_A = "char_a"
_CHAR_ID_B = "char_b"


# ============================================================
# Mock LLM クライアント
# ============================================================


class _MockLLMClient(BaseLLMClient):
    """テスト用モック LLMクライアント。固定テキストを返す。"""

    def __init__(self) -> None:
        self.call_args_list: list[tuple[str, str]] = []

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def supports_concurrent(self) -> bool:
        return True

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        **kwargs,
    ) -> LLMResponse:
        self.call_args_list.append((system_prompt, user_prompt))
        return LLMResponse(
            text="テスト発言。",
            model="mock",
            provider="mock",
            prompt_tokens=5,
            completion_tokens=5,
            latency_ms=1,
        )

    async def health_check(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["mock"]


class _ErrorLLMClient(_MockLLMClient):
    """generate() が LLMError を送出するクライアント。"""

    async def generate(  # type: ignore[override]
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        **kwargs,
    ) -> LLMResponse:
        raise LLMError("mock LLM error")


# ============================================================
# ヘルパー
# ============================================================


def _make_router(error: bool = False) -> tuple[LLMRouter, _MockLLMClient]:
    """モッククライアントを注入した LLMRouter と クライアントのタプルを返す。"""
    config = LLMConfig(
        default_provider="mock",
        default_model="mock",
        cloud_concurrency=3,
        providers={},
    )
    router = LLMRouter(config=config)
    client: _MockLLMClient = _ErrorLLMClient() if error else _MockLLMClient()
    router._clients["mock"] = client
    return router, client


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


async def _insert_story(db: DatabaseManager, story_id: str = _STORY_ID) -> None:
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories
            (id, title, season_start, turn_minutes, turn_interval_sec, llm_provider, llm_model)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (story_id, "統合テストストーリー", "2025-04-01", 30, 0, "mock", "mock"),
    )
    await db._conn.commit()


async def _insert_place(db: DatabaseManager, story_id: str = _STORY_ID) -> None:
    assert db._conn is not None
    await db._conn.execute(
        "INSERT INTO places (id, story_id, label, zone) VALUES (?, ?, ?, ?);",
        ("classroom", story_id, "教室", "indoor"),
    )
    await db._conn.commit()


async def _insert_character(
    db: DatabaseManager, char_id: str, story_id: str = _STORY_ID
) -> None:
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
async def _make_db() -> AsyncIterator[DatabaseManager]:
    """in-memory DB（1ストーリー + 1キャラ）。"""
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    await _insert_story(manager)
    await _insert_place(manager)
    await _insert_character(manager, _CHAR_ID_A)
    try:
        yield manager
    finally:
        await manager.close()


@asynccontextmanager
async def _make_db_two_chars() -> AsyncIterator[DatabaseManager]:
    """in-memory DB（1ストーリー + 2キャラ）。"""
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    await _insert_story(manager)
    await _insert_place(manager)
    await _insert_character(manager, _CHAR_ID_A)
    await _insert_character(manager, _CHAR_ID_B)
    try:
        yield manager
    finally:
        await manager.close()


async def _make_engine(db: DatabaseManager) -> StoryEngine:
    """初期化済み StoryEngine（1キャラ）。"""
    router, _ = _make_router()
    eng = StoryEngine(_STORY_ID, db, router)
    await eng.initialize()
    return eng


# ============================================================
# テスト 1: キャラロード確認
# ============================================================


@async_to_sync
async def test_engine_initializes_characters() -> None:
    """StoryEngine が DB からキャラを正常ロードする。"""
    async with _make_db() as db:
        router, _ = _make_router()
        eng = StoryEngine(_STORY_ID, db, router)
        await eng.initialize()
        assert len(eng._characters) == 1
        assert eng._characters[0]["id"] == _CHAR_ID_A


# ============================================================
# テスト 2: 1ターン後に chat_logs に1行挿入される
# ============================================================


@async_to_sync
async def test_run_one_turn_inserts_chat_log() -> None:
    """1ターン後に chat_logs に1行挿入される。"""
    async with _make_db() as db:
        engine = await _make_engine(db)
        await engine.run_one_turn()
        logs = await db.get_unposted_logs(_STORY_ID)
        assert len(logs) == 1


# ============================================================
# テスト 3: 1ターン後に character_states に1行挿入される
# ============================================================


@async_to_sync
async def test_run_one_turn_inserts_character_state() -> None:
    """1ターン後に character_states に1行挿入される。"""
    async with _make_db() as db:
        engine = await _make_engine(db)
        await engine.run_one_turn()
        state = await db.get_latest_character_state(_STORY_ID, _CHAR_ID_A)
        assert state is not None
        assert state["char_id"] == _CHAR_ID_A


# ============================================================
# テスト 4: LLMRouter.generate() が呼ばれ system/user prompt を受け取る
# ============================================================


@async_to_sync
async def test_run_one_turn_llm_called_with_prompts() -> None:
    """LLMRouter.generate() が呼ばれ system/user prompt を受け取る。"""
    async with _make_db() as db:
        router, client = _make_router()
        eng = StoryEngine(_STORY_ID, db, router)
        await eng.initialize()
        await eng.run_one_turn()
        assert len(client.call_args_list) == 1
        system_prompt, user_prompt = client.call_args_list[0]
        assert len(system_prompt) > 0
        assert len(user_prompt) > 0


# ============================================================
# テスト 5: chat_log の各フィールドが正しく設定される
# ============================================================


@async_to_sync
async def test_chat_log_has_correct_fields() -> None:
    """chat_log に char_id/msg_type/message/expression が正しく設定される。"""
    async with _make_db() as db:
        engine = await _make_engine(db)
        await engine.run_one_turn()
        logs = await db.get_unposted_logs(_STORY_ID)
        log = logs[0]
        assert log["char_id"] == _CHAR_ID_A
        assert log["msg_type"] in ("monologue", "reply", "broadcast", "whisper")
        assert log["message"] == "テスト発言。"
        assert log["expression"] in (
            "happy", "angry", "sad", "surprised", "worried",
            "content", "lonely", "tired", "neutral",
        )


# ============================================================
# テスト 6: emotion_snapshot が JSON 文字列として保存される
# ============================================================


@async_to_sync
async def test_chat_log_emotion_snapshot_is_json() -> None:
    """emotion_snapshot カラムが JSON 文字列として保存される。"""
    async with _make_db() as db:
        engine = await _make_engine(db)
        await engine.run_one_turn()
        logs = await db.get_unposted_logs(_STORY_ID)
        snapshot = json.loads(logs[0]["emotion_snapshot"])
        assert "stress" in snapshot
        assert "motivation" in snapshot
        assert 0.0 <= snapshot["stress"] <= 1.0


# ============================================================
# テスト 7: 3ターン実行後、chat_logs が3件になる
# ============================================================


@async_to_sync
async def test_run_multiple_turns_increments_logs() -> None:
    """3ターン実行後、chat_logs が3件になる。"""
    async with _make_db() as db:
        engine = await _make_engine(db)
        for _ in range(3):
            await engine.run_one_turn()
        logs = await db.get_unposted_logs(_STORY_ID, limit=10)
        assert len(logs) == 3


# ============================================================
# テスト 8: 全キャラ1周後に WorldClock が進む
# ============================================================


@async_to_sync
async def test_clock_advances_after_all_chars() -> None:
    """全キャラ（2キャラ）1周後に WorldClock の時刻が進む。"""
    async with _make_db_two_chars() as db_two_chars:
        router, _ = _make_router()
        eng = StoryEngine(_STORY_ID, db_two_chars, router)
        await eng.initialize()
        initial_time = eng.world_clock.sim_datetime

        # 1ターン目（char_a）: まだ1周未完了
        await eng.run_one_turn()
        assert eng.world_clock.sim_datetime == initial_time

        # 2ターン目（char_b）: 1周完了 → 時計が進む
        await eng.run_one_turn()
        assert eng.world_clock.sim_datetime != initial_time


# ============================================================
# テスト 9: web_poster=None でも例外なく動作する
# ============================================================


@async_to_sync
async def test_no_web_poster_is_ok() -> None:
    """web_poster=None でも run_one_turn() が例外なく完了する。"""
    async with _make_db() as db:
        router, _ = _make_router()
        eng = StoryEngine(_STORY_ID, db, router, web_poster=None)
        await eng.initialize()
        await eng.run_one_turn()
        logs = await db.get_unposted_logs(_STORY_ID)
        assert len(logs) == 1


# ============================================================
# テスト 10: web_poster が mock でも例外なく動作する
# ============================================================


@async_to_sync
async def test_web_poster_enqueued_after_turn() -> None:
    """web_poster を渡しても run_one_turn() が例外なく完了し、ログが DB に残る。"""
    async with _make_db() as db:
        router, _ = _make_router()
        mock_poster = AsyncMock()
        eng = StoryEngine(_STORY_ID, db, router, web_poster=mock_poster)
        await eng.initialize()
        await eng.run_one_turn()
        logs = await db.get_unposted_logs(_STORY_ID)
        assert len(logs) == 1


# ============================================================
# テスト 11: LLMError が発生しても run_one_turn() が例外を伝播させる
# ============================================================


@async_to_sync
async def test_llm_error_does_not_crash_engine() -> None:
    """LLMRouter.generate() が LLMError を送出した場合、run_one_turn() から伝播する。
    DB には何も挿入されない（LLM呼び出し後の DB 保存ステップに到達しないため）。
    """
    async with _make_db() as db:
        router, _ = _make_router(error=True)
        eng = StoryEngine(_STORY_ID, db, router)
        await eng.initialize()
        with pytest.raises(LLMError):
            await eng.run_one_turn()
        logs = await db.get_unposted_logs(_STORY_ID)
        assert len(logs) == 0


# ============================================================
# テスト 12: 2キャラが全員発言機会を得る
# ============================================================


@async_to_sync
async def test_multiple_characters_each_get_turn() -> None:
    """2キャラのストーリーで全キャラが発言機会を得る（1周2ターン）。"""
    async with _make_db_two_chars() as db_two_chars:
        router, _ = _make_router()
        eng = StoryEngine(_STORY_ID, db_two_chars, router)
        await eng.initialize()
        await eng.run_one_turn()
        await eng.run_one_turn()
        logs = await db_two_chars.get_unposted_logs(_STORY_ID, limit=10)
        char_ids = {log["char_id"] for log in logs}
        assert _CHAR_ID_A in char_ids
        assert _CHAR_ID_B in char_ids


# ============================================================
# テスト 13: insert_chat_log → get_unposted_logs の連携
# ============================================================


@async_to_sync
async def test_get_unposted_logs_returns_inserted() -> None:
    """insert_chat_log → get_unposted_logs の連携が正しく動作する。"""
    async with _make_db() as db:
        router, _ = _make_router()
        eng = StoryEngine(_STORY_ID, db, router)
        await eng.initialize()
        await eng.run_one_turn()
        logs = await db.get_unposted_logs(_STORY_ID)
        assert len(logs) == 1
        assert logs[0]["posted_to_web"] == 0
        assert logs[0]["story_id"] == _STORY_ID


# ============================================================
# テスト 14: stop() 後に run() が安全に終了する
# ============================================================


@async_to_sync
async def test_stop_signal_exits_loop() -> None:
    """StoryEngine.stop() 後に run() が安全に終了する。"""
    async with _make_db() as db:
        router, _ = _make_router()
        eng = StoryEngine(_STORY_ID, db, router)

        async def _stopper() -> None:
            await asyncio.sleep(0.05)
            await eng.stop()

        task = asyncio.create_task(eng.run())
        stopper = asyncio.create_task(_stopper())
        done, _pending = await asyncio.wait(
            {task, stopper}, timeout=5.0, return_when=asyncio.ALL_COMPLETED
        )
        assert task in done, "run() が timeout 以内に終了しなかった"
        assert task.exception() is None
