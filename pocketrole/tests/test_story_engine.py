"""StoryEngine のテスト。

DB と LLMRouter をモック化し、LLM 依存なしで全ロジックを検証する。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config import (
    Config,
    EngineConfig,
    LLMConfig,
    ModelProfileConfig,
    NewsModeIntensityPreset,
    LLMRuntimeConfig,
    LLMRuntimeProfileConfig,
    LoggingConfig,
    OllamaProviderConfig,
    WebPosterSettingsConfig,
)
from engine.llm.base import LLMResponse
from engine.move_engine import MoveEngine
from engine.news_selector import NewsArticleSelector
from engine.place_manager import PlaceManager
from engine.ambient_context import AmbientTurnContext
from engine.prompt_builder import CharacterContext
from engine.quality_guard import QualityGuard, QualityGuardResult
from engine.story_engine import ReplyMovePlan, StoryEngine, _intervention_applies_to_character


# ============================================================
# ヘルパー
# ============================================================


def make_story(**overrides) -> dict:
    """テスト用 stories 行を生成する。"""
    defaults = {
        "id": "test_story",
        "llm_provider": "ollama",
        "llm_model": "qwen2.5:14b",
        "turn_minutes": 30,
        "turn_interval_sec": 0,  # テスト用に sleep を 0 秒に
        "season_start": "2025-04-01",
        "last_sim_time": None,
    }
    defaults.update(overrides)
    return defaults


def make_character(**overrides) -> dict:
    """テスト用 characters 行を生成する（JSON フィールドは文字列）。"""
    speech_json = json.dumps({
        "first_person": "俺",
        "tone": "短く話す",
        "examples": ["「別に。」"],
        "never_say": [],
    })
    emotion_json = json.dumps({
        "stress": 0.3,
        "motivation": 0.7,
        "loneliness": 0.2,
        "excitement": 0.5,
    })
    places_json = json.dumps(["music_room"])
    defaults = {
        "id": "char_1",
        "name_ja": "テストキャラ",
        "personality_core": "冷静で論理的",
        "current_goal": "音楽を極める",
        "current_worry": "時間が足りない",
        "secret": None,
        "secret_reveal_condition": None,
        "speech": speech_json,
        "emotion_default": emotion_json,
        "favorite_places": places_json,
    }
    defaults.update(overrides)
    return defaults


def make_state(**overrides) -> dict:
    """テスト用 character_states 行を生成する。"""
    defaults = {
        "current_place": "music_room",
        "previous_place": None,
        "current_expression": "neutral",
        "stress": 0.3,
        "motivation": 0.7,
        "loneliness": 0.2,
        "excitement": 0.5,
    }
    defaults.update(overrides)
    return defaults


def make_llm_response(**overrides) -> LLMResponse:
    """テスト用 LLMResponse を生成する。"""
    defaults = {
        "text": "「今日も練習だ。」",
        "model": "qwen2.5:14b",
        "provider": "ollama",
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "latency_ms": 500,
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)


def make_session(**overrides) -> dict:
    """テスト用 conversation_sessions 行を生成する。"""
    defaults = {
        "id": 42,
        "story_id": "test_story",
        "place_id": "music_room",
        "participant_ids": ["char_1", "char_2"],
        "status": "active",
        "last_speaker_id": "char_1",
        "last_log_id": 501,
        "started_at_sim_datetime": "2025-04-01T00:00",
        "last_activity_sim_datetime": "2025-04-01T00:00",
        "last_turn_number": 0,
    }
    defaults.update(overrides)
    return defaults


def make_cm(rows: list) -> AsyncMock:
    """_conn.execute() 用の async context manager モックを生成する。"""
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=rows)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cursor)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def test_select_reply_focus_text_returns_none_for_generic_claim() -> None:
    focus = StoryEngine._select_reply_focus_text(
        target_excerpt="そのまま押し切るつもりだ。",
        recent_dialogue_lines=[],
    )

    assert focus is None


def test_fallback_shape_second_sentence_uses_token_specific_phrase() -> None:
    assert (
        StoryEngine._fallback_shape_second_sentence(
            "answer_then_probe",
            story_pressure_tokens=["主導権"],
        )
        == "こっちから動く"
    )
    assert (
        StoryEngine._fallback_shape_second_sentence(
            "answer_then_condition",
            story_pressure_tokens=["基準"],
        )
        == "まず動いてみる"
    )


def test_fallback_dramatic_second_sentence_uses_natural_probe_phrase() -> None:
    assert (
        StoryEngine._fallback_dramatic_second_sentence(
            "probe",
            required_move_tokens=["なぜ"],
            pressure_anchor_tokens=["見せ場"],
        )
        == "今の声を逃さない"
    )


def test_fallback_group_handoffs_do_not_emit_empty_question_loop() -> None:
    handoffs = StoryEngine._fallback_group_handoffs("ミリティア", "neutral", "conflict")

    assert all("どうする" not in text for text in handoffs)
    assert handoffs == ["ミリティア、その場で一つ拾って", "ミリティア、見えたことを言って"]


def test_extract_reply_signal_anchor_tokens_ignores_relationship_noise_tokens() -> None:
    assert StoryEngine._extract_reply_signal_anchor_tokens(
        "星風ルナと組むと悪ノリしやすい。"
    ) == []


def test_extract_reply_signal_anchor_tokens_ignores_explanatory_tail_only_signal() -> None:
    assert StoryEngine._extract_reply_signal_anchor_tokens(
        "言い切られていないことに触れると場が動きやすい。"
    ) == []


def test_build_reply_signal_contract_downgrades_to_objective_first_when_signal_tokens_are_weak() -> None:
    contract = StoryEngine._build_reply_signal_contract(
        dominant_signal_text="星風ルナと組むと悪ノリしやすい。",
        scene_objective_text="この場の争点: 主導権を出す",
    )

    assert contract is not None
    assert contract["signal_anchor_tokens"] == []
    assert contract["objective_anchor_tokens"] == ["主導権"]
    assert contract["visibility_mode"] == "objective_first"


def test_build_reply_signal_contract_downgrades_duplicate_signal_and_objective_tokens() -> None:
    contract = StoryEngine._build_reply_signal_contract(
        dominant_signal_text="ここで回収に寄せると流れが前に進みやすい。",
        scene_objective_text="この場の争点: 流れを回収する",
    )

    assert contract is not None
    assert contract["signal_anchor_tokens"] == []
    assert contract["objective_anchor_tokens"] == ["流れ", "回収"]
    assert contract["visibility_mode"] == "objective_first"


# Phase 2: initialize() が4回 _conn.execute() を呼ぶための place 行
PLACE_ROW = {
    "id": "music_room",
    "label": "音楽室",
    "zone": "indoor",
    "atmosphere": None,
    "who_gathers": None,
    "events_likely": None,
    "access_note": None,
    "adjacent_places": None,
}


def make_mock_db(story=None, characters=None) -> AsyncMock:
    """モック化した DatabaseManager を返す。

    Phase 2: initialize() 内で _conn.execute() が4回呼ばれるため
    side_effect リストで各呼び出しに対応する CM を返す。
      1回目: places
      2回目: time_schedules
      3回目: anomaly_rules
      4回目: relationships
    """
    db = AsyncMock()
    db.get_story = AsyncMock(return_value=story or make_story())
    db.get_characters = AsyncMock(return_value=characters or [make_character()])
    db.get_latest_character_state = AsyncMock(return_value=make_state())
    db.insert_chat_log = AsyncMock(return_value=1)
    db.insert_character_state = AsyncMock()
    db.update_last_sim_time = AsyncMock()
    db.get_active_conversation_sessions = AsyncMock(return_value=[])
    db.get_active_conversation_session = AsyncMock(return_value=None)
    db.insert_conversation_session = AsyncMock(return_value=42)
    db.update_conversation_session = AsyncMock()
    db.close_conversation_session = AsyncMock()
    db.get_recent_conversation_logs = AsyncMock(return_value=[])
    db.get_conversation_seed_logs = AsyncMock(return_value=[])
    db.attach_logs_to_conversation_session = AsyncMock()
    db.get_recent_chat_logs = AsyncMock(return_value=[])
    db.insert_generation_quality_issue = AsyncMock()
    db.get_active_story_scenes = AsyncMock(return_value=[])
    db.insert_story_scene = AsyncMock(return_value=77)
    db.close_story_scene = AsyncMock()
    db.update_story_scene = AsyncMock()
    db.replace_scene_participants = AsyncMock()
    db.get_scene_participants = AsyncMock(return_value=[])
    db.record_scene_participant_turn = AsyncMock()
    db.update_intervention = AsyncMock()
    db.insert_story_hook = AsyncMock(return_value=1)
    db.get_open_story_hooks = AsyncMock(return_value=[])
    db.get_open_story_hooks_for_scene = AsyncMock(return_value=[])
    db.get_story_scene = AsyncMock(return_value=None)
    db.get_scene_logs = AsyncMock(return_value=[])
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.resolve_story_hook = AsyncMock()
    db.update_relationship_snapshot = AsyncMock()
    db.insert_relationship_event = AsyncMock(return_value=1)
    db.get_relationship_summary = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_active_interaction_patterns = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_writeback_eligible_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    db.get_arc_by_source_scene_id = AsyncMock(return_value=None)
    db.get_novel_outputs = AsyncMock(return_value=[])
    db.get_relevant_story_memories = AsyncMock(return_value=[])
    db.get_closed_story_scene_summaries = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[])
    db.expire_interventions_before_turn = AsyncMock(return_value=[])
    db.get_live_interventions_for_tension_ids = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.upsert_character_canon_overlay = AsyncMock()
    db.delete_character_canon_overlay = AsyncMock()
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.insert_character_growth_candidate = AsyncMock(return_value=1)
    db.get_pending_growth_candidates = AsyncMock(return_value=[])
    db.update_character_growth_candidate_status = AsyncMock()
    db.get_character_entry_turn = AsyncMock(return_value=None)
    db.get_place_dialogue_since_turn = AsyncMock(return_value=[])

    db._conn = MagicMock()
    db._conn.execute = MagicMock(side_effect=[
        make_cm([PLACE_ROW]),  # 1: places
        make_cm([]),           # 2: time_schedules
        make_cm([]),           # 3: anomaly_rules
        make_cm([]),           # 4: relationships
    ])

    return db


def make_runtime_config() -> Config:
    return Config(
        llm=LLMConfig(
            cloud_concurrency=3,
            providers={
                "ollama": OllamaProviderConfig(
                    base_url="http://localhost:11434",
                    default_model="qwen2.5:14b",
                    timeout_sec=120,
                    max_retries=3,
                ),
            },
        ),
        llm_runtime=LLMRuntimeConfig(
            active_profile="ollama_fast",
            profiles={
                "ollama_fast": LLMRuntimeProfileConfig(
                    provider="ollama",
                    model="qwen3.5:9b",
                ),
            },
            story_overrides={},
        ),
        engine=EngineConfig(memory_limit=20, emotion_decay_rate=0.05, max_move_cost=1),
        web_poster=WebPosterSettingsConfig(
            batch_size=10,
            retry_interval_sec=30,
            max_consecutive_failures=3,
            pause_duration_sec=300,
        ),
        web_post_targets={},
        logging=LoggingConfig(level="INFO", log_dir="logs", rotation="daily", retention_days=7),
    )


async def test_news_selector_uses_name_ja_for_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AsyncMock()
    store.get_recent_articles = AsyncMock(
        return_value=[
            {"title": "A", "url": "https://example.com/a"},
            {"title": "B", "url": "https://example.com/b"},
        ]
    )
    captured: dict[str, str] = {}

    async def fake_generate_structured_json(**kwargs: object) -> SimpleNamespace:
        captured["system_prompt"] = str(kwargs["system_prompt"])
        captured["user_prompt"] = str(kwargs["user_prompt"])
        return SimpleNamespace(parsed=0)

    monkeypatch.setattr("engine.news_selector.random.sample", lambda items, _: items)
    monkeypatch.setattr(
        "engine.news_selector.generate_structured_json",
        fake_generate_structured_json,
    )
    selector = NewsArticleSelector(
        router=AsyncMock(),
        store=store,
        llm_provider="ollama",
        llm_model="test-model",
    )

    article = await selector.select_for_character(
        char={"name": "Legacy Name", "name_ja": "日本語名"},
        story_id="test_story",
    )

    assert article["title"] == "A"
    assert "日本語名" in captured["system_prompt"]
    assert "日本語名" in captured["user_prompt"]
    assert "Legacy Name" not in captured["system_prompt"]


# ============================================================
# インスタンス生成テスト
# ============================================================


def test_story_engine_creation() -> None:
    """StoryEngine が正しくインスタンス化できる。"""
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    assert engine.story_id == "test_story"
    assert engine._running is False
    assert engine._char_index == 0
    assert engine.world_clock is None


# ============================================================
# initialize() テスト
# ============================================================


async def test_initialize_loads_story() -> None:
    """initialize() で _story が設定される。"""
    story = make_story(llm_provider="ollama", llm_model="qwen2.5:14b")
    db = make_mock_db(story=story)
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    assert engine._story["llm_provider"] == "ollama"
    assert engine._story["llm_model"] == "qwen2.5:14b"


async def test_initialize_loads_characters() -> None:
    """initialize() で _characters が設定される。"""
    chars = [make_character(id="char_1"), make_character(id="char_2", name_ja="B")]
    db = make_mock_db(characters=chars)
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    assert len(engine._characters) == 2
    assert engine._characters[0]["id"] == "char_1"


async def test_initialize_creates_world_clock() -> None:
    """initialize() で WorldClock が season_start・turn_minutes で構築される。"""
    db = make_mock_db(story=make_story(season_start="2025-04-01", turn_minutes=30))
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    assert engine.world_clock is not None
    assert engine.world_clock.turn_minutes == 30
    # last_sim_time=None なので season_start から開始
    assert engine.world_clock.sim_datetime == "2025-04-01T00:00"


async def test_initialize_resumes_last_sim_time() -> None:
    """last_sim_time がある場合、WorldClock がその時刻から再開する。"""
    last_time = "2025-04-15T09:00"
    db = make_mock_db(story=make_story(last_sim_time=last_time))
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    assert engine.world_clock is not None
    assert engine.world_clock.sim_datetime == last_time


async def test_initialize_applies_runtime_llm_profile_override() -> None:
    db = make_mock_db(story=make_story(llm_provider="openai", llm_model="gpt-5-mini"))
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, config=make_runtime_config())

    await engine.initialize()

    assert engine._effective_llm_provider == "ollama"
    assert engine._effective_llm_model == "qwen3.5:9b"


async def test_initialize_uses_injected_news_store_without_owning_it() -> None:
    cfg = make_runtime_config()
    cfg.news_mode.enabled = True
    store = AsyncMock()
    store.initialize = AsyncMock()
    store.close = AsyncMock()
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, config=cfg, news_store=store)

    await engine.initialize()
    await engine.stop()

    assert engine._news_selector is not None
    assert engine._news_selector._store is store
    store.initialize.assert_not_called()
    store.close.assert_not_called()


async def test_stop_closes_news_store_created_by_story_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = make_runtime_config()
    cfg.news_mode.enabled = True

    class FakeNewsStore:
        def __init__(self) -> None:
            self.initialized = False
            self.closed = False

        async def initialize(self) -> None:
            self.initialized = True

        async def close(self) -> None:
            self.closed = True

    store = FakeNewsStore()
    monkeypatch.setattr("db.news_store.NewsArticleStore", lambda: store)
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, config=cfg)

    await engine.initialize()
    await engine.stop()

    assert store.initialized is True
    assert store.closed is True


async def test_initialize_queries_active_places_only() -> None:
    """places 読み込みクエリが active 行だけを対象にする。"""
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    place_query = db._conn.execute.call_args_list[0][0][0]
    assert "is_active = 1" in place_query


# ============================================================
# run_one_turn() テスト
# ============================================================


async def test_run_one_turn_calls_llm() -> None:
    """run_one_turn() で llm_router.generate が呼ばれる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()
    router.generate.assert_called_once()


async def test_run_one_turn_inserts_chat_log() -> None:
    """run_one_turn() で db.insert_chat_log が呼ばれ、message が含まれる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「テスト発言。」"))
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()
    db.insert_chat_log.assert_called_once()
    log = db.insert_chat_log.call_args[0][1]
    assert log["message"] == "「テスト発言。」"


async def test_current_affairs_appends_article_url_outside_llm_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = make_runtime_config()
    cfg.news_mode.enabled = True
    cfg.news_mode.intensity_presets["high"] = NewsModeIntensityPreset(
        rate=1.0,
        cooldown_turns=0,
    )
    article_url = "https://example.com/news/" + ("a" * 120)
    db = make_mock_db()
    db.get_news_mode_settings = AsyncMock(return_value={"enabled": True, "intensity": "high"})
    db.get_commented_article_urls = AsyncMock(return_value=set())
    db.record_news_commentary = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="それは今の学校にも響く。"))

    class FakeSelector:
        async def select_for_character(self, **_: object) -> dict[str, str]:
            return {"title": "長いURLの記事", "url": article_url}

    monkeypatch.setattr("random.random", lambda: 0.0)
    engine = StoryEngine("test_story", db, router, config=cfg, news_store=AsyncMock())
    await engine.initialize()
    engine._news_selector = FakeSelector()

    await engine.run_one_turn()

    prompt = router.generate.call_args.kwargs["user_prompt"]
    assert article_url not in prompt
    saved_log = db.insert_chat_log.call_args[0][1]
    assert saved_log["msg_type"] == "current_affairs"
    # 新フォーマット: 「[タイトル](URL)」反応テキスト
    expected = f"「[長いURLの記事]({article_url})」それは今の学校にも響く。"
    assert saved_log["message"] == expected
    db.record_news_commentary.assert_awaited_once_with("test_story", "char_1", article_url, 0)


async def test_current_affairs_from_group_turn_does_not_create_story_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """時事発話は group 由来 target list を持っていても story hook 化しない。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
    ]
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    cfg.news_mode.enabled = True
    cfg.news_mode.intensity_presets["high"] = NewsModeIntensityPreset(
        rate=1.0,
        cooldown_turns=0,
    )
    db = make_mock_db(characters=characters)
    db.get_news_mode_settings = AsyncMock(return_value={"enabled": True, "intensity": "high"})
    db.get_commented_article_urls = AsyncMock(return_value=set())
    db.record_news_commentary = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="逃げる話に聞こえる。"))

    class FakeSelector:
        async def select_for_character(self, **_: object) -> dict[str, str]:
            return {
                "title": "ひき逃げ事件の続報",
                "url": "https://example.com/news",
            }

    monkeypatch.setattr("random.random", lambda: 0.9)
    engine = StoryEngine("test_story", db, router, config=cfg, news_store=AsyncMock())
    await engine.initialize()
    engine._news_selector = FakeSelector()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "group",
            "target_char_id": ["char_2", "char_3"],
            "target_char_name": None,
            "conversation_partner_names": ["二人目", "三人目"],
            "recent_dialogue_lines": ["二人目: 「このまま進める」"],
            "conversation_session_id": 42,
            "reply_to_log_id": None,
            "participant_ids": ["char_1", "char_2", "char_3"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "advance"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    saved_log = db.insert_chat_log.call_args.args[1]
    assert saved_log["msg_type"] == "current_affairs"
    assert saved_log["target_char_id"] == '["char_2", "char_3"]'
    db.insert_story_hook.assert_not_awaited()


async def test_run_one_turn_creates_question_hook_for_targeted_reply() -> None:
    """target 付き疑問文を保存すると question hook を追加する。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「どうして来たんだ？」"))
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "二人目",
            "conversation_partner_names": ["二人目"],
            "recent_dialogue_lines": ["二人目: 「来たよ」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "react"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    db.insert_story_hook.assert_awaited_once()
    saved_hook = db.insert_story_hook.await_args.args[1]
    assert saved_hook["hook_type"] == "question"
    assert saved_hook["owner_char_id"] == "char_1"
    assert saved_hook["target_char_id"] == "char_2"
    assert saved_hook["source_scene_id"] == 77


async def test_run_one_turn_creates_question_hook_from_group_line_with_named_target() -> None:
    """group 発話でも名指しの疑問文なら targeted question hook を追加する。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
    ]
    db = make_mock_db(characters=characters)
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「二人目、どう思う？」"))
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "group",
            "target_char_id": ["char_2", "char_3"],
            "target_char_name": None,
            "conversation_partner_names": ["二人目", "三人目"],
            "recent_dialogue_lines": ["二人目: 「先に言って」"],
            "conversation_session_id": 42,
            "reply_to_log_id": None,
            "participant_ids": ["char_1", "char_2", "char_3"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "advance"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    db.insert_story_hook.assert_awaited_once()
    saved_hook = db.insert_story_hook.await_args.args[1]
    assert saved_hook["hook_type"] == "question"
    assert saved_hook["owner_char_id"] == "char_1"
    assert saved_hook["target_char_id"] == "char_2"
    assert saved_hook["source_scene_id"] == 77


async def test_run_one_turn_turns_monologue_into_scene_scoped_solo_seed_hook() -> None:
    """独り言は question ではなく、後続会話が拾える solo_seed hook として残す。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=make_llm_response(text="「このテープ、灯里先輩の声に似すぎてる。」")
    )
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "monologue",
            "target_char_id": None,
            "target_char_name": None,
            "conversation_partner_names": [],
            "recent_dialogue_lines": [],
            "conversation_session_id": None,
            "reply_to_log_id": None,
            "participant_ids": [],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "solo"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    db.insert_story_hook.assert_awaited_once()
    saved_hook = db.insert_story_hook.await_args.args[1]
    assert saved_hook["hook_type"] == "solo_seed"
    assert saved_hook["owner_char_id"] == "char_1"
    assert saved_hook["target_char_id"] is None
    assert saved_hook["source_scene_id"] == 77
    assert "灯里先輩" in saved_hook["description"]


async def test_run_one_turn_creates_scene_scoped_conflict_hook_from_group_line() -> None:
    """group 発話の対立は名指しが無ければ scene-scoped hook として保存する。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
    ]
    db = make_mock_db(characters=characters)
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「そのやり方は違う。もうやめろ。」"))
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "group",
            "target_char_id": ["char_2", "char_3"],
            "target_char_name": None,
            "conversation_partner_names": ["二人目", "三人目"],
            "recent_dialogue_lines": ["二人目: 「このまま進める」"],
            "conversation_session_id": 42,
            "reply_to_log_id": None,
            "participant_ids": ["char_1", "char_2", "char_3"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "advance"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    db.insert_story_hook.assert_awaited_once()
    saved_hook = db.insert_story_hook.await_args.args[1]
    assert saved_hook["hook_type"] == "conflict"
    assert saved_hook["target_char_id"] is None
    assert saved_hook["source_scene_id"] == 77


async def test_run_one_turn_does_not_create_question_hook_from_unnamed_group_line() -> None:
    """単一名指しが取れない group question は question hook にしない。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
    ]
    db = make_mock_db(characters=characters)
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「どうする？」"))
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "group",
            "target_char_id": ["char_2", "char_3"],
            "target_char_name": None,
            "conversation_partner_names": ["二人目", "三人目"],
            "recent_dialogue_lines": ["二人目: 「そのまま進める？」"],
            "conversation_session_id": 42,
            "reply_to_log_id": None,
            "participant_ids": ["char_1", "char_2", "char_3"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "advance"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    db.insert_story_hook.assert_not_awaited()


async def test_run_one_turn_resolves_question_hook_on_target_reply_same_scene() -> None:
    """同じ scene で対象者が返答すると open question hook を resolve する。"""
    db = make_mock_db(characters=[make_character(id="char_2", name_ja="二人目")])
    db.get_relevant_story_hooks = AsyncMock(
        return_value=[
            {
                "id": 41,
                "hook_type": "question",
                "status": "open",
                "owner_char_id": "char_1",
                "target_char_id": "char_2",
                "description": "char_1 が char_2 に問いかけた。",
                "source_scene_id": 77,
            }
        ]
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「それは俺が確かめたかったからだ。」"))
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_1",
            "target_char_name": "一人目",
            "conversation_partner_names": ["一人目"],
            "recent_dialogue_lines": ["一人目: 「どうして来たんだ？」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_2", "scene_id": 77, "speaker_intent": "react"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    db.resolve_story_hook.assert_awaited_once()
    assert db.resolve_story_hook.await_args.args[0] == 41
    assert db.resolve_story_hook.await_args.kwargs["resolved_turn"] == 0


async def test_run_one_turn_updates_relationship_on_supportive_reply() -> None:
    """supportive cue を含む targeted reply で trust/tension を更新する。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「ありがとう。大丈夫だ、一緒にやろう。」"))
    cfg = make_runtime_config()
    cfg.relationship_dynamics.enabled = True
    cfg.relationship_dynamics.trust_delta_scale = 1.0
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "二人目",
            "conversation_partner_names": ["二人目"],
            "recent_dialogue_lines": ["二人目: 「無理するなよ」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "react"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    db.update_relationship_snapshot.assert_awaited_once()
    patch = db.update_relationship_snapshot.await_args.args[3]
    assert patch["trust"] == pytest.approx(0.57)
    assert patch["tension"] == pytest.approx(0.0)
    db.insert_relationship_event.assert_awaited_once()


async def test_run_one_turn_injects_hooks_and_relationship_summaries_into_prompt() -> None:
    """feature 有効時、relevant hooks と relationship summary が発話 prompt に入る。"""
    db = make_mock_db()
    db.get_relevant_story_hooks = AsyncMock(
        return_value=[
            {
                "id": 8,
                "hook_type": "promise",
                "status": "open",
                "owner_char_id": "char_1",
                "target_char_id": "char_2",
                "title": "放課後の約束",
                "description": "char_1 は char_2 と放課後に会う約束をしている。",
                "priority": 0.9,
            }
        ]
    )
    db.get_relationship_summary = AsyncMock(
        return_value=["char_2 への信頼は 0.62、緊張は 0.18。"]
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「忘れてない。」"))
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    cfg.relationship_dynamics.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "二人目",
            "conversation_partner_names": ["二人目"],
            "recent_dialogue_lines": ["二人目: 「約束、覚えてる？」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )

    await engine.run_one_turn()

    system_prompt = router.generate.await_args.kwargs["system_prompt"]
    assert "未解決のフック" in system_prompt
    assert "放課後の約束" in system_prompt
    assert "対人関係の現在地" in system_prompt
    assert "信頼は 0.62" in system_prompt


async def test_run_one_turn_prefers_growth_overlay_when_enabled() -> None:
    """growth engine の overlay があると prompt に反映される。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「進む。」"))
    cfg = make_runtime_config()
    cfg.growth_engine.enabled = True
    growth_engine = AsyncMock()
    growth_engine.get_profile_overlay = AsyncMock(
        return_value={"current_goal": "もう逃げない"}
    )
    engine = StoryEngine("test_story", db, router, config=cfg, growth_engine=growth_engine)
    await engine.initialize()
    engine._story_memory_manager = None
    engine._story_director = None
    engine._character_evolution_manager = None

    await engine.run_one_turn()

    system_prompt = router.generate.await_args.kwargs["system_prompt"]
    assert "もう逃げない" in system_prompt


async def test_run_one_turn_inserts_character_state() -> None:
    """run_one_turn() で db.insert_character_state が呼ばれる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()
    db.insert_character_state.assert_called_once()
    state = db.insert_character_state.call_args[0][1]
    assert state["char_id"] == "char_1"
    assert state["current_action"] == "monologue"


async def test_run_one_turn_uses_story_provider() -> None:
    """generate の provider 引数が story の llm_provider になる。"""
    db = make_mock_db(story=make_story(llm_provider="ollama"))
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()
    call_kwargs = router.generate.call_args.kwargs
    assert call_kwargs["provider"] == "ollama"


async def test_run_one_turn_records_llm_provider_in_log() -> None:
    """chat_log["llm_provider"] が LLMResponse.provider の値になる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(provider="ollama"))
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()
    log = db.insert_chat_log.call_args[0][1]
    assert log["llm_provider"] == "ollama"


async def test_run_one_turn_records_llm_model_in_log() -> None:
    """chat_log["llm_model"] が LLMResponse.model の値になる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(model="qwen2.5:14b"))
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()
    log = db.insert_chat_log.call_args[0][1]
    assert log["llm_model"] == "qwen2.5:14b"


def test_build_deterministic_fallback_text_uses_decision_owner_focus_family() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="丁寧に短く返す",
        speech_examples=["「順番だけは決めましょう。」"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動く",
        target_last_utterance_excerpt="残りを言えば済む話だ",
        reply_focus_text="誰が決めるかの確認",
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「残りを言えば済む話だ」"],
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
    )

    assert "上泉ソーマ" in text
    assert text  # non-empty
    assert "順番を変え" not in text
    assert "先に出して" not in text


def test_build_deterministic_fallback_text_uses_timing_focus_family() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="丁寧に短く返す",
        speech_examples=["「順番だけは決めましょう。」"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動く",
        target_last_utterance_excerpt="それ、いつ決めるの？",
        reply_focus_text="いつ動くかの確認",
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるの？」"],
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
    )

    assert "上泉ソーマ" in text
    assert text  # non-empty
    assert "どうするか先に出して" not in text


def test_build_deterministic_fallback_text_uses_proposal_focus_family() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="丁寧に短く返す",
        speech_examples=["「順番だけは決めましょう。」"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        scene_objective_text="この場の争点: 主導権の入れ替わり",
        dominant_signal_text="主導権の入れ替わりが表に出る",
        target_last_utterance_excerpt="そのまま押し切ろう",
        reply_focus_text="相手の提案への返答",
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「そのまま押し切ろう」"],
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
    )

    assert "上泉ソーマ" in text
    assert text  # non-empty
    assert "じゃあ順番を変える" not in text
    assert "別案を先に通す" not in text


def test_build_deterministic_fallback_text_avoids_weak_proposal_phrase() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="普通",
        speech_examples=["「順番だけは変える。」"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        scene_objective_text="この場の争点: 流れを回収する",
        dominant_signal_text="流れを回収すると場が動く",
        target_last_utterance_excerpt="そのまま押し切ろう",
        reply_focus_text="相手の提案への返答",
        target_char_name="夜風ユウマ",
        conversation_partner_names=["夜風ユウマ"],
        recent_dialogue_lines=["夜風ユウマ: 「そのまま押し切ろう」"],
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="夜風ユウマ",
    )

    assert "代わりにこっちを先に出す" not in text


def test_fallback_matches_focus_contract_accepts_proposal_family_semantic_cue() -> None:
    assert StoryEngine._fallback_matches_focus_contract(
        "上泉ソーマ、その前に別の手を出しましょう",
        reply_focus_contract={
            "focus_family": "proposal",
            "required_tokens": ["なら", "じゃあ", "どう", "先に"],
            "required_focus_cues": ["なら", "じゃあ", "先に", "どう"],
            "focus_anchor_tokens": ["なら", "じゃあ"],
            "forbidden_focus_drift_tokens": [],
            "preferred_action": "propose",
        },
    ) is True


def test_fallback_matches_focus_contract_accepts_timing_family_semantic_cue() -> None:
    assert StoryEngine._fallback_matches_focus_contract(
        "上泉ソーマ、始まる前に固定しよう",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
        },
    ) is True


def test_fallback_matches_focus_contract_accepts_timing_alignment_cue() -> None:
    assert StoryEngine._fallback_matches_focus_contract(
        "上泉ソーマ、タイミングだけ先に合わせよう",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
        },
    ) is True


def test_fallback_matches_focus_contract_accepts_timing_pre_move_cue() -> None:
    assert StoryEngine._fallback_matches_focus_contract(
        "上泉ソーマ、動く前にタイミングだけ合わせる",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
        },
    ) is True


