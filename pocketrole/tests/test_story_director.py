"""
tests/test_story_director.py — StoryDirector のテスト

in-memory SQLite を使用。LLMRouter はモック化する。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.db_manager import DatabaseManager
from engine.config import StoryDirectorConfig
from engine.llm.base import LLMResponse
from engine.story_intent import get_story_intent_profile
from engine.story_director import StoryDirector, StoryDirectorContext, _safe_float
from tests._async_harness import async_to_sync

# ------------------------------------------------------------------
# 定数・ヘルパー
# ------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_STORY_ID = "test_story"


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


def _make_cfg(**overrides: object) -> StoryDirectorConfig:
    """デフォルト設定を返す。overrides でフィールドを上書き可能。"""
    defaults: dict[str, object] = dict(
        enabled=True,
        analysis_interval_rounds=3,
        max_active_tensions=5,
        max_active_interventions=3,
        tension_escalation_rounds=5,
        stagnation_detection=True,
        intervention_strength="moderate",
    )
    defaults.update(overrides)
    return StoryDirectorConfig(**defaults)  # type: ignore[arg-type]


def _make_ctx(turn_number: int = 3) -> StoryDirectorContext:
    """デフォルトのコンテキストを返す。"""
    return StoryDirectorContext(
        story_id=_STORY_ID,
        turn_number=turn_number,
        world_rules="魔法が使える世界",
        recent_memory_texts=["アリスが謎の石碑を発見した。", "ボブが行方不明になった。"],
        recent_scene_outcomes=[],
        open_hook_summaries=[],
        recent_relationship_event_summaries=[],
    )


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


# ------------------------------------------------------------------
# テスト 1: enabled=False のとき generate は呼ばれない
# ------------------------------------------------------------------

@async_to_sync
async def test_disabled_skips_processing() -> None:
    """enabled=False のとき、process_round は何もせず generate を呼ばない。"""
    router = _make_llm_router()
    cfg = _make_cfg(enabled=False)
    async with _make_db() as db:
        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 2: interval 外のターンはスキップされる
# ------------------------------------------------------------------

@async_to_sync
async def test_interval_skips_processing() -> None:
    """turn=1, interval=3 のとき、1 % 3 != 0 → generate は呼ばれない。"""
    router = _make_llm_router()
    cfg = _make_cfg(analysis_interval_rounds=3)
    async with _make_db() as db:
        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(1, _make_ctx(turn_number=1))

    router.generate.assert_not_called()


@async_to_sync
async def test_detect_tensions_sets_request_tag_and_gemma_budget() -> None:
    router = _make_llm_router('{"new_tensions":[],"tension_updates":[],"merge_groups":[],"resolve_intervention_ids":[],"new_interventions":[]}')
    cfg = _make_cfg()
    async with _make_db() as db:
        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="gemma4:e4b")
        ctx = _make_ctx(turn_number=6)
        ctx.recent_memory_texts = ["長い要約 " * 12 for _ in range(6)]
        ctx.recent_scene_outcomes = ["長い場面要約 " * 10 for _ in range(4)]
        ctx.open_hook_summaries = ["長いフック説明 " * 10 for _ in range(4)]
        ctx.recent_relationship_event_summaries = ["長い関係変化 " * 10 for _ in range(4)]
        await director._detect_tensions(ctx, [], [])

    kwargs = router.generate.await_args.kwargs
    assert kwargs["request_tag"] == "story_director:analysis:turn_6"
    assert kwargs["max_tokens"] == 320
    assert kwargs["temperature"] == 0.0
    assert kwargs["reasoning_mode"] == "off"
    assert len(kwargs["user_prompt"]) < 900
    assert "JSONのみ" in kwargs["user_prompt"]


@async_to_sync
async def test_generate_intervention_sets_request_tag_and_gemma_budget() -> None:
    router = _make_llm_router(
        '{"intervention_type":"plot_twist","title":"火種","description":"火種","prompt_injection":"火種"}'
    )
    cfg = _make_cfg()
    async with _make_db() as db:
        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="gemma4:e4b")
        ctx = _make_ctx(turn_number=9)
        ctx.recent_scene_outcomes = ["長い場面要約 " * 10 for _ in range(4)]
        ctx.open_hook_summaries = ["長いフック説明 " * 10 for _ in range(4)]
        await director._generate_intervention(
            ctx,
            {
                "id": 12,
                "tension_type": "conflict",
                "description": "対立が続いている",
                "intensity": 0.7,
            },
        )

    kwargs = router.generate.await_args.kwargs
    assert kwargs["request_tag"] == "story_director:intervention:12"
    assert kwargs["max_tokens"] == 220
    assert kwargs["temperature"] == 0.0
    assert kwargs["reasoning_mode"] == "off"


@async_to_sync
async def test_insert_analysis_interventions_prefers_story_intent_types() -> None:
    router = _make_llm_router()
    db = MagicMock()
    db._conn = None
    db.insert_intervention = AsyncMock()
    director = StoryDirector(
        _make_cfg(max_active_interventions=1),
        db,
        router,
        llm_provider="ollama",
        llm_model="test_model",
        intent_profile=get_story_intent_profile("ankoku_gakuen"),
    )

    inserted = await director._insert_analysis_interventions(
        "ankoku_gakuen",
        12,
        [
            {
                "intervention_type": "crisis",
                "title": "危機",
                "description": "危機",
                "prompt_injection": "危機",
            },
            {
                "intervention_type": "relationship_catalyst",
                "title": "火種",
                "description": "火種",
                "prompt_injection": "火種",
            },
        ],
        active_interventions=[],
    )

    assert inserted == 1
    db.insert_intervention.assert_awaited_once()
    payload = db.insert_intervention.await_args.args[1]
    assert payload["intervention_type"] == "relationship_catalyst"


def test_select_fallback_tension_prefers_story_intent_types() -> None:
    director = StoryDirector(
        _make_cfg(),
        MagicMock(),
        _make_llm_router(),
        llm_provider="ollama",
        llm_model="test_model",
        intent_profile=get_story_intent_profile("ankoku_gakuen"),
    )

    selected = director._select_fallback_tension(
        12,
        [
            {"id": 1, "tension_type": "moral_dilemma", "status": "escalating", "detected_turn": 6},
            {"id": 2, "tension_type": "mystery", "status": "escalating", "detected_turn": 6},
        ],
        set(),
    )

    assert selected is not None
    assert int(selected["id"]) == 2


def test_select_fallback_tension_spreads_away_from_recently_intervened_pair() -> None:
    director = StoryDirector(
        _make_cfg(),
        MagicMock(),
        _make_llm_router(),
        llm_provider="ollama",
        llm_model="test_model",
    )

    selected = director._select_fallback_tension(
        12,
        [
            {
                "id": 1,
                "tension_type": "conflict",
                "status": "escalating",
                "detected_turn": 6,
                "involved_chars": ["alice", "bob"],
            },
            {
                "id": 2,
                "tension_type": "conflict",
                "status": "escalating",
                "detected_turn": 6,
                "involved_chars": ["charlie", "diana"],
            },
        ],
        set(),
        recent_tension_ids={1},
    )

    assert selected is not None
    assert int(selected["id"]) == 2


def test_pressure_bonus_for_tension_prefers_matching_pressure() -> None:
    director = StoryDirector(
        _make_cfg(),
        MagicMock(),
        _make_llm_router(),
        llm_provider="ollama",
        llm_model="test_model",
    )

    bonus = director._pressure_bonus_for_tension(
        {"tension_type": "conflict", "involved_chars": ["char_a", "char_b"]},
        [
            {
                "pressure_type": "status_flashpoint",
                "focus_char_ids": ["char_a", "char_b"],
                "score": 0.8,
            }
        ],
    )

    assert bonus > 0.0


def test_normalize_intervention_data_applies_min_duration_floor() -> None:
    director = StoryDirector(
        _make_cfg(min_intervention_duration_rounds=3),
        MagicMock(),
        _make_llm_router(),
        llm_provider="ollama",
        llm_model="test_model",
    )

    normalized = director._normalize_intervention_data(
        {
            "intervention_type": "opportunity",
            "title": "押し返し",
            "description": "二人に選択を迫る。",
            "prompt_injection": "今ここで返事を決める空気にする。",
            "scope": "char:alice,bob",
            "duration_rounds": 1,
        },
        turn_number=12,
    )

    assert normalized is not None
    assert normalized["active_until_turn"] == 14


# ------------------------------------------------------------------
# テスト 3: 正常系 — 有効 JSON 応答 → DB に tension 1件保存
# ------------------------------------------------------------------

_VALID_TENSION_JSON = json.dumps([
    {
        "tension_type": "conflict",
        "description": "アリスとボブの対立が深まっている。",
        "intensity": 0.6,
        "involved_chars": ["alice", "bob"],
    }
])


@async_to_sync
async def test_detect_tensions_saves_to_db() -> None:
    """有効な JSON 応答が DB に保存され、各フィールドが正しいことを確認する。"""
    router = _make_llm_router(_VALID_TENSION_JSON)
    cfg = _make_cfg()
    async with _make_db() as db:
        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert len(tensions) == 1
    t = tensions[0]
    assert t["tension_type"] == "conflict"
    assert t["status"] == "simmering"
    assert t["detected_turn"] == 3
    assert abs(t["intensity"] - 0.6) < 1e-6


# ------------------------------------------------------------------
# テスト 4: 無効 JSON 応答 → 例外なし・tensions 空
# ------------------------------------------------------------------

@async_to_sync
async def test_detect_tensions_parse_failure_no_crash() -> None:
    """無効な JSON 応答でも例外を起こさず、tensions は空のまま。"""
    router = _make_llm_router("invalid json")
    cfg = _make_cfg()
    async with _make_db() as db:
        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        # 例外が発生しないことを確認
        await director.process_round(3, _make_ctx(turn_number=3))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert tensions == []


# ------------------------------------------------------------------
# テスト 5: 古い simmering → status=escalating
# ------------------------------------------------------------------

@async_to_sync
async def test_escalate_simmering_to_escalating() -> None:
    """detected_turn=0, turn=9, escalation_rounds=5 → elapsed=9>=5 → escalating。"""
    # intervention 生成に対応するモック（escalating tension が生まれる）
    intervention_json = json.dumps({
        "intervention_type": "plot_twist",
        "title": "突然の嵐",
        "description": "嵐が村を襲った。",
        "prompt_injection": "今夜、嵐が来る。",
    })
    router = _make_llm_router(intervention_json)

    cfg = _make_cfg(max_active_tensions=1, tension_escalation_rounds=5)
    async with _make_db() as db:
        # detected_turn=0, status="simmering" の tension を pre-insert
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO narrative_tensions
                (story_id, tension_type, description, intensity, detected_turn, status)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "conflict", "対立が続いている。", 0.5, 0, "simmering"),
        )
        await db._conn.commit()

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(9, _make_ctx(turn_number=9))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert tensions[0]["status"] == "escalating"


# ------------------------------------------------------------------
# テスト 6: 新しい simmering → status 変わらず
# ------------------------------------------------------------------

@async_to_sync
async def test_simmering_not_escalated_too_early() -> None:
    """detected_turn=9, turn=9 → elapsed=0 < 5 → stays simmering。"""
    router = _make_llm_router()
    cfg = _make_cfg(max_active_tensions=1, tension_escalation_rounds=5)
    async with _make_db() as db:
        # detected_turn=9, status="simmering" の tension を pre-insert
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO narrative_tensions
                (story_id, tension_type, description, intensity, detected_turn, status)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "mystery", "謎が深まる。", 0.4, 9, "simmering"),
        )
        await db._conn.commit()

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(9, _make_ctx(turn_number=9))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert tensions[0]["status"] == "simmering"


# ------------------------------------------------------------------
# テスト 7: escalating → intervention DB に挿入
# ------------------------------------------------------------------

@async_to_sync
async def test_generate_intervention_for_escalating_tension() -> None:
    """escalating な tension に対して intervention が生成・保存される。"""
    intervention_json = json.dumps({
        "intervention_type": "revelation",
        "title": "秘密の暴露",
        "description": "隠された真実が明らかになる。",
        "prompt_injection": "今こそ真実を話す時だ。",
    })
    router = _make_llm_router(intervention_json)

    cfg = _make_cfg(max_active_tensions=1)
    async with _make_db() as db:
        # detected_turn=0, status="escalating" の tension を pre-insert
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO narrative_tensions
                (story_id, tension_type, description, intensity, detected_turn, status)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "secret", "誰かが秘密を抱えている。", 0.7, 0, "escalating"),
        )
        await db._conn.commit()

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(9, _make_ctx(turn_number=9))

        interventions = await db.get_active_interventions(_STORY_ID, 9)

    assert len(interventions) == 1
    iv = interventions[0]
    assert iv["intervention_type"] == "revelation"
    assert iv["status"] == "active"


# ------------------------------------------------------------------
# テスト 8: max_active_tensions 到達 → detection スキップ（generate 未呼び出し）
# ------------------------------------------------------------------

@async_to_sync
async def test_max_active_tensions_prevents_detection() -> None:
    """active tensions が max_active_tensions に達していると generate を呼ばない。"""
    router = _make_llm_router()
    cfg = _make_cfg(max_active_tensions=1, tension_escalation_rounds=100)
    async with _make_db() as db:
        # max_active_tensions=1 なので 1件 pre-insert → 上限到達
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO narrative_tensions
                (story_id, tension_type, description, intensity, detected_turn, status)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "rivalry", "ライバル関係が続く。", 0.5, 3, "simmering"),
        )
        await db._conn.commit()

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト 9: _safe_float — "high" などの非数値は default を返す
# ------------------------------------------------------------------

def test_safe_float_returns_default_for_non_numeric() -> None:
    """_safe_float は float 変換できない文字列にデフォルトを返す。"""
    assert _safe_float("high", 0.3) == 0.3
    assert _safe_float(None, 0.5) == 0.5
    assert _safe_float("", 0.2) == 0.2


def test_safe_float_converts_valid_values() -> None:
    """_safe_float は有効な数値を float に変換する。"""
    assert _safe_float(0.7, 0.3) == 0.7
    assert _safe_float("0.8", 0.3) == 0.8
    assert _safe_float(1, 0.3) == 1.0


# ------------------------------------------------------------------
# テスト 10: intensity="high" でもクラッシュしない
# ------------------------------------------------------------------

@async_to_sync
async def test_non_numeric_intensity_uses_default() -> None:
    """intensity に非数値が入っていてもデフォルト 0.3 が使われる。"""
    bad_json = json.dumps([{
        "tension_type": "conflict",
        "description": "テスト",
        "intensity": "high",
        "involved_chars": [],
    }])
    router = _make_llm_router(bad_json)
    cfg = _make_cfg()
    async with _make_db() as db:
        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert len(tensions) == 1
    assert abs(tensions[0]["intensity"] - 0.3) < 1e-6


@async_to_sync
async def test_tension_analysis_can_update_existing_tension_status() -> None:
    """tension_updates で既存 tension を resolving に更新できる。"""
    analysis_json = json.dumps(
        {
            "new_tensions": [],
            "tension_updates": [
                {
                    "tension_id": 1,
                    "new_status": "resolving",
                    "intensity": 0.25,
                    "note": "対立は収束に向かっている。",
                }
            ],
        }
    )
    router = _make_llm_router(analysis_json)
    cfg = _make_cfg(max_active_tensions=5, tension_escalation_rounds=100)
    async with _make_db() as db:
        assert db._conn is not None
        cursor = await db._conn.execute(
            """
            INSERT INTO narrative_tensions
                (story_id, tension_type, description, intensity, detected_turn, status)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "conflict", "対立が続いている。", 0.6, 0, "simmering"),
        )
        await db._conn.commit()
        assert cursor.lastrowid is not None

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        assert db._conn is not None
        row_cursor = await db._conn.execute(
            "SELECT status, intensity, resolution_note FROM narrative_tensions WHERE id = ?;",
            (cursor.lastrowid,),
        )
        row = await row_cursor.fetchone()

    assert row is not None
    assert row["status"] == "resolving"
    assert row["intensity"] == pytest.approx(0.25)
    assert row["resolution_note"] == "対立は収束に向かっている。"


