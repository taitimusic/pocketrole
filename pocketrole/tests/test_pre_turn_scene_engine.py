"""tests/test_pre_turn_scene_engine.py — PreTurnSceneEngine のテスト

in-memory SQLite を使用。LLMRouter はモック化する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from db.db_manager import DatabaseManager
from engine.config import SceneScriptConfig
from engine.llm.base import LLMResponse
from engine.pre_turn_scene_engine import (
    PreTurnSceneEngine,
    SceneScriptContext,
    _clean_script_text,
    _parse_director_payload,
    _render_director_payload,
)
from tests._async_harness import async_to_sync

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_STORY_ID = "test_story"


# ------------------------------------------------------------------
# フィクスチャ
# ------------------------------------------------------------------

def _make_router(response_text: str = "廊下に薄暗い蛍光灯の光が落ちている。") -> MagicMock:
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


def _director_payload_json() -> str:
    return """{
      "scene_frame": "廊下の蛍光灯が点滅し、窓際だけが青白く浮かぶ。",
      "turn_goal": "楽譜の断片に全員の注意が集まる。",
      "turn_shift": "ルナが隠したい情報に、ちゅるるんが先に触れかける。",
      "banned_surface_patterns": ["どうするの？", "目の前の一点を拾う"],
      "characters": {
        "hoshikaze_runa": {
          "name": "星風ルナ",
          "role": "情報を守る側",
          "next_move": "ちゅるるんの視線を手帳からそらす",
          "speech_task": "手帳に触れられたくない理由をぼかして返す",
          "actable_behavior": "手帳を胸元へ引き寄せる",
          "target_char_id": "chururun",
          "avoid": ["直接命令", "抽象的な問い"]
        }
      }
    }"""


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title, world_rules) VALUES (?, ?, ?);",
        (_STORY_ID, "テストストーリー", "ダークアカデミア世界"),
    )
    await manager._conn.commit()
    try:
        yield manager
    finally:
        await manager.close()


def _make_engine(
    db: DatabaseManager,
    router: MagicMock,
    *,
    enabled: bool = True,
    generation_mode: str = "per_round",
) -> PreTurnSceneEngine:
    config = SceneScriptConfig(
        enabled=enabled,
        generation_mode=generation_mode,
        max_sentences=3,
        reference_previous_n=1,
    )
    return PreTurnSceneEngine(
        story_id=_STORY_ID,
        db=db,
        llm_router=router,
        config=config,
        llm_provider="ollama",
        llm_model="test_model",
    )


def _make_ctx(**kwargs) -> SceneScriptContext:
    defaults = {
        "sim_datetime": "2026-04-08T00:30",
        "world_rules": "ダークアカデミアの学園",
        "recent_dialogue_lines": [],
        "active_character_states": [],
        "current_places": {},
    }
    defaults.update(kwargs)
    return SceneScriptContext(**defaults)


def _required_ctx(*char_ids: str) -> SceneScriptContext:
    return _make_ctx(
        required_character_ids=list(char_ids),
        active_character_states=[
            {
                "char_id": char_id,
                "name": f"キャラ{index}",
                "place": "廊下",
                "current_goal": "会話に参加する",
                "current_worry": "出遅れる",
                "dominant_emotion": "neutral",
            }
            for index, char_id in enumerate(char_ids, start=1)
        ],
    )


# ------------------------------------------------------------------
# テスト: 基本生成
# ------------------------------------------------------------------

@async_to_sync
async def test_generate_for_turn_produces_text() -> None:
    """enabled=True で generate_for_turn() が LLM テキストを返す。"""
    router = _make_router(_director_payload_json())
    async with _make_db() as db:
        engine = _make_engine(db, router)
        await engine.initialize()
        ctx = _make_ctx()
        result = await engine.generate_for_turn(1, ctx)

    assert result is not None
    assert "廊下" in result


@async_to_sync
async def test_generate_for_turn_disabled_returns_none() -> None:
    """enabled=False で generate_for_turn() は None を返す。"""
    router = _make_router()
    async with _make_db() as db:
        engine = _make_engine(db, router, enabled=False)
        await engine.initialize()
        ctx = _make_ctx()
        result = await engine.generate_for_turn(1, ctx)

    assert result is None
    router.generate.assert_not_called()


# ------------------------------------------------------------------
# テスト: DB 保存と取得
# ------------------------------------------------------------------

@async_to_sync
async def test_generate_saves_to_db() -> None:
    """生成したスクリプトが DB に保存される。"""
    router = _make_router(_director_payload_json())
    async with _make_db() as db:
        engine = _make_engine(db, router)
        await engine.initialize()
        ctx = _make_ctx(director_persona_id="mystery_unfolder", chapter_id="ch1_anniversary")
        await engine.generate_for_turn(5, ctx)

        saved = await db.get_scene_script_by_turn(_STORY_ID, 5)
        assert saved is not None
        assert "〔シーン〕" in saved["script_text"]
        assert saved["turn_number"] == 5


@async_to_sync
async def test_generate_structured_director_payload_saves_metadata_and_rendered_text() -> None:
    """directorial 生成は structured payload を metadata に保存し、表示文も残す。"""
    router = _make_router(_director_payload_json())
    async with _make_db() as db:
        engine = _make_engine(db, router)
        await engine.initialize()
        await engine.generate_for_turn(6, _make_ctx())

        saved = await db.get_scene_script_by_turn(_STORY_ID, 6)
        assert saved is not None

    assert "〔シーン〕" in saved["script_text"]
    assert isinstance(saved["generation_metadata"], dict)
    assert "director_payload" in saved["generation_metadata"]
    assert "hoshikaze_runa" in saved["generation_metadata"]["director_payload"]["characters"]


@async_to_sync
async def test_generate_structured_director_payload_repairs_malformed_json() -> None:
    """scene script JSON が壊れても repair で structured payload を回復する。"""
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            LLMResponse(
                text='{"scene_frame":"broken"',
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
                done_reason="length",
            ),
            LLMResponse(
                text=_director_payload_json(),
                model="test_model",
                provider="ollama",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=100,
            ),
        ]
    )
    async with _make_db() as db:
        engine = _make_engine(db, router)
        await engine.initialize()
        result = await engine.generate_for_turn(7, _make_ctx())

        saved = await db.get_scene_script_by_turn(_STORY_ID, 7)
        assert saved is not None

    assert result is not None
    assert "〔各キャラの実行カード〕" in result
    assert router.generate.await_count == 2
    assert "director_payload" in saved["generation_metadata"]


def test_render_director_payload_produces_human_readable_sections() -> None:
    """structured payload は人間向けの 6 セクション表示文へ整形できる。"""
    rendered = _render_director_payload(
        {
            "scene_frame": "窓の外で雨音が響く。",
            "turn_goal": "手帳への注目を集める。",
            "turn_shift": "ルナが視線を逸らす。",
            "banned_surface_patterns": ["どうするの？"],
            "characters": {
                "hoshikaze_runa": {
                    "name": "星風ルナ",
                    "role": "秘密を守る側",
                    "next_move": "手帳を守る",
                    "speech_task": "見せたくない理由をぼかして返す",
                    "actable_behavior": "手帳を胸元へ寄せる",
                    "target_char_id": "chururun",
                    "avoid": ["直接命令"],
                }
            },
        }
    )

    assert "〔シーン〕" in rendered
    assert "〔各キャラの実行カード〕" in rendered
    assert "星風ルナ" in rendered
    assert "見せたくない理由をぼかして返す" in rendered


def test_parse_director_payload_accepts_fenced_json_with_partial_active_cards() -> None:
    payload = _parse_director_payload(
        f"```json\n{_director_payload_json()}\n```",
        required_char_ids={"hoshikaze_runa", "chururun"},
    )

    assert payload is not None
    assert "hoshikaze_runa" in payload["characters"]


def test_parse_director_payload_rejects_empty_character_cards() -> None:
    payload = _parse_director_payload(
        """{
          "scene_frame": "廊下で紙片が揺れる。",
          "turn_goal": "全員の目線をそろえる。",
          "turn_shift": "ルナだけが反応を遅らせる。",
          "characters": {}
        }""",
        required_char_ids={"hoshikaze_runa"},
    )

    assert payload is None


def test_parse_director_payload_rejects_unknown_character_cards() -> None:
    payload = _parse_director_payload(
        """{
          "scene_frame": "廊下で紙片が揺れる。",
          "turn_goal": "全員の目線をそろえる。",
          "turn_shift": "ルナだけが反応を遅らせる。",
          "characters": {
            "unknown_actor": {
              "name": "誰か",
              "role": "割り込む側",
              "next_move": "紙片を見る",
              "speech_task": "話を戻す",
              "actable_behavior": "紙片を指す",
              "target_char_id": "hoshikaze_runa",
              "avoid": []
            }
          }
        }""",
        required_char_ids={"hoshikaze_runa"},
    )

    assert payload is None


@async_to_sync
async def test_generate_autofills_missing_required_character_cards() -> None:
    router = _make_router(
        """{
          "scene_frame": "廊下で紙片が揺れる。",
          "turn_goal": "全員の目線をそろえる。",
          "turn_shift": "ルナだけが反応を遅らせる。",
          "banned_surface_patterns": ["どうするの？"],
          "characters": {
            "hoshikaze_runa": {
              "name": "星風ルナ",
              "role": "秘密を守る側",
              "next_move": "紙片から視線を外す",
              "speech_task": "理由をぼかして返す",
              "actable_behavior": "片手で紙片を隠す",
              "target_char_id": "chururun",
              "avoid": ["直接命令"]
            }
          }
        }"""
    )
    async with _make_db() as db:
        await db.insert_scene_script(
            _STORY_ID,
            {"turn_number": 5, "script_text": "最後の正常 script"},
        )
        engine = _make_engine(db, router)
        await engine.initialize()
        result = await engine.generate_for_turn(
            6,
            _required_ctx("hoshikaze_runa", "chururun"),
        )

        saved = await db.get_scene_script_by_turn(_STORY_ID, 6)
        latest = await db.get_latest_scene_script(_STORY_ID)

    assert "〔シーン〕" in result
    assert saved is not None
    metadata = saved["generation_metadata"]
    assert metadata["director_payload"]["characters"].keys() == {"hoshikaze_runa", "chururun"}
    assert metadata["required_character_ids"] == ["hoshikaze_runa", "chururun"]
    assert metadata["auto_filled_character_ids"] == ["chururun"]
    assert latest is not None
    assert latest["turn_number"] == 6


@async_to_sync
async def test_generate_caps_required_character_cards_at_configured_limit() -> None:
    router = _make_router(
        """{
          "scene_frame": "廊下で紙片が揺れる。",
          "turn_goal": "全員の目線をそろえる。",
          "turn_shift": "ルナだけが反応を遅らせる。",
          "banned_surface_patterns": ["どうするの？"],
          "characters": {
            "char_1": {
              "name": "キャラ1",
              "role": "最初に反応する",
              "next_move": "紙片を見る",
              "speech_task": "紙片の違和感を短く返す",
              "actable_behavior": "紙片に目を落とす",
              "target_char_id": "char_2",
              "avoid": []
            }
          }
        }"""
    )
    async with _make_db() as db:
        config = SceneScriptConfig(
            enabled=True,
            generation_mode="per_round",
            max_directive_characters=3,
        )
        engine = PreTurnSceneEngine(
            story_id=_STORY_ID,
            db=db,
            llm_router=router,
            config=config,
            llm_provider="ollama",
            llm_model="test_model",
        )
        await engine.initialize()
        await engine.generate_for_turn(
            7,
            _required_ctx("char_1", "char_2", "char_3", "char_4", "char_5"),
        )

        saved = await db.get_scene_script_by_turn(_STORY_ID, 7)

    assert saved is not None
    metadata = saved["generation_metadata"]
    assert metadata["required_character_ids"] == ["char_1", "char_2", "char_3"]
    assert metadata["auto_filled_character_ids"] == ["char_2", "char_3"]
    assert set(metadata["director_payload"]["characters"]) == {"char_1", "char_2", "char_3"}
    assert metadata["max_directive_characters"] == 3


@async_to_sync
async def test_get_latest_scene_script_returns_newest() -> None:
    """get_latest_scene_script は最新ターンを返す。"""
    router = _make_router(_director_payload_json())
    async with _make_db() as db:
        engine = _make_engine(db, router)
        await engine.initialize()
        ctx = _make_ctx()
        await engine.generate_for_turn(3, ctx)
        await engine.generate_for_turn(7, ctx)

        latest = await db.get_latest_scene_script(_STORY_ID)
        assert latest is not None
        assert latest["turn_number"] == 7


# ------------------------------------------------------------------
# テスト: キャッシュ動作 (per_round)
# ------------------------------------------------------------------

@async_to_sync
async def test_per_round_returns_cached_on_same_turn() -> None:
    """per_round モードで同じ turn_number を再呼び出すとキャッシュを返す（LLM 追加呼び出しなし）。"""
    router = _make_router(_director_payload_json())
    async with _make_db() as db:
        engine = _make_engine(db, router, generation_mode="per_round")
        await engine.initialize()
        ctx = _make_ctx()
        first = await engine.generate_for_turn(10, ctx)
        second = await engine.generate_for_turn(10, ctx)

    assert first == second
    assert router.generate.call_count == 1  # 2回目はキャッシュ


# ------------------------------------------------------------------
# テスト: 前ターンスクリプトの参照
# ------------------------------------------------------------------

@async_to_sync
async def test_previous_script_is_referenced_in_prompt() -> None:
    """前ターンのスクリプトがプロンプトに含まれる。"""
    router = _make_router(_director_payload_json())
    async with _make_db() as db:
        engine = _make_engine(db, router)
        await engine.initialize()

        # ターン 1 を生成して DB に保存
        ctx = _make_ctx()
        await engine.generate_for_turn(1, ctx)

        # ターン 2 で前スクリプトが参照されているか、user_prompt に含まれるか確認
        router.generate.reset_mock()
        await engine.generate_for_turn(2, ctx)

        call_kwargs = router.generate.call_args.kwargs
        user_prompt = call_kwargs.get("user_prompt", "")
        assert "直前の演技指導ノート" in user_prompt


# ------------------------------------------------------------------
# テスト: delete_scene_scripts_from_turn
# ------------------------------------------------------------------

@async_to_sync
async def test_delete_from_turn_removes_correct_records() -> None:
    """delete_scene_scripts_from_turn でターン N 以降が削除される。"""
    router = _make_router("古い本が積み上げられた書架がある。")
    async with _make_db() as db:
        engine = _make_engine(db, router)
        await engine.initialize()
        ctx = _make_ctx()
        for turn in [1, 2, 3, 4, 5]:
            await db.insert_scene_script(
                _STORY_ID,
                {"turn_number": turn, "script_text": f"ターン{turn}の場面。"},
            )

        deleted = await db.delete_scene_scripts_from_turn(_STORY_ID, 3)
        assert deleted == 3  # ターン 3, 4, 5 が削除

        remaining = await db.get_recent_scene_scripts(_STORY_ID, limit=10)
        assert len(remaining) == 2
        turns = {s["turn_number"] for s in remaining}
        assert turns == {1, 2}


# ------------------------------------------------------------------
# テスト: _clean_script_text
# ------------------------------------------------------------------

_NARRATIVE_CFG = SceneScriptConfig(format_mode="narrative", max_sentences=3)


def test_clean_script_text_limits_sentences() -> None:
    text = "一文目。二文目。三文目。四文目。五文目。"
    result = _clean_script_text(text, _NARRATIVE_CFG)
    assert result.count("。") <= 3


def test_clean_script_text_empty_input() -> None:
    assert _clean_script_text("", _NARRATIVE_CFG) == ""


def test_clean_script_text_adds_period_if_missing() -> None:
    result = _clean_script_text("廊下に人影がある", _NARRATIVE_CFG)
    assert result.endswith("。")


# ------------------------------------------------------------------
# テスト: initialize() による DB からのキャッシュ復元
# ------------------------------------------------------------------

@async_to_sync
async def test_initialize_restores_cache_from_db() -> None:
    """initialize() が DB の最新スクリプトを読んでキャッシュを復元する。"""
    router = _make_router()
    async with _make_db() as db:
        await db.insert_scene_script(
            _STORY_ID,
            {"turn_number": 42, "script_text": "以前のキャッシュ。"},
        )
        engine = _make_engine(db, router)
        await engine.initialize()

        assert engine._last_generated_turn == 42
        assert engine._cached_script == "以前のキャッシュ。"


# ------------------------------------------------------------------
# テスト: directorial format_mode
# ------------------------------------------------------------------

_DIRECTORIAL_SAMPLE = """\
〔シーン〕
廊下の蛍光灯が一本切れていて、窓の外から雨が屋根を叩く音が断続的に聞こえる。
〔配置〕
ルナは入り口に最も近く、ユウマは窓際。ちゅるるんはその間の柱の影。
〔各キャラの今ここ〕（内的指示。台詞にしない）
・ルナ（状態:低）: ユウマを試す。本心は3日前の電話の内容を引きずっている。
・ユウマ（状態:高）: ルナの視線から逃げる。本心は今夜のステージを諦めかけている。
〔転機〕
ちゅるるんが一歩前に踏み出し、ルナとユウマの視線の間に割り込む。
〔身体〕
・ルナ: 手帳の角を爪で繰り返し押している
・ユウマ: 楽譜のページを撫でている
〔禁止〕
- 「決める」「先に」「順番」「判断」を台詞に出さない
- 直接的な問い（「どうするの？」）を避ける
"""

_DIRECTORIAL_CFG = SceneScriptConfig(format_mode="directorial")


def test_directorial_format_outputs_six_sections() -> None:
    """directorial モードの出力が 6 つの〔〕セクションを全て含む。"""
    sections = ("〔シーン〕", "〔配置〕", "〔各キャラの今ここ〕", "〔転機〕", "〔身体〕", "〔禁止〕")
    for section in sections:
        assert section in _DIRECTORIAL_SAMPLE, f"Missing section: {section}"


def test_clean_script_text_directorial_preserves_sections() -> None:
    """directorial モードの clean 処理が〔〕セクション構造を破壊しない。"""
    result = _clean_script_text(_DIRECTORIAL_SAMPLE, _DIRECTORIAL_CFG)
    assert "〔シーン〕" in result
    assert "〔禁止〕" in result
    assert len(result) > 50


def test_clean_script_text_directorial_strips_leading_preamble() -> None:
    """directorial モードで LLM が先頭に余分な前置きを追加した場合に除去する。"""
    text_with_preamble = "以下が演技指導ノートです。\n\n" + _DIRECTORIAL_SAMPLE
    result = _clean_script_text(text_with_preamble, _DIRECTORIAL_CFG)
    assert result.startswith("〔シーン〕"), f"先頭は〔シーン〕であるべき。実際: {result[:30]}"


def test_clean_script_text_directorial_hard_caps_at_800_chars() -> None:
    """directorial モードで 800 字を超える場合はハードカットする。"""
    long_text = _DIRECTORIAL_SAMPLE * 5  # 約 1500 字以上
    result = _clean_script_text(long_text, _DIRECTORIAL_CFG)
    assert len(result) <= 800


@async_to_sync
async def test_directorial_format_system_prompt_includes_template_markers() -> None:
    """_build_system_prompt が structured JSON schema を明示する。"""
    router = _make_router()
    async with _make_db() as db:
        engine = _make_engine(db, router)
        ctx = _make_ctx(
            active_character_states=[
                {
                    "name": "ルナ",
                    "place": "廊下",
                    "current_goal": "秘密を守る",
                    "current_worry": "発覚を恐れている",
                    "dominant_emotion": "ストレス",
                },
            ],
        )
        prompt = engine._build_system_prompt(ctx)

    for token in ("scene_frame", "turn_goal", "turn_shift", "banned_surface_patterns", "characters"):
        assert token in prompt
    assert "JSON 以外を返してはいけません" in prompt
    assert "台詞本文を書いてはいけません" in prompt