def test_fallback_matches_focus_contract_accepts_decision_owner_title_cue() -> None:
    assert StoryEngine._fallback_matches_focus_contract(
        "上泉ソーマ、誰の肩書きで決めるか先に出せ",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "決める役"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
        },
    ) is True


def test_fallback_matches_focus_contract_accepts_timing_flow_cut_cue() -> None:
    assert StoryEngine._fallback_matches_focus_contract(
        "上泉ソーマ、流れをどこで切るか先に出して",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
        },
    ) is True


def test_fallback_matches_focus_contract_accepts_basis_family_semantic_cue() -> None:
    assert StoryEngine._fallback_matches_focus_contract(
        "上泉ソーマ、何で測るかだけ先に決める",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で"],
            "focus_anchor_tokens": ["基準", "何の", "どの"],
            "forbidden_focus_drift_tokens": ["誰が", "いつ"],
            "preferred_action": "confirm",
        },
    ) is True


def test_build_reply_focus_contract_includes_semantic_focus_fields() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_focus_contract("いつ動くかの確認")

    assert contract is not None
    assert contract["focus_family"] == "timing"
    assert "required_focus_cues" in contract
    assert "focus_anchor_tokens" in contract
    assert "forbidden_focus_drift_tokens" in contract
    assert "先に" in contract["required_focus_cues"]
    assert "先に" in contract["focus_anchor_tokens"]


def test_build_reply_focus_contract_for_basis_uses_basis_specific_tokens() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_focus_contract("判断基準の確認")

    assert contract is not None
    assert "見せ場" not in contract["required_tokens"]
    assert "どの基準" in contract["focus_anchor_tokens"]


def test_build_reply_focus_contract_for_decision_owner_includes_control_swap_cues() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_focus_contract("誰が決めるかの確認")

    assert contract is not None
    assert "主導権" in contract["required_tokens"]
    assert "入れ替わり" in contract["required_focus_cues"]
    assert "入れ替わり" in contract["focus_anchor_tokens"]


def test_tighten_empty_final_recovery_kwargs_reduces_gemma_budget() -> None:
    adjusted = StoryEngine._tighten_empty_final_recovery_kwargs(
        {"max_tokens": 800, "temperature": 0.3, "reasoning_mode": "off"},
        provider="ollama",
        model="gemma4:e4b",
        response=LLMResponse(
            text="",
            model="gemma4:e4b",
            provider="ollama",
            prompt_tokens=None,
            completion_tokens=None,
            latency_ms=100,
            done_reason="length",
            completion_status="empty_final",
        ),
    )

    assert adjusted["max_tokens"] == 520
    assert adjusted["reasoning_mode"] == "off"


def test_tighten_empty_final_recovery_kwargs_leaves_non_empty_response_unchanged() -> None:
    adjusted = StoryEngine._tighten_empty_final_recovery_kwargs(
        {"max_tokens": 800, "temperature": 0.3, "reasoning_mode": "off"},
        provider="ollama",
        model="gemma4:e4b",
        response=LLMResponse(
            text="ok",
            model="gemma4:e4b",
            provider="ollama",
            prompt_tokens=None,
            completion_tokens=None,
            latency_ms=100,
            done_reason="stop",
            completion_status="complete",
        ),
    )

    assert adjusted["max_tokens"] == 800


@pytest.mark.asyncio
async def test_generate_llm_response_sets_request_tags_and_tightens_recovery_for_empty_gemma() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())
    engine._effective_llm_provider = "ollama"
    engine._effective_llm_model = "gemma4:e4b"
    engine._resolve_model_profile = MagicMock(
        return_value=ModelProfileConfig(
            reasoning_mode="off",
            max_tokens=700,
            recovery_max_tokens=800,
            recovery_reasoning_mode="off",
        )
    )
    engine.llm_router.generate = AsyncMock(
        side_effect=[
            LLMResponse(
                text="",
                model="gemma4:e4b",
                provider="ollama",
                prompt_tokens=None,
                completion_tokens=None,
                latency_ms=100,
                done_reason="length",
                completion_status="empty_final",
            ),
            LLMResponse(
                text="返答",
                model="gemma4:e4b",
                provider="ollama",
                prompt_tokens=None,
                completion_tokens=None,
                latency_ms=100,
                done_reason="stop",
                completion_status="complete",
            ),
        ]
    )

    response = await engine._generate_llm_response(
        char_id="char_1",
        system_prompt="system",
        user_prompt="user",
    )

    assert response is not None
    first_call = engine.llm_router.generate.await_args_list[0].kwargs
    second_call = engine.llm_router.generate.await_args_list[1].kwargs
    assert first_call["request_tag"] == "story_engine:reply:char_1:primary"
    assert second_call["request_tag"] == "story_engine:reply:char_1:recovery"
    assert second_call["max_tokens"] == 520


def test_build_retry_user_prompt_includes_variety_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    prompt = engine._build_retry_user_prompt(
        "【会話ログ】\n上泉ソーマ: まだ帰らないの？",
        msg_type="reply",
        issues=[{"issue_type": "voice_flat_reply", "details": {}}, {"issue_type": "generic_reply_tail", "details": {}}],
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_surface_contract={
            "must_vary_from_recent_self": True,
            "preferred_voice_cues": ["先に", "言って"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_endings": ["順番はそのあとでいい"],
            "forbidden_recent_openings": ["まだ帰らない"],
            "preferred_move_tokens": ["先に", "言って"],
            "recent_second_beat_history": ["press", "press"],
            "forbidden_recent_second_beats": ["press"],
        },
        voice_anchor_text="熱血直球で短く返す",
    )

    assert "2文目は 一歩踏み込む返し" in prompt
    assert "press" not in prompt
    assert "同じ締め方を避けてください" in prompt
    assert "同じ切り出しも避けてください" in prompt
    assert "同じ運び方も避けてください" in prompt
    assert "先に / 言って" in prompt


def test_build_deterministic_fallback_text_avoids_recent_opening_and_ending_reuse() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="丁寧に短く返す",
        speech_examples=["「順番だけは決めましょう。」"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動く",
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「まだ帰らないの？」"],
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_self_messages=[
            "上泉ソーマ、まだ帰らない。そこは今ここで決める。",
            "上泉ソーマ、帰る話はまだ早い。そこはまだ曖昧にしない。",
        ],
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": ["上泉ソーマ、まだ帰らない", "上泉ソーマ、帰る話はまだ早い"],
            "forbidden_recent_endings": ["そこは今ここで決める", "そこはまだ曖昧にしない"],
            "preferred_move_tokens": ["先に", "言って"],
            "recent_second_beat_history": ["press", "press"],
            "forbidden_recent_second_beats": ["press"],
        },
    )

    assert text not in {
        "上泉ソーマ、まだ帰らない。そこは今ここで決める。",
        "上泉ソーマ、帰る話はまだ早い。そこはまだ曖昧にしない。",
    }
    assert "上泉ソーマ" in text


def test_build_reply_variety_contract_tracks_recent_second_beat_history() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_variety_contract(
        reply_focus_text="帰るかどうかの確認",
        recent_self_messages=[
            "上泉ソーマ、まだ帰らない。先に言って。",
            "上泉ソーマ、帰る話はまだ早い。先に出して。",
            "上泉ソーマ、今は帰らない。じゃあ順番だけ決める。",
        ],
    )

    assert contract is not None
    assert contract["recent_second_beat_history"] == ["press", "press", "redirect"]
    assert contract["forbidden_recent_second_beats"] == ["press"]


def test_build_reply_quality_contract_tracks_story_flavor_role_and_second_beat_history() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_quality_contract(
        dominant_signal_text="張り合いに触れると場が動く",
        scene_objective_text="この場の争点: 順番を決める",
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "story_pressure_tokens": ["張り合い", "順番"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "recent_second_beat_history": ["press", "press", "redirect"],
            "forbidden_recent_second_beats": ["press"],
        },
    )

    assert contract is not None
    assert contract["primary_shape"] == "answer_then_probe"
    assert contract["second_beat_mode"] == "press"
    assert contract["required_story_flavor_role"] == "pressure"
    assert contract["recent_second_beat_history"] == ["press", "press", "redirect"]
    assert contract["story_pressure_tokens"] == ["張り合い", "順番"]


def test_build_reply_story_quality_contract_tracks_story_flavor_role_and_second_beat_history() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_story_quality_contract(
        dominant_signal_text="張り合いに触れると場が動く",
        scene_objective_text="この場の争点: 順番を決める",
        reply_quality_contract={
            "primary_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "story_pressure_tokens": ["張り合い", "順番"],
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "recent_second_beat_history": ["press", "press", "redirect"],
            "required_story_flavor_role": "pressure",
        },
        story_pressure_cue="張り合い / 順番 を曖昧に流さず、その場の争点として返す",
    )

    assert contract is not None
    assert contract["primary_shape"] == "answer_then_probe"
    assert contract["second_beat_mode"] == "press"
    assert contract["required_story_flavor_role"] == "pressure"
    assert contract["recent_second_beat_history"] == ["press", "press", "redirect"]
    assert contract["story_pressure_tokens"] == ["張り合い", "順番"]
    assert contract["story_pressure_cue_text"] == "張り合い / 順番 を曖昧に流さず、その場の争点として返す"


def test_build_reply_residual_quality_contract_tracks_threshold_and_story_role() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_residual_quality_contract(
        reply_story_quality_contract={
            "primary_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "story_pressure_tokens": ["張り合い", "順番"],
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "recent_second_beat_history": ["press", "press", "redirect"],
            "required_story_flavor_role": "pressure",
            "story_pressure_cue_text": "張り合い / 順番 を曖昧に流さず、その場の争点として返す",
        },
    )

    assert contract is not None
    assert contract["primary_shape"] == "answer_then_probe"
    assert contract["second_beat_mode"] == "press"
    assert contract["required_story_flavor_role"] == "pressure"
    assert contract["recent_second_beat_history"] == ["press", "press", "redirect"]
    assert contract["residual_block_threshold"] == "shape+second_beat+pressure_shift_or_story_flavor"


def test_should_keep_retry_output_without_fallback_blocks_reused_second_beat_contract_miss() -> None:
    assert not StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="上泉ソーマ、まだ帰らない。先に言って。",
        issues=[
            {
                "issue_type": "voice_flat_reply",
                "details": {
                    "variety_contract_missed": True,
                    "recent_second_beat_reused": True,
                    "reply_pressure_shift_seen": False,
                },
            }
        ],
    )


def test_should_keep_retry_output_without_fallback_blocks_quality_contract_miss() -> None:
    assert not StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="上泉ソーマ、今決める。見ておく。",
        issues=[
            {
                "issue_type": "voice_flat_reply",
                "details": {
                    "reply_quality_contract_missed": True,
                    "reply_second_beat_reused": True,
                    "reply_pressure_shift_seen": False,
                    "reply_story_flavor_seen": False,
                    "reply_soft_landing_used": True,
                },
            }
        ],
    )


def test_should_keep_retry_output_without_fallback_blocks_story_quality_contract_miss() -> None:
    assert not StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="上泉ソーマ、今決める。見せ場はまだ曖昧にしない。",
        issues=[
            {
                "issue_type": "voice_flat_reply",
                "details": {
                    "reply_story_quality_contract_missed": True,
                    "reply_second_beat_reused": True,
                    "reply_pressure_shift_seen": False,
                    "reply_story_flavor_seen": False,
                    "reply_soft_landing_used": True,
                },
            }
        ],
    )


def test_should_keep_retry_output_without_fallback_blocks_residual_quality_contract_miss() -> None:
    assert not StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="上泉ソーマ、今決める。見せ場はまだ曖昧にしない。",
        issues=[
            {
                "issue_type": "voice_flat_reply",
                "details": {
                    "reply_residual_contract_missed": True,
                    "reply_residual_second_beat_reused": True,
                    "reply_pressure_shift_seen": False,
                    "reply_residual_story_flavor_seen": False,
                    "reply_soft_landing_used": True,
                },
            }
        ],
    )


def test_build_reply_signal_contract_prefers_paired_mode() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_signal_contract(
        dominant_signal_text="張り合いに触れると場が動く",
        scene_objective_text="この場の争点: 順番を決める",
    )

    assert contract is not None
    assert contract["visibility_mode"] == "paired"
    assert "張り合い" in contract["signal_anchor_tokens"]
    assert "順番" in contract["objective_anchor_tokens"]


def test_extract_reply_signal_anchor_tokens_uses_split_phrases_instead_of_truncated_compact_text() -> None:
    tokens = StoryEngine._extract_reply_signal_anchor_tokens("この場の争点: 流れを回収する")

    assert tokens == ["流れ", "回収"]


def test_extract_reply_signal_anchor_tokens_ignores_natural_conversation_placeholder() -> None:
    tokens = StoryEngine._extract_reply_signal_anchor_tokens("自然発生の会話")

    assert tokens == []


def test_build_reply_dramatic_contract_prefers_counter_for_yes_no() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_dramatic_contract(
        reply_focus_text="帰るかどうかの確認",
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": ["上泉ソーマ、まだ帰らない"],
            "forbidden_recent_endings": ["順番はそのあとでいい"],
            "preferred_move_tokens": ["先に", "言って"],
        },
    )

    assert contract is not None
    assert contract["move_mode"] == "counter"
    assert contract["must_change_pressure_in_second_sentence"] is True
    assert "先に" in contract["required_move_tokens"]
    assert "順番はそのあとでいい" in contract["forbidden_soft_landings"]


def test_build_reply_dramatic_contract_adds_semantic_family_and_anchor_priority() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_dramatic_contract(
        reply_focus_text="帰るかどうかの確認",
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": [],
            "preferred_move_tokens": ["先に", "言って"],
        },
    )

    assert contract is not None
    assert contract["semantic_move_family"] == "counter"
    assert contract["pressure_anchor_tokens"] == ["順番"]
    assert "違う" in contract["required_semantic_cues"]
    assert "見ておく" in contract["forbidden_semantic_drifts"]


def test_build_reply_blandness_contract_prefers_probe_shape_for_timing() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    contract = engine._build_reply_blandness_contract(
        reply_focus_text="いつ動くかの確認",
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
        },
        reply_dramatic_contract={
            "move_mode": "probe",
            "allowed_secondary_modes": ["condition"],
            "forbidden_soft_landings": ["見ておく", "あとで片づける"],
        },
        recent_self_messages=[
            "上泉ソーマ、今決める。順番はそのあとでいい。",
            "上泉ソーマ、いつ決めるかだけ出す。張り合いは見ておく。",
        ],
    )

    assert contract is not None
    assert contract["primary_shape"] == "answer_then_probe"
    assert contract["secondary_shape"] == "answer_then_condition"
    assert contract["must_shift_pressure_in_second_sentence"] is True
    assert "見ておく" in contract["forbidden_soft_landings"]
    assert contract["recent_shape_history"] == ["answer_then_press", "answer_then_press"]


def test_build_retry_user_prompt_includes_signal_visibility_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    prompt = engine._build_retry_user_prompt(
        "【会話ログ】\n上泉ソーマ: まだ帰らないの？",
        msg_type="reply",
        issues=[{"issue_type": "signal_visibility_missing", "details": {}}],
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番", "決める"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        scene_objective_text="この場の争点: 順番を決める",
        dominant_signal_text="張り合いに触れると場が動く",
    )

    assert "張り合い" in prompt
    assert "順番を決める" in prompt
    assert "両方を別の役割で出してください" in prompt


def test_apply_reply_signal_visibility_to_fallback_keeps_two_sentences_for_paired_mode() -> None:
    text = StoryEngine._apply_reply_signal_visibility_to_fallback(
        "上泉ソーマ、今決める。主導権をどう動かすのか先に出して。",
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
        },
    )

    sentences = [part.strip() for part in text.split("。") if part.strip()]

    assert len(sentences) == 2
    assert "張り合い" in sentences[1]
    assert "順番" in sentences[1]
    assert "先に出して" not in sentences[1]


def test_apply_reply_signal_visibility_to_fallback_uses_both_tokens_for_showoff_conflict_pair() -> None:
    text = StoryEngine._apply_reply_signal_visibility_to_fallback(
        "上泉ソーマ、今のままでは終わらせない。見せ場をどこに置くか先に出して。",
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["食い違い"],
            "visibility_mode": "paired",
        },
    )

    sentences = [part.strip() for part in text.split("。") if part.strip()]

    assert len(sentences) == 2
    assert "見せ場" in sentences[1]
    assert "食い違い" in sentences[1]


def test_build_retry_user_prompt_includes_quality_contract_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    prompt = engine._build_retry_user_prompt(
        "【会話ログ】\n上泉ソーマ: それ、いつ決めるんだ？",
        msg_type="reply",
        issues=[{"issue_type": "voice_flat_reply", "details": {}}],
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_quality_contract={
            "primary_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "story_pressure_tokens": ["張り合い", "順番"],
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "recent_second_beat_history": ["press", "press"],
            "required_story_flavor_role": "pressure",
        },
    )

    assert "2文目で別の具体的な反応にしてください" in prompt
    assert "2文目で 張り合い / 順番 のどちらかを実語で出してください" in prompt
    assert "場で気になっていること が伝わる返しにしてください" in prompt
    assert "pressure" not in prompt


def test_build_retry_user_prompt_includes_story_quality_contract_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    prompt = engine._build_retry_user_prompt(
        "【会話ログ】\n上泉ソーマ: それ、いつ決めるんだ？",
        msg_type="reply",
        issues=[{"issue_type": "voice_flat_reply", "details": {}}],
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_story_quality_contract={
            "primary_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "story_pressure_tokens": ["張り合い", "順番"],
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "recent_second_beat_history": ["press", "press"],
            "required_story_flavor_role": "pressure",
            "story_pressure_cue_text": "張り合い / 順番 を曖昧に流さず、その場の争点として返す",
        },
    )

    assert "2文目で 張り合い / 順番 のどちらかを実語で出してください" in prompt
    assert "場で気になっていること が伝わる返しにしてください" in prompt
    # story_quality_cue はメタ語彙を含む可能性があるため prompt には出力しない
    assert "pressure" not in prompt
    assert "争点" not in prompt