@async_to_sync
async def test_generate_intervention_stores_scope_and_duration() -> None:
    """intervention の scope と duration_rounds が active_until_turn に反映される。"""
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            LLMResponse(
                text=json.dumps({"new_tensions": [], "tension_updates": []}),
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
            ),
            LLMResponse(
                text=json.dumps(
                    {
                        "intervention_type": "revelation",
                        "title": "屋上での暴露",
                        "description": "秘密が一部だけ共有される。",
                        "prompt_injection": "屋上にいる当事者だけが、秘密の断片を聞く。",
                        "scope": "char:alice,bob",
                        "duration_rounds": 2,
                    }
                ),
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
            ),
        ]
    )

    cfg = _make_cfg(max_active_tensions=5, analysis_interval_rounds=3)
    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO narrative_tensions
                (story_id, tension_type, description, intensity, detected_turn, status)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "secret", "秘密が揺れている。", 0.8, 0, "escalating"),
        )
        await db._conn.commit()

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        interventions = await db.get_active_interventions(_STORY_ID, 3)

    assert len(interventions) == 1
    iv = interventions[0]
    assert iv["scope"] == "char:alice,bob"
    assert iv["active_from_turn"] == 3
    assert iv["active_until_turn"] == 5


@async_to_sync
async def test_single_analysis_pass_can_create_new_intervention() -> None:
    """single analysis response の new_interventions だけで intervention を作成できる。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [
                    {
                        "intervention_type": "crisis",
                        "title": "鐘が鳴る",
                        "description": "古い時計塔の鐘が不吉に鳴る。",
                        "prompt_injection": "遠くで不吉な鐘が鳴った。",
                        "scope": "all",
                        "duration_rounds": 2,
                        "tension_id": 1,
                    }
                ],
            }
        )
    )
    cfg = _make_cfg(max_active_interventions=3)
    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO narrative_tensions
                (story_id, tension_type, description, intensity, detected_turn, status)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "crisis", "不穏な気配が高まる。", 0.8, 0, "escalating"),
        )
        await db._conn.commit()

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        interventions = await db.get_active_interventions(_STORY_ID, 3)

    assert len(interventions) == 1
    assert interventions[0]["title"] == "鐘が鳴る"
    router.generate.assert_awaited_once()


@async_to_sync
async def test_insert_analysis_interventions_skips_recent_duplicate_combo_during_cooldown() -> None:
    router = _make_llm_router()
    cfg = _make_cfg(repeat_intervention_cooldown_rounds=4)
    async with _make_db() as db:
        tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "alice と bob の張り合いが続いている。",
                "involved_chars": ["alice", "bob"],
                "intensity": 0.55,
                "detected_turn": 6,
                "status": "escalating",
            },
        )
        await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "relationship_catalyst",
                "title": "火種を前に出す",
                "description": "二人の張り合いを前景化する。",
                "prompt_injection": "いまは二人の火種を避けない。",
                "scope": "char:alice,bob",
                "tension_id": tension_id,
                "active_from_turn": 8,
                "active_until_turn": 10,
                "status": "expired",
            },
        )

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        inserted = await director._insert_analysis_interventions(
            _STORY_ID,
            12,
            [
                {
                    "intervention_type": "relationship_catalyst",
                    "title": "火種を前に出す",
                    "description": "二人の張り合いを前景化する。",
                    "prompt_injection": "いまは二人の火種を避けない。",
                    "scope": "char:alice,bob",
                    "tension_id": tension_id,
                    "duration_rounds": 2,
                }
            ],
            active_interventions=[],
        )

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT COUNT(*) FROM director_interventions WHERE story_id = ?;",
            (_STORY_ID,),
        )
        row = await cursor.fetchone()

    assert inserted == 0
    assert row is not None
    assert int(row[0]) == 1


@async_to_sync
async def test_merge_groups_keep_oldest_and_resolve_duplicate_tension() -> None:
    """merge_groups は最古 tension を残し、重複分を resolved にする。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [{"tension_ids": [1, 2], "note": "同じ対立を指している。"}],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_tensions=5)
    async with _make_db() as db:
        assert db._conn is not None
        for description in ("廊下での対立。", "同じ廊下での対立。"):
            await db._conn.execute(
                """
                INSERT INTO narrative_tensions
                    (story_id, tension_type, description, intensity, detected_turn, status)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (_STORY_ID, "conflict", description, 0.6, 0, "simmering"),
            )
        await db._conn.commit()

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT id, status, resolution_note FROM narrative_tensions ORDER BY id ASC;"
        )
        rows = await cursor.fetchall()

    assert rows[0]["status"] != "resolved"
    assert rows[1]["status"] == "resolved"
    assert "1" in str(rows[1]["resolution_note"])


@async_to_sync
async def test_merge_resolves_linked_live_interventions() -> None:
    """merge で閉じた tension に紐づく live intervention も resolved にする。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [{"tension_ids": [1, 2], "note": "一本化する。"}],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_tensions=5)
    async with _make_db() as db:
        t1 = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "旧対立A",
                "involved_chars": [],
                "intensity": 0.4,
                "detected_turn": 1,
            },
        )
        t2 = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "旧対立B",
                "involved_chars": [],
                "intensity": 0.5,
                "detected_turn": 2,
            },
        )
        iv_id = await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "mood",
                "title": "不穏な沈黙",
                "description": "空気が重い。",
                "prompt_injection": "重い沈黙が流れる。",
                "tension_id": t2,
                "active_from_turn": 1,
            },
        )

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        assert db._conn is not None
        iv_cursor = await db._conn.execute(
            "SELECT status, resolution_summary FROM director_interventions WHERE id = ?;",
            (iv_id,),
        )
        iv_row = await iv_cursor.fetchone()

    assert t1 < t2
    assert iv_row is not None
    assert iv_row["status"] == "resolved"
    assert "1" in str(iv_row["resolution_summary"])


