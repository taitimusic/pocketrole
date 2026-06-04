"""
tests/test_novel_generator.py — NovelGenerator のテスト

in-memory SQLite を使用。LLMRouter はモック化する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.db_manager import DatabaseManager
from engine.config import NovelGeneratorConfig
from engine.llm.base import LLMResponse
from engine.novel_generator import NovelGenerator, NovelGeneratorContext
from tests._async_harness import async_to_sync

# ------------------------------------------------------------------
# 定数・ヘルパー
# ------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_STORY_ID = "test_story"

_VALID_RESPONSE = (
    '{"title": "夜の出会い", "summary": "二人が初めて話す場面。",'
    ' "prose": "月明かりが差し込む教室で..."}'
)


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    """in-memory DB を初期化し、テスト用ストーリー行を挿入する。"""
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


def _make_llm_router(response_text: str = "") -> MagicMock:
    """LLMRouter のモックを返す。generate は AsyncMock で指定テキストを返す。"""
    router = MagicMock()
    router.generate = AsyncMock(
        return_value=LLMResponse(
            text=response_text,
            model="test_model",
            provider="ollama",
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=100,
        )
    )
    return router


def _make_cfg(**overrides: object) -> NovelGeneratorConfig:
    """デフォルト設定を返す。overrides でフィールドを上書き可能。"""
    defaults: dict[str, object] = dict(
        enabled=True,
        generate_on_session_end=True,
        generate_on_chapter_break=True,
    )
    defaults.update(overrides)
    return NovelGeneratorConfig(**defaults)  # type: ignore[arg-type]


def _make_generator(
    db: DatabaseManager,
    router: MagicMock,
    *,
    enabled: bool = True,
    on_session_end: bool = True,
    on_chapter_break: bool = True,
    llm_model: str = "test_model",
    response_max_tokens: int | None = None,
    repair_max_tokens: int | None = None,
) -> NovelGenerator:
    """NovelGenerator インスタンスを返す。"""
    cfg = _make_cfg(
        enabled=enabled,
        generate_on_session_end=on_session_end,
        generate_on_chapter_break=on_chapter_break,
        **(
            {"response_max_tokens": response_max_tokens}
            if response_max_tokens is not None
            else {}
        ),
        **(
            {"repair_max_tokens": repair_max_tokens}
            if repair_max_tokens is not None
            else {}
        ),
    )
    return NovelGenerator(
        story_id=_STORY_ID,
        config=cfg,
        db=db,
        llm_router=router,
        llm_provider="ollama",
        llm_model=llm_model,
    )


def _make_ctx(
    trigger: str = "session_end",
    log_lines: list[str] | None = None,
    memory_texts: list[str] | None = None,
    source_log_ids: list[int] | None = None,
    scene_id: int | None = None,
    scene_type: str | None = None,
    scene_outcome_summary: str | None = None,
    hook_summaries: list[str] | None = None,
    tension_summaries: list[str] | None = None,
    place_id: str | None = None,
    participant_names: list[str] | None = None,
    episode_id: int | None = None,
    episode_type: str | None = None,
    episode_goal: str | None = None,
    episode_summary: str | None = None,
) -> NovelGeneratorContext:
    """デフォルトのコンテキストを返す。"""
    return NovelGeneratorContext(
        world_rules="魔法が使える世界",
        recent_log_lines=log_lines if log_lines is not None else ["Alice: こんにちは"],
        story_memory_texts=memory_texts or [],
        trigger=trigger,
        source_log_ids=source_log_ids or [],
        scene_id=scene_id,
        scene_type=scene_type,
        scene_outcome_summary=scene_outcome_summary,
        hook_summaries=hook_summaries or [],
        tension_summaries=tension_summaries or [],
        place_id=place_id,
        participant_names=participant_names or [],
        episode_id=episode_id,
        episode_type=episode_type,
        episode_goal=episode_goal,
        episode_summary=episode_summary,
    )


# ------------------------------------------------------------------
# テスト 1: enabled=False のとき LLM 未呼び出し・DB 変化なし
# ------------------------------------------------------------------

@async_to_sync
async def test_disabled_skips_processing() -> None:
    """enabled=False のとき process_round は何もせず LLM を呼ばない。"""
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(db, router, enabled=False)
        await gen.process_round(1, _make_ctx())

        router.generate.assert_not_called()
        arcs = await db.get_arcs(_STORY_ID)
        assert len(arcs) == 0


# ------------------------------------------------------------------
# テスト 2: generate_on_session_end=False, trigger=session_end → skip
# ------------------------------------------------------------------

@async_to_sync
async def test_session_end_flag_disabled_skips() -> None:
    """generate_on_session_end=False のとき session_end トリガーは無視される。"""
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(db, router, on_session_end=False)
        await gen.process_round(1, _make_ctx(trigger="session_end"))

        router.generate.assert_not_called()
        arcs = await db.get_arcs(_STORY_ID)
        assert len(arcs) == 0


@async_to_sync
async def test_gemma_uses_tighter_structured_json_policy() -> None:
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(
            db,
            router,
            llm_model="gemma4:e4b",
            response_max_tokens=900,
            repair_max_tokens=1100,
        )

        policy = gen._structured_json_policy()

        assert policy.name == "novel_generator"
        assert policy.response_max_tokens == 420
        assert policy.repair_max_tokens == 520


@async_to_sync
async def test_gemma_compacts_novel_generator_prompts_before_generate() -> None:
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(
            db,
            router,
            llm_model="gemma4:e4b",
        )
        ctx = _make_ctx(
            trigger="scene_close",
            log_lines=[
                f"Alice: {'あ' * 220}",
                f"Bob: {'い' * 220}",
                f"Carol: {'う' * 220}",
                f"Dave: {'え' * 220}",
                f"Eve: {'お' * 220}",
                f"Frank: {'か' * 220}",
                f"Grace: {'き' * 220}",
                f"Heidi: {'く' * 220}",
            ],
            memory_texts=["記憶" * 80, "伏線" * 80, "余韻" * 80, "残件" * 80],
            hook_summaries=["hook-" + ("x" * 140), "hook-" + ("y" * 140), "hook-" + ("z" * 140)],
            tension_summaries=["tension-" + ("a" * 140), "tension-" + ("b" * 140), "tension-" + ("c" * 140)],
            scene_id=99,
            scene_type="conversation",
            scene_outcome_summary="summary-" + ("s" * 160),
            place_id="music_room",
            participant_names=["Alice", "Bob", "Carol", "Dave"],
        )
        ctx.world_rules = "世界設定" * 120

        await gen.process_round(12, ctx)

        primary_call = router.generate.await_args_list[0].kwargs
        assert len(primary_call["system_prompt"]) <= 650
        assert len(primary_call["user_prompt"]) <= 420
        assert primary_call["user_prompt"].count("\n") < len(ctx.recent_log_lines) + 2


# ------------------------------------------------------------------
# テスト 3: generate_on_chapter_break=False, trigger=chapter_break → skip
# ------------------------------------------------------------------

@async_to_sync
async def test_chapter_break_flag_disabled_skips() -> None:
    """generate_on_chapter_break=False のとき chapter_break トリガーは無視される。"""
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(db, router, on_chapter_break=False)
        await gen.process_round(1, _make_ctx(trigger="chapter_break"))

        router.generate.assert_not_called()
        arcs = await db.get_arcs(_STORY_ID)
        assert len(arcs) == 0


# ------------------------------------------------------------------
# テスト 4: recent_log_lines=[] → skip
# ------------------------------------------------------------------

@async_to_sync
async def test_empty_log_lines_skips() -> None:
    """recent_log_lines が空のとき LLM を呼ばない。"""
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(db, router)
        await gen.process_round(1, _make_ctx(log_lines=[]))

        router.generate.assert_not_called()
        arcs = await db.get_arcs(_STORY_ID)
        assert len(arcs) == 0


# ------------------------------------------------------------------
# テスト 5: happy path — arc と novel_output が保存され arc が閉じる
# ------------------------------------------------------------------

@async_to_sync
async def test_generates_prose_and_saves_to_db() -> None:
    """正常系: arc が作成され novel_output が保存され arc が閉じること。"""
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(db, router)
        ctx = _make_ctx(
            trigger="session_end",
            log_lines=["Alice: こんにちは", "Bob: よろしく"],
            source_log_ids=[1, 2, 3],
        )
        await gen.process_round(5, ctx)

        router.generate.assert_called_once()

        arcs = await db.get_arcs(_STORY_ID)
        assert len(arcs) == 1
        assert arcs[0]["arc_type"] == "scene"
        assert arcs[0]["title"] == "夜の出会い"
        assert arcs[0]["turn_to"] is not None  # close_arc が呼ばれた

        outputs = await db.get_novel_outputs(_STORY_ID, arcs[0]["id"])
        assert len(outputs) == 1
        assert outputs[0]["content_type"] == "prose"
        assert outputs[0]["content"] == "月明かりが差し込む教室で..."
        assert outputs[0]["ordering"] == 1
        assert outputs[0]["source_log_ids"] == [1, 2, 3]


# ------------------------------------------------------------------
# テスト 6: chapter_break トリガーでも生成される
# ------------------------------------------------------------------

@async_to_sync
async def test_chapter_break_trigger_generates_prose() -> None:
    """trigger=chapter_break かつ flag=True のとき arc と novel_output が保存される。"""
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(db, router, on_chapter_break=True)
        ctx = _make_ctx(trigger="chapter_break", log_lines=["Alice: 章の終わり"])
        await gen.process_round(10, ctx)

        router.generate.assert_called_once()

        arcs = await db.get_arcs(_STORY_ID)
        assert len(arcs) == 1
        assert arcs[0]["turn_to"] is not None

        outputs = await db.get_novel_outputs(_STORY_ID, arcs[0]["id"])
        assert len(outputs) == 1
        assert outputs[0]["content_type"] == "prose"


# ------------------------------------------------------------------
# テスト 7: LLM が不正テキスト返却 → 例外なし・DB に何も保存されない
# ------------------------------------------------------------------

@async_to_sync
async def test_parse_failure_no_crash() -> None:
    """LLM が不正テキストを返しても例外を送出せず、DB に何も保存されない。"""
    router = _make_llm_router("これは不正なレスポンスです。JSONではありません。")
    async with _make_db() as db:
        gen = _make_generator(db, router)
        ctx = _make_ctx(log_lines=["Alice: こんにちは"])

        # 例外が発生しないこと
        await gen.process_round(1, ctx)

        arcs = await db.get_arcs(_STORY_ID)
        assert len(arcs) == 0


@async_to_sync
async def test_scene_close_reuses_existing_arc_without_duplicate() -> None:
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        existing_arc_id = await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "scene",
                "title": "既存の場面",
                "summary": "既存 summary",
                "turn_from": 4,
                "source_scene_id": 12,
            },
        )
        gen = _make_generator(db, router)
        ctx = _make_ctx(
            trigger="scene_close",
            scene_id=12,
            scene_type="conversation",
            scene_outcome_summary="閉じた場面の結果。",
            log_lines=["Alice: こんにちは", "Bob: よろしく"],
        )

        await gen.process_round(5, ctx)

        arcs = await db.get_arcs(_STORY_ID)
        outputs = await db.get_novel_outputs(_STORY_ID, existing_arc_id)

    assert len(arcs) == 1
    assert arcs[0]["id"] == existing_arc_id
    assert len(outputs) == 1
    assert outputs[0]["content"] == "月明かりが差し込む教室で..."


@async_to_sync
async def test_scene_close_without_logs_uses_fallback_summary_and_generates() -> None:
    router = _make_llm_router("not json")
    async with _make_db() as db:
        gen = _make_generator(db, router)
        ctx = _make_ctx(
            trigger="scene_close",
            log_lines=[],
            scene_id=18,
            scene_type="conversation",
            scene_outcome_summary=None,
            place_id="rooftop",
            participant_names=["Alice", "Bob"],
            hook_summaries=["放課後の約束"],
        )

        await gen.process_round(7, ctx)

        arcs = await db.get_arcs(_STORY_ID)
        outputs = await db.get_novel_outputs(_STORY_ID, arcs[0]["id"]) if arcs else []

    assert len(arcs) == 1
    assert arcs[0]["source_scene_id"] == 18
    assert outputs
    assert "Alice" in outputs[0]["content"] or "Bob" in outputs[0]["content"]


@async_to_sync
async def test_scene_close_without_logs_or_summary_still_persists_arc_from_participants() -> None:
    router = _make_llm_router("not json")
    async with _make_db() as db:
        gen = _make_generator(db, router)
        ctx = _make_ctx(
            trigger="scene_close",
            log_lines=[],
            scene_id=28,
            scene_type="conversation",
            scene_outcome_summary=None,
            place_id="rooftop",
            participant_names=["Alice", "Bob"],
            hook_summaries=[],
            tension_summaries=[],
        )

        await gen.process_round(7, ctx)

        arcs = await db.get_arcs(_STORY_ID)
        outputs = await db.get_novel_outputs(_STORY_ID, arcs[0]["id"]) if arcs else []

    assert len(arcs) == 1
    assert arcs[0]["source_scene_id"] == 28
    assert outputs
    assert "Alice" in outputs[0]["content"] and "Bob" in outputs[0]["content"]


@async_to_sync
async def test_scene_close_without_summary_uses_recent_logs_before_generic_fallback() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        return_value=LLMResponse(
            text='{"title":"夜の余韻","summary":"会話の余韻が残る。","prose":"屋上で、あとで決めるという言葉だけが残った。"}',
            model="test_model",
            provider="ollama",
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=100,
        )
    )
    async with _make_db() as db:
        gen = _make_generator(db, router)
        ctx = _make_ctx(
            trigger="scene_close",
            log_lines=["Alice: あとで屋上で決める。", "Bob: じゃあ順番だけ先に出して。"],
            source_log_ids=[],
            scene_id=19,
            scene_type="conversation",
            scene_outcome_summary=None,
            place_id="rooftop",
            participant_names=["Alice", "Bob"],
        )

        await gen.process_round(7, ctx)

        system_prompt = router.generate.await_args.kwargs["system_prompt"]

    assert "あとで屋上で決める" in system_prompt
    assert "じゃあ順番だけ先に出して" in system_prompt


@async_to_sync
async def test_parse_failure_uses_repair_once_and_then_saves() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            LLMResponse(
                text='{"title":"壊れた場面","summary":"broken"',
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
                done_reason="length",
            ),
            LLMResponse(
                text='{"title":"夜の出会い","summary":"二人が初めて話す場面。","prose":"月明かりが差し込む教室で..."}',
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
            ),
        ]
    )
    async with _make_db() as db:
        gen = _make_generator(db, router)
        await gen.process_round(5, _make_ctx(log_lines=["Alice: こんにちは", "Bob: よろしく"]))

        arcs = await db.get_arcs(_STORY_ID)

    assert len(arcs) == 1
    assert router.generate.await_count == 2


@async_to_sync
async def test_scene_close_trigger_uses_scene_context_and_persists_source_scene_id() -> None:
    """scene_close では outcome/hooks/tensions を prompt に入れ、arc に source_scene_id を保存する。"""
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(db, router)
        ctx = _make_ctx(
            trigger="scene_close",
            log_lines=["Alice: まだ終わっていない。", "Bob: 放課後に続きを話そう。"],
            source_log_ids=[10, 11],
            scene_id=91,
            scene_type="conversation",
            scene_outcome_summary="放課後に再会する約束だけが残った。",
            hook_summaries=["放課後の約束: 屋上で再会する。"],
            tension_summaries=["conflict: まだ解けていない対立がある。"],
            place_id="rooftop",
            participant_names=["Alice", "Bob"],
        )

        await gen.process_round(7, ctx)

        system_prompt = router.generate.await_args.kwargs["system_prompt"]
        assert "放課後に再会する約束だけが残った。" in system_prompt
        assert "放課後の約束: 屋上で再会する。" in system_prompt
        assert "conflict: まだ解けていない対立がある。" in system_prompt
        assert "Alice, Bob" in system_prompt

        arc = await db.get_arc_by_source_scene_id(_STORY_ID, 91)

    assert arc is not None
    assert arc["source_scene_id"] == 91


@async_to_sync
async def test_episode_close_trigger_generates_episode_arc() -> None:
    """episode_close では episode arc を保存する。"""
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        gen = _make_generator(db, router)
        ctx = _make_ctx(
            trigger="episode_close",
            log_lines=["Alice: すれ違ったままだ。", "Bob: でも次で話せる。"],
            source_log_ids=[10, 11],
            episode_id=41,
            episode_type="misunderstanding",
            episode_goal="誤解をほどく",
            episode_summary="言葉の食い違いが次の場面へ持ち越された。",
        )

        await gen.process_round(7, ctx)

        arcs = await db.get_arcs(_STORY_ID)

    assert len(arcs) == 1
    assert arcs[0]["arc_type"] == "episode"


@async_to_sync
async def test_scene_close_parse_failure_persists_deterministic_fallback() -> None:
    """scene_close は repair 後も壊れていれば deterministic fallback を保存する。"""
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            LLMResponse(
                text='{"title":"壊れた場面","summary":"broken"',
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
                done_reason="length",
            ),
            LLMResponse(
                text="still broken",
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
                done_reason="stop",
            ),
        ]
    )
    async with _make_db() as db:
        gen = _make_generator(db, router)
        ctx = _make_ctx(
            trigger="scene_close",
            log_lines=["Alice: また後で。", "Bob: 屋上で待ってる。"],
            source_log_ids=[10, 11],
            scene_id=91,
            scene_type="conversation",
            scene_outcome_summary="放課後に再会する約束だけが残った。",
            hook_summaries=["放課後の約束: 屋上で再会する。"],
            tension_summaries=["conflict: まだ解けていない対立がある。"],
            place_id="rooftop",
            participant_names=["Alice", "Bob"],
        )

        await gen.process_round(7, ctx)

        arc = await db.get_arc_by_source_scene_id(_STORY_ID, 91)
        assert arc is not None
        outputs = await db.get_novel_outputs(_STORY_ID, arc["id"])

    assert len(outputs) == 1
    assert "放課後に再会する約束だけが残った。" in outputs[0]["content"]


@async_to_sync
async def test_scene_close_skips_when_arc_for_scene_output_already_exists() -> None:
    """同じ source_scene_id の arc に prose があれば scene_close prose を再生成しない。"""
    router = _make_llm_router(_VALID_RESPONSE)
    async with _make_db() as db:
        arc_id = await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "scene",
                "title": "既存場面",
                "summary": "すでに生成済み。",
                "turn_from": 5,
                "source_scene_id": 91,
            },
        )
        await db.insert_novel_output(
            _STORY_ID,
            arc_id,
            {
                "content_type": "prose",
                "content": "既存 prose。",
                "ordering": 1,
            },
        )
        gen = _make_generator(db, router)
        await gen.process_round(
            7,
            _make_ctx(
                trigger="scene_close",
                log_lines=["Alice: まだ終わっていない。"],
                scene_id=91,
                scene_type="conversation",
                scene_outcome_summary="既存 scene。",
            ),
        )

    router.generate.assert_not_called()


@async_to_sync
async def test_scene_close_existing_arc_without_logs_reuses_arc_summary_for_prose_completion() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        return_value=LLMResponse(
            text='{"title":"屋上の余韻","summary":"順番が宙に浮いた。","prose":"屋上では、順番だけ決まらないまま話の熱が残った。"}',
            model="test_model",
            provider="ollama",
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=100,
        )
    )
    async with _make_db() as db:
        arc_id = await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "scene",
                "title": "屋上の余韻",
                "summary": "順番だけ決まらないまま屋上の話が閉じた。",
                "turn_from": 5,
                "source_scene_id": 93,
            },
        )
        gen = _make_generator(db, router)
        await gen.process_round(
            7,
            _make_ctx(
                trigger="scene_close",
                log_lines=[],
                scene_id=93,
                scene_type="conversation",
                scene_outcome_summary=None,
                place_id="rooftop",
                participant_names=["Alice", "Bob"],
            ),
        )

        outputs = await db.get_novel_outputs(_STORY_ID, arc_id)
        system_prompt = router.generate.await_args.kwargs["system_prompt"]

    assert len(outputs) == 1
    assert "順番だけ決まらないまま屋上の話が閉じた。" in system_prompt


@async_to_sync
async def test_scene_close_existing_arc_parse_failure_uses_arc_title_and_summary_fallback() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            LLMResponse(
                text='{"title":"壊れた場面","summary":"broken"',
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
                done_reason="length",
            ),
            LLMResponse(
                text="still broken",
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
                done_reason="stop",
            ),
        ]
    )
    async with _make_db() as db:
        arc_id = await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "scene",
                "title": "屋上の余韻",
                "summary": "順番だけ決まらないまま屋上の話が閉じた。",
                "turn_from": 5,
                "source_scene_id": 94,
            },
        )
        gen = _make_generator(db, router)
        await gen.process_round(
            7,
            _make_ctx(
                trigger="scene_close",
                log_lines=[],
                scene_id=94,
                scene_type="conversation",
                scene_outcome_summary=None,
                place_id="rooftop",
                participant_names=["Alice", "Bob"],
            ),
        )

        outputs = await db.get_novel_outputs(_STORY_ID, arc_id)

    assert len(outputs) == 1
    assert outputs[0]["content"].startswith("順番だけ決まらないまま屋上の話が閉じた。")