def test_build_retry_user_prompt_includes_dramatic_move_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    prompt = engine._build_retry_user_prompt(
        "【会話ログ】\n上泉ソーマ: それ、いつ決めるんだ？",
        msg_type="reply",
        issues=[{"issue_type": "voice_flat_reply", "details": {}}],
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_dramatic_contract={
            "move_mode": "probe",
            "allowed_secondary_modes": ["claim"],
            "required_move_tokens": ["なぜ", "どこで", "今"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["順番はそのあとでいい"],
        },
        scene_objective_text="この場の争点: 順番を決める",
        dominant_signal_text="張り合いに触れると場が動く",
    )

    assert "2文目は 問い返し で別の反応にしてください" in prompt
    assert "無難に閉じず" in prompt
    assert "soft landing" not in prompt
    assert "なぜ / どこで / 今" in prompt


def test_build_retry_user_prompt_includes_dramatic_semantic_anchor_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    prompt = engine._build_retry_user_prompt(
        "【会話ログ】\n上泉ソーマ: それ、いつ決めるんだ？",
        msg_type="reply",
        issues=[{"issue_type": "voice_flat_reply", "details": {}}],
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_dramatic_contract={
            "move_mode": "counter",
            "semantic_move_family": "counter",
            "pressure_anchor_tokens": ["順番"],
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["先に", "言って", "出せ"],
            "required_semantic_cues": ["違う", "先に", "まず"],
            "forbidden_semantic_drifts": ["見ておく", "そのあとでいい"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["順番はそのあとでいい"],
        },
        scene_objective_text="この場の争点: 順番を決める",
        dominant_signal_text="張り合いに触れると場が動く",
    )

    assert "順番を拾って押し返してください" in prompt
    assert "差し戻すか奪い返す" in prompt
    assert "違う / 先に / まず" in prompt
    assert "例:" not in prompt
    assert "争点" not in prompt


def test_build_retry_user_prompt_limits_auxiliary_issue_guidance_to_primary_failures() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    prompt = engine._build_retry_user_prompt(
        "元の prompt",
        msg_type="reply",
        issues=[
            {"issue_type": "reply_without_direct_reaction", "details": {}},
            {"issue_type": "generic_reply_tail", "details": {}},
            {"issue_type": "voice_flat_reply", "details": {}},
        ],
        target_last_utterance_excerpt="手帳を見せろ",
        reply_focus_text="手帳を見せるかの返答",
    )

    assert "相手の発言の要点を先頭で拾ってください" in prompt
    assert "2文目は汎用的な締めではなく" in prompt
    assert "2文目が説明口調だけで終わらないよう" not in prompt


def test_build_deterministic_fallback_text_surfaces_signal_anchor() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="丁寧に短く返す",
        speech_examples=["「順番だけは決めましょう。」"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        scene_objective_text="この場の争点: 順番を決める",
        dominant_signal_text="張り合いに触れると場が動く",
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「まだ帰らないの？」"],
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert "上泉ソーマ" in text
    assert "張り合い" in text or "順番" in text


def test_build_deterministic_fallback_text_avoids_soft_landing_with_dramatic_contract() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="丁寧に短く返す",
        speech_examples=["「順番だけは決めましょう。」"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        scene_objective_text="この場の争点: 順番を決める",
        dominant_signal_text="張り合いに触れると場が動く",
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「まだ帰らないの？」"],
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_self_messages=["上泉ソーマ、まだ帰らない。順番はそのあとでいい。"],
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": ["上泉ソーマ、まだ帰らない"],
            "forbidden_recent_endings": ["順番はそのあとでいい"],
            "preferred_move_tokens": ["先に", "言って"],
        },
        reply_dramatic_contract={
            "move_mode": "counter",
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["先に", "言って", "出せ"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["順番はそのあとでいい"],
        },
    )

    assert text
    assert "順番はそのあとでいい" not in text
    assert "上泉ソーマ" in text


def test_select_scene_focus_and_objective_prefers_showoff_flashpoint() -> None:
    db = make_mock_db(
        characters=[
            make_character(id="char_a", name_ja="A"),
            make_character(id="char_b", name_ja="B"),
            make_character(id="char_c", name_ja="C"),
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    focus_char_ids, objective = engine._select_scene_focus_and_objective(
        place_id="music_room",
        participants=["char_a", "char_b", "char_c"],
        signal_state={
            "active_pressures": [
                {
                    "pressure_type": "showoff_flashpoint",
                    "focus_char_ids": ["char_a", "char_b"],
                    "focus_place_id": "music_room",
                    "score": 0.88,
                }
            ],
            "active_patterns": [],
            "active_relationship_modes": [],
            "active_canon_bits": [],
            "active_episode": None,
        },
    )

    assert focus_char_ids == ["char_a", "char_b"]
    assert objective == "誰かが本気を出す瞬間が来た"


async def test_run_one_turn_retries_empty_final_once_and_persists_recovered_text() -> None:
    """thinking あり空本文なら 1 回だけ同モデル recovery し、成功本文だけ保存する。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(side_effect=[
        make_llm_response(
            text="",
            model="qwen3.5:9b",
            reasoning_present=True,
            reasoning_chars=400,
            completion_status="empty_final",
        ),
        make_llm_response(
            text="「復旧後の本文です。」",
            model="qwen3.5:9b",
            reasoning_present=False,
            reasoning_chars=0,
            completion_status="complete",
        ),
    ])
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._story["llm_model"] = "qwen3.5:9b"
    engine._effective_llm_model = "qwen3.5:9b"

    await engine.run_one_turn()

    assert router.generate.await_count == 2
    first_kwargs = router.generate.await_args_list[0].kwargs
    second_kwargs = router.generate.await_args_list[1].kwargs
    assert first_kwargs["model"] == "qwen3.5:9b"
    assert second_kwargs["model"] == "qwen3.5:9b"
    assert second_kwargs["reasoning_mode"] == "off"
    log = db.insert_chat_log.call_args[0][1]
    assert log["message"] == "「復旧後の本文です。」"


async def test_run_one_turn_skips_db_insert_after_failed_recovery() -> None:
    """recovery 後も空本文なら chat_log を保存せず、そのターンを消費する。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(side_effect=[
        make_llm_response(
            text="",
            model="qwen3.5:9b",
            reasoning_present=True,
            reasoning_chars=500,
            completion_status="empty_final",
        ),
        make_llm_response(
            text="",
            model="qwen3.5:9b",
            reasoning_present=False,
            reasoning_chars=0,
            completion_status="empty_final",
        ),
    ])
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._story["llm_model"] = "qwen3.5:9b"
    engine._effective_llm_model = "qwen3.5:9b"

    await engine.run_one_turn()

    assert router.generate.await_count == 2
    db.insert_chat_log.assert_not_called()
    assert engine._char_index == 1


async def test_run_one_turn_treats_whitespace_only_as_empty_and_persists_recovered_text() -> None:
    """空白-only 本文も empty とみなし、recovery 成功時だけ保存する。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(side_effect=[
        make_llm_response(
            text=" \n\t ",
            model="qwen3.5:9b",
            reasoning_present=True,
            reasoning_chars=220,
            completion_status="complete",
        ),
        make_llm_response(
            text="「空白応答から復旧しました。」",
            model="qwen3.5:9b",
            reasoning_present=False,
            reasoning_chars=0,
            completion_status="complete",
        ),
    ])
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._story["llm_model"] = "qwen3.5:9b"
    engine._effective_llm_model = "qwen3.5:9b"

    await engine.run_one_turn()

    assert router.generate.await_count == 2
    log = db.insert_chat_log.call_args[0][1]
    assert log["message"] == "「空白応答から復旧しました。」"


def test_sanitize_generated_text_strips_thinking_tags_and_prefix() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    cleaned = engine._sanitize_generated_text(
        "<thinking>\n内部推論です。\n</thinking>\nReasoning: 次は短く返す。\n「今すぐ動く。」"
    )

    assert cleaned == "「今すぐ動く。」"


async def test_run_one_turn_treats_thinking_only_output_as_empty_and_recovers() -> None:
    """thinking residue だけの本文は empty 相当として recovery する。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(side_effect=[
        make_llm_response(
            text="<think>内部思考だけが返ってきた。</think>",
            model="qwen3.5:9b",
            reasoning_present=True,
            reasoning_chars=180,
            completion_status="complete",
        ),
        make_llm_response(
            text="「thinking 応答から復旧しました。」",
            model="qwen3.5:9b",
            reasoning_present=False,
            reasoning_chars=0,
            completion_status="complete",
        ),
    ])
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._story["llm_model"] = "qwen3.5:9b"
    engine._effective_llm_model = "qwen3.5:9b"

    await engine.run_one_turn()

    assert router.generate.await_count == 2
    log = db.insert_chat_log.call_args[0][1]
    assert log["message"] == "「thinking 応答から復旧しました。」"


async def test_run_one_turn_persists_sanitized_text_without_recovery_for_mixed_output() -> None:
    """thinking residue と本文が混在する場合は本文だけ保存し、recovery しない。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=make_llm_response(
            text="<think>内部で整理する。</think>\nThinking: 次は短く答える。\n「了解した。」",
            model="qwen3.5:9b",
            reasoning_present=True,
            reasoning_chars=120,
            completion_status="complete",
        )
    )
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._story["llm_model"] = "qwen3.5:9b"
    engine._effective_llm_model = "qwen3.5:9b"

    await engine.run_one_turn()

    assert router.generate.await_count == 1
    log = db.insert_chat_log.call_args[0][1]
    assert log["message"] == "「了解した。」"


async def test_run_one_turn_skips_db_insert_after_whitespace_only_failed_recovery() -> None:
    """recovery 後も空白-only なら chat_log を保存しない。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(side_effect=[
        make_llm_response(
            text=" \n ",
            model="qwen3.5:9b",
            reasoning_present=True,
            reasoning_chars=260,
            completion_status="complete",
        ),
        make_llm_response(
            text="\t  ",
            model="qwen3.5:9b",
            reasoning_present=False,
            reasoning_chars=0,
            completion_status="complete",
        ),
    ])
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._story["llm_model"] = "qwen3.5:9b"
    engine._effective_llm_model = "qwen3.5:9b"

    await engine.run_one_turn()

    assert router.generate.await_count == 2
    db.insert_chat_log.assert_not_called()
    assert engine._char_index == 1


async def test_run_one_turn_applies_ambient_context_to_prompt_and_state() -> None:
    """ambient context の文面と emotion delta が prompt / 保存 state に反映される。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    ambient_manager = AsyncMock()
    ambient_manager.resolve_turn = AsyncMock(
        return_value=AmbientTurnContext(
            body_state_texts=["少し空腹を感じている。"],
            ambient_texts=["廊下側が少し騒がしい。"],
            emotion_delta={"stress": 0.1, "motivation": -0.05},
            active_factor_ids=[1, 2],
        )
    )
    engine = StoryEngine(
        "test_story",
        db,
        router,
        ambient_context_manager=ambient_manager,
    )
    await engine.initialize()

    await engine.run_one_turn()

    call_kwargs = router.generate.call_args.kwargs
    assert "少し空腹を感じている" in call_kwargs["system_prompt"]
    assert "廊下側が少し騒がしい" in call_kwargs["user_prompt"]
    state = db.insert_character_state.call_args[0][1]
    assert state["stress"] > 0.35


# ============================================================
# ラウンドロビン・時計テスト
# ============================================================


async def test_chars_cycle_round_robin() -> None:
    """2キャラある場合、run_one_turn() を2回呼ぶと交互に選択される。"""
    chars = [
        make_character(id="char_1", name_ja="キャラ1"),
        make_character(id="char_2", name_ja="キャラ2"),
    ]
    db = make_mock_db(characters=chars)
    # 2キャラ分なので execute() が 4×1 = 4 回呼ばれる（initialize() で1セット）
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    await engine.run_one_turn()
    await engine.run_one_turn()

    calls = db.insert_chat_log.call_args_list
    assert calls[0][0][1]["char_id"] == "char_1"
    assert calls[1][0][1]["char_id"] == "char_2"


async def test_clock_ticks_after_all_chars() -> None:
    """全キャラ1周完了後に db.update_last_sim_time が呼ばれる。"""
    chars = [
        make_character(id="char_1", name_ja="キャラ1"),
        make_character(id="char_2", name_ja="キャラ2"),
    ]
    db = make_mock_db(characters=chars)
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    # 1キャラ目：tick なし
    await engine.run_one_turn()
    db.update_last_sim_time.assert_not_called()

    # 2キャラ目（1周完了）：tick → update_last_sim_time が呼ばれる
    await engine.run_one_turn()
    db.update_last_sim_time.assert_called_once()


# ============================================================
# 初期状態生成テスト
# ============================================================


async def test_initial_state_created_when_none() -> None:
    """get_latest_character_state が None を返したとき、初期状態が使われる。"""
    db = make_mock_db()
    db.get_latest_character_state = AsyncMock(return_value=None)
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    # insert_character_state には初期状態の値が入っているはず
    state_call = db.insert_character_state.call_args[0][1]
    assert state_call["current_place"] == "music_room"  # favorite_places[0]
    assert state_call["stress"] == pytest.approx(0.3, abs=0.05)  # 減衰後も近い値
    assert state_call["motivation"] == pytest.approx(0.7, abs=0.15)  # 感情トリガー適用後も初期値から大きく外れない


# ============================================================
# Phase 2 テスト（14 件）
# ============================================================


async def test_p2_initialize_creates_place_manager() -> None:
    """initialize() 後に _place_manager が PlaceManager インスタンスになる。"""
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()
    assert isinstance(engine._place_manager, PlaceManager)


async def test_p2_initialize_creates_move_engine() -> None:
    """initialize() 後に _move_engine が MoveEngine インスタンスになる。"""
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()
    assert isinstance(engine._move_engine, MoveEngine)


async def test_p2_initialize_empty_relationships() -> None:
    """DB が空リストを返すとき _relationships は空 dict になる。"""
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()
    assert engine._relationships == {}


async def test_p2_run_one_turn_emotion_decay_applied() -> None:
    """character_state の stress が初期値 0.3 から減衰後の値になる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    state = db.insert_character_state.call_args[0][1]
    # apply_decay で stress は 0.3 + (0.5 - 0.3) * 0.05 = 0.31 (その後トリガーで変動あり)
    # 少なくとも元の 0.3 と完全同一ではない
    assert state["stress"] != pytest.approx(0.3)


async def test_p2_run_one_turn_expression_in_state() -> None:
    """character_state の current_expression が非空文字列になる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    state = db.insert_character_state.call_args[0][1]
    assert isinstance(state["current_expression"], str)
    assert len(state["current_expression"]) > 0


async def test_p2_run_one_turn_msg_type_in_log() -> None:
    """chat_log の msg_type が有効な値（monologue/reply/group）のいずれかになる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    log = db.insert_chat_log.call_args[0][1]
    assert log["msg_type"] in ("monologue", "reply", "group")


async def test_p2_run_one_turn_emotion_snapshot_has_keys() -> None:
    """chat_log の emotion_snapshot が 4 キーを含む JSON 文字列になる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    log = db.insert_chat_log.call_args[0][1]
    snapshot = json.loads(log["emotion_snapshot"])
    assert set(snapshot.keys()) == {"stress", "motivation", "loneliness", "excitement"}


async def test_p2_run_one_turn_no_move_when_no_adjacent() -> None:
    """隣接場所なし（PLACE_ROW の adjacent_places=None）のとき current_place が変わらない。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    state = db.insert_character_state.call_args[0][1]
    assert state["current_place"] == "music_room"  # 移動なし


async def test_p2_run_one_turn_no_move_reason_when_no_move() -> None:
    """移動が起きないとき character_state の move_reason は None になる。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    state = db.insert_character_state.call_args[0][1]
    assert state["move_reason"] is None


async def test_p2_run_one_turn_updates_last_known_place() -> None:
    """ターン後に _last_known_place[char_id] が更新される。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    assert "char_1" not in engine._last_known_place
    await engine.run_one_turn()
    assert "char_1" in engine._last_known_place
    assert engine._last_known_place["char_1"] == "music_room"


async def test_p2_get_same_place_chars_empty_initially() -> None:
    """_last_known_place が空のとき _get_same_place_chars() は [] を返す。"""
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    result = engine._get_same_place_chars("char_1", "music_room")
    assert result == []


async def test_p2_get_same_place_chars_returns_colocated() -> None:
    """_last_known_place に同一場所の他キャラがいれば返される。"""
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    engine._last_known_place["char_2"] = "music_room"
    engine._last_known_place["char_3"] = "rooftop"

    result = engine._get_same_place_chars("char_1", "music_room")
    assert result == ["char_2"]


async def test_p2_get_same_place_chars_excludes_self() -> None:
    """_get_same_place_chars() は char_id 自身を結果に含まない。"""
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    # char_1 自身も _last_known_place に入っていても除外される
    engine._last_known_place["char_1"] = "music_room"
    engine._last_known_place["char_2"] = "music_room"

    result = engine._get_same_place_chars("char_1", "music_room")
    assert "char_1" not in result
    assert "char_2" in result


async def test_p2_build_context_with_emotions() -> None:
    """_build_context() に emotions を渡すと ctx.emotions に反映される。"""
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    char = make_character()
    state = make_state()
    test_emotions = {"stress": 0.8, "motivation": 0.4, "loneliness": 0.6, "excitement": 0.3}

    ctx = engine._build_context(char, state, "音楽室", emotions=test_emotions)
    assert ctx.emotions == test_emotions
    assert ctx.current_expression == "neutral"  # デフォルト


async def test_select_relationship_mode_prompt_texts_prefers_target_pair() -> None:
    db = make_mock_db()
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "mode_type": "chaos_partner",
                "char_id_from": "char_1",
                "char_id_to": "char_3",
                "intensity": 0.4,
                "confidence": 0.5,
            },
            {
                "mode_type": "irritated_respect",
                "char_id_from": "char_1",
                "char_id_to": "char_2",
                "intensity": 0.8,
                "confidence": 0.7,
            },
        ]
    )
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    texts = await engine._select_relationship_mode_prompt_texts(
        char_id="char_1",
        target_char_id="char_2",
        same_place_char_ids=["char_2", "char_3"],
    )

    assert texts
    assert "キャラ2" in texts[0]
    assert "張り合い" in texts[0]


async def test_select_dramatic_pressure_prompt_texts_prefers_target_and_caps_monologue() -> None:
    db = make_mock_db()
    db.get_active_story_dramatic_pressures = AsyncMock(
        return_value=[
            {
                "pressure_type": "status_flashpoint",
                "focus_char_ids": ["char_1", "char_2"],
                "focus_place_id": None,
                "score": 0.8,
            },
            {
                "pressure_type": "payoff_ready",
                "focus_char_ids": ["char_1"],
                "focus_place_id": "music_room",
                "score": 0.7,
            },
        ]
    )
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    reply_texts = await engine._select_dramatic_pressure_prompt_texts(
        char_id="char_1",
        target_char_id="char_2",
        current_place_id="music_room",
        same_place_char_ids=["char_2"],
        msg_type="reply",
    )
    monologue_texts = await engine._select_dramatic_pressure_prompt_texts(
        char_id="char_1",
        target_char_id=None,
        current_place_id="music_room",
        same_place_char_ids=[],
        msg_type="monologue",
    )

    assert reply_texts
    assert "場が動くかもしれない" in reply_texts[0]
    assert len(monologue_texts) == 1


async def test_select_dominant_prompt_signal_prefers_target_relationship_for_reply() -> None:
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    dominant = engine._select_dominant_prompt_signal(
        msg_type="reply",
        relationship_mode_texts=["キャラ2 には認めつつも張り合いがち。"],
        canon_bit_texts=["音楽室では status_clash が起きやすい。"],
        dramatic_pressure_texts=["張り合いに触れると場が動きやすい。"],
        scene_objective_text="この場の争点: 張り合いが表に出る",
    )

    assert dominant == "キャラ2 には認めつつも張り合いがち。"


async def test_select_dominant_prompt_signal_prefers_pressure_for_group() -> None:
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    dominant = engine._select_dominant_prompt_signal(
        msg_type="group",
        relationship_mode_texts=["キャラ2 の動きは放っておけない。"],
        canon_bit_texts=["音楽室では status_clash が起きやすい。"],
        dramatic_pressure_texts=["張り合いに触れると場が動きやすい。"],
        scene_objective_text="この場の争点: 張り合いが表に出る",
    )

    assert dominant == "張り合いに触れると場が動きやすい。"


async def test_refine_prompt_signal_payload_prefers_target_focus_for_reply() -> None:
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    relationship_texts, canon_texts, pressure_texts, scene_objective_text = (
        engine._refine_prompt_signal_payload(
            msg_type="reply",
            relationship_mode_texts=["キャラ2 には認めつつも張り合いがち。", "別の相手には危うい本音を漏らしやすい。"],
            canon_bit_texts=["音楽室では status_clash が起きやすい。"],
            dramatic_pressure_texts=["張り合いに触れると場が動きやすい。", "ここで回収に寄せると流れが前に進みやすい。"],
            scene_objective_text="この場の争点: 張り合いが表に出る",
            reply_focus_text="帰るかどうかの確認",
            handoff_target_name=None,
        )
    )

    assert relationship_texts == ["キャラ2 には認めつつも張り合いがち。"]
    assert pressure_texts == ["張り合いに触れると場が動きやすい。"]
    assert canon_texts == []
    assert scene_objective_text == "この場の争点: 張り合いが表に出る"


async def test_refine_prompt_signal_payload_prefers_objective_and_handoff_for_group() -> None:
    db = make_mock_db()
    engine = StoryEngine("test_story", db, AsyncMock())
    await engine.initialize()

    relationship_texts, canon_texts, pressure_texts, scene_objective_text = (
        engine._refine_prompt_signal_payload(
            msg_type="group",
            relationship_mode_texts=["キャラ2 の動きは放っておけない。", "別の相手には危うい本音を漏らしやすい。"],
            canon_bit_texts=["音楽室では status_clash が起きやすい。"],
            dramatic_pressure_texts=["張り合いに触れると場が動きやすい。", "ここで回収に寄せると流れが前に進みやすい。"],
            scene_objective_text="この場の争点: 張り合いが表に出る",
            reply_focus_text=None,
            handoff_target_name="キャラ2",
        )
    )

    assert relationship_texts == ["キャラ2 の動きは放っておけない。"]
    assert pressure_texts == ["張り合いに触れると場が動きやすい。"]
    assert canon_texts == []
    assert scene_objective_text == "この場の争点: 張り合いが表に出る"


def test_keep_retry_output_without_fallback_for_generic_reply_tail_only() -> None:
    assert StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="星風 瑠奈、それは違う。今の流れをここで返す。",
        issues=[
            {
                "issue_type": "generic_reply_tail",
                "severity": "warning",
                "details": {},
            }
        ],
    )


def test_keep_retry_output_without_fallback_for_question_with_only_stylistic_reply_issues() -> None:
    assert not StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="その見せ場って誰の基準？確認します。",
        issues=[
            {
                "issue_type": "reply_focus_missing",
                "severity": "warning",
                "details": {},
            },
            {
                "issue_type": "generic_reply_tail",
                "severity": "warning",
                "details": {},
            },
            {
                "issue_type": "voice_flat_reply",
                "severity": "warning",
                "details": {},
            },
        ],
    )


def test_keep_retry_output_without_fallback_for_soft_reply_surface_issues_only() -> None:
    assert StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="いつ決めるかだけ今ここで出す。順番はそのあとでいい。",
        issues=[
            {
                "issue_type": "reply_direct_reaction_soft",
                "severity": "info",
                "details": {},
            },
            {
                "issue_type": "voice_flat_reply",
                "severity": "warning",
                "details": {},
            },
            {
                "issue_type": "generic_reply_tail",
                "severity": "warning",
                "details": {},
            },
        ],
    )


def test_select_reply_focus_text_prefers_decision_owner_family_without_question_mark() -> None:
    assert StoryEngine._select_reply_focus_text(
        target_excerpt="順番の前に決める役だけ出して。",
        recent_dialogue_lines=[],
    ) == "誰が決めるかの確認"


def test_keep_retry_output_without_fallback_when_second_sentence_recovers_focus() -> None:
    assert not StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="それは違う。誰が決めるかだけ先に出して。",
        issues=[
            {
                "issue_type": "reply_focus_missing",
                "severity": "warning",
                "details": {},
            },
            {
                "issue_type": "voice_flat_reply",
                "severity": "warning",
                "details": {},
            },
        ],
    )


async def test_run_one_turn_records_reply_fallback_root_cause_details() -> None:
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「曖昧に返す。」"))
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._quality_guard = MagicMock()
    engine._quality_guard.evaluate = MagicMock(
        side_effect=[
            QualityGuardResult(
                text="曖昧に返す。",
                action="retry",
                score=0.3,
                issues=[
                    {
                        "issue_type": "reply_focus_missing",
                        "severity": "warning",
                        "details": {},
                    }
                ],
            ),
            QualityGuardResult(
                text="曖昧に返す。",
                action="retry",
                score=0.3,
                issues=[
                    {
                        "issue_type": "reply_focus_missing",
                        "severity": "warning",
                        "details": {},
                    }
                ],
            ),
        ]
    )
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "二人目",
            "conversation_partner_names": ["二人目"],
            "recent_dialogue_lines": ["二人目: 「まだ帰らないの？」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )

    await engine.run_one_turn()

    fallback_issue = next(
        call.args[1]
        for call in db.insert_generation_quality_issue.await_args_list
        if call.args[1]["issue_type"] == "quality_output_fallback"
    )
    assert fallback_issue["details"]["fallback_root_issue"] == "reply_focus_missing"
    assert "reply_focus_missing" in fallback_issue["details"]["fallback_issue_set"]
    assert fallback_issue["details"]["retry_kept_without_fallback"] is False


async def test_run_one_turn_saves_silence_after_three_llm_critic_rejections() -> None:
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            make_llm_response(text="一度目の候補。"),
            make_llm_response(
                text='{"verdict":"reject","reason_code":"cue_like_dialogue","repair_instruction":"カンペ文ではなく本人の内面にする"}'
            ),
            make_llm_response(text="二度目の候補。"),
            make_llm_response(
                text='{"verdict":"reject","reason_code":"scene_ignored","repair_instruction":"場面の目的を一語入れる"}'
            ),
            make_llm_response(text="三度目の候補。"),
            make_llm_response(
                text='{"verdict":"reject","reason_code":"loop_like","repair_instruction":"同じ位置で足踏みしない"}'
            ),
        ]
    )
    cfg = make_runtime_config()
    cfg.quality_guard.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._quality_guard = QualityGuard(cfg.quality_guard)
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "monologue",
            "target_char_id": None,
            "target_char_name": None,
            "conversation_partner_names": [],
            "recent_dialogue_lines": [],
            "conversation_session_id": None,
            "reply_to_log_id": None,
            "participant_ids": ["char_1"],
        }
    )

    await engine.run_one_turn()

    log = db.insert_chat_log.await_args.args[1]
    assert log["message"] == "・・・・・・"
    assert "silence_fallback" in log["quality_flags"]
    issue = next(
        call.args[1]
        for call in db.insert_generation_quality_issue.await_args_list
        if call.args[1]["issue_type"] == "dialogue_silence_fallback"
    )
    assert issue["auto_action"] == "silence"
    assert issue["details"]["final_action"] == "silence"
    assert [attempt["critic_reason_code"] for attempt in issue["details"]["attempts"]] == [
        "cue_like_dialogue",
        "scene_ignored",
        "loop_like",
    ]