@async_to_sync
async def test_conflict_hook_seeds_conflict_tension_without_llm_new_tension() -> None:
    """open conflict hook があれば、analysis が空でも conflict tension を seed する。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_tensions=5)
    async with _make_db() as db:
        await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "conflict",
                "owner_char_id": "alice",
                "target_char_id": "bob",
                "title": "路線対立",
                "description": "alice と bob の路線対立が未解決のまま残っている。",
                "priority": 0.9,
            },
        )

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert len(tensions) == 1
    assert tensions[0]["tension_type"] == "conflict"
    assert tensions[0]["involved_chars"] == ["alice", "bob"]


@async_to_sync
async def test_question_hook_seeds_mystery_tension() -> None:
    """targeted question hook があれば mystery tension を seed する。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_tensions=5)
    async with _make_db() as db:
        await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "question",
                "owner_char_id": "alice",
                "target_char_id": "bob",
                "title": "返答待ち",
                "description": "alice が bob に投げた問いがまだ返されていない。",
                "priority": 0.8,
            },
        )

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert len(tensions) == 1
    assert tensions[0]["tension_type"] == "mystery"
    assert tensions[0]["involved_chars"] == ["alice", "bob"]


@async_to_sync
async def test_repeated_tension_up_relationship_events_seed_rivalry() -> None:
    """同じ pair の tension 上昇イベントが重なると rivalry tension を seed する。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_tensions=5)
    async with _make_db() as db:
        await db.insert_relationship_event(
            _STORY_ID,
            {
                "char_id_from": "alice",
                "char_id_to": "bob",
                "event_type": "conflict",
                "delta_tension": 0.08,
                "summary": "alice が bob に強く反発した。",
                "turn_number": 2,
            },
        )
        await db.insert_relationship_event(
            _STORY_ID,
            {
                "char_id_from": "alice",
                "char_id_to": "bob",
                "event_type": "conflict",
                "delta_tension": 0.09,
                "summary": "bob も alice に反発を返した。",
                "turn_number": 3,
            },
        )

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert len(tensions) == 1
    assert tensions[0]["tension_type"] == "rivalry"
    assert tensions[0]["involved_chars"] == ["alice", "bob"]


@async_to_sync
async def test_existing_seed_match_is_not_duplicated() -> None:
    """同じ deterministic seed key の active tension がある場合は重複挿入しない。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_tensions=5)
    async with _make_db() as db:
        await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "conflict",
                "owner_char_id": "alice",
                "target_char_id": "bob",
                "title": "路線対立",
                "description": "alice と bob の路線対立が未解決のまま残っている。",
                "priority": 0.9,
            },
        )
        await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "alice と bob の衝突が未解決のまま残っている。",
                "involved_chars": ["alice", "bob"],
                "intensity": 0.45,
                "detected_turn": 1,
                "status": "simmering",
            },
        )

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(3, _make_ctx(turn_number=3))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert len(tensions) == 1


