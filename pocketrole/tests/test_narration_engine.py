"""
tests/test_narration_engine.py — NarrationEngine のテスト

in-memory SQLite を使用。LLMRouter はモック化する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.db_manager import DatabaseManager
from engine.config import NarrationConfig
from engine.llm.base import LLMResponse
from engine.narration_engine import NarrationContext, NarrationEngine
from tests._async_harness import async_to_sync

# ------------------------------------------------------------------
# 定数・フィクスチャ
# ------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

_STORY_ID = "test_story"


def _make_llm_router(response_text: str = "静かな夜が更けていった。") -> MagicMock:
    """LLMRouter のモックを作る。generate は AsyncMock で指定テキストを返す。"""
    router = MagicMock()
    router.generate = AsyncMock(
        return_value=_make_llm_response(response_text)
    )
    return router


def _make_llm_response(
    text: str,
    *,
    done_reason: str | None = None,
) -> LLMResponse:
    """NarrationEngine テスト用の LLMResponse を作る。"""
    return LLMResponse(
        text=text,
        model="test_model",
        provider="ollama",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=100,
        done_reason=done_reason,
    )


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    """in-memory DB を初期化し、テスト用ストーリーを挿入する。"""
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()

    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title, world_rules) VALUES (?, ?, ?);",
        (_STORY_ID, "テストストーリー", "魔法が使える世界"),
    )
    await manager._conn.commit()

    try:
        yield manager
    finally:
        await manager.close()


def _make_engine(
    db: DatabaseManager,
    llm_router: MagicMock,
    *,
    enabled: bool = True,
    min_interval_turns: int = 3,
    emotion_change_threshold: float = 0.3,
    include_chapter_breaks: bool = False,
    response_max_tokens: int = 480,
    retry_max_tokens: int = 360,
    chapter_max_tokens: int = 120,
) -> NarrationEngine:
    config = NarrationConfig(
        enabled=enabled,
        min_interval_turns=min_interval_turns,
        emotion_change_threshold=emotion_change_threshold,
        include_chapter_breaks=include_chapter_breaks,
        response_max_tokens=response_max_tokens,
        retry_max_tokens=retry_max_tokens,
        chapter_max_tokens=chapter_max_tokens,
    )
    return NarrationEngine(
        story_id=_STORY_ID,
        db=db,
        llm_router=llm_router,
        config=config,
        llm_provider="ollama",
        llm_model="test_model",
    )


def _make_ctx(**kwargs) -> NarrationContext:  # type: ignore[type-arg]
    """デフォルト値付き NarrationContext を作る。"""
    defaults = dict(
        sim_datetime="2024-01-01T12:00",
        place_id="plaza",
        place_name="広場",
        world_rules="魔法が使える世界",
        recent_log_lines=["アリス: こんにちは"],
        emotion_delta=0.0,
        character_moved=False,
        story_memory_texts=[],
    )
    defaults.update(kwargs)
    return NarrationContext(**defaults)


def test_narration_prompt_compacts_repeated_reply_tail_before_llm_generation() -> None:
    router = _make_llm_router()
    engine = _make_engine(MagicMock(), router)
    prompt = engine._build_user_prompt(
        "narration_scene",
        _make_ctx(
            recent_log_lines=[
                "ちゅるるん: 星風ルナ、その話は聞いた。話を少し戻したい",
                "夜風ユウマ: ちゅるるん、その話は聞いた。話を少し戻したい",
                "ミリティア: 夜風ユウマ、その話は聞いた。話を少し戻したい",
            ]
        ),
    )

    assert prompt.count("話を少し戻したい") == 1


# ------------------------------------------------------------------
# テスト 1: enabled=False → generate 非呼び出し
# ------------------------------------------------------------------

@async_to_sync
async def test_disabled_skips_generation() -> None:
    """enabled=False のとき、process_round は generate を呼ばない。"""
    router = _make_llm_router()
    async with _make_db() as db:
        engine = _make_engine(db, router, enabled=False)
        await engine.process_round(10, _make_ctx())

    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 2: インターバル未達 → スキップ
# ------------------------------------------------------------------

@async_to_sync
async def test_interval_not_reached_skips() -> None:
    """last=5, current=7, interval=3 → 7-5=2 < 3 なのでスキップ。"""
    router = _make_llm_router()
    async with _make_db() as db:
        engine = _make_engine(db, router, min_interval_turns=3)
        # 最初に turn 5 で生成させて _last_narration_turn を 5 に設定する
        engine._last_narration_turn = 5
        # turn 7: 7-5=2 < 3 → スキップ
        await engine.process_round(7, _make_ctx())

    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 3: インターバル到達 → 生成
# ------------------------------------------------------------------

@async_to_sync
async def test_interval_reached_generates() -> None:
    """last=5, current=8, interval=3 → 8-5=3 >= 3 なので生成。"""
    router = _make_llm_router()
    async with _make_db() as db:
        engine = _make_engine(db, router, min_interval_turns=3)
        engine._last_narration_turn = 5
        # turn 8: 8-5=3 >= 3 → 生成
        await engine.process_round(8, _make_ctx())

    router.generate.assert_called_once()


# ------------------------------------------------------------------
# テスト 4: 生成後 DB に char_id='_narrator' で保存される
# ------------------------------------------------------------------

@async_to_sync
async def test_saves_as_narrator_in_chat_logs() -> None:
    """生成後、chat_logs に char_id='_narrator' で行が保存されること。"""
    router = _make_llm_router("夕暮れが広場を染めた。")
    async with _make_db() as db:
        engine = _make_engine(db, router, min_interval_turns=1)
        await engine.process_round(1, _make_ctx())

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT char_id, message, msg_type FROM chat_logs WHERE story_id = ?;",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()
        assert row is not None
        data = dict(row)

    assert data["char_id"] == "_narrator"
    assert data["message"] == "夕暮れが広場を染めた。"
    assert data["msg_type"] == "narration_scene"


# ------------------------------------------------------------------
# テスト 5: emotion_delta >= threshold → インターバル未満でも生成
# ------------------------------------------------------------------

@async_to_sync
async def test_emotion_trigger_before_interval() -> None:
    """emotion_delta >= emotion_change_threshold のとき、インターバル未満でも生成する。"""
    router = _make_llm_router()
    async with _make_db() as db:
        engine = _make_engine(
            db, router,
            min_interval_turns=10,
            emotion_change_threshold=0.3,
        )
        engine._last_narration_turn = 0
        # turn 2: 2-0=2 < 10 だが emotion_delta=0.5 >= 0.3 → 生成
        ctx = _make_ctx(emotion_delta=0.5)
        await engine.process_round(2, ctx)

    router.generate.assert_called_once()


# ------------------------------------------------------------------
# テスト 6: character_moved=True → msg_type='narration_scene' で保存
# ------------------------------------------------------------------

@async_to_sync
async def test_move_trigger_generates_scene() -> None:
    """character_moved=True のとき、msg_type='narration_scene' で保存される。"""
    router = _make_llm_router("彼女は森の奥へと消えていった。")
    async with _make_db() as db:
        engine = _make_engine(
            db, router,
            min_interval_turns=100,
            emotion_change_threshold=1.0,  # 感情トリガーは発火させない
        )
        ctx = _make_ctx(character_moved=True, emotion_delta=0.0)
        await engine.process_round(1, ctx)

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT msg_type FROM chat_logs WHERE story_id = ? AND char_id = '_narrator';",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()
        assert row is not None

    assert dict(row)["msg_type"] == "narration_scene"


@async_to_sync
async def test_ankoku_profile_uses_literary_narration_prompt() -> None:
    """ankoku_gakuen (drama モード) では文学的ナレーションが有効になる。"""
    router = _make_llm_router("誰かが短く息をのんだ。")
    async with _make_db() as db:
        await db._conn.execute(
            "UPDATE stories SET id = ?, title = ? WHERE id = ?;",
            ("ankoku_gakuen", "暗黒学園", _STORY_ID),
        )
        await db._conn.commit()
        engine = NarrationEngine(
            story_id="ankoku_gakuen",
            db=db,
            llm_router=router,
            config=NarrationConfig(enabled=True, min_interval_turns=3, emotion_change_threshold=0.3),
            llm_provider="ollama",
            llm_model="test_model",
        )
        await engine.process_round(4, _make_ctx(world_rules="部室コメディ"))

    system_prompt = router.generate.await_args.kwargs["system_prompt"]
    assert "文学的な日本語" in system_prompt
    assert system_prompt  # non-empty


@async_to_sync
async def test_gemma_narration_scene_compacts_prompt_sections() -> None:
    """Gemma4 では narration_scene 向け prompt を短く圧縮する。"""
    router = _make_llm_router("廊下の空気がぴたりと止まった。")
    async with _make_db() as db:
        engine = NarrationEngine(
            story_id=_STORY_ID,
            db=db,
            llm_router=router,
            config=NarrationConfig(enabled=True, min_interval_turns=1, emotion_change_threshold=0.3),
            llm_provider="ollama",
            llm_model="gemma4:e4b",
        )
        ctx = _make_ctx(
            world_rules=" ".join(["魔法と音楽と秘密結社が同居する学園世界"] * 20),
            recent_log_lines=[
                f"キャラ{i}: " + ("かなり長い会話の断片です。 " * 10)
                for i in range(6)
            ],
            story_memory_texts=[
                "過去の重要イベントが長く続く説明です。 " * 8,
                "二つ目の重要イベントがさらに長く続く説明です。 " * 8,
            ],
            character_moved=True,
        )
        await engine.process_round(1, ctx)

    kwargs = router.generate.await_args.kwargs
    system_prompt = kwargs["system_prompt"]
    user_prompt = kwargs["user_prompt"]
    assert "比喩・抽象論・長い前置きは禁止です。" in system_prompt
    assert len(system_prompt) < 320
    assert len(user_prompt) < 420
    assert "キャラ0:" not in user_prompt
    assert "キャラ5:" in user_prompt
    assert user_prompt.count("キャラ") == 3
    assert user_prompt.count("最近の重要な出来事") == 1


@async_to_sync
async def test_gemma_narration_scene_sets_compact_generation_kwargs() -> None:
    """Gemma4 の narration_scene は request tag と低い token budget を使う。"""
    router = _make_llm_router("廊下に妙な静けさが落ちた。")
    async with _make_db() as db:
        engine = NarrationEngine(
            story_id=_STORY_ID,
            db=db,
            llm_router=router,
            config=NarrationConfig(enabled=True, min_interval_turns=1, emotion_change_threshold=0.3),
            llm_provider="ollama",
            llm_model="gemma4:e4b",
        )
        await engine.process_round(1, _make_ctx(place_id="corridor", place_name="廊下", character_moved=True))

    kwargs = router.generate.await_args.kwargs
    assert kwargs["request_tag"] == "narration:narration_scene:corridor"
    assert kwargs["max_tokens"] == 180
    assert kwargs["temperature"] == pytest.approx(0.1)
    assert kwargs["reasoning_mode"] == "off"


@async_to_sync
async def test_standard_narration_uses_configured_generation_budget_and_completion_prompt() -> None:
    """通常ナレーションは明示 budget と完結指示を使う。"""
    router = _make_llm_router("廊下の冷気が一同の足を遅らせた。")
    async with _make_db() as db:
        engine = _make_engine(
            db,
            router,
            min_interval_turns=1,
            response_max_tokens=512,
        )
        await engine.process_round(1, _make_ctx(character_moved=True))

    kwargs = router.generate.await_args.kwargs
    assert kwargs["max_tokens"] == 512
    assert "最後の文まで完結" in kwargs["system_prompt"]
    assert "400〜700字" in kwargs["system_prompt"]


@async_to_sync
async def test_length_truncated_narration_retries_and_saves_recovered_text() -> None:
    """length 終了時は短縮再生成を 1 回行い、回復した本文を保存する。"""
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_llm_response("廊下は静まり返っていたが、誰かの気配が", done_reason="length"),
            _make_llm_response("廊下は静まり返り、誰かの気配だけが残っていた。"),
        ]
    )
    async with _make_db() as db:
        engine = _make_engine(
            db,
            router,
            min_interval_turns=1,
            response_max_tokens=480,
            retry_max_tokens=360,
        )
        await engine.process_round(1, _make_ctx(character_moved=True))

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT message FROM chat_logs WHERE story_id = ? AND char_id = '_narrator';",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()
        assert row is not None

    assert router.generate.await_count == 2
    assert router.generate.await_args_list[0].kwargs["max_tokens"] == 480
    assert router.generate.await_args_list[1].kwargs["max_tokens"] == 360
    assert "前回のナレーションは文の途中で終わりました" in router.generate.await_args_list[1].kwargs[
        "user_prompt"
    ]
    assert dict(row)["message"] == "廊下は静まり返り、誰かの気配だけが残っていた。"


@async_to_sync
async def test_length_truncated_retry_falls_back_to_last_complete_sentence() -> None:
    """再生成も length なら、最後の完全文だけを保存する。"""
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_llm_response("廊下は静かだった。冷たい風が窓から吹き込み、", done_reason="length"),
            _make_llm_response("廊下は静かだった。冷たい風が窓から吹き込み、まだ", done_reason="length"),
        ]
    )
    async with _make_db() as db:
        engine = _make_engine(db, router, min_interval_turns=1)
        await engine.process_round(1, _make_ctx(character_moved=True))

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT message FROM chat_logs WHERE story_id = ? AND char_id = '_narrator';",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()
        assert row is not None

    assert dict(row)["message"] == "廊下は静かだった。"


@async_to_sync
async def test_length_truncated_retry_without_complete_sentence_is_not_saved() -> None:
    """再生成後も完全文がなければ、不完全文を保存しない。"""
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_llm_response("廊下は静かで", done_reason="length"),
            _make_llm_response("窓から冷気が入り", done_reason="length"),
        ]
    )
    async with _make_db() as db:
        engine = _make_engine(db, router, min_interval_turns=1)
        await engine.process_round(1, _make_ctx(character_moved=True))

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT COUNT(*) AS count FROM chat_logs WHERE story_id = ? AND char_id = '_narrator';",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()
        assert row is not None

    assert dict(row)["count"] == 0


# ------------------------------------------------------------------
# テスト 7: chapter break — 閉じたアークなし → False
# ------------------------------------------------------------------

@async_to_sync
async def test_chapter_break_not_triggered_when_no_arcs() -> None:
    """閉じたアークがない場合、process_round は False を返す。"""
    router = _make_llm_router()
    async with _make_db() as db:
        engine = _make_engine(db, router, include_chapter_breaks=True, min_interval_turns=100)
        result = await engine.process_round(1, _make_ctx())

    assert result is False
    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 8: chapter break — initialize() 後は既存 closed arc では発火しない
# ------------------------------------------------------------------

@async_to_sync
async def test_chapter_break_not_triggered_for_existing_closed_arcs_after_init() -> None:
    """initialize() で既存 closed arc を同期済みなら、それだけでは chapter_break にならない。"""
    router = _make_llm_router("第一章 — 夜明けの予兆")
    async with _make_db() as db:
        arc_id = await db.insert_arc(_STORY_ID, {
            "arc_type": "scene", "title": "夜明け", "summary": "夜明けの場面。", "turn_from": 1,
        })
        await db.close_arc(arc_id, turn_to=1)

        engine = _make_engine(db, router, include_chapter_breaks=True, min_interval_turns=100)
        await engine.initialize()
        result = await engine.process_round(2, _make_ctx())

    assert result is False
    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 9: chapter break — include_chapter_breaks=False → スキップ
# ------------------------------------------------------------------

@async_to_sync
async def test_chapter_break_disabled_skips() -> None:
    """include_chapter_breaks=False のとき、閉じたアークがあっても False を返す。"""
    router = _make_llm_router()
    async with _make_db() as db:
        arc_id = await db.insert_arc(_STORY_ID, {
            "arc_type": "scene", "title": "夜明け", "summary": "夜明けの場面。", "turn_from": 1,
        })
        await db.close_arc(arc_id, turn_to=1)

        engine = _make_engine(db, router, include_chapter_breaks=False, min_interval_turns=100)
        result = await engine.process_round(2, _make_ctx())

    assert result is False


# ------------------------------------------------------------------
# テスト 10: initialize() 後に新しい arc を閉じると chapter break が発生する
# ------------------------------------------------------------------

@async_to_sync
async def test_chapter_break_triggered_after_new_arc_closed_post_init() -> None:
    """initialize() 後に新しい arc を閉じると chapter_break が True になり narration_chapter が保存される。"""
    router = _make_llm_router("第二章 — 新たな展開")
    async with _make_db() as db:
        # 既存の closed arc（initialize で同期される）
        existing_arc_id = await db.insert_arc(_STORY_ID, {
            "arc_type": "scene", "title": "夜明け", "summary": "夜明けの場面。", "turn_from": 1,
        })
        await db.close_arc(existing_arc_id, turn_to=1)

        engine = _make_engine(db, router, include_chapter_breaks=True, min_interval_turns=100)
        await engine.initialize()

        # initialize 後に新しい arc を追加・閉じる
        new_arc_id = await db.insert_arc(_STORY_ID, {
            "arc_type": "scene", "title": "新展開", "summary": "新しい場面。", "turn_from": 2,
        })
        await db.close_arc(new_arc_id, turn_to=3)

        result = await engine.process_round(4, _make_ctx())

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT msg_type, message FROM chat_logs WHERE story_id = ? AND char_id = '_narrator';",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()
        assert row is not None
        data = dict(row)

    assert result is True
    assert data["msg_type"] == "narration_chapter"
    assert data["message"] == "第二章 — 新たな展開"


# ------------------------------------------------------------------
# テスト 11: 既存 arc + initialize + 新 arc 閉じ → True（統合テスト）
# ------------------------------------------------------------------

@async_to_sync
async def test_chapter_break_full_lifecycle() -> None:
    """既存 closed arc あり → initialize() → 新 arc 閉じ → chapter_break=True。
    2回目の process_round では再発火しない。"""
    router = _make_llm_router("新章タイトル")
    async with _make_db() as db:
        # 既存 closed arc
        old_arc = await db.insert_arc(_STORY_ID, {
            "arc_type": "scene", "title": "旧場面", "summary": "旧。", "turn_from": 1,
        })
        await db.close_arc(old_arc, turn_to=2)

        engine = _make_engine(db, router, include_chapter_breaks=True, min_interval_turns=100)
        await engine.initialize()

        # 新 arc を閉じる
        new_arc = await db.insert_arc(_STORY_ID, {
            "arc_type": "scene", "title": "新場面", "summary": "新。", "turn_from": 3,
        })
        await db.close_arc(new_arc, turn_to=4)

        # 1回目: 新 arc がある → True
        result1 = await engine.process_round(5, _make_ctx())
        assert result1 is True

        # 2回目: 同じ arc → False（再発火しない）
        result2 = await engine.process_round(6, _make_ctx())
        assert result2 is False