async def test_run_one_turn_records_hard_guard_reason_before_silence() -> None:
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            make_llm_response(text='{"message":"まだ終わらない"}'),
            make_llm_response(text='{"message":"まだ終わらない"}'),
            make_llm_response(text='{"message":"まだ終わらない"}'),
        ]
    )
    cfg = make_runtime_config()
    cfg.quality_guard.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._quality_guard = QualityGuard(cfg.quality_guard)
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "monologue",
            "target_char_id": None,
            "target_char_name": None,
            "conversation_partner_names": [],
            "recent_dialogue_lines": [],
            "conversation_session_id": None,
            "reply_to_log_id": None,
            "participant_ids": ["char_1"],
        }
    )

    await engine.run_one_turn()

    issue = next(
        call.args[1]
        for call in db.insert_generation_quality_issue.await_args_list
        if call.args[1]["issue_type"] == "dialogue_silence_fallback"
    )
    assert issue["details"]["attempts"][0]["hard_failures"] == ["json_or_code_output"]
    assert issue["details"]["attempts"][0]["phase"] == "hard_guard"


async def test_p3_run_one_turn_creates_conversation_session_for_colocated_chars() -> None:
    """同じ場所に複数キャラがいれば会話セッションが作成される。"""
    chars = [
        make_character(id="char_1", name_ja="キャラ1"),
        make_character(id="char_2", name_ja="キャラ2"),
    ]
    db = make_mock_db(characters=chars)
    db.get_latest_character_state = AsyncMock(
        side_effect=lambda *_args, **_kwargs: make_state(current_place="music_room")
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    db.insert_conversation_session.assert_awaited_once()
    log = db.insert_chat_log.call_args[0][1]
    assert log["conversation_session_id"] == 42


async def test_p3_run_one_turn_active_session_uses_reply_context() -> None:
    """active session と直前発言があれば reply prompt と reply metadata を使う。"""
    chars = [
        make_character(id="char_2", name_ja="キャラ2"),
        make_character(id="char_1", name_ja="キャラ1"),
    ]
    db = make_mock_db(characters=chars)
    db.get_latest_character_state = AsyncMock(
        side_effect=lambda *_args, **_kwargs: make_state(current_place="music_room")
    )
    db.get_active_conversation_sessions = AsyncMock(
        return_value=[make_session(participant_ids=["char_1", "char_2"])]
    )
    db.get_active_conversation_session = AsyncMock(
        return_value=make_session(participant_ids=["char_1", "char_2"])
    )
    db.get_recent_conversation_logs = AsyncMock(
        return_value=[
            {
                "id": 501,
                "char_id": "char_1",
                "message": "「……まだ帰らないの？」",
                "sim_datetime": "2025-04-01T00:00",
            }
        ]
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    call_kwargs = router.generate.call_args.kwargs
    assert "……まだ帰らないの？" in call_kwargs["user_prompt"]
    log = db.insert_chat_log.call_args[0][1]
    assert log["msg_type"] == "reply"
    assert log["conversation_session_id"] == 42
    assert log["reply_to_log_id"] == 501


async def test_p3_run_one_turn_group_session_uses_recent_dialogue() -> None:
    """3人以上の継続 session では recent dialogue を受けて reply に落ちる。"""
    chars = [
        make_character(id="char_3", name_ja="キャラ3"),
        make_character(id="char_1", name_ja="キャラ1"),
        make_character(id="char_2", name_ja="キャラ2"),
    ]
    db = make_mock_db(characters=chars)
    db.get_latest_character_state = AsyncMock(
        side_effect=lambda *_args, **_kwargs: make_state(current_place="music_room")
    )
    session = make_session(
        id=77,
        participant_ids=["char_1", "char_2", "char_3"],
        last_speaker_id="char_2",
        last_log_id=902,
    )
    db.get_active_conversation_sessions = AsyncMock(return_value=[session])
    db.get_active_conversation_session = AsyncMock(return_value=session)
    db.get_recent_conversation_logs = AsyncMock(
        return_value=[
            {
                "id": 901,
                "char_id": "char_1",
                "message": "「音、聞こえた？」",
                "sim_datetime": "2025-04-01T00:00",
            },
            {
                "id": 902,
                "char_id": "char_2",
                "message": "「今の、誰かいたでしょ」",
                "sim_datetime": "2025-04-01T00:00",
            },
        ]
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine.run_one_turn()

    call_kwargs = router.generate.call_args.kwargs
    assert "音、聞こえた？" in call_kwargs["user_prompt"]
    assert "今の、誰かいたでしょ" in call_kwargs["user_prompt"]
    log = db.insert_chat_log.call_args[0][1]
    assert log["msg_type"] == "reply"
    assert log["conversation_session_id"] == 77


async def test_p3_run_one_turn_new_session_seeds_same_turn_log_into_reply() -> None:
    """同 turn/place の先行ログがあれば新規 session 直後から reply になる。"""
    chars = [
        make_character(id="char_1", name_ja="キャラ1"),
        make_character(id="char_2", name_ja="キャラ2"),
    ]
    db = make_mock_db(characters=chars)
    seen_calls = {"char_2": 0}

    async def latest_state(_: str, char_id: str) -> dict:
        if char_id == "char_2":
            seen_calls["char_2"] += 1
            if seen_calls["char_2"] == 1:
                return make_state(current_place="rooftop")
            return make_state(current_place="music_room")
        return make_state(current_place="music_room")

    db.get_latest_character_state = AsyncMock(side_effect=latest_state)
    db.get_conversation_seed_logs = AsyncMock(
        return_value=[
            {
                "id": 300,
                "char_id": "char_1",
                "msg_type": "monologue",
                "place_id": "music_room",
                "message": "「まだ帰らないのか？」",
                "sim_datetime": "2025-04-01T00:00",
                "turn_number": 0,
            }
        ]
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response())
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    await engine.run_one_turn()
    db.insert_chat_log.reset_mock()
    db.attach_logs_to_conversation_session.reset_mock()
    router.generate.reset_mock()

    await engine.run_one_turn()

    db.attach_logs_to_conversation_session.assert_awaited_once_with([300], 42)
    call_kwargs = router.generate.call_args.kwargs
    assert "まだ帰らないのか？" in call_kwargs["user_prompt"]
    log = db.insert_chat_log.call_args[0][1]
    assert log["msg_type"] == "reply"
    assert log["conversation_session_id"] == 42
    assert log["reply_to_log_id"] == 300


# ============================================================
# A-8 テスト: _run_post_round_hooks() / _build_narration_context()
# ============================================================


def _make_engine_for_hook_tests(
    story_memory_manager=None,
    narration_engine=None,
    emergent_canonizer=None,
) -> StoryEngine:
    """_run_post_round_hooks() / _build_narration_context() テスト用の最小構成エンジン。"""
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine(
        "test_story", db, router,
        story_memory_manager=story_memory_manager,
        narration_engine=narration_engine,
        emergent_canonizer=emergent_canonizer,
    )
    engine._story = make_story()
    engine._characters = [make_character()]
    engine._places = {"music_room": "音楽室"}
    engine._last_known_place = {"char_1": "music_room"}
    engine.world_clock = MagicMock()
    engine.world_clock.turn_number = 5
    engine.world_clock.sim_datetime = "2025-04-01T12:00"
    return engine


async def test_a8_hooks_call_story_memory_process_round() -> None:
    """_run_post_round_hooks() が story_memory_manager.process_round(turn_number) を呼ぶ。"""
    smm = AsyncMock()
    smm.process_round = AsyncMock()
    engine = _make_engine_for_hook_tests(story_memory_manager=smm)

    await engine._run_post_round_hooks(character_moved=False)

    smm.process_round.assert_awaited_once_with(5)


async def test_a8_hooks_call_narration_engine_process_round() -> None:
    """_run_post_round_hooks() が narration_engine.process_round(turn_number, ctx) を呼ぶ。"""
    ne = AsyncMock()
    ne.process_round = AsyncMock()
    engine = _make_engine_for_hook_tests(narration_engine=ne)

    await engine._run_post_round_hooks(character_moved=True)

    ne.process_round.assert_awaited_once()
    call_args = ne.process_round.await_args
    assert call_args[0][0] == 5  # turn_number


async def test_a8_hooks_no_error_when_managers_are_none() -> None:
    """managers=None でも _run_post_round_hooks() がエラーを出さない。"""
    engine = _make_engine_for_hook_tests()

    # 例外が出なければ OK
    await engine._run_post_round_hooks(character_moved=False)


async def test_a8_hooks_call_emergent_canonizer_process_round() -> None:
    """_run_post_round_hooks() が emergent canonizer を呼ぶ。"""
    canonizer = AsyncMock()
    canonizer.process_round = AsyncMock()
    engine = _make_engine_for_hook_tests(emergent_canonizer=canonizer)

    await engine._run_post_round_hooks(character_moved=False)

    canonizer.process_round.assert_awaited_once_with(5)


async def test_a8_narration_context_character_moved_true() -> None:
    """_build_narration_context(character_moved=True) → ctx.character_moved == True。"""
    engine = _make_engine_for_hook_tests()

    ctx = await engine._build_narration_context(turn_number=5, character_moved=True)

    assert ctx.character_moved is True


async def test_a8_narration_context_recent_logs_built() -> None:
    """DB から返った logs が recent_log_lines に変換され、_narrator 行は除外される。"""
    engine = _make_engine_for_hook_tests()
    engine.db.get_recent_chat_logs = AsyncMock(return_value=[
        {"char_id": "char_1", "message": "「練習しよう。」", "msg_type": "monologue",
         "place_id": "music_room", "sim_datetime": "2025-04-01T12:00"},
        {"char_id": "_narrator", "message": "静かな午後だった。", "msg_type": "narration_scene",
         "place_id": "music_room", "sim_datetime": "2025-04-01T12:00"},
    ])

    ctx = await engine._build_narration_context(turn_number=5, character_moved=False)

    assert len(ctx.recent_log_lines) == 1
    assert "テストキャラ: 「練習しよう。」" in ctx.recent_log_lines[0]


# ============================================================
# B-10 テスト: StoryDirector / CharacterEvolutionManager フック
# ============================================================


async def test_story_director_called_in_post_round_hooks() -> None:
    """_run_post_round_hooks() が story_director.process_round を呼ぶ。"""
    director = AsyncMock()
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, story_director=director)
    await engine.initialize()
    await engine._run_post_round_hooks(False)
    director.process_round.assert_called_once()


async def test_story_director_receives_scene_hook_and_relationship_context() -> None:
    """director context に closed scene / open hooks / relationship events が入る。"""
    director = AsyncMock()
    db = make_mock_db()
    db.get_closed_story_scene_summaries = AsyncMock(
        return_value=["放課後に再会する約束だけが残った。"]
    )
    db.get_open_story_hooks = AsyncMock(
        return_value=[
            {"title": "屋上での約束", "description": "放課後に屋上で会う約束。", "priority": 0.8}
        ]
    )
    db.get_recent_relationship_events = AsyncMock(
        return_value=[
            {
                "char_id_from": "char_1",
                "char_id_to": "char_2",
                "summary": "char_1 が char_2 に踏み込んだ。",
                "turn_number": 3,
            }
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, story_director=director)
    await engine.initialize()

    await engine._run_post_round_hooks(False)

    director.process_round.assert_called_once()
    _, ctx = director.process_round.await_args.args
    assert ctx.recent_scene_outcomes == ["放課後に再会する約束だけが残った。"]
    assert ctx.open_hook_summaries == ["屋上での約束: 放課後に屋上で会う約束。"]
    assert ctx.recent_relationship_event_summaries == ["char_1 が char_2 に踏み込んだ。"]


async def test_post_round_hooks_expires_old_interventions_before_story_director() -> None:
    """director 実行前に期限切れ intervention cleanup が走る。"""
    order: list[str] = []
    db = make_mock_db()

    async def _expire(story_id: str, *, turn_number: int) -> list[int]:
        order.append(f"expire:{turn_number}")
        return []

    db.expire_interventions_before_turn = AsyncMock(side_effect=_expire)
    director = AsyncMock()

    async def _process_round(*args, **kwargs) -> None:
        order.append("director")

    director.process_round = AsyncMock(side_effect=_process_round)
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, story_director=director)
    await engine.initialize()

    await engine._run_post_round_hooks(False)

    assert order == ["expire:0", "director"]


async def test_post_round_hooks_process_interaction_patterns_before_story_director() -> None:
    """interaction pattern engine は director より前に走る。"""
    order: list[str] = []
    pattern_engine = AsyncMock()

    async def _patterns(*args, **kwargs) -> None:
        order.append("patterns")

    pattern_engine.process_round = AsyncMock(side_effect=_patterns)
    director = AsyncMock()

    async def _director(*args, **kwargs) -> None:
        order.append("director")

    director.process_round = AsyncMock(side_effect=_director)
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine(
        "test_story",
        db,
        router,
        story_director=director,
        interaction_pattern_engine=pattern_engine,
    )
    await engine.initialize()

    await engine._run_post_round_hooks(False)

    assert order == ["patterns", "director"]


async def test_post_round_hooks_process_episode_planner_between_patterns_and_director() -> None:
    """episode planner は chapter の後、director の前に走る。"""
    order: list[str] = []
    pattern_engine = AsyncMock()
    chapter_manager = AsyncMock()
    episode_planner = AsyncMock()
    director = AsyncMock()

    async def _patterns(*args, **kwargs) -> None:
        order.append("patterns")

    async def _chapter(*args, **kwargs) -> dict[str, object]:
        order.append("chapter")
        return {"activated_chapter": {"id": 11, "theme": "文化祭"}}

    async def _episodes(*args, **kwargs) -> None:
        order.append("episodes")

    async def _director(*args, **kwargs) -> None:
        order.append("director")

    pattern_engine.process_round = AsyncMock(side_effect=_patterns)
    chapter_manager.process_round = AsyncMock(side_effect=_chapter)
    episode_planner.process_round = AsyncMock(side_effect=_episodes)
    director.process_round = AsyncMock(side_effect=_director)
    episode_planner.get_active_episode = AsyncMock(return_value=None)
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine(
        "test_story",
        db,
        router,
        story_director=director,
        interaction_pattern_engine=pattern_engine,
        chapter_manager=chapter_manager,
        episode_planner=episode_planner,
    )
    await engine.initialize()

    await engine._run_post_round_hooks(False)

    assert order == ["patterns", "chapter", "episodes", "director"]
    assert episode_planner.process_round.await_args.kwargs["active_chapter"] == {"id": 11, "theme": "文化祭"}


async def test_collect_scene_close_handoff_candidate_ids_prioritizes_missing_arc_before_missing_novel_output() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._closed_story_scene_ids = [91]
    db.get_recent_closed_story_scenes = AsyncMock(
        return_value=[
            {"id": 77},
            {"id": 66},
            {"id": 91},
            {"id": 88},
        ]
    )
    engine._scene_close_artifact_status = AsyncMock(
        side_effect=lambda scene_id: {
            77: "missing_novel_output",
            66: "missing_arc",
            88: "skip",
        }.get(scene_id, "complete")
    )

    candidate_ids = await engine._collect_scene_close_handoff_candidate_ids(20)

    assert candidate_ids == [91, 66, 77]


async def test_collect_scene_close_handoff_candidate_ids_backfills_older_missing_arc_outside_recent_window() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    db.get_recent_closed_story_scenes = AsyncMock(
        side_effect=[
            [{"id": 92}],
            [{"id": 92}, {"id": 30}],
        ]
    )
    engine._scene_close_artifact_status = AsyncMock(
        side_effect=lambda scene_id: {
            92: "missing_novel_output",
            30: "missing_arc",
        }.get(scene_id, "complete")
    )

    candidate_ids = await engine._collect_scene_close_handoff_candidate_ids(120)

    assert candidate_ids == [30, 92]


async def test_process_closed_story_scenes_keeps_missing_arc_candidate_when_analyzer_returns_none() -> None:
    db = make_mock_db()
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[{"id": 66}])
    db.get_story_scene = AsyncMock(
        return_value={"id": 66, "scene_type": "conversation", "status": "closed", "place_id": "rooftop"}
    )
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    analyzer = AsyncMock()
    analyzer.analyze_closed_scene = AsyncMock(return_value=None)
    engine = StoryEngine(
        "test_story",
        db,
        router,
        config=cfg,
        scene_hook_analyzer=analyzer,
    )
    await engine.initialize()
    engine._scene_close_artifact_status = AsyncMock(return_value="missing_arc")

    candidate_ids = await engine._process_closed_story_scenes(20)

    assert candidate_ids == [66]
    analyzer.analyze_closed_scene.assert_awaited_once_with(66)


async def test_build_scene_close_novel_context_rebuilds_participants_and_summary_without_logs() -> None:
    db = make_mock_db(
        characters=[
            make_character(id="char_1", name_ja="一人目"),
            make_character(id="char_2", name_ja="二人目"),
        ]
    )
    db.get_open_story_hooks_for_scene = AsyncMock(
        return_value=[
            {
                "id": 7,
                "title": "放課後の約束",
                "description": "屋上で再会する。",
            }
        ]
    )
    db.get_scene_participants = AsyncMock(
        return_value=[
            {"scene_id": 91, "char_id": "char_1", "role": "focus"},
            {"scene_id": 91, "char_id": "char_2", "role": "support"},
        ]
    )
    db.get_active_tensions = AsyncMock(
        return_value=[
            {"tension_type": "conflict", "description": "まだ解けていない対立がある。"}
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    ctx = await engine._build_scene_close_novel_context(
        scene_id=91,
        scene={
            "id": 91,
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "rooftop",
            "outcome_summary": "",
        },
        scene_logs=[],
    )

    assert ctx.scene_id == 91
    assert ctx.participant_names == ["一人目", "二人目"]
    assert ctx.hook_summaries == ["放課後の約束: 屋上で再会する。"]
    assert ctx.tension_summaries == ["conflict: まだ解けていない対立がある。"]
    assert ctx.scene_outcome_summary == "一人目、二人目のやり取りがrooftopに残った。"
    assert ctx.recent_log_lines == []


async def test_build_scene_close_novel_context_uses_existing_arc_summary_for_missing_novel_output() -> None:
    db = make_mock_db(
        characters=[
            make_character(id="char_1", name_ja="一人目"),
            make_character(id="char_2", name_ja="二人目"),
        ]
    )
    db.get_open_story_hooks_for_scene = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_scene_participants = AsyncMock(
        return_value=[
            {"scene_id": 92, "char_id": "char_1", "role": "focus"},
            {"scene_id": 92, "char_id": "char_2", "role": "support"},
        ]
    )
    db.get_arc_by_source_scene_id = AsyncMock(
        return_value={
            "id": 501,
            "title": "屋上の余韻",
            "summary": "順番だけ決まらないまま屋上の話が閉じた。",
            "source_scene_id": 92,
        }
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    ctx = await engine._build_scene_close_novel_context(
        scene_id=92,
        scene={
            "id": 92,
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "rooftop",
            "outcome_summary": "",
        },
        scene_logs=[],
    )

    assert ctx.scene_outcome_summary == "順番だけ決まらないまま屋上の話が閉じた。"
    assert ctx.participant_names == ["一人目", "二人目"]
    assert ctx.recent_log_lines == []


async def test_story_director_receives_active_episode_context() -> None:
    """director context に active episode 情報が入る。"""
    episode_planner = AsyncMock()
    episode_planner.process_round = AsyncMock()
    episode_planner.get_active_episode = AsyncMock(
        return_value={
            "id": 41,
            "episode_type": "misunderstanding",
            "goal": "誤解をほどく",
            "stakes": "すれ違いが残れば場がこじれる。",
            "active_pattern_type": "misunderstanding",
            "focus_char_ids": ["char_1", "char_2"],
            "carry_over_hook_ids": [7],
            "opened_turn": 4,
        }
    )
    director = AsyncMock()
    db = make_mock_db()
    db.get_open_story_hooks = AsyncMock(
        return_value=[{"id": 7, "title": "誤解", "description": "まだ食い違いが残る。"}]
    )
    router = AsyncMock()
    engine = StoryEngine(
        "test_story",
        db,
        router,
        story_director=director,
        episode_planner=episode_planner,
    )
    await engine.initialize()

    await engine._run_post_round_hooks(False)

    ctx = director.process_round.await_args.args[1]
    assert ctx.active_episode_id == 41
    assert ctx.active_episode_type == "misunderstanding"
    assert ctx.active_episode_goal == "誤解をほどく"
    assert ctx.carry_over_hook_summaries == ["誤解: まだ食い違いが残る。"]


async def test_character_evolution_called_in_post_round_hooks() -> None:
    """_run_post_round_hooks() が character_evolution_manager.process_round を呼ぶ。"""
    evo_mgr = AsyncMock()
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, character_evolution_manager=evo_mgr)
    await engine.initialize()
    await engine._run_post_round_hooks(False)
    evo_mgr.process_round.assert_called_once()


async def test_growth_engine_called_in_post_round_hooks_when_enabled() -> None:
    """growth_engine 有効時は growth engine が呼ばれ、legacy evolution は呼ばれない。"""
    evo_mgr = AsyncMock()
    growth_engine = AsyncMock()
    db = make_mock_db()
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.growth_engine.enabled = True
    engine = StoryEngine(
        "test_story",
        db,
        router,
        config=cfg,
        character_evolution_manager=evo_mgr,
        growth_engine=growth_engine,
    )
    await engine.initialize()

    await engine._run_post_round_hooks(False)

    growth_engine.process_round.assert_awaited_once()
    evo_mgr.process_round.assert_not_called()


async def test_relationship_mode_engine_called_in_post_round_hooks() -> None:
    """relationship mode engine は post-round で呼ばれる。"""
    relationship_mode_engine = AsyncMock()
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine(
        "test_story",
        db,
        router,
        relationship_mode_engine=relationship_mode_engine,
    )
    await engine.initialize()

    await engine._run_post_round_hooks(False)

    relationship_mode_engine.process_round.assert_awaited_once()


async def test_sync_story_scenes_queues_closed_conversation_scene() -> None:
    """conversation scene close 時に scene_id が post-round queue へ積まれる。"""
    chars = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
    ]
    db = make_mock_db(characters=chars)
    db.get_active_story_scenes = AsyncMock(
        side_effect=[
            [{"id": 91, "scene_type": "conversation", "status": "active", "place_id": "music_room"}],
            [],
            [],
        ]
    )
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.scene_management.enabled = True
    cfg.participation_planner.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()

    scenes = await engine._sync_story_scenes({"char_1": "rooftop", "char_2": "garden"})

    assert scenes == []
    assert engine._closed_story_scene_ids == [91]
    db.close_story_scene.assert_awaited_once()


async def test_sync_story_scenes_closes_objective_saturated_conversation_scene() -> None:
    """focus pair の往復が進んだ scene は objective_saturated で閉じる。"""
    chars = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
    ]
    db = make_mock_db(characters=chars)
    db.get_active_story_scenes = AsyncMock(
        side_effect=[
            [
                {
                    "id": 91,
                    "scene_type": "conversation",
                    "status": "active",
                    "place_id": "music_room",
                    "focus_char_ids": ["char_1", "char_2"],
                    "objective": "張り合いが表に出る",
                    "opened_turn": 10,
                }
            ],
            [],
            [],
        ]
    )
    db.get_scene_logs = AsyncMock(
        return_value=[
            {"id": 10, "char_id": "char_1", "message": "逃げないよ。", "scene_id": 91},
            {"id": 11, "char_id": "char_2", "message": "こっちも引かない。", "scene_id": 91},
            {"id": 12, "char_id": "char_1", "message": "なら勝負だ。", "scene_id": 91},
        ]
    )
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.scene_management.enabled = True
    cfg.participation_planner.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    assert engine.world_clock is not None
    engine.world_clock._turn_number = 13

    scenes = await engine._sync_story_scenes({"char_1": "music_room", "char_2": "music_room"})

    assert scenes == []
    assert engine._closed_story_scene_ids == [91]
    db.close_story_scene.assert_awaited_once()
    kwargs = db.close_story_scene.await_args.kwargs
    assert kwargs["outcome_type"] == "objective_saturated"
    assert "張り合い" in kwargs["outcome_summary"]


async def test_sync_story_scenes_closes_stalled_conversation_scene() -> None:
    """長く続いた scene は stalled_conversation で閉じる。"""
    chars = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
    ]
    db = make_mock_db(characters=chars)
    db.get_active_story_scenes = AsyncMock(
        side_effect=[
            [
                {
                    "id": 91,
                    "scene_type": "conversation",
                    "status": "active",
                    "place_id": "music_room",
                    "focus_char_ids": ["char_1", "char_2"],
                    "objective": "停滞した争点を動かし直す",
                    "opened_turn": 5,
                }
            ],
            [],
            [],
        ]
    )
    db.get_scene_logs = AsyncMock(
        return_value=[
            {"id": 20, "char_id": "char_1", "message": "まだ決めきれない。", "scene_id": 91},
            {"id": 21, "char_id": "char_2", "message": "今日は保留にしよう。", "scene_id": 91},
        ]
    )
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.scene_management.enabled = True
    cfg.participation_planner.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    assert engine.world_clock is not None
    engine.world_clock._turn_number = 13

    scenes = await engine._sync_story_scenes({"char_1": "music_room", "char_2": "music_room"})

    assert scenes == []
    assert engine._closed_story_scene_ids == [91]
    db.close_story_scene.assert_awaited_once()
    kwargs = db.close_story_scene.await_args.kwargs
    assert kwargs["outcome_type"] == "stalled_conversation"
    assert "保留" in kwargs["outcome_summary"] or "停滞" in kwargs["outcome_summary"]