@async_to_sync
async def test_active_tension_without_analysis_intervention_gets_fallback_intervention() -> None:
    """analysis が介入を返さなくても、古い simmering tension に fallback intervention を作る。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_interventions=3, analysis_interval_rounds=3)
    async with _make_db() as db:
        tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "alice と bob の衝突が長引いている。",
                "involved_chars": ["alice", "bob"],
                "intensity": 0.45,
                "detected_turn": 0,
                "status": "simmering",
            },
        )

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(6, _make_ctx(turn_number=6))

        interventions = await db.get_active_interventions(_STORY_ID, 6)

    assert len(interventions) == 1
    assert interventions[0]["tension_id"] == tension_id
    assert interventions[0]["intervention_type"] == "relationship_catalyst"
    assert interventions[0]["scope"] == "char:alice,bob"


@async_to_sync
async def test_fallback_intervention_prefers_tension_backed_by_active_pattern() -> None:
    """active pattern がある tension を fallback intervention が優先する。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_interventions=3, analysis_interval_rounds=3)
    async with _make_db() as db:
        older_tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "moral_dilemma",
                "description": "alice が迷っている。",
                "involved_chars": ["alice"],
                "intensity": 0.45,
                "detected_turn": 0,
                "status": "escalating",
            },
        )
        preferred_tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "alice と bob が張り合っている。",
                "involved_chars": ["alice", "bob"],
                "intensity": 0.50,
                "detected_turn": 0,
                "status": "escalating",
            },
        )
        await db.insert_interaction_pattern(
            _STORY_ID,
            {
                "pattern_type": "status_clash",
                "status": "active",
                "title": "張り合い",
                "description": "alice と bob の主導権争い。",
                "involved_chars": ["alice", "bob"],
                "dedupe_key": f"tension:{preferred_tension_id}:status_clash",
                "source_tension_id": preferred_tension_id,
                "first_detected_turn": 3,
                "last_detected_turn": 3,
                "recurrence_count": 1,
                "intensity": 0.7,
                "confidence": 0.8,
            },
        )

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(6, _make_ctx(turn_number=6))

        interventions = await db.get_active_interventions(_STORY_ID, 6)

    assert older_tension_id != preferred_tension_id
    assert len(interventions) == 1
    assert interventions[0]["tension_id"] == preferred_tension_id