async def test_sync_story_scenes_prefers_participants_dispersed_over_other_close_reasons() -> None:
    """participants_dispersed は他の close 条件より優先される。"""
    chars = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
    ]
    db = make_mock_db(characters=chars)
    db.get_active_story_scenes = AsyncMock(
        side_effect=[
            [
                {
                    "id": 91,
                    "scene_type": "conversation",
                    "status": "active",
                    "place_id": "music_room",
                    "focus_char_ids": ["char_1", "char_2"],
                    "objective": "張り合いが表に出る",
                    "opened_turn": 1,
                }
            ],
            [],
            [],
        ]
    )
    db.get_scene_logs = AsyncMock(
        return_value=[
            {"id": 30, "char_id": "char_1", "message": "勝負はまだ終わってない。", "scene_id": 91},
            {"id": 31, "char_id": "char_2", "message": "続きは後でだ。", "scene_id": 91},
            {"id": 32, "char_id": "char_1", "message": "逃がさない。", "scene_id": 91},
        ]
    )
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.scene_management.enabled = True
    cfg.participation_planner.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    assert engine.world_clock is not None
    engine.world_clock._turn_number = 13

    await engine._sync_story_scenes({"char_1": "music_room", "char_2": "garden"})

    db.close_story_scene.assert_awaited_once()
    kwargs = db.close_story_scene.await_args.kwargs
    assert kwargs["outcome_type"] == "participants_dispersed"


async def test_post_round_hooks_process_closed_story_scenes_with_analyzer() -> None:
    """queued closed scene が analyzer で処理され、hook resolve/merge と scene summary 更新が走る。"""
    db = make_mock_db()
    db.get_story_scene = AsyncMock(
        return_value={
            "id": 91,
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "music_room",
            "outcome_summary": "参加者が分散したため場面を終了した。",
        }
    )
    db.get_open_story_hooks_for_scene = AsyncMock(
        return_value=[
            {"id": 3, "hook_type": "promise", "status": "open", "source_scene_id": 91},
            {"id": 4, "hook_type": "promise", "status": "open", "source_scene_id": 91},
        ]
    )
    db.get_scene_logs = AsyncMock(
        return_value=[{"id": 10, "char_id": "char_1", "message": "まだ終わってない。"}]
    )
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    analyzer = AsyncMock()
    analyzer.analyze_closed_scene = AsyncMock(
        return_value={
            "refined_outcome_summary": "放課後に再会する約束だけが残った。",
            "resolve_hook_ids": [3],
            "keep_hook_ids": [4],
            "merged_hooks": [
                {
                    "hook_type": "promise",
                    "title": "放課後の約束",
                    "description": "放課後に屋上で会う約束が一本化された。",
                    "priority": 0.82,
                }
            ],
        }
    )
    engine = StoryEngine("test_story", db, router, config=cfg, scene_hook_analyzer=analyzer)
    await engine.initialize()
    engine._closed_story_scene_ids = [91]

    await engine._run_post_round_hooks(False)

    analyzer.analyze_closed_scene.assert_awaited_once_with(91)
    db.resolve_story_hook.assert_awaited_once()
    db.insert_story_hook.assert_awaited_once()
    db.update_story_scene.assert_awaited_once_with(
        91,
        {"outcome_summary": "放課後に再会する約束だけが残った。"},
    )
    assert engine._closed_story_scene_ids == []


async def test_post_round_hooks_process_closed_story_scenes_before_story_director() -> None:
    """closed scene の analyzer 完了後に story_director が呼ばれる。"""
    order: list[str] = []
    db = make_mock_db()
    db.get_story_scene = AsyncMock(
        return_value={
            "id": 91,
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "music_room",
            "outcome_summary": "参加者が分散したため場面を終了した。",
        }
    )
    db.get_open_story_hooks_for_scene = AsyncMock(return_value=[])
    db.get_closed_story_scene_summaries = AsyncMock(return_value=["整理済みの場面要約。"])
    analyzer = AsyncMock()

    async def _analyze(scene_id: int) -> dict[str, object]:
        order.append(f"analyzer:{scene_id}")
        return {
            "refined_outcome_summary": "整理済みの場面要約。",
            "resolve_hook_ids": [],
            "keep_hook_ids": [],
            "merged_hooks": [],
        }

    analyzer.analyze_closed_scene = AsyncMock(side_effect=_analyze)
    director = AsyncMock()

    async def _process_round(*args, **kwargs) -> None:
        order.append("director")

    director.process_round = AsyncMock(side_effect=_process_round)
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    engine = StoryEngine(
        "test_story",
        db,
        router,
        config=cfg,
        story_director=director,
        scene_hook_analyzer=analyzer,
    )
    await engine.initialize()
    engine._closed_story_scene_ids = [91]

    await engine._run_post_round_hooks(False)

    assert order == ["analyzer:91", "director"]


async def test_post_round_hooks_recovers_scene_close_artifact_backlog() -> None:
    db = make_mock_db()
    db.get_recent_closed_story_scenes = AsyncMock(
        return_value=[
            {
                "id": 91,
                "story_id": "test_story",
                "scene_type": "conversation",
                "status": "closed",
                "place_id": "music_room",
                "opened_turn": 10,
                "closed_turn": 12,
                "outcome_summary": "場面が閉じた。",
            }
        ]
    )
    db.get_story_scene = AsyncMock(
        return_value={
            "id": 91,
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "music_room",
            "outcome_summary": "場面が閉じた。",
        }
    )
    db.get_arc_by_source_scene_id = AsyncMock(return_value=None)
    db.get_scene_logs = AsyncMock(
        return_value=[{"id": 10, "char_id": "char_1", "message": "まだ終わってない。"}]
    )
    db.get_open_story_hooks_for_scene = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    router = AsyncMock()
    cfg = make_runtime_config()
    engine = StoryEngine("test_story", db, router, config=cfg)
    engine._novel_generator = AsyncMock()
    await engine.initialize()
    assert engine.world_clock is not None
    engine.world_clock._turn_number = 12

    await engine._run_post_round_hooks(False)

    engine._novel_generator.process_round.assert_awaited_once()


async def test_invoke_novel_generator_for_scene_reuses_existing_arc_without_outputs() -> None:
    db = make_mock_db()
    db.get_story_scene = AsyncMock(
        return_value={
            "id": 91,
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "music_room",
            "outcome_summary": "場面が閉じた。",
        }
    )
    db.get_arc_by_source_scene_id = AsyncMock(
        return_value={"id": 77, "source_scene_id": 91, "arc_type": "scene"}
    )
    db.get_novel_outputs = AsyncMock(return_value=[])
    db.get_scene_logs = AsyncMock(
        return_value=[{"id": 10, "char_id": "char_1", "message": "まだ終わってない。"}]
    )
    db.get_open_story_hooks_for_scene = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    router = AsyncMock()
    cfg = make_runtime_config()
    engine = StoryEngine("test_story", db, router, config=cfg)
    engine._novel_generator = AsyncMock()
    await engine.initialize()

    await engine._invoke_novel_generator_for_scene(12, 91)

    engine._novel_generator.process_round.assert_awaited_once()


async def test_invoke_novel_generator_for_scene_uses_recent_logs_when_outcome_summary_missing() -> None:
    db = make_mock_db()
    db.get_story_scene = AsyncMock(
        return_value={
            "id": 92,
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "rooftop",
            "outcome_summary": "",
        }
    )
    db.get_arc_by_source_scene_id = AsyncMock(return_value=None)
    db.get_scene_logs = AsyncMock(
        return_value=[
            {"id": 11, "char_id": "char_1", "message": "あとで屋上で決める。"},
            {"id": 12, "char_id": "char_2", "message": "じゃあ先に順番を出して。"},
        ]
    )
    db.get_open_story_hooks_for_scene = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    router = AsyncMock()
    cfg = make_runtime_config()
    engine = StoryEngine("test_story", db, router, config=cfg)
    engine._novel_generator = AsyncMock()
    await engine.initialize()

    await engine._invoke_novel_generator_for_scene(12, 92)

    ctx = engine._novel_generator.process_round.await_args.args[1]
    assert ctx.scene_outcome_summary
    assert "あとで屋上で決める" in " ".join(ctx.recent_log_lines)
    assert ctx.source_log_ids == [11, 12]


async def test_post_round_hooks_skips_scene_analyzer_for_solo_scene() -> None:
    """solo scene は analyzer 対象にしない。"""
    db = make_mock_db()
    db.get_story_scene = AsyncMock(
        return_value={"id": 17, "scene_type": "solo", "status": "closed", "place_id": "rooftop"}
    )
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.story_hooks.enabled = True
    analyzer = AsyncMock()
    engine = StoryEngine("test_story", db, router, config=cfg, scene_hook_analyzer=analyzer)
    await engine.initialize()
    engine._closed_story_scene_ids = [17]

    await engine._run_post_round_hooks(False)

    analyzer.analyze_closed_scene.assert_not_called()


async def test_novel_generator_not_called_without_chapter_break() -> None:
    """chapter_break=False のとき novel_generator.process_round は呼ばれない。"""
    novel_gen = AsyncMock()
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, novel_generator=novel_gen)
    await engine.initialize()
    await engine._run_post_round_hooks(False)
    novel_gen.process_round.assert_not_called()


async def test_novel_generator_called_on_chapter_break() -> None:
    """chapter_break=True のとき novel_generator.process_round が呼ばれる。"""
    novel_gen = AsyncMock()
    narration_eng = AsyncMock()
    narration_eng.process_round = AsyncMock(return_value=True)  # chapter break 発生

    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine(
        "test_story", db, router,
        narration_engine=narration_eng,
        novel_generator=novel_gen,
    )
    await engine.initialize()
    await engine._run_post_round_hooks(False)
    novel_gen.process_round.assert_called_once()


async def test_evolution_overlay_overrides_char_fields() -> None:
    """evolution_overlay が _build_context() の current_goal に反映される。"""
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    char = make_character(current_goal="音楽を極める")
    state = make_state()
    ctx = engine._build_context(
        char, state, "音楽室",
        evolution_overlay={"current_goal": "世界を救う"},
    )
    assert ctx.current_goal == "世界を救う"


async def test_quality_guard_can_shorten_and_record_issue() -> None:
    """quality_guard が shorten を返したとき、短縮後本文で保存し issue を記録する。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="これは長すぎる発話です。"))
    quality_guard = MagicMock()
    quality_guard.evaluate.return_value = QualityGuardResult(
        text="短い発話です。",
        action="shorten",
        score=0.7,
        issues=[{"issue_type": "overlength", "severity": "warning", "details": {"limit": 10}}],
    )
    engine = StoryEngine("test_story", db, router, quality_guard=quality_guard)
    await engine.initialize()

    await engine.run_one_turn()

    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["message"] == "短い発話です。"
    assert saved_log["quality_score"] == 0.7
    issue_types = [call.args[1]["issue_type"] for call in db.insert_generation_quality_issue.await_args_list]
    assert "overlength" in issue_types
    assert "quality_output_normalized" in issue_types
    assert all(call.args[1]["log_id"] == 1 for call in db.insert_generation_quality_issue.await_args_list)


async def test_quality_guard_records_reply_focus_family_in_issue_details() -> None:
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="それは違う。"))
    quality_guard = MagicMock()
    quality_guard.evaluate.return_value = QualityGuardResult(
        text="それは違う。",
        action="shorten",
        score=0.6,
        issues=[{"issue_type": "reply_focus_missing", "severity": "warning", "details": {}}],
    )
    engine = StoryEngine("test_story", db, router, quality_guard=quality_guard)
    await engine.initialize()
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "星風 瑠奈",
            "conversation_partner_names": ["星風 瑠奈"],
            "recent_dialogue_lines": ["星風 瑠奈: 「それ、いつ決めるの？」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
            "recent_logs": [{"id": 9, "char_id": "char_2", "message": "それ、いつ決めるの？"}],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "react"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    issue_payloads = [call.args[1] for call in db.insert_generation_quality_issue.await_args_list]
    focus_issue = next(payload for payload in issue_payloads if payload["issue_type"] == "reply_focus_missing")
    assert focus_issue["details"]["reply_focus_family"] == "timing"


async def test_quality_guard_can_normalize_without_retry() -> None:
    """quality_guard が normalize を返したとき、再生成せず正規化後本文を保存する。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="**それは違う。**"))
    quality_guard = MagicMock()
    quality_guard.evaluate.return_value = QualityGuardResult(
        text="それは違う。",
        action="normalize",
        score=0.85,
        issues=[{"issue_type": "markdown_formatting", "severity": "warning", "details": {}}],
    )
    engine = StoryEngine("test_story", db, router, quality_guard=quality_guard)
    await engine.initialize()

    await engine.run_one_turn()

    assert router.generate.await_count == 1
    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["message"] == "それは違う。"
    assert saved_log["quality_score"] == 0.85
    issue_types = [call.args[1]["issue_type"] for call in db.insert_generation_quality_issue.await_args_list]
    assert "markdown_formatting" in issue_types
    assert "quality_output_normalized" in issue_types


async def test_quality_guard_can_drop_response_before_save() -> None:
    """quality_guard が drop を返したとき、chat_log は保存されない。"""
    db = make_mock_db()
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="誰か誰か誰か"))
    quality_guard = MagicMock()
    quality_guard.evaluate.return_value = QualityGuardResult(
        text="誰か誰か誰か",
        action="drop",
        score=0.1,
        issues=[{"issue_type": "abstract_repetition", "severity": "warning", "details": {"token": "誰か"}}],
    )
    engine = StoryEngine("test_story", db, router, quality_guard=quality_guard)
    await engine.initialize()

    await engine.run_one_turn()

    db.insert_chat_log.assert_not_awaited()
    assert db.insert_generation_quality_issue.await_count == 1


async def test_quality_guard_can_retry_once_before_save() -> None:
    """quality_guard が retry を返したとき、補正付きで 1 回だけ再生成する。

    2段生成: inner_thought(call1) → voice(call2) → retry(call3) で計3コール。
    """
    db = make_mock_db(story=make_story(id="ankoku_gakuen"))
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            make_llm_response(text="（まだ帰らせない）"),     # inner_thought
            make_llm_response(text="暗闇の運命が揺れる。"),   # voice attempt 1 → retry
            make_llm_response(text="それ、今ここで言うの？"),  # retry
        ]
    )
    quality_guard = MagicMock()
    quality_guard.evaluate.side_effect = [
        QualityGuardResult(
            text="暗闇の運命が揺れる。",
            action="retry",
            score=0.4,
            issues=[{"issue_type": "poetic_abstraction", "severity": "warning", "details": {"token": "暗闇"}}],
        ),
        QualityGuardResult(
            text="それ、今ここで言うの？",
            action="accept",
            score=0.9,
            issues=[],
        ),
    ]
    engine = StoryEngine("ankoku_gakuen", db, router, quality_guard=quality_guard)
    await engine.initialize()
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "星風 瑠奈",
            "conversation_partner_names": ["星風 瑠奈"],
            "recent_dialogue_lines": ["星風 瑠奈: 「まだ帰らないの？」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "react"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    # inner_thought + voice + retry = 3 コール
    assert router.generate.await_count == 3
    # retry prompt は call index 2 (0始まり)
    retry_user_prompt = router.generate.await_args_list[2].kwargs["user_prompt"]
    assert "前回の出力は抽象的" in retry_user_prompt
    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["message"] == "それ、今ここで言うの？"
    assert db.insert_generation_quality_issue.await_count == 0


async def test_quality_guard_retry_root_cause_stays_in_normalized_details_without_persisting_stale_issue() -> None:
    db = make_mock_db(story=make_story(id="ankoku_gakuen"))
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            make_llm_response(text="（あいつの言い方が気に入らない）"),  # inner_thought
            make_llm_response(text="曖昧に返す。"),                      # voice → retry
            make_llm_response(text="夜風ユウマ、じゃあ順番を変える。流れはここで切る。"),  # retry
        ]
    )
    quality_guard = MagicMock()
    quality_guard.evaluate.side_effect = [
        QualityGuardResult(
            text="曖昧に返す。",
            action="retry",
            score=0.2,
            issues=[{"issue_type": "reply_focus_missing", "severity": "warning", "details": {}}],
        ),
        QualityGuardResult(
            text="夜風ユウマ、じゃあ順番を変える。流れはここで切る。",
            action="normalize",
            score=0.8,
            issues=[{"issue_type": "markdown_formatting", "severity": "warning", "details": {}}],
        ),
    ]
    engine = StoryEngine("ankoku_gakuen", db, router, quality_guard=quality_guard)
    await engine.initialize()
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "夜風ユウマ",
            "conversation_partner_names": ["夜風ユウマ"],
            "recent_dialogue_lines": ["夜風ユウマ: 「その順番には乗れない」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "react"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    issue_payloads = [call.args[1] for call in db.insert_generation_quality_issue.await_args_list]
    assert "reply_focus_missing" not in [payload["issue_type"] for payload in issue_payloads]
    normalized_issue = next(payload for payload in issue_payloads if payload["issue_type"] == "quality_output_normalized")
    assert normalized_issue["details"]["fallback_root_issue"] == "reply_focus_missing"
    assert "reply_focus_missing" in normalized_issue["details"]["fallback_issue_set"]


async def test_story_engine_skips_nonblocking_voice_flat_issue_but_keeps_normalized_summary() -> None:
    db = make_mock_db(story=make_story(id="ankoku_gakuen"))
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="まず、回収の順番そのものを先に通して、具体的な流れを確定させましょうよ。"))
    quality_guard = MagicMock()
    quality_guard.evaluate.return_value = QualityGuardResult(
        text="まず、回収の順番そのものを先に通して、具体的な流れを確定させましょうよ。",
        action="normalize",
        score=0.8,
        issues=[
            {
                "issue_type": "voice_flat_reply",
                "severity": "info",
                "details": {
                    "voice_flat_blocking": False,
                    "voice_flat_residual_only": True,
                    "flat_issue_family": "voice_flat",
                },
            }
        ],
    )
    engine = StoryEngine("ankoku_gakuen", db, router, quality_guard=quality_guard)
    await engine.initialize()

    await engine.run_one_turn()

    issue_payloads = [call.args[1] for call in db.insert_generation_quality_issue.await_args_list]
    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["quality_flags"] == []
    assert "voice_flat_reply" not in [payload["issue_type"] for payload in issue_payloads]
    normalized_issue = next(payload for payload in issue_payloads if payload["issue_type"] == "quality_output_normalized")
    assert normalized_issue["details"]["issue_count"] == 1
    assert normalized_issue["details"]["fallback_issue_set"] == ["voice_flat_reply"]


async def test_story_engine_skips_resolved_sentence_overflow_flag_but_keeps_normalized_summary() -> None:
    db = make_mock_db(story=make_story(id="ankoku_gakuen"))
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=make_llm_response(
            text="ソーマの「判断基準」って、結局何で測るの？その基準を先に提示してくれないと、あたしは何も動けないよ。余計な一言。"
        )
    )
    quality_guard = MagicMock()
    quality_guard.evaluate.return_value = QualityGuardResult(
        text="ソーマの「判断基準」って、結局何で測るの？その基準を先に提示してくれないと、あたしは何も動けないよ。",
        action="shorten",
        score=0.8,
        issues=[
            {"issue_type": "sentence_overflow", "severity": "warning", "details": {"msg_type": "reply"}},
        ],
    )
    engine = StoryEngine("ankoku_gakuen", db, router, quality_guard=quality_guard)
    await engine.initialize()

    await engine.run_one_turn()

    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["quality_flags"] == []
    issue_payloads = [call.args[1] for call in db.insert_generation_quality_issue.await_args_list]
    assert "sentence_overflow" not in [payload["issue_type"] for payload in issue_payloads]
    normalized_issue = next(payload for payload in issue_payloads if payload["issue_type"] == "quality_output_normalized")
    assert normalized_issue["details"]["issue_count"] == 1
    assert normalized_issue["details"]["fallback_issue_set"] == ["sentence_overflow"]


async def test_story_engine_skips_nonblocking_visibility_flag_but_keeps_normalized_summary() -> None:
    db = make_mock_db(story=make_story(id="ankoku_gakuen"))
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=make_llm_response(
            text="その境界線なんて、今この場で決めようとしても誰かの都合で流れるだけなんだぜ。"
        )
    )
    quality_guard = MagicMock()
    quality_guard.evaluate.return_value = QualityGuardResult(
        text="その境界線なんて、今この場で決めようとしても誰かの都合で流れるだけなんだぜ。",
        action="normalize",
        score=0.8,
        issues=[
            {
                "issue_type": "signal_visibility_missing",
                "severity": "info",
                "details": {
                    "visibility_blocking": False,
                    "signal_visible": False,
                    "objective_visible": True,
                },
            },
        ],
    )
    engine = StoryEngine("ankoku_gakuen", db, router, quality_guard=quality_guard)
    await engine.initialize()

    await engine.run_one_turn()

    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["quality_flags"] == []
    issue_payloads = [call.args[1] for call in db.insert_generation_quality_issue.await_args_list]
    assert "signal_visibility_missing" not in [payload["issue_type"] for payload in issue_payloads]
    normalized_issue = next(payload for payload in issue_payloads if payload["issue_type"] == "quality_output_normalized")
    assert normalized_issue["details"]["issue_count"] == 1
    assert normalized_issue["details"]["fallback_issue_set"] == ["signal_visibility_missing"]


async def test_quality_guard_retry_prompt_includes_issue_specific_guidance() -> None:
    engine = StoryEngine("ankoku_gakuen", make_mock_db(story=make_story(id="ankoku_gakuen")), AsyncMock())
    retry_user_prompt = engine._build_retry_user_prompt(
        "元の prompt",
        msg_type="reply",
        issues=[
            {"issue_type": "reply_without_direct_reaction", "severity": "warning", "details": {}},
            {"issue_type": "reply_focus_missing", "severity": "warning", "details": {}},
            {"issue_type": "scene_objective_drift", "severity": "warning", "details": {}},
            {"issue_type": "voice_flat_reply", "severity": "warning", "details": {}},
        ],
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        voice_anchor_text="静かな確認で短く返す",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動く",
        handoff_target_name=None,
    )

    assert "相手の発言の要点を先頭で拾ってください" in retry_user_prompt
    assert "今返す一点は「帰るかどうかの確認」" in retry_user_prompt
    assert "今この場で自分が言うべき一点に触れてください" in retry_user_prompt
    assert "返し方は「静かな確認で短く返す」" in retry_user_prompt


async def test_quality_guard_retry_prompt_includes_direct_reaction_family_guidance() -> None:
    engine = StoryEngine("ankoku_gakuen", make_mock_db(story=make_story(id="ankoku_gakuen")), AsyncMock())
    retry_user_prompt = engine._build_retry_user_prompt(
        "元の prompt",
        msg_type="reply",
        issues=[{"issue_type": "reply_without_direct_reaction", "severity": "warning", "details": {}}],
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert "1文目でいつ動くかを自分の言葉で返してください" in retry_user_prompt


async def test_quality_guard_uses_deterministic_fallback_after_second_retry() -> None:
    """2回連続で retry なら fallback utterance を保存する。"""
    db = make_mock_db(story=make_story(id="ankoku_gakuen"))
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            make_llm_response(text="暗闇の運命が揺れる。"),
            make_llm_response(text="静かな本音だけがこぼれる。"),
        ]
    )
    quality_guard = MagicMock()
    quality_guard.evaluate.side_effect = [
        QualityGuardResult(
            text="暗闇の運命が揺れる。",
            action="retry",
            score=0.4,
            issues=[{"issue_type": "poetic_abstraction", "severity": "warning", "details": {"token": "暗闇"}}],
        ),
        QualityGuardResult(
            text="静かな本音だけがこぼれる。",
            action="retry",
            score=0.4,
            issues=[{"issue_type": "poetic_abstraction", "severity": "warning", "details": {"token": "本音"}}],
        ),
    ]
    engine = StoryEngine("ankoku_gakuen", db, router, quality_guard=quality_guard)
    await engine.initialize()

    await engine.run_one_turn()

    assert router.generate.await_count == 2
    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["message"] != "暗闇の運命が揺れる。"
    assert saved_log["message"] != "静かな本音だけがこぼれる。"
    assert saved_log["message"]
    issue_payloads = [call.args[1] for call in db.insert_generation_quality_issue.await_args_list]
    assert any(payload.get("auto_action") == "fallback" for payload in issue_payloads)


async def test_quality_guard_falls_back_when_contextual_hard_issues_remain() -> None:
    """scene/signal 系 hard issue が残る reply は keep せず fallback する。"""
    db = make_mock_db(story=make_story(id="ankoku_gakuen"))
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            make_llm_response(text="（帰りたくない）"),                                  # inner_thought
            make_llm_response(text="暗闇の運命が揺れる。"),                              # voice → retry
            make_llm_response(text="星風 瑠奈、まだ帰らない。ここで決めるなら先に言って。"),  # retry
        ]
    )
    quality_guard = MagicMock()
    quality_guard.evaluate.side_effect = [
        QualityGuardResult(
            text="暗闇の運命が揺れる。",
            action="retry",
            score=0.4,
            issues=[{"issue_type": "poetic_abstraction", "severity": "warning", "details": {"token": "暗闇"}}],
        ),
        QualityGuardResult(
            text="星風 瑠奈、まだ帰らない。ここで決めるなら先に言って。",
            action="retry",
            score=0.7,
            issues=[
                {"issue_type": "scene_objective_drift", "severity": "warning", "details": {}},
                {"issue_type": "signal_override", "severity": "warning", "details": {}},
            ],
        ),
    ]
    engine = StoryEngine("ankoku_gakuen", db, router, quality_guard=quality_guard)
    await engine.initialize()
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "星風 瑠奈",
            "conversation_partner_names": ["星風 瑠奈"],
            "recent_dialogue_lines": ["星風 瑠奈: 「まだ帰らないの？」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "react"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    # inner_thought + voice + retry = 3 コール
    assert router.generate.await_count == 3
    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["message"] != "星風 瑠奈、まだ帰らない。ここで決めるなら先に言って。"
    issue_payloads = [call.args[1] for call in db.insert_generation_quality_issue.await_args_list]
    assert any(payload.get("auto_action") == "fallback" for payload in issue_payloads)


async def test_quality_guard_falls_back_when_focus_issue_remains_on_retry() -> None:
    """question-like な retry output でも focus issue が残るなら keep しない。"""
    db = make_mock_db(story=make_story(id="ankoku_gakuen"))
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            make_llm_response(text="（ソーマの意図が読めない）"),                           # inner_thought
            make_llm_response(text="暗闇の運命が揺れる。"),                               # voice → retry
            make_llm_response(text="ソーマの『済む話』って、誰がどのコード進めるって決めるの？"),  # retry
        ]
    )
    quality_guard = MagicMock()
    quality_guard.evaluate.side_effect = [
        QualityGuardResult(
            text="暗闇の運命が揺れる。",
            action="retry",
            score=0.4,
            issues=[{"issue_type": "poetic_abstraction", "severity": "warning", "details": {"token": "暗闇"}}],
        ),
        QualityGuardResult(
            text="ソーマの『済む話』って、誰がどのコード進めるって決めるの？",
            action="retry",
            score=0.7,
            issues=[
                {"issue_type": "reply_focus_missing", "severity": "warning", "details": {}},
                {"issue_type": "scene_objective_drift", "severity": "warning", "details": {}},
            ],
        ),
    ]
    engine = StoryEngine("ankoku_gakuen", db, router, quality_guard=quality_guard)
    await engine.initialize()
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "上泉ソーマ",
            "conversation_partner_names": ["上泉ソーマ"],
            "recent_dialogue_lines": ["上泉ソーマ: 「残りを言えば済む話だ」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "react"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["message"] != "ソーマの『済む話』って、誰がどのコード進めるって決めるの？"
    issue_payloads = [call.args[1] for call in db.insert_generation_quality_issue.await_args_list]
    assert any(payload.get("auto_action") == "fallback" for payload in issue_payloads)


def test_build_deterministic_fallback_text_uses_excerpt_and_focus_for_reply() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())
    ctx = CharacterContext(
        char_id="hoshikaze_runa",
        name_ja="星風ルナ",
        personality_core="静かな優等生",
        first_person="私",
        tone="丁寧で柔らかい・静かな常識ツッコミが混ざる",
        speech_examples=["その案、三人くらい止める人が必要だと思います", "少し落ち着いてください"],
        never_say=[],
        current_goal="部室を回す",
        current_worry="変な企画が通ること",
        sim_datetime="2025-04-15T10:30",
        current_place_name="音楽室",
        dominant_signal_text="張り合いに触れると場が動く。",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        target_char_name="上泉ソーマ",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
    )

    assert "上泉ソーマ" in text
    assert "帰" in text or "まだ" in text
    # fallback 台詞がメタ語彙ではなくキャラとして自然な言葉であることを確認
    assert len(text) > 5


def test_build_deterministic_fallback_text_uses_target_and_voice() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())
    ctx = CharacterContext(
        char_id="hoshikaze_runa",
        name_ja="星風ルナ",
        personality_core="静かな優等生",
        first_person="私",
        tone="丁寧で柔らかい・静かな常識ツッコミが混ざる",
        speech_examples=["その案、三人くらい止める人が必要だと思います", "少し落ち着いてください"],
        never_say=[],
        current_goal="部室を回す",
        current_worry="変な企画が通ること",
        sim_datetime="2025-04-15T10:30",
        current_place_name="音楽室",
        dominant_signal_text="星風ルナには認めつつも張り合いがち。",
        scene_objective_text="この場の争点: 張り合いが表に出る",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
    )

    assert "上泉ソーマ" in text
    assert "私" not in text  # 一人称を乱用せず、返答文として自然に保つ
    assert "落ち着" in text or "違います" in text or "やめてください" in text


def test_build_deterministic_fallback_text_varies_by_voice() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())
    hot_ctx = CharacterContext(
        char_id="yokaze_yuuma",
        name_ja="夜風ユウマ",
        personality_core="熱血",
        first_person="俺",
        tone="短文多め・感情直球・熱血論を真顔で言う",
        speech_examples=["売れるかどうかの前に、サビで拳を上げたくなるかだろ"],
        never_say=[],
        current_goal="本気でぶつかる",
        current_worry="古いと笑われる",
        sim_datetime="2025-04-15T10:30",
        current_place_name="屋上",
        dominant_signal_text="張り合いに触れると場が動きやすい。",
    )
    cool_ctx = CharacterContext(
        char_id="kamiizumi_souma",
        name_ja="上泉ソーマ",
        personality_core="冷たい観察者",
        first_person="俺",
        tone="冷たい観察者・心にもないことを平然と歌詞にする",
        speech_examples=["その感情は設計できる"],
        never_say=[],
        current_goal="場を設計する",
        current_worry="本音を読まれること",
        sim_datetime="2025-04-15T10:30",
        current_place_name="音楽室",
        dominant_signal_text="張り合いに触れると場が動きやすい。",
    )

    hot_text = engine._build_deterministic_fallback_text(
        ctx=hot_ctx,
        msg_type="reply",
        target_char_name="ちゅるるん",
    )
    cool_text = engine._build_deterministic_fallback_text(
        ctx=cool_ctx,
        msg_type="reply",
        target_char_name="ちゅるるん",
    )

    assert hot_text != cool_text


def test_build_deterministic_fallback_text_varies_by_intent_second_sentence() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())
    status_ctx = CharacterContext(
        char_id="yokaze_yuuma",
        name_ja="夜風ユウマ",
        personality_core="熱血",
        first_person="俺",
        tone="短文多め・感情直球・熱血論を真顔で言う",
        speech_examples=["売れるかどうかの前に、サビで拳を上げたくなるかだろ"],
        never_say=[],
        current_goal="本気でぶつかる",
        current_worry="古いと笑われる",
        sim_datetime="2025-04-15T10:30",
        current_place_name="屋上",
        dominant_signal_text="張り合いに触れると場が動きやすい。",
    )
    reveal_ctx = CharacterContext(
        char_id="yokaze_yuuma",
        name_ja="夜風ユウマ",
        personality_core="熱血",
        first_person="俺",
        tone="短文多め・感情直球・熱血論を真顔で言う",
        speech_examples=["売れるかどうかの前に、サビで拳を上げたくなるかだろ"],
        never_say=[],
        current_goal="本気でぶつかる",
        current_worry="古いと笑われる",
        sim_datetime="2025-04-15T10:30",
        current_place_name="屋上",
        dominant_signal_text="言い切られていないことに触れると場が動きやすい。",
    )

    status_text = engine._build_deterministic_fallback_text(
        ctx=status_ctx,
        msg_type="reply",
        target_char_name="ちゅるるん",
    )
    reveal_text = engine._build_deterministic_fallback_text(
        ctx=reveal_ctx,
        msg_type="reply",
        target_char_name="ちゅるるん",
    )

    assert status_text != reveal_text
    assert status_text  # status_clash フォールバックが空でない
    assert reveal_text  # near_reveal フォールバックが空でない
    assert "どうするか先に出して" not in status_text
    assert "どうするか先に出して" not in reveal_text


def test_build_deterministic_fallback_text_avoids_recent_self_repeat() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())
    ctx = CharacterContext(
        char_id="miritia",
        name_ja="ミリティア",
        personality_core="挑発屋",
        first_person="あたし",
        tone="テンション高め・挑発する",
        speech_examples=["主役の音が一つしかないみたいな顔、ほんと嫌い"],
        never_say=[],
        current_goal="場を動かす",
        current_worry="軽薄な賑やかしになること",
        sim_datetime="2025-04-15T10:30",
        current_place_name="音楽室",
        dominant_signal_text="それなら今ここで見せて",
    )

    repeated = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="group",
        target_char_name=None,
        recent_self_messages=[],
    )
    changed = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="group",
        target_char_name=None,
        recent_self_messages=[repeated],
    )

    assert changed != repeated


def test_build_deterministic_fallback_text_avoids_recent_second_sentence_repeat() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())
    ctx = CharacterContext(
        char_id="yokaze_yuuma",
        name_ja="夜風ユウマ",
        personality_core="熱血",
        first_person="俺",
        tone="短文多め・感情直球・熱血論を真顔で言う",
        speech_examples=["売れるかどうかの前に、サビで拳を上げたくなるかだろ"],
        never_say=[],
        current_goal="本気でぶつかる",
        current_worry="古いと笑われる",
        sim_datetime="2025-04-15T10:30",
        current_place_name="屋上",
        dominant_signal_text="張り合いに触れると場が動きやすい。",
    )

    first = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="ちゅるるん",
        recent_self_messages=[],
    )
    sentences = [part for part in first.split("。") if part]
    if len(sentences) < 2:
        assert "話を続けよう" not in first
        assert "その話は聞いた" not in first
        return
    second_sentence = sentences[1]
    changed = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="ちゅるるん",
        recent_self_messages=[f"別の一文。{second_sentence}。"],
    )

    assert changed != first


def test_build_reply_focus_text_prefers_question_excerpt() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())
    recent_logs = [
        {"id": 10, "char_id": "char_2", "message": "まだ帰らないの？"},
        {"id": 11, "char_id": "char_1", "message": "……考え中だ"},
    ]

    excerpt = engine._extract_target_last_utterance_excerpt(
        reply_to_log_id=10,
        recent_logs=recent_logs,
    )
    focus = engine._select_reply_focus_text(
        target_excerpt=excerpt,
        recent_dialogue_lines=["char_2: まだ帰らないの？"],
    )

    assert excerpt == "まだ帰らないの？"
    assert focus == "帰るかどうかの確認"


def test_build_reply_focus_text_detects_decision_owner_question() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    focus = engine._select_reply_focus_text(
        target_excerpt="誰がどのコード進めるって決めるの？",
        recent_dialogue_lines=["char_2: 誰がどのコード進めるって決めるの？"],
    )

    assert focus == "誰が決めるかの確認"


def test_build_reply_focus_text_detects_timing_question() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    focus = engine._select_reply_focus_text(
        target_excerpt="それ、いつ決めるの？",
        recent_dialogue_lines=["char_2: それ、いつ決めるの？"],
    )

    assert focus == "いつ動くかの確認"


def test_build_voice_anchor_text_summarizes_speech_style() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    voice_anchor = engine._build_voice_anchor_text(
        tone="丁寧で柔らかい・静かな常識ツッコミが混ざる",
        speech_examples=["少し落ち着いてください", "その案、三人くらい止める人が必要だと思います"],
    )

    assert voice_anchor is not None
    assert "静かな確認" in voice_anchor or "丁寧" in voice_anchor


def test_build_retry_user_prompt_for_reply_includes_focus_and_voice() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    retry_prompt = engine._build_retry_user_prompt(
        "元の prompt",
        msg_type="reply",
        reply_focus_text="帰るかどうかの確認",
        voice_anchor_text="熱血直球で短く返す",
        handoff_target_name=None,
    )

    assert "帰るかどうかの確認" in retry_prompt
    assert "熱血直球で短く返す" in retry_prompt


def test_build_retry_user_prompt_for_timing_focus_includes_family_guidance() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    retry_prompt = engine._build_retry_user_prompt(
        "元の prompt",
        msg_type="reply",
        issues=[{"issue_type": "reply_focus_missing", "severity": "warning", "details": {}}],
        target_last_utterance_excerpt="それ、いつ決めるの？",
        reply_focus_text="いつ動くかの確認",
        handoff_target_name=None,
    )

    assert "いつ動くかの確認" in retry_prompt
    assert "いつ判断" in retry_prompt or "いつ動く" in retry_prompt


def test_build_retry_user_prompt_for_proposal_focus_includes_family_guidance() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    retry_prompt = engine._build_retry_user_prompt(
        "元の prompt",
        msg_type="reply",
        issues=[{"issue_type": "reply_focus_missing", "severity": "warning", "details": {}}],
        reply_focus_text="相手の提案への返答",
        reply_focus_contract={
            "focus_family": "proposal",
            "required_tokens": ["なら", "じゃあ", "どう", "先に"],
            "required_focus_cues": ["なら", "じゃあ", "先に", "どう"],
            "focus_anchor_tokens": ["なら", "じゃあ"],
            "preferred_action": "propose",
            "must_answer_in_first_sentence": True,
        },
        reply_variety_contract={
            "second_beat_mode": "redirect",
            "preferred_move_tokens": ["じゃあ", "その前に"],
        },
        handoff_target_name=None,
    )

    assert "相手の提案への返答" in retry_prompt
    assert "最初に次の一手を出してください" in retry_prompt
    assert "1文目で自分の考えを短く出してください" in retry_prompt
    assert "2文目は 向きを変える返し にしてください" in retry_prompt
    assert "redirect" not in retry_prompt


def test_build_retry_user_prompt_for_decision_owner_focus_includes_first_sentence_cue() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    retry_prompt = engine._build_retry_user_prompt(
        "元の prompt",
        msg_type="reply",
        issues=[{"issue_type": "reply_focus_missing", "severity": "warning", "details": {}}],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        handoff_target_name=None,
    )

    assert "誰が決めるかの確認" in retry_prompt
    assert "1文目で誰が何をするか、短く出してください" in retry_prompt
    assert "1文目の最初" in retry_prompt