@async_to_sync
async def test_fallback_intervention_prefers_tension_backed_by_active_canon_bit() -> None:
    """active canon bit がある tension を fallback intervention が優先する。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_interventions=3, analysis_interval_rounds=3)
    async with _make_db() as db:
        older_tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "moral_dilemma",
                "description": "alice が迷っている。",
                "involved_chars": ["alice"],
                "intensity": 0.45,
                "detected_turn": 0,
                "status": "escalating",
            },
        )
        preferred_tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "alice と bob が張り合っている。",
                "involved_chars": ["alice", "bob"],
                "intensity": 0.50,
                "detected_turn": 0,
                "status": "escalating",
            },
        )
        await db.insert_story_canon_bit(
            _STORY_ID,
            {
                "bit_type": "pair_dynamic",
                "motif_key": "irritated_respect",
                "canon_level": "recurring_bit",
                "status": "active",
                "title": "張り合いの二人",
                "summary": "alice と bob は張り合いに戻りやすい。",
                "focus_char_ids": ["alice", "bob"],
                "focus_place_id": None,
                "dedupe_key": "pair:alice:bob:irritated_respect",
                "evidence_sources": ["relationship_mode"],
                "anchor_pattern_id": None,
                "anchor_episode_id": None,
                "anchor_relationship_mode_id": None,
                "anchor_scene_id": None,
                "first_detected_turn": 3,
                "last_reinforced_turn": 3,
                "recurrence_count": 2,
                "confidence": 0.8,
                "novelty": 0.6,
                "intent_alignment": 0.7,
            },
        )

        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")
        await director.process_round(6, _make_ctx(turn_number=6))

        interventions = await db.get_active_interventions(_STORY_ID, 6)

    assert older_tension_id != preferred_tension_id
    assert len(interventions) == 1
    assert interventions[0]["tension_id"] == preferred_tension_id


@async_to_sync
async def test_analysis_new_tension_prefers_pattern_aligned_candidate_when_slots_are_limited() -> None:
    """max slot が 1 のとき、active pattern に沿う new_tension を優先保存する。"""
    router = _make_llm_router(
        json.dumps(
            {
                "new_tensions": [
                    {
                        "tension_type": "moral_dilemma",
                        "description": "alice が迷っている。",
                        "involved_chars": ["alice"],
                        "intensity": 0.4,
                    },
                    {
                        "tension_type": "conflict",
                        "description": "alice と bob が張り合っている。",
                        "involved_chars": ["alice", "bob"],
                        "intensity": 0.5,
                    },
                ],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
    )
    cfg = _make_cfg(max_active_tensions=1, max_active_interventions=0)
    async with _make_db() as db:
        source_tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "alice と bob が張り合っている。",
                "involved_chars": ["alice", "bob"],
                "intensity": 0.5,
                "detected_turn": 0,
                "status": "resolved",
            },
        )
        await db.insert_interaction_pattern(
            _STORY_ID,
            {
                "pattern_type": "status_clash",
                "status": "active",
                "title": "張り合い",
                "description": "alice と bob が張り合っている。",
                "involved_chars": ["alice", "bob"],
                "dedupe_key": f"tension:{source_tension_id}:status_clash",
                "source_tension_id": source_tension_id,
                "first_detected_turn": 3,
                "last_detected_turn": 3,
                "recurrence_count": 1,
                "intensity": 0.7,
                "confidence": 0.8,
            },
        )
        director = StoryDirector(cfg, db, router, llm_provider="ollama", llm_model="test_model")

        await director.process_round(3, _make_ctx(turn_number=3))

        tensions = await db.get_active_tensions(_STORY_ID)

    assert len(tensions) == 1
    assert tensions[0]["tension_type"] == "conflict"