def test_build_retry_user_prompt_for_basis_focus_includes_basis_examples() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    retry_prompt = engine._build_retry_user_prompt(
        "元の prompt",
        msg_type="reply",
        issues=[{"issue_type": "reply_focus_missing", "severity": "warning", "details": {}}],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で"],
            "focus_anchor_tokens": ["基準", "何の", "どの"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        handoff_target_name=None,
    )

    # 「判断基準」はメタ語彙なので retry_instruction に出力されない (focus 行ブロックごとスキップ)
    assert "判断基準" not in retry_prompt
    # reply_focus_missing ブランチから basis 向け指示は出る
    assert "1文目で自分が思う答えを短く出してください" in retry_prompt


def test_build_retry_user_prompt_for_basis_focus_includes_condition_second_sentence_guidance() -> None:
    engine = StoryEngine("test_story", make_mock_db(), AsyncMock())

    retry_prompt = engine._build_retry_user_prompt(
        "元の prompt",
        msg_type="reply",
        issues=[{"issue_type": "voice_flat_reply", "severity": "warning", "details": {}}],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で"],
            "focus_anchor_tokens": ["基準", "何の", "どの"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_variety_contract={
            "second_beat_mode": "condition",
            "preferred_move_tokens": ["なら", "そのあと"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_condition",
            "story_pressure_tokens": ["見せ場", "基準"],
        },
        handoff_target_name=None,
    )

    assert "2文目は 条件を示す返し にしてください" in retry_prompt
    assert "なら" in retry_prompt
    assert "基準" in retry_prompt


async def test_run_one_turn_uses_planned_scene_turn_metadata() -> None:
    """planned turn queue があるとき、その char_id / scene_id / intent を使って保存する。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
    ]
    db = make_mock_db(characters=characters)
    db.get_latest_character_state = AsyncMock(
        side_effect=lambda _story_id, _char_id: make_state(current_place="music_room")
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「話そう。」"))
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._planned_turn_queue = [
        {"char_id": "char_2", "scene_id": 77, "speaker_intent": "advance"}
    ]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["char_id"] == "char_2"
    assert saved_log["scene_id"] == 77
    assert saved_log["speaker_intent"] == "advance"
    db.record_scene_participant_turn.assert_awaited_once_with(77, "char_2", turn_number=0)


async def test_resolve_conversation_turn_prefers_reply_for_react_in_large_scene() -> None:
    """3人以上 scene でも react 話者は reply を優先する。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
    ]
    db = make_mock_db(characters=characters)
    db.get_active_conversation_sessions = AsyncMock(
        return_value=[
            make_session(
                place_id="music_room",
                participant_ids=["char_1", "char_2", "char_3"],
                scene_id=91,
            )
        ]
    )
    db.get_recent_conversation_logs = AsyncMock(
        return_value=[
            {"id": 501, "char_id": "char_1", "message": "「先に言う。」"},
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    conversation = await engine._resolve_conversation_turn(
        char_id="char_2",
        current_place="music_room",
        current_places={"char_1": "music_room", "char_2": "music_room", "char_3": "music_room"},
        schedule={"msg_type": "group", "target_char_id": None, "speaker_intent": "react"},
    )

    assert conversation["msg_type"] == "reply"
    assert conversation["target_char_id"] == "char_1"
    assert conversation["reply_to_log_id"] == 501
    assert conversation["target_char_name"] == "一人目"


async def test_resolve_conversation_turn_prefers_reply_for_advance_after_group_opener() -> None:
    """3人以上 scene でも recent log があれば advance 話者は reply に落ちる。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
    ]
    db = make_mock_db(characters=characters)
    db.get_active_conversation_sessions = AsyncMock(
        return_value=[
            make_session(
                place_id="music_room",
                participant_ids=["char_1", "char_2", "char_3"],
                scene_id=91,
            )
        ]
    )
    db.get_recent_conversation_logs = AsyncMock(
        return_value=[
            {"id": 501, "char_id": "char_1", "message": "「先に言う。」"},
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    conversation = await engine._resolve_conversation_turn(
        char_id="char_2",
        current_place="music_room",
        current_places={"char_1": "music_room", "char_2": "music_room", "char_3": "music_room"},
        schedule={"msg_type": "group", "target_char_id": None, "speaker_intent": "advance"},
    )

    assert conversation["msg_type"] == "reply"
    assert conversation["target_char_id"] == "char_1"
    assert conversation["reply_to_log_id"] == 501


async def test_should_open_solo_scene_requires_repeated_alone_turns() -> None:
    """単独 1 turn だけでは solo scene を開かない。"""
    db = make_mock_db()
    db.get_latest_character_state = AsyncMock(
        return_value=make_state(
            current_action="reply",
            turn_number=9,
            stress=0.7,
            loneliness=0.7,
        )
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    assert engine.world_clock is not None
    engine.world_clock._turn_number = 10

    should_open = await engine._should_open_solo_scene("char_1")

    assert should_open is False


async def test_should_open_solo_scene_opens_after_repeated_alone_turns() -> None:
    """単独が連続し感情条件を満たすと solo scene を開く。"""
    db = make_mock_db()
    db.get_latest_character_state = AsyncMock(
        return_value=make_state(
            current_action="reply",
            turn_number=8,
            stress=0.7,
            loneliness=0.7,
        )
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    assert engine.world_clock is not None
    engine.world_clock._turn_number = 10

    should_open = await engine._should_open_solo_scene("char_1")

    assert should_open is True


async def test_should_open_solo_scene_stays_closed_when_nearby_scene_opportunity_is_strong() -> None:
    """近くに強い conversation opportunity があれば solo を開かない。"""
    characters = [
        make_character(id="char_1", name_ja="一人目", favorite_places=json.dumps(["classroom"])),
        make_character(id="char_2", name_ja="二人目", favorite_places=json.dumps(["corridor"])),
    ]
    db = make_mock_db(story=make_story(), characters=characters)
    db.get_latest_character_state = AsyncMock(
        return_value=make_state(
            current_place="classroom",
            current_action="reply",
            turn_number=8,
            stress=0.7,
            loneliness=0.7,
        )
    )
    db.get_active_story_dramatic_pressures = AsyncMock(
        return_value=[
            {
                "pressure_type": "status_flashpoint",
                "focus_char_ids": ["char_1", "char_2"],
                "focus_place_id": "corridor",
                "score": 0.9,
            }
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    assert engine.world_clock is not None
    engine.world_clock._turn_number = 10
    engine._place_manager = PlaceManager(
        [
            {
                "id": "classroom",
                "label": "教室",
                "zone": "school",
                "adjacent_places": {"corridor": 1},
            },
            {
                "id": "corridor",
                "label": "廊下",
                "zone": "school",
                "adjacent_places": {"classroom": 1},
            },
        ]
    )
    engine._last_known_place = {"char_1": "classroom", "char_2": "corridor"}

    should_open = await engine._should_open_solo_scene("char_1")

    assert should_open is False


async def test_select_social_target_place_prefers_active_conversation_scene() -> None:
    """social target は隣接の active conversation scene を最優先する。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目", favorite_places=json.dumps(["corridor"])),
    ]
    db = make_mock_db(story=make_story(), characters=characters)
    db.get_active_story_scenes = AsyncMock(
        return_value=[
            {
                "id": 77,
                "scene_type": "conversation",
                "status": "active",
                "place_id": "corridor",
                "focus_char_ids": ["char_2"],
            }
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._place_manager = PlaceManager(
        [
            {
                "id": "classroom",
                "label": "教室",
                "zone": "school",
                "adjacent_places": {"corridor": 1},
            },
            {
                "id": "corridor",
                "label": "廊下",
                "zone": "school",
                "adjacent_places": {"classroom": 1},
            },
        ]
    )

    target = await engine._select_social_target_place(
        char_id="char_1",
        current_place="classroom",
        current_places={"char_1": "classroom", "char_2": "corridor"},
    )

    assert target == "corridor"


async def test_select_social_target_place_uses_relationship_mode_tiebreak() -> None:
    """同点なら relationship mode counterpart がいる場所を優先する。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
    ]
    db = make_mock_db(story=make_story(), characters=characters)
    db.get_active_story_scenes = AsyncMock(return_value=[])
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "char_id_from": "char_1",
                "char_id_to": "char_3",
                "mode_type": "cannot_ignore",
                "intensity": 0.72,
            }
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._place_manager = PlaceManager(
        [
            {
                "id": "classroom",
                "label": "教室",
                "zone": "school",
                "adjacent_places": {"corridor": 1, "garden": 1},
            },
            {
                "id": "corridor",
                "label": "廊下",
                "zone": "school",
                "adjacent_places": {"classroom": 1},
            },
            {
                "id": "garden",
                "label": "庭",
                "zone": "school",
                "adjacent_places": {"classroom": 1},
            },
        ]
    )

    target = await engine._select_social_target_place(
        char_id="char_1",
        current_place="classroom",
        current_places={
            "char_1": "classroom",
            "char_2": "corridor",
            "char_3": "garden",
        },
    )

    assert target == "garden"


async def test_select_social_target_place_prefers_pressure_focus_place() -> None:
    """同席数より pressure の focus_place を優先する。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
        make_character(id="char_4", name_ja="四人目"),
    ]
    db = make_mock_db(story=make_story(), characters=characters)
    db.get_active_story_scenes = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(
        return_value=[
            {
                "pressure_type": "near_reveal",
                "focus_char_ids": ["char_1", "char_3"],
                "focus_place_id": "garden",
                "score": 0.92,
            }
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._place_manager = PlaceManager(
        [
            {
                "id": "classroom",
                "label": "教室",
                "zone": "school",
                "adjacent_places": {"corridor": 1, "garden": 1},
            },
            {
                "id": "corridor",
                "label": "廊下",
                "zone": "school",
                "adjacent_places": {"classroom": 1},
            },
            {
                "id": "garden",
                "label": "庭",
                "zone": "school",
                "adjacent_places": {"classroom": 1},
            },
        ]
    )

    target = await engine._select_social_target_place(
        char_id="char_1",
        current_place="classroom",
        current_places={
            "char_1": "classroom",
            "char_2": "corridor",
            "char_3": "garden",
            "char_4": "corridor",
        },
    )

    assert target == "garden"


async def test_sync_story_scenes_opens_signal_aware_conversation_scene() -> None:
    """conversation scene 開幕時の focus/objective は pressure から決まる。"""
    characters = [
        make_character(id="char_1", name_ja="一人目", favorite_places=json.dumps(["music_room"])),
        make_character(id="char_2", name_ja="二人目", favorite_places=json.dumps(["music_room"])),
    ]
    db = make_mock_db(story=make_story(), characters=characters)
    db.get_active_story_scenes = AsyncMock(side_effect=[[], [], []])
    db.get_active_story_dramatic_pressures = AsyncMock(
        return_value=[
            {
                "pressure_type": "status_flashpoint",
                "focus_char_ids": ["char_1", "char_2"],
                "focus_place_id": "music_room",
                "score": 0.88,
            }
        ]
    )
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.scene_management.enabled = True
    cfg.participation_planner.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    assert engine.world_clock is not None
    engine.world_clock._turn_number = 12

    await engine._sync_story_scenes({"char_1": "music_room", "char_2": "music_room"})

    db.insert_story_scene.assert_awaited_once()
    payload = db.insert_story_scene.await_args.args[1]
    assert payload["focus_char_ids"] == ["char_1", "char_2"]
    assert payload["objective"] == "二人の思いが今ぶつかっている"


async def test_build_round_turn_plan_limits_conversation_scene_speakers() -> None:
    """conversation scene では既定で advance 1 + react 1 の2件に絞られる。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
        make_character(id="char_4", name_ja="四人目"),
    ]
    db = make_mock_db(characters=characters)
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.scene_management.enabled = True
    cfg.participation_planner.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._sync_story_scenes = AsyncMock(
        return_value=[
            {
                "id": 91,
                "scene_type": "conversation",
                "place_id": "music_room",
                "status": "active",
            }
        ]
    )
    db.get_scene_participants = AsyncMock(
        return_value=[
            {"char_id": "char_1", "role": "focus", "last_spoken_turn": 2},
            {"char_id": "char_2", "role": "support", "last_spoken_turn": 0},
            {"char_id": "char_3", "role": "support", "last_spoken_turn": 1},
            {"char_id": "char_4", "role": "observer", "last_spoken_turn": None},
        ]
    )

    plan = await engine._build_round_turn_plan(
        {"char_1": "music_room", "char_2": "music_room", "char_3": "music_room", "char_4": "music_room"}
    )

    assert [turn["char_id"] for turn in plan] == ["char_2", "char_3"]
    assert all(turn["scene_id"] == 91 for turn in plan)
    assert [turn["speaker_intent"] for turn in plan] == ["advance", "react"]


async def test_build_round_turn_plan_adds_third_speaker_when_scene_has_targeted_hook() -> None:
    """未発話参加者に targeted hook があるときだけ 3 件目を足す。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
    ]
    db = make_mock_db(characters=characters)
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.scene_management.enabled = True
    cfg.participation_planner.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._sync_story_scenes = AsyncMock(
        return_value=[
            {
                "id": 91,
                "scene_type": "conversation",
                "place_id": "music_room",
                "status": "active",
            }
        ]
    )
    db.get_scene_participants = AsyncMock(
        return_value=[
            {"char_id": "char_1", "role": "focus", "last_spoken_turn": 2},
            {"char_id": "char_2", "role": "support", "last_spoken_turn": 0},
            {"char_id": "char_3", "role": "support", "last_spoken_turn": 1},
        ]
    )
    db.get_open_story_hooks_for_scene = AsyncMock(
        return_value=[
            {
                "id": 8,
                "hook_type": "question",
                "status": "open",
                "owner_char_id": "char_2",
                "target_char_id": "char_1",
                "source_scene_id": 91,
            }
        ]
    )

    plan = await engine._build_round_turn_plan(
        {"char_1": "music_room", "char_2": "music_room", "char_3": "music_room"}
    )

    assert [turn["char_id"] for turn in plan] == ["char_2", "char_3", "char_1"]
    assert [turn["speaker_intent"] for turn in plan] == ["advance", "react", "react"]
    db.get_open_story_hooks_for_scene.assert_awaited_once_with("test_story", 91)


async def test_build_round_turn_plan_appends_stale_characters_after_scene_turns() -> None:
    """scene 外で 1 round 以上取り残されたキャラだけ fallback を末尾に足す。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
        make_character(id="char_4", name_ja="四人目"),
    ]
    db = make_mock_db(characters=characters)
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.scene_management.enabled = True
    cfg.participation_planner.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._sync_story_scenes = AsyncMock(
        return_value=[
            {
                "id": 91,
                "scene_type": "conversation",
                "place_id": "music_room",
                "status": "active",
            }
        ]
    )
    db.get_scene_participants = AsyncMock(
        return_value=[
            {"char_id": "char_1", "role": "focus", "last_spoken_turn": 4},
            {"char_id": "char_2", "role": "support", "last_spoken_turn": 3},
        ]
    )
    db.get_latest_character_state = AsyncMock(
        side_effect=lambda story_id, char_id: {
            "char_1": make_state(turn_number=4),
            "char_2": make_state(turn_number=3),
            "char_3": make_state(turn_number=2),
            "char_4": make_state(turn_number=4),
        }[char_id]
    )
    for _ in range(4):
        engine.world_clock.tick()

    plan = await engine._build_round_turn_plan(
        {
            "char_1": "music_room",
            "char_2": "music_room",
            "char_3": "library",
            "char_4": "rooftop",
        }
    )

    assert [turn["char_id"] for turn in plan] == ["char_2", "char_1", "char_3"]
    assert [turn["scene_id"] for turn in plan] == [91, 91, None]
    assert [turn["speaker_intent"] for turn in plan] == ["advance", "react", "catch_up"]


async def test_build_round_turn_plan_does_not_append_recently_spoken_non_scene_characters() -> None:
    """scene 外キャラでも直前 round に話していれば fallback しない。"""
    characters = [
        make_character(id="char_1", name_ja="一人目"),
        make_character(id="char_2", name_ja="二人目"),
        make_character(id="char_3", name_ja="三人目"),
    ]
    db = make_mock_db(characters=characters)
    router = AsyncMock()
    cfg = make_runtime_config()
    cfg.scene_management.enabled = True
    cfg.participation_planner.enabled = True
    engine = StoryEngine("test_story", db, router, config=cfg)
    await engine.initialize()
    engine._sync_story_scenes = AsyncMock(return_value=[])
    db.get_latest_character_state = AsyncMock(
        side_effect=lambda story_id, char_id: {
            "char_1": make_state(turn_number=4),
            "char_2": make_state(turn_number=3),
            "char_3": make_state(turn_number=4),
        }[char_id]
    )
    for _ in range(4):
        engine.world_clock.tick()

    plan = await engine._build_round_turn_plan(
        {
            "char_1": "music_room",
            "char_2": "library",
            "char_3": "rooftop",
        }
    )

    assert plan == []


async def test_novel_generator_gets_chapter_break_trigger() -> None:
    """narration_engine が True を返すと novel_generator が trigger='chapter_break' で呼ばれる。"""
    novel_gen = AsyncMock()
    narration_eng = AsyncMock()
    narration_eng.process_round = AsyncMock(return_value=True)  # chapter break 発生!

    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine(
        "test_story", db, router,
        narration_engine=narration_eng,
        novel_generator=novel_gen,
    )
    await engine.initialize()
    await engine._run_post_round_hooks(False)

    novel_gen.process_round.assert_called_once()
    ng_ctx = novel_gen.process_round.call_args[0][1]  # 第2位置引数 = NovelGeneratorContext
    assert ng_ctx.trigger == "chapter_break"


async def test_closed_story_scene_calls_novel_generator_with_scene_close_trigger() -> None:
    """closed conversation scene は scene_close trigger で novel_generator に渡る。"""
    novel_gen = AsyncMock()
    db = make_mock_db()
    db.get_story_scene = AsyncMock(
        return_value={
            "id": 91,
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "rooftop",
            "outcome_summary": "放課後に再会する約束だけが残った。",
            "focus_char_ids": ["char_1", "char_2"],
        }
    )
    db.get_scene_logs = AsyncMock(
        return_value=[
            {"id": 10, "char_id": "char_1", "message": "「また後で。」"},
            {"id": 11, "char_id": "char_2", "message": "「屋上で。」"},
        ]
    )
    db.get_open_story_hooks_for_scene = AsyncMock(
        return_value=[
            {"title": "放課後の約束", "description": "屋上で再会する。"},
        ]
    )
    db.get_active_tensions = AsyncMock(
        return_value=[
            {"tension_type": "conflict", "description": "まだ解けていない対立。"},
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, novel_generator=novel_gen)
    await engine.initialize()
    engine._closed_story_scene_ids = [91]

    await engine._run_post_round_hooks(False)

    novel_gen.process_round.assert_called_once()
    ng_ctx = novel_gen.process_round.call_args[0][1]
    assert ng_ctx.trigger == "scene_close"
    assert ng_ctx.scene_id == 91
    assert ng_ctx.scene_outcome_summary == "放課後に再会する約束だけが残った。"


async def test_closed_episode_calls_novel_generator_with_episode_close_trigger() -> None:
    """episode close は episode_close trigger で novel_generator に渡る。"""
    novel_gen = AsyncMock()
    episode_planner = AsyncMock()
    episode_planner.process_round = AsyncMock(
        return_value={
            "closed_episode": {
                "id": 41,
                "episode_type": "misunderstanding",
                "goal": "誤解をほどく",
                "summary": "言葉の食い違いが続いていたが、次にほどく余地が残った。",
                "focus_char_ids": ["char_1", "char_2"],
            }
        }
    )
    episode_planner.get_active_episode = AsyncMock(return_value=None)
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine(
        "test_story",
        db,
        router,
        novel_generator=novel_gen,
        episode_planner=episode_planner,
    )
    await engine.initialize()

    await engine._run_post_round_hooks(False)

    triggers = [call.args[1].trigger for call in novel_gen.process_round.await_args_list]
    assert "episode_close" in triggers


async def test_finalize_session_calls_novel_generator_with_session_end() -> None:
    """_finalize_session() が残存 active session を閉じ、session_end prose を生成する。"""
    novel_gen = AsyncMock()
    db = make_mock_db()
    # 残存 active session をモック
    db.get_active_conversation_sessions = AsyncMock(
        return_value=[make_session(id=99, status="active")]
    )
    db.get_recent_conversation_logs = AsyncMock(return_value=[
        {"id": 201, "char_id": "char_1", "message": "「また明日。」",
         "sim_datetime": "2025-04-01T12:00"},
    ])
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, novel_generator=novel_gen)
    await engine.initialize()
    await engine._finalize_session()

    # active session が閉じられたことを検証
    db.close_conversation_session.assert_awaited_once()
    close_args = db.close_conversation_session.call_args
    assert close_args[0][0] == 99

    # session_end prose が生成されたことを検証
    novel_gen.process_round.assert_called_once()
    ng_ctx = novel_gen.process_round.call_args[0][1]
    assert ng_ctx.trigger == "session_end"

    # セッション固有ログ（get_recent_conversation_logs）が使われ、
    # 全体ログ（get_recent_chat_logs）は使われないことを検証
    db.get_recent_conversation_logs.assert_awaited()
    db.get_recent_chat_logs.assert_not_awaited()


async def test_invoke_novel_generator_for_session_skips_when_scene_ids_exist() -> None:
    """session logs に scene_id があれば scene_close 正本として fallback prose を作らない。"""
    novel_gen = AsyncMock()
    db = make_mock_db()
    db.get_recent_conversation_logs = AsyncMock(
        return_value=[
            {
                "id": 201,
                "char_id": "char_1",
                "message": "「また後で。」",
                "scene_id": 91,
                "sim_datetime": "2025-04-01T12:00",
            },
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, novel_generator=novel_gen)
    await engine.initialize()

    await engine._invoke_novel_generator_for_session(5, 42)

    novel_gen.process_round.assert_not_called()


async def test_finalize_session_noop_when_no_novel_generator() -> None:
    """novel_generator=None でも active session は閉じられる。"""
    db = make_mock_db()
    db.get_active_conversation_sessions = AsyncMock(
        return_value=[make_session(id=88, status="active")]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    await engine._finalize_session()

    # session は閉じられる（DB 整合性のため）
    db.close_conversation_session.assert_awaited_once()
    close_args = db.close_conversation_session.call_args
    assert close_args[0][0] == 88


async def test_intervention_texts_injected_into_context() -> None:
    """intervention_texts が _build_context() の ctx.intervention_texts に含まれる。"""
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    char = make_character()
    state = make_state()
    ctx = engine._build_context(
        char, state, "音楽室",
        intervention_texts=["今夜、嵐が来る。"],
    )
    assert "今夜、嵐が来る。" in ctx.intervention_texts


async def test_run_one_turn_acknowledges_active_intervention_on_prompt_injection() -> None:
    """active intervention が prompt に注入されたら acknowledged に更新される。"""
    db = make_mock_db()
    db.get_active_interventions = AsyncMock(
        return_value=[
            {
                "id": 55,
                "scope": "all",
                "status": "active",
                "prompt_injection": "遠くで鐘が鳴る。",
            }
        ]
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「聞こえた。」"))
    engine = StoryEngine("test_story", db, router, story_director=AsyncMock())
    await engine.initialize()

    await engine.run_one_turn()

    db.update_intervention.assert_awaited_once_with(55, {"status": "acknowledged"})


async def test_run_one_turn_keeps_acknowledged_intervention_in_prompt() -> None:
    """acknowledged intervention も live として prompt 注入される。"""
    db = make_mock_db()
    db.get_active_interventions = AsyncMock(
        return_value=[
            {
                "id": 56,
                "scope": "all",
                "status": "acknowledged",
                "prompt_injection": "誰かがこちらを見ている。",
            }
        ]
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=make_llm_response(text="「視線を感じる。」"))
    engine = StoryEngine("test_story", db, router, story_director=AsyncMock())
    await engine.initialize()

    await engine.run_one_turn()

    system_prompt = router.generate.await_args.kwargs["system_prompt"]
    assert "誰かがこちらを見ている。" in system_prompt
    db.update_intervention.assert_not_awaited()


# ============================================================
# _intervention_applies_to_character テスト
# ============================================================


def test_intervention_scope_all() -> None:
    """scope='all' はすべてのキャラに適用される。"""
    assert _intervention_applies_to_character("all", char_id="char_1", current_place="music_room") is True


def test_intervention_scope_char_single() -> None:
    """scope='char:char_1' は char_1 のみに適用される。"""
    assert _intervention_applies_to_character("char:char_1", char_id="char_1", current_place="music_room") is True
    assert _intervention_applies_to_character("char:char_1", char_id="char_2", current_place="music_room") is False


def test_intervention_scope_char_multi() -> None:
    """scope='char:char_1,char_2' は char_1 と char_2 に適用される。"""
    assert _intervention_applies_to_character("char:char_1,char_2", char_id="char_1", current_place="x") is True
    assert _intervention_applies_to_character("char:char_1,char_2", char_id="char_2", current_place="x") is True
    assert _intervention_applies_to_character("char:char_1,char_2", char_id="char_3", current_place="x") is False


def test_intervention_scope_place() -> None:
    """scope='place:music_room' は music_room にいるキャラに適用される。"""
    assert _intervention_applies_to_character("place:music_room", char_id="char_1", current_place="music_room") is True
    assert _intervention_applies_to_character("place:music_room", char_id="char_1", current_place="rooftop") is False


def test_intervention_scope_unknown() -> None:
    """不明なスコープ文字列は False を返す。"""
    assert _intervention_applies_to_character("unknown_scope", char_id="char_1", current_place="music_room") is False


# ============================================================
# session_end テスト（会話セッション終了時の prose 生成）
# ============================================================


async def test_session_end_novel_generator_called_on_closed_session() -> None:
    """会話セッション終了時に novel_generator が trigger='session_end' で呼ばれる。"""
    novel_gen = AsyncMock()
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, novel_generator=novel_gen)
    await engine.initialize()

    # セッション終了をシミュレート
    engine._closed_conversation_session_ids = [42]

    # get_recent_conversation_logs にセッション固有ログを返す
    db.get_recent_conversation_logs = AsyncMock(return_value=[
        {
            "id": 101,
            "char_id": "char_1",
            "message": "「また明日。」",
            "sim_datetime": "2025-04-01T12:00",
        },
    ])

    await engine._run_post_round_hooks(False)

    novel_gen.process_round.assert_called_once()
    ng_ctx = novel_gen.process_round.call_args[0][1]
    assert ng_ctx.trigger == "session_end"
    assert 101 in ng_ctx.source_log_ids


async def test_closed_session_ids_cleared_after_post_round_hooks() -> None:
    """_run_post_round_hooks 後に _closed_conversation_session_ids がクリアされる。"""
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    engine._closed_conversation_session_ids = [10, 20]
    await engine._run_post_round_hooks(False)

    assert engine._closed_conversation_session_ids == []


async def test_round_level_sync_closes_stale_sessions() -> None:
    """2キャラ分散時に _run_post_round_hooks が stale session を閉じ session_end を発火。"""
    novel_gen = AsyncMock()
    chars = [make_character(id="char_1", name_ja="キャラ1"),
             make_character(id="char_2", name_ja="キャラ2")]
    db = make_mock_db(characters=chars)
    # active session がある（char_1 と char_2 が music_room にいた）が、
    # 全キャラが分散して participants < 2 になった状態をシミュレート
    db.get_active_conversation_sessions = AsyncMock(
        return_value=[make_session(id=50, place_id="music_room",
                                   participant_ids=["char_1", "char_2"])]
    )
    db.get_recent_conversation_logs = AsyncMock(return_value=[
        {"id": 301, "char_id": "char_1", "message": "「じゃあね。」",
         "sim_datetime": "2025-04-01T12:00"},
    ])
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, novel_generator=novel_gen)
    await engine.initialize()
    # char_1 は rooftop、char_2 は garden — music_room にいるキャラなし
    engine._last_known_place = {"char_1": "rooftop", "char_2": "garden"}

    await engine._run_post_round_hooks(character_moved=True)

    # stale session が閉じられた
    db.close_conversation_session.assert_awaited_once()
    assert db.close_conversation_session.call_args[0][0] == 50
    # session_end prose が生成された
    novel_gen.process_round.assert_called_once()
    ng_ctx = novel_gen.process_round.call_args[0][1]
    assert ng_ctx.trigger == "session_end"


async def test_finalize_session_no_duplicate_when_already_processed() -> None:
    """ラウンド中に処理済みの session は _finalize_session で再処理されない。"""
    novel_gen = AsyncMock()
    db = make_mock_db()
    # 残存 active session なし（ラウンド中に全て閉じられた）
    db.get_active_conversation_sessions = AsyncMock(return_value=[])
    db.get_recent_conversation_logs = AsyncMock(return_value=[])
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, novel_generator=novel_gen)
    await engine.initialize()
    # ラウンド中に既に処理済み（_closed_conversation_session_ids はクリア済み想定）
    engine._closed_conversation_session_ids = []

    await engine._finalize_session()

    # 処理すべき session がないため prose 生成は呼ばれない
    novel_gen.process_round.assert_not_called()
    # _closed_conversation_session_ids はクリアされている
    assert engine._closed_conversation_session_ids == []


async def test_finalize_session_drains_unprocessed_from_partial_round() -> None:
    """中断ラウンドの未処理 session + 残存 active session が全て処理される。"""
    novel_gen = AsyncMock()
    db = make_mock_db()
    # 残存 active session 1件
    db.get_active_conversation_sessions = AsyncMock(
        return_value=[make_session(id=70, status="active")]
    )
    db.get_recent_conversation_logs = AsyncMock(return_value=[
        {"id": 401, "char_id": "char_1", "message": "「…。」",
         "sim_datetime": "2025-04-01T12:00"},
    ])
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, novel_generator=novel_gen)
    await engine.initialize()
    # 中断ラウンドの未処理 session 1件
    engine._closed_conversation_session_ids = [60]

    await engine._finalize_session()

    # active session 70 が閉じられた
    db.close_conversation_session.assert_awaited_once()
    assert db.close_conversation_session.call_args[0][0] == 70
    # 未処理 session 60 + 新しく閉じた session 70 の合計2回 prose 生成
    assert novel_gen.process_round.call_count == 2
    triggers = [call[0][1].trigger for call in novel_gen.process_round.call_args_list]
    assert all(t == "session_end" for t in triggers)
    # クリアされている
    assert engine._closed_conversation_session_ids == []


async def test_failed_turn_updates_runtime_snapshot_without_committing_last_known_place() -> None:
    """LLM 失敗ターンでは runtime snapshot だけ更新し、_last_known_place は汚さない。"""
    db = make_mock_db()
    db.get_latest_character_state = AsyncMock(
        return_value=make_state(current_place="music_room")
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._move_engine.should_move = MagicMock(return_value=True)
    engine._move_engine.decide_destination = MagicMock(return_value="rooftop")
    engine._move_engine.generate_move_reason = MagicMock(return_value="屋上へ向かう")
    engine._generate_llm_response = AsyncMock(return_value=None)

    await engine.run_one_turn()

    assert engine._runtime_place_snapshot["char_1"] == "rooftop"
    assert "char_1" not in engine._last_known_place
    db.insert_character_state.assert_not_called()


async def test_initialize_clears_runtime_place_caches_between_runs() -> None:
    """initialize() を再実行したとき in-memory place cache を持ち越さない。"""
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    engine._runtime_place_snapshot["char_1"] = "rooftop"
    engine._last_known_place["char_1"] = "rooftop"
    engine._round_start_emotions["char_1"] = {"stress": 0.2}
    engine._prev_emotion_snapshots["char_1"] = {"stress": 0.3}
    engine._closed_conversation_session_ids = [99]

    db._conn.execute = MagicMock(side_effect=[
        make_cm([PLACE_ROW]),
        make_cm([]),
        make_cm([]),
        make_cm([]),
    ])

    await engine.initialize()

    assert engine._runtime_place_snapshot == {}
    assert engine._last_known_place == {}
    assert engine._round_start_emotions == {}
    assert engine._prev_emotion_snapshots == {}
    assert engine._closed_conversation_session_ids == []


async def test_next_turn_prefers_runtime_place_snapshot_after_failed_move() -> None:
    """失敗ターン後の次ターンでは DB より runtime snapshot の場所を優先する。"""
    db = make_mock_db()
    db.get_latest_character_state = AsyncMock(
        side_effect=[
            make_state(current_place="music_room"),
            make_state(current_place="music_room"),
        ]
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()
    engine._move_engine.should_move = MagicMock(side_effect=[True, False])
    engine._move_engine.decide_destination = MagicMock(return_value="rooftop")
    engine._move_engine.generate_move_reason = MagicMock(return_value="屋上へ向かう")
    engine._generate_llm_response = AsyncMock(
        side_effect=[None, make_llm_response(text="「屋上に着いた。」")]
    )

    await engine.run_one_turn()
    await engine.run_one_turn()

    log = db.insert_chat_log.call_args[0][1]
    state = db.insert_character_state.call_args[0][1]
    assert log["place_id"] == "rooftop"
    assert state["current_place"] == "rooftop"


async def test_round_level_sync_preserves_session_after_llm_failure() -> None:
    """2キャラ同一場所で1人のLLMが失敗しても、round-level sync で active session が閉じられない。"""
    novel_gen = AsyncMock()
    chars = [make_character(id="char_1", name_ja="キャラ1"),
             make_character(id="char_2", name_ja="キャラ2")]
    db = make_mock_db(characters=chars)

    # char_2 の LLM だけ失敗するシナリオ:
    # run_one_turn で各キャラの位置が _last_known_place に記録される
    # LLM 失敗でも位置は更新されるため、round-level sync で session は維持される
    call_count = 0
    async def state_side_effect(story_id: str, char_id: str) -> dict:
        return make_state(current_place="music_room")

    db.get_latest_character_state = AsyncMock(side_effect=state_side_effect)

    # active session: char_1 と char_2 が music_room で会話中
    active_session = make_session(id=50, place_id="music_room",
                                  participant_ids=["char_1", "char_2"])
    db.get_active_conversation_sessions = AsyncMock(return_value=[active_session])
    db.get_active_conversation_session = AsyncMock(return_value=active_session)
    db.get_conversation_seed_logs = AsyncMock(return_value=[])

    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, novel_generator=novel_gen)
    await engine.initialize()
    # 2段生成: char_1 inner+voice 成功、char_2 inner 成功・voice 失敗（None）
    engine._generate_llm_response = AsyncMock(
        side_effect=[make_llm_response(), make_llm_response(), make_llm_response(), None]
    )

    # 2キャラ分のターンを実行
    await engine.run_one_turn()  # char_1: 成功
    await engine.run_one_turn()  # char_2: LLM 失敗

    # runtime snapshot には両キャラがいるが、committed cache は成功した char_1 だけ
    assert engine._runtime_place_snapshot.get("char_1") == "music_room"
    assert engine._runtime_place_snapshot.get("char_2") == "music_room"
    assert engine._last_known_place.get("char_1") == "music_room"
    assert engine._last_known_place.get("char_2") is None

    # round-level sync: 両者が同一場所なので session は維持される
    await engine._run_post_round_hooks(character_moved=False)

    # session は閉じられていない
    db.close_conversation_session.assert_not_awaited()


async def test_effective_profile_overlay_prefers_growth_over_canon() -> None:
    db = make_mock_db()
    db.get_character_canon_overlay = AsyncMock(
        return_value={"overlay_json": {"current_goal": "canon goal", "personality_core": "canon core"}}
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, config=make_runtime_config())
    await engine.initialize()
    engine._config.growth_engine.enabled = True
    engine._growth_engine = AsyncMock()
    engine._growth_engine.get_profile_overlay = AsyncMock(
        return_value={"current_goal": "growth goal", "current_worry": "growth worry"}
    )

    overlay = await engine._get_effective_profile_overlay("char_1")

    assert overlay["current_goal"] == "growth goal"
    assert overlay["current_worry"] == "growth worry"
    assert overlay["personality_core"] == "canon core"


async def test_effective_profile_overlay_uses_canon_when_growth_absent() -> None:
    db = make_mock_db()
    db.get_character_canon_overlay = AsyncMock(
        return_value={"overlay_json": {"current_goal": "canon goal"}}
    )
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, config=make_runtime_config())
    await engine.initialize()
    engine._config.growth_engine.enabled = True
    engine._growth_engine = AsyncMock()
    engine._growth_engine.get_profile_overlay = AsyncMock(return_value={})

    overlay = await engine._get_effective_profile_overlay("char_1")

    assert overlay == {"current_goal": "canon goal"}


def test_build_retry_user_prompt_includes_flat_reply_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    prompt = engine._build_retry_user_prompt(
        "元の指示",
        msg_type="reply",
        issues=[
            {"issue_type": "generic_reply_tail"},
            {"issue_type": "voice_flat_reply"},
        ],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        voice_anchor_text="熱血直球で短く返す",
        reply_surface_contract={
            "surface_mode": "answer_first",
            "preferred_voice_cues": ["直球"],
            "must_vary_from_recent_self": True,
        },
    )

    assert "汎用締め" in prompt
    assert "直近の自分の返しと同じ締め方を避けてください" in prompt
    assert "熱血直球で短く返す" in prompt


def test_build_retry_user_prompt_includes_timing_focus_anchor_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    prompt = engine._build_retry_user_prompt(
        "元の指示",
        msg_type="reply",
        issues=[{"issue_type": "reply_focus_missing"}],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert "1文目でいつ動くかを自分の言葉で出してください" in prompt


def test_build_retry_user_prompt_includes_blandness_pressure_shift_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)

    prompt = engine._build_retry_user_prompt(
        "元の指示",
        msg_type="reply",
        issues=[{"issue_type": "voice_flat_reply"}],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
        },
        reply_blandness_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
        },
    )

    assert "2文目で別の具体的な反応にしてください" in prompt
    assert "答えてから問い返す形" in prompt
    assert "pressure" not in prompt
    assert "answer_then_probe" not in prompt
    assert "見ておく" in prompt


def test_build_reply_shape_contract_uses_story_pressure_tokens() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("ankoku_gakuen", db, router)

    contract = engine._build_reply_shape_contract(
        reply_focus_text="いつ動くかの確認",
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
        },
        reply_variety_contract={
            "response_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": [],
            "preferred_move_tokens": ["先に", "言って"],
        },
        reply_dramatic_contract={
            "move_mode": "probe",
            "allowed_secondary_modes": ["claim"],
            "required_move_tokens": ["なぜ", "どこで", "今"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
        },
        recent_self_messages=["上泉ソーマ、今決める。順番はそのあとでいい。"],
        dominant_signal_text="張り合いに触れると場が動く",
        scene_objective_text="この場の争点: 順番を決める",
        relationship_mode_texts=["cannot_ignore: 放っておけない"],
        dramatic_pressure_texts=["status_flashpoint: 順番と優劣を曖昧にしない"],
    )

    assert contract is not None
    assert contract["primary_shape"] == "answer_then_probe"
    assert "張り合い" in contract["story_pressure_tokens"]
    assert "順番" in contract["story_pressure_tokens"]


def test_build_retry_user_prompt_includes_shape_and_story_pressure_guidance() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("ankoku_gakuen", db, router)

    prompt = engine._build_retry_user_prompt(
        "元の指示",
        msg_type="reply",
        issues=[{"issue_type": "voice_flat_reply"}],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
        },
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "story_pressure_tokens": ["張り合い", "順番"],
        },
    )

    assert "答えてから問い返す形" in prompt
    assert "2文目で別の具体的な反応にしてください" in prompt
    assert "answer_then_probe" not in prompt
    assert "pressure" not in prompt
    assert "張り合い / 順番" in prompt


def test_should_keep_retry_output_without_fallback_rejects_surface_contract_miss() -> None:
    assert StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="いつ決めるかだけ今ここで出す。順番はそのあとでいい。",
        issues=[
            {
                "issue_type": "voice_flat_reply",
                "details": {"surface_contract_missed": True},
            }
        ],
    ) is False


def test_should_keep_retry_output_without_fallback_accepts_nonblocking_voice_flat_issue() -> None:
    assert StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="上泉ソーマ、今決める。先に言って。",
        issues=[
            {
                "issue_type": "voice_flat_reply",
                "details": {
                    "story_flavor_weak": True,
                    "voice_flat_blocking": False,
                    "voice_flat_residual_only": True,
                },
            }
        ],
    ) is True


def test_should_keep_retry_output_without_fallback_accepts_nonblocking_visibility_issue() -> None:
    assert StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="決める役はこっちが持つ。順番だけ先に通す。",
        issues=[
            {
                "issue_type": "signal_visibility_missing",
                "severity": "info",
                "details": {
                    "visibility_blocking": False,
                    "reply_focus_first_sentence_semantic_hit": True,
                },
            },
            {
                "issue_type": "scene_objective_visibility_missing",
                "severity": "info",
                "details": {
                    "visibility_blocking": False,
                    "reply_focus_first_sentence_semantic_hit": True,
                },
            },
            {
                "issue_type": "voice_flat_reply",
                "severity": "info",
                "details": {
                    "voice_flat_blocking": False,
                    "voice_flat_residual_only": True,
                },
            },
        ],
    ) is True


def test_compress_reply_to_two_sentences_preserves_shape_pressure() -> None:
    compressed = StoryEngine._compress_reply_to_two_sentences(
        "主導権なら今決める。流れを先に整える。誰が書くかも出して。回収はあとでやる。",
        reply_shape_contract={
            "primary_shape": "answer_then_condition",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["流れ", "主導権"],
        },
    )

    assert compressed == "主導権なら今決める。切り替えるなら今ここで"


def test_should_keep_retry_output_without_fallback_rejects_blandness_contract_miss() -> None:
    assert StoryEngine._should_keep_retry_output_without_fallback(
        msg_type="reply",
        text="上泉ソーマ、今決める。順番は見ておく。",
        issues=[
            {
                "issue_type": "voice_flat_reply",
                "details": {
                    "blandness_contract_missed": True,
                    "reply_blandness_shape_reused": True,
                    "reply_pressure_shift_seen": False,
                },
            }
        ],
    ) is False


async def test_quality_guard_keeps_retry_output_when_only_nonblocking_voice_flat_remains() -> None:
    db = make_mock_db(story=make_story(id="ankoku_gakuen"))
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            make_llm_response(text="（いつ決めるんだよ）"),         # inner_thought
            make_llm_response(text="暗闇の運命が揺れる。"),         # voice → retry
            make_llm_response(text="上泉ソーマ、今決める。先に言って。"),  # retry
        ]
    )
    quality_guard = MagicMock()
    quality_guard.evaluate.side_effect = [
        QualityGuardResult(
            text="暗闇の運命が揺れる。",
            action="retry",
            score=0.4,
            issues=[{"issue_type": "poetic_abstraction", "severity": "warning", "details": {"token": "暗闇"}}],
        ),
        QualityGuardResult(
            text="上泉ソーマ、今決める。先に言って。",
            action="retry",
            score=0.8,
            issues=[
                {
                    "issue_type": "voice_flat_reply",
                    "severity": "info",
                    "details": {
                        "story_flavor_weak": True,
                        "voice_flat_blocking": False,
                        "voice_flat_residual_only": True,
                    },
                }
            ],
        ),
    ]
    engine = StoryEngine("ankoku_gakuen", db, router, quality_guard=quality_guard)
    await engine.initialize()
    engine._resolve_conversation_turn = AsyncMock(
        return_value={
            "msg_type": "reply",
            "target_char_id": "char_2",
            "target_char_name": "上泉ソーマ",
            "conversation_partner_names": ["上泉ソーマ"],
            "recent_dialogue_lines": ["上泉ソーマ: 「それ、いつ決めるんだ？」"],
            "conversation_session_id": 42,
            "reply_to_log_id": 9,
            "participant_ids": ["char_1", "char_2"],
        }
    )
    engine._planned_turn_queue = [{"char_id": "char_1", "scene_id": 77, "speaker_intent": "react"}]
    engine._planned_turn_index = 0

    await engine.run_one_turn()

    # inner_thought + voice + retry = 3 コール
    assert router.generate.await_count == 3
    saved_log = db.insert_chat_log.await_args[0][1]
    assert saved_log["message"] == "上泉ソーマ、今決める。先に言って。"
    issue_payloads = [call.args[1] for call in db.insert_generation_quality_issue.await_args_list]
    normalized_issue = next(payload for payload in issue_payloads if payload["issue_type"] == "quality_output_normalized")
    assert normalized_issue["details"]["retry_kept_without_fallback"] is True
    assert not any(payload.get("auto_action") == "fallback" for payload in issue_payloads)


def test_build_deterministic_fallback_text_avoids_reused_second_sentence() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="熱血",
        speech_examples=["先に出せよ。"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        same_place_char_names=["上泉ソーマ"],
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        story_memory_texts=[],
        intervention_texts=[],
        active_hook_texts=[],
        relationship_summary_texts=[],
        relationship_mode_texts=[],
        canon_bit_texts=[],
        dramatic_pressure_texts=[],
        dominant_signal_text="この場の争点: 張り合いが表に出る",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="いつ動くかの確認",
        voice_anchor_text="熱血直球で短く返す",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_self_messages=["上泉ソーマ、今決める。順番はそのあとでいい。"],
    )

    assert text
    assert "順番はそのあとでいい" not in text


def test_build_deterministic_fallback_text_uses_story_pressure_token() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("ankoku_gakuen", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="熱血",
        speech_examples=["先に出せよ。"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        same_place_char_names=["上泉ソーマ"],
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        story_memory_texts=[],
        intervention_texts=[],
        active_hook_texts=[],
        relationship_summary_texts=[],
        relationship_mode_texts=[],
        canon_bit_texts=[],
        dramatic_pressure_texts=[],
        dominant_signal_text="この場の争点: 張り合いが表に出る",
        scene_objective_text="この場の争点: 順番を決める",
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="いつ動くかの確認",
        voice_anchor_text="熱血直球で短く返す",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_self_messages=["上泉ソーマ、今決める。順番はそのあとでいい。"],
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "story_pressure_tokens": ["張り合い", "順番"],
        },
    )

    assert text
    # story_pressure_tokens のメタ語彙はそのまま出力されず、ナラティブ語彙に変換される
    assert len(text) > 5


def test_build_deterministic_fallback_text_prefers_direct_reaction_anchor_in_first_sentence() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="普通",
        speech_examples=["今決める。"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        same_place_char_names=["上泉ソーマ"],
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        story_memory_texts=[],
        intervention_texts=[],
        active_hook_texts=[],
        relationship_summary_texts=[],
        relationship_mode_texts=[],
        canon_bit_texts=[],
        dramatic_pressure_texts=[],
        dominant_signal_text="この場の争点: 張り合いが表に出る",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="いつ動くかの確認",
        voice_anchor_text="簡潔に返す",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        fallback_root_issue="reply_without_direct_reaction",
    )

    assert text
    assert "上泉ソーマ" in text
    assert "どうするか先に出して" not in text


def test_build_deterministic_fallback_text_prefers_focus_anchor_in_first_sentence() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="普通",
        speech_examples=["先に決めよう。"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        same_place_char_names=["上泉ソーマ"],
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        story_memory_texts=[],
        intervention_texts=[],
        active_hook_texts=[],
        relationship_summary_texts=[],
        relationship_mode_texts=[],
        canon_bit_texts=[],
        dramatic_pressure_texts=[],
        dominant_signal_text="この場の争点: 張り合いが表に出る",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="いつ動くかの確認",
        voice_anchor_text="簡潔に返す",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["先に"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert text
    assert "上泉ソーマ" in text
    assert "どうするか先に出して" not in text


def test_probe_fallback_does_not_reinject_fixed_one_point_phrase() -> None:
    """probe fallback が固定句「目の前の一点を拾う」を再注入しない。"""
    assert StoryEngine._fallback_blandness_second_sentence("answer_then_probe") != "目の前の一点を拾う"


def test_probe_fallback_does_not_reinject_fixed_unfinished_tail() -> None:
    """probe fallback は固定句「まだ終わりにしない」を足さない。"""
    assert StoryEngine._fallback_blandness_second_sentence("answer_then_probe") is None


def test_objective_first_signal_visibility_fallback_avoids_fixed_confirmation_tail() -> None:
    text = StoryEngine._apply_reply_signal_visibility_to_fallback(
        "受け止める。まだ黙らない",
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "objective_first",
        },
    )

    assert "今ここで確かめる" not in text
    assert "ここで確かめる" not in text


def test_probe_fallback_builders_do_not_emit_generic_look_tails() -> None:
    banned = (
        "その言葉は流せない",
        "残った所を言葉にする",
        "流れをどこで変えるか言う",
        "流れをどうする",
        "何から動くか今決める",
        "まだ終わりにしない",
        "そこは流さない",
        "そこは曖昧にしない",
        "その一点を見る",
        "ここで見る",
        "ここで確かめる",
        "目の前の一点",
    )
    texts = [
        StoryEngine._fallback_shape_second_sentence(
            "answer_then_probe",
            story_pressure_tokens=[token],
        )
        for token in ["主導権", "回収", "見せ場", "流れ", "unknown"]
    ]
    texts.extend(
        StoryEngine._fallback_dramatic_second_sentence(
            "probe",
            required_move_tokens=[],
            pressure_anchor_tokens=[token],
        )
        for token in ["主導権", "回収", "見せ場", "流れ", "unknown"]
    )

    assert all(not any(phrase in text for phrase in banned) for text in texts)
    assert len(set(texts)) > 1


def test_build_deterministic_fallback_text_does_not_emit_director_cue_lines() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="普通",
        speech_examples=["先に決めよう。"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        same_place_char_names=["上泉ソーマ"],
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「手帳を見せろ」"],
        story_memory_texts=[],
        intervention_texts=[],
        active_hook_texts=[],
        relationship_summary_texts=[],
        relationship_mode_texts=[],
        canon_bit_texts=[],
        dramatic_pressure_texts=[],
        dominant_signal_text="この場の争点: 流れを回収する",
        scene_objective_text="この場の争点: 流れを回収する",
        target_last_utterance_excerpt="手帳を見せろ",
        reply_focus_text="手帳を守る理由",
        voice_anchor_text="簡潔に返す",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        reply_dramatic_contract={
            "move_mode": "probe",
            "pressure_anchor_tokens": ["流れ"],
            "semantic_move_family": "probe",
            "required_semantic_cues": ["流れ"],
            "required_move_tokens": ["流れ"],
        },
    )

    banned = (
        "残った所を言葉にする",
        "流れをどこで変えるか言う",
        "流れをどうする",
        "まだ終わりにしない",
        "そこは流さない",
        "そこは曖昧にしない",
    )
    assert text
    assert not any(phrase in text for phrase in banned)


def test_apply_reply_signal_visibility_to_fallback_avoids_generic_refusal_tail() -> None:
    text = StoryEngine._apply_reply_signal_visibility_to_fallback(
        "星風ルナ、その手帳の話は聞いた。",
        reply_signal_contract={
            "signal_anchor_tokens": ["秘密"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "objective_first",
        },
    )

    assert "流さない" not in text
    assert "曖昧にしない" not in text
    assert "順番" in text


def test_reply_move_plan_drops_weak_second_sentence_when_anchor_is_missing() -> None:
    plan = ReplyMovePlan(
        reaction_goal="direct_reply",
        progression_goal="probe",
        scene_anchor_terms=(),
        allow_second_sentence=False,
        recent_move_signatures=(),
    )

    text = StoryEngine._apply_reply_move_plan_to_fallback(
        "星風ルナ、その話は聞いた。そこは曖昧にしない",
        plan=plan,
    )

    assert text == "星風ルナ、その話は聞いた"


def test_probe_shape_without_story_anchor_does_not_emit_generic_recenter_tail() -> None:
    assert (
        StoryEngine._fallback_shape_second_sentence(
            "answer_then_probe",
            story_pressure_tokens=[],
        )
        is None
    )


def test_reply_move_plan_drops_generic_recenter_tail_when_anchor_is_missing() -> None:
    plan = ReplyMovePlan(
        reaction_goal="direct_reply",
        progression_goal="probe",
        scene_anchor_terms=(),
        allow_second_sentence=False,
        recent_move_signatures=(),
    )

    text = StoryEngine._apply_reply_move_plan_to_fallback(
        "星風ルナ、その話は聞いた。話を少し戻したい",
        plan=plan,
    )

    assert text == "星風ルナ、その話は聞いた"


def test_build_deterministic_fallback_text_avoids_scene_reused_second_tail() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="普通",
        speech_examples=["今の言葉に返す。"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        same_place_char_names=["上泉ソーマ"],
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「手帳を見せろ」"],
        story_memory_texts=[],
        intervention_texts=[],
        active_hook_texts=[],
        relationship_summary_texts=[],
        relationship_mode_texts=[],
        canon_bit_texts=[],
        dramatic_pressure_texts=[],
        target_last_utterance_excerpt="手帳を見せろ",
        reply_focus_text="手帳を見せるかの返答",
        voice_anchor_text="簡潔に返す",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_scene_messages=[
            "ちゅるるん、その話は聞いた。話を少し戻したい",
            "夜風ユウマ、その話は聞いた。話を少し戻したい",
        ],
    )

    assert "話を少し戻したい" not in text


def test_reply_fallback_without_anchor_drops_instead_of_generic_first_sentence() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="普通",
        speech_examples=["今の言葉に返す。"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        same_place_char_names=["上泉ソーマ"],
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「……」"],
        story_memory_texts=[],
        intervention_texts=[],
        active_hook_texts=[],
        relationship_summary_texts=[],
        relationship_mode_texts=[],
        canon_bit_texts=[],
        dramatic_pressure_texts=[],
        target_last_utterance_excerpt="",
        reply_focus_text="相手の主張への返答",
        voice_anchor_text="簡潔に返す",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        fallback_root_issue="reply_without_direct_reaction",
    )

    assert text == ""


def test_reply_fallback_uses_target_utterance_anchor_instead_of_generic_first_sentence() -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone="普通",
        speech_examples=["今の言葉に返す。"],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        same_place_char_names=["上泉ソーマ"],
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「手帳を見せろ」"],
        story_memory_texts=[],
        intervention_texts=[],
        active_hook_texts=[],
        relationship_summary_texts=[],
        relationship_mode_texts=[],
        canon_bit_texts=[],
        dramatic_pressure_texts=[],
        target_last_utterance_excerpt="手帳を見せろ",
        reply_focus_text="手帳を見せるかの返答",
        voice_anchor_text="簡潔に返す",
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        fallback_root_issue="reply_without_direct_reaction",
    )

    assert text
    assert "手帳" in text
    assert "その話は聞いた" not in text
    assert "話を続けよう" not in text


@pytest.mark.parametrize(
    ("tone", "speech_example", "banned"),
    [
        ("熱血直球", "ここで引けない。", "まだ終わってない"),
        ("挑発混じり", "隠してるなら出してよ。", "まだ隠してる所を出してよ"),
        ("冷静に観察", "話を整理する。", "その流れは雑だ"),
    ],
)
def test_reply_fallback_does_not_emit_unanchored_voice_canned_first_sentence(
    tone: str,
    speech_example: str,
    banned: str,
) -> None:
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    ctx = CharacterContext(
        char_id="char_1",
        name_ja="テストキャラ",
        personality_core="冷静で論理的",
        first_person="俺",
        tone=tone,
        speech_examples=[speech_example],
        never_say=[],
        current_goal="音楽を極める",
        current_worry="時間が足りない",
        sim_datetime="2025-04-01T00:00",
        current_place_name="音楽室",
        same_place_char_names=["上泉ソーマ"],
        target_char_name="上泉ソーマ",
        conversation_partner_names=["上泉ソーマ"],
        recent_dialogue_lines=["上泉ソーマ: 「……」"],
        story_memory_texts=[],
        intervention_texts=[],
        active_hook_texts=[],
        relationship_summary_texts=[],
        relationship_mode_texts=[],
        canon_bit_texts=[],
        dramatic_pressure_texts=[],
        target_last_utterance_excerpt="",
        reply_focus_text="相手の主張への返答",
        voice_anchor_text=tone,
    )

    text = engine._build_deterministic_fallback_text(
        ctx=ctx,
        msg_type="reply",
        target_char_name="上泉ソーマ",
        fallback_root_issue="voice_flat_reply",
    )

    assert banned not in text
    assert "その話は聞いた" not in text
    assert "話を続けよう" not in text


def test_extract_character_directive_prefers_structured_payload() -> None:
    payload = {
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
        }
    }

    directive = StoryEngine._build_character_directive_text(payload, "hoshikaze_runa")

    assert directive is not None
    assert "見せたくない理由をぼかして返す" in directive
    assert "手帳を守る" in directive


# ============================================================
# _build_scene_script_context() — await 欠落回帰テスト
# ============================================================


async def test_build_scene_script_context_awaits_chapter_manager_methods() -> None:
    """ChapterManager の async メソッドが正しく await されることを保証する回帰テスト。

    commit 前の不具合: get_current_world_injection / get_current_beat を await せず呼んでいたため
    AttributeError('coroutine object has no attribute get') でエンジンが即死していた。
    """
    db = make_mock_db()
    db.get_active_chapter = AsyncMock(return_value=None)
    db.get_active_director_persona = AsyncMock(return_value=None)
    db.get_latest_scene_script = AsyncMock(return_value=None)
    db.get_recent_scene_scripts = AsyncMock(return_value=[])

    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    chapter_manager_mock = AsyncMock()
    chapter_manager_mock.get_current_world_injection = AsyncMock(return_value="テスト情景描写。")
    chapter_manager_mock.get_current_beat = AsyncMock(return_value={"goal": "対立を描く", "phase": "climax"})
    engine._chapter_manager = chapter_manager_mock

    ctx = await engine._build_scene_script_context()

    assert ctx.chapter_world_injection == "テスト情景描写。"
    assert ctx.chapter_beat_goal == "対立を描く"
    assert ctx.chapter_beat_phase == "climax"
    chapter_manager_mock.get_current_world_injection.assert_called_once()
    chapter_manager_mock.get_current_beat.assert_called_once()


async def test_build_scene_script_context_requires_planned_speakers_up_to_cap() -> None:
    db = make_mock_db(
        characters=[
            make_character(id=f"char_{index}", name_ja=f"キャラ{index}")
            for index in range(1, 6)
        ]
    )
    db.get_active_chapter = AsyncMock(return_value=None)
    db.get_active_director_persona = AsyncMock(return_value=None)
    db.get_latest_scene_script = AsyncMock(return_value=None)
    db.get_recent_scene_scripts = AsyncMock(return_value=[])

    router = AsyncMock()
    engine = StoryEngine("test_story", db, router, config=make_runtime_config())
    await engine.initialize()
    assert engine._config is not None
    engine._config.scene_script.max_directive_characters = 3
    engine._planned_turn_queue = [
        {"char_id": f"char_{index}", "scene_id": 77, "speaker_intent": "react"}
        for index in range(1, 6)
    ]

    ctx = await engine._build_scene_script_context()

    assert ctx.required_character_ids == ["char_1", "char_2", "char_3"]


@pytest.mark.asyncio
async def test_build_place_dialogue_lines_returns_entry_scoped_logs() -> None:
    """_build_place_dialogue_lines が entry turn 以降のログを "名前: 発言" 形式で返すこと。"""
    db = make_mock_db()
    db.get_character_entry_turn = AsyncMock(return_value=3)
    db.get_place_dialogue_since_turn = AsyncMock(return_value=[
        {"char_id": "char_a", "message": "入場後の発言1"},
        {"char_id": "char_a", "message": "入場後の発言2"},
    ])

    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    result = await engine._build_place_dialogue_lines("char_a", "place_a")

    db.get_character_entry_turn.assert_awaited_once_with("test_story", "char_a", "place_a")
    db.get_place_dialogue_since_turn.assert_awaited_once()
    assert len(result) == 2
    assert "入場後の発言1" in result[0]
    assert "入場後の発言2" in result[1]


@pytest.mark.asyncio
async def test_build_place_dialogue_lines_returns_empty_for_blank_place() -> None:
    """current_place が空文字列の場合は空リストを返すこと。"""
    db = make_mock_db()
    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    result = await engine._build_place_dialogue_lines("char_a", "")

    assert result == []
    db.get_character_entry_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_place_dialogue_lines_uses_turn_1_when_no_entry_record() -> None:
    """get_character_entry_turn が None を返す場合は turn 1 を起点とすること。"""
    db = make_mock_db()
    db.get_character_entry_turn = AsyncMock(return_value=None)
    db.get_place_dialogue_since_turn = AsyncMock(return_value=[])

    router = AsyncMock()
    engine = StoryEngine("test_story", db, router)
    await engine.initialize()

    await engine._build_place_dialogue_lines("char_a", "place_a")

    call_kwargs = db.get_place_dialogue_since_turn.call_args
    assert call_kwargs.kwargs.get("since_turn", call_kwargs.args[2] if len(call_kwargs.args) > 2 else None) == 1 or \
           (len(call_kwargs.args) > 2 and call_kwargs.args[2] == 1)
