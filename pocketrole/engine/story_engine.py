"""engine/story_engine.py — メインシミュレーションループ（Phase 2 版）。

全キャラがラウンドロビンで14ステップのターン処理を行い、1周ごとに WorldClock を進める。

14ステップ:
  1. キャラ選択（ラウンドロビン）
  2. 最新状態取得
  3. 感情減衰
  4. 推奨場所算出
  5. 移動判定
  6. 移動実行（条件付き）
  7. 同一場所キャラ収集
  8. 異常検知
  9. 感情トリガー適用
 10. 秘密開示判定
 11. 発言スケジューリング
 12. 表情判定
 13. プロンプト構築 + LLM呼び出し
 14. DB保存
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from db.db_manager import DatabaseManager
from engine.config import ModelProfileConfig
from engine.emotion_manager import EmotionManager
from engine.llm.router import LLMRouter
from engine.llm_runtime import ResolvedStoryLLMConfig, resolve_story_llm_config
from engine.move_engine import MoveEngine
from engine.place_manager import PlaceManager
from engine.prompt_builder import (
    CharacterContext,
    _contains_backend_meta_vocabulary,
    build_current_affairs_prompt,
    build_group_prompt,
    build_initiation_prompt,
    build_inner_thought_prompt,
    build_monologue_prompt,
    build_reply_prompt,
    build_voiced_group_prompt,
    build_voiced_initiation_prompt,
    build_voiced_reply_prompt,
)
from engine.quality_guard import QualityGuard, QualityGuardResult
from engine.scheduler import Scheduler
from engine.secret_manager import SecretManager
from engine.story_style import get_story_style_profile, get_story_style_profile_by_mode
from engine.world_clock import WorldClock

from engine.ambient_context import AmbientContextManager, AmbientTurnContext
from engine.narration_engine import NarrationContext, NarrationEngine
from engine.pre_turn_scene_engine import PreTurnSceneEngine, SceneScriptContext
from engine.scene_hook_analyzer import SceneHookAnalyzer
from engine.story_memory import StoryMemoryManager
from engine.story_intent import get_story_intent_profile, get_story_intent_profile_by_mode
from engine.character_evolution import CharacterEvolutionManager
from engine.episode_planner import EpisodePlanner
from engine.chapter_manager import ChapterManager
from engine.director_persona import DirectorPersona
from engine.growth_engine import GrowthEngine
from engine.relationship_modes import RelationshipModeEngine
from engine.story_director import StoryDirector, StoryDirectorContext
from engine.interaction_patterns import InteractionPatternEngine
from engine.emergent_canonizer import EmergentCanonizer
from engine.canon_reignition import CanonReignitionEngine
from engine.canon_profile_writeback import CanonProfileWritebackEngine
from engine.conversation_motifs import ConversationMotifEngine
from engine.dramatic_pressure import DramaticPressureEvaluator

if TYPE_CHECKING:
    from engine.config import Config
    from engine.web_poster import WebPoster
    from engine.novel_generator import NovelGenerator
    from engine.chapter_generator import ChapterGenerator
    from db.news_store import NewsArticleStore

logger = logging.getLogger(__name__)

_RE_INNER_THOUGHT = re.compile(r"[（(]([^）)]+)[）)]")
_HOOK_PROMISE_CUES = ("約束", "必ず", "今度", "しよう", "してみせる")
_HOOK_CONFLICT_CUES = ("嫌", "違う", "やめ", "許せ", "おかしい", "ふざけ", "逃げ")
_HOOK_SUPPORTIVE_CUES = ("ありがとう", "大丈夫", "任せ", "一緒", "守る", "受け止める")
_HOOK_INTERROGATIVE_MARKERS = ("どうして", "なぜ", "何", "どこ", "いつ", "誰", "本当")
_SCENE_TURN_STARVATION_GAP = 2
_OBJECTIVE_SATURATED_MIN_LOGS = 3
_STALLED_CONVERSATION_MIN_AGE = 6
_SCENE_CLOSE_LOG_LIMIT = 6
_SCENE_CLOSE_BACKLOG_LOOKBACK_TURNS = 20
_SCENE_CLOSE_BACKLOG_LIMIT = 6


@dataclass(frozen=True)
class ReplyMovePlan:
    """fallback reply を表層句ではなく意味単位で整えるための軽量プラン。"""

    reaction_goal: str
    progression_goal: str
    scene_anchor_terms: tuple[str, ...]
    allow_second_sentence: bool
    recent_move_signatures: tuple[str, ...]


def _intervention_applies_to_character(
    scope: str, *, char_id: str, current_place: str
) -> bool:
    """介入スコープがキャラクターに適用されるかを判定する。"""
    if scope == "all":
        return True
    if scope.startswith("char:"):
        char_ids = {cid.strip() for cid in scope[5:].split(",")}
        return char_id in char_ids
    if scope.startswith("place:"):
        place = scope[6:].strip()
        return current_place == place
    return False


def _contains_any_cue(text: str, cues: tuple[str, ...]) -> bool:
    return any(cue in text for cue in cues)


def _looks_like_question(text: str) -> bool:
    stripped = text.strip()
    trailing_trimmed = stripped.rstrip("」』\"'）)]】")
    return (
        stripped.endswith("?")
        or stripped.endswith("？")
        or trailing_trimmed.endswith("?")
        or trailing_trimmed.endswith("？")
        or _contains_any_cue(stripped, _HOOK_INTERROGATIVE_MARKERS)
    )


def _normalize_hook_text(text: str) -> str:
    return " ".join(text.replace("？", "?").replace("。", " ").replace("、", " ").split())


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, value))


class StoryEngine:
    """シミュレーションエンジン本体（Phase 2 版）。

    DB からキャラ・状態・関係を読み、14ステップのターン処理を経て
    LLM で発言を生成し、結果を DB に保存する。
    全キャラ1周後に WorldClock を tick する。
    """

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        llm_router: LLMRouter,
        web_poster: "WebPoster | None" = None,
        config: "Config | None" = None,
        story_memory_manager: StoryMemoryManager | None = None,
        narration_engine: NarrationEngine | None = None,
        story_director: StoryDirector | None = None,
        character_evolution_manager: CharacterEvolutionManager | None = None,
        growth_engine: GrowthEngine | None = None,
        interaction_pattern_engine: InteractionPatternEngine | None = None,
        relationship_mode_engine: RelationshipModeEngine | None = None,
        emergent_canonizer: EmergentCanonizer | None = None,
        canon_reignition_engine: CanonReignitionEngine | None = None,
        canon_profile_writeback_engine: CanonProfileWritebackEngine | None = None,
        dramatic_pressure_evaluator: DramaticPressureEvaluator | None = None,
        episode_planner: EpisodePlanner | None = None,
        chapter_manager: ChapterManager | None = None,
        director_persona: DirectorPersona | None = None,
        scene_hook_analyzer: SceneHookAnalyzer | None = None,
        novel_generator: "NovelGenerator | None" = None,
        ambient_context_manager: AmbientContextManager | None = None,
        quality_guard: QualityGuard | None = None,
        resolved_llm_config: ResolvedStoryLLMConfig | None = None,
        news_store: "NewsArticleStore | None" = None,
    ) -> None:
        self.story_id = story_id
        self.db = db
        self.llm_router = llm_router
        self._web_poster = web_poster
        self._config = config
        self._story_memory_manager = story_memory_manager
        self._narration_engine = narration_engine
        self._story_director = story_director
        self._character_evolution_manager = character_evolution_manager
        self._growth_engine = growth_engine
        self._interaction_pattern_engine = interaction_pattern_engine
        self._relationship_mode_engine = relationship_mode_engine
        self._emergent_canonizer = emergent_canonizer
        self._canon_reignition_engine = canon_reignition_engine
        self._canon_profile_writeback_engine = canon_profile_writeback_engine
        self._dramatic_pressure_evaluator = dramatic_pressure_evaluator
        self._episode_planner = episode_planner
        self._chapter_manager = chapter_manager
        self._director_persona = director_persona
        self._scene_hook_analyzer = scene_hook_analyzer
        self._conversation_motif_engine = ConversationMotifEngine(db, story_id)
        self._chapter_generator: "ChapterGenerator | None" = None  # Phase 4: 後注入
        self._novel_generator = novel_generator
        self._ambient_context_manager = ambient_context_manager
        self._quality_guard = quality_guard
        self._pre_turn_scene_engine: PreTurnSceneEngine | None = None
        self._round_scene_script: str | None = None  # per_round キャッシュ
        self._round_scene_payload: dict[str, Any] | None = None
        self._resolved_llm_config = resolved_llm_config
        self._initialized: bool = False
        # 時事モード
        self._news_selector: Any | None = None          # NewsArticleSelector（遅延 import）
        self._news_store: "NewsArticleStore | None" = news_store
        self._owns_news_store = False
        self._last_news_turn_per_char: dict[str, int] = {}
        self._style_profile = get_story_style_profile(story_id)
        self._intent_profile = get_story_intent_profile(story_id)
        self._story_utterance_max_chars: int = 180      # initialize() で per-story 設定で上書き
        self._story_utterance_max_sentences: int = 2  # initialize() で per-story 設定で上書き
        self._news_tag_filter: list[str] = []         # initialize() で per-story 設定で上書き
        self._running: bool = False
        self._story: dict[str, Any] = {}
        self._characters: list[dict[str, Any]] = []
        self._places: dict[str, str] = {}  # {place_id: label}
        self.world_clock: WorldClock | None = None
        self._char_index: int = 0

        # Phase 2 マネージャー（引数不要のものはここで生成）
        self._emotion_manager = EmotionManager()
        self._scheduler = self._make_scheduler()
        self._secret_manager = SecretManager()

        # initialize() で生成するもの
        self._place_manager: PlaceManager | None = None
        self._move_engine: MoveEngine | None = None

        # Phase 2 ロードデータ
        self._time_schedules: list[dict[str, Any]] = []
        self._anomaly_rules: list[dict[str, Any]] = []
        self._relationships: dict[tuple[str, str], dict[str, Any]] = {}

        # 成功ターンだけ反映する committed place cache
        self._last_known_place: dict[str, str] = {}
        # 失敗ターンの移動も含む process-local な現在地 snapshot
        self._runtime_place_snapshot: dict[str, str] = {}

        # ラウンド開始時の感情スナップショット（emotion_delta 計算用）
        self._round_start_emotions: dict[str, dict[str, float]] = {}

        # ラウンド中の最新感情（emotion_delta 計算用）
        self._prev_emotion_snapshots: dict[str, dict[str, float]] = {}

        # ラウンド中に閉じた会話セッション ID（session_end prose 生成用）
        self._closed_conversation_session_ids: list[int] = []
        # ラウンド中に閉じた story scene ID（scene close hook 整理用）
        self._closed_story_scene_ids: list[int] = []
        self._effective_llm_provider: str = ""
        self._effective_llm_model: str = ""
        self._planned_turn_queue: list[dict[str, Any]] = []
        self._planned_turn_index: int = 0

    async def initialize(self) -> None:
        """DB からストーリー・キャラ・場所・スケジュール・関係をロードし、各マネージャーを構築する。"""
        # 同一インスタンス再利用時に前回セッションの in-memory 状態を持ち越さない
        self._last_known_place.clear()
        self._runtime_place_snapshot.clear()
        self._round_start_emotions.clear()
        self._prev_emotion_snapshots.clear()
        self._closed_conversation_session_ids.clear()
        self._closed_story_scene_ids.clear()
        self._planned_turn_queue.clear()
        self._planned_turn_index = 0
        self._round_scene_payload = None

        story = await self.db.get_story(self.story_id)
        if story is None:
            raise ValueError(f"Story not found: {self.story_id}")
        self._story = story
        # story_mode ベースでプロファイルを動的に選択する。
        # world_config.yaml の story.story_mode が stories.story_mode カラムに格納される。
        story_mode = str(story.get("story_mode") or "drama").strip()
        self._style_profile = get_story_style_profile_by_mode(story_mode)
        self._intent_profile = get_story_intent_profile_by_mode(story_mode)
        if self._resolved_llm_config is None and self._config is not None:
            self._resolved_llm_config = resolve_story_llm_config(
                self._config,
                self.story_id,
            )
        if self._resolved_llm_config is not None:
            self._effective_llm_provider = self._resolved_llm_config.provider
            self._effective_llm_model = self._resolved_llm_config.model
        else:
            self._effective_llm_provider = story["llm_provider"]
            self._effective_llm_model = story["llm_model"]

        self._characters = await self.db.get_characters(self.story_id)

        # places テーブルからフル列取得 → PlaceManager 構築
        async with self.db._conn.execute(
            "SELECT * FROM places WHERE story_id = ? AND is_active = 1",
            (self.story_id,),
        ) as cursor:
            place_rows = [dict(r) for r in await cursor.fetchall()]
        self._places = {r["id"]: r["label"] for r in place_rows}
        self._place_manager = PlaceManager(place_rows)

        # time_schedules
        async with self.db._conn.execute(
            "SELECT * FROM time_schedules WHERE story_id = ?", (self.story_id,)
        ) as cursor:
            self._time_schedules = [dict(r) for r in await cursor.fetchall()]

        # anomaly_rules
        async with self.db._conn.execute(
            "SELECT * FROM anomaly_rules WHERE story_id = ?", (self.story_id,)
        ) as cursor:
            self._anomaly_rules = [dict(r) for r in await cursor.fetchall()]

        # relationships
        async with self.db._conn.execute(
            """
            SELECT char_id_from, char_id_to, trust, tension, affinity, familiarity
            FROM relationships
            WHERE story_id = ?
            """,
            (self.story_id,),
        ) as cursor:
            rel_rows = await cursor.fetchall()
        self._relationships = {
            (r["char_id_from"], r["char_id_to"]): {
                "trust": r["trust"],
                "tension": r["tension"],
                "affinity": r["affinity"],
                "familiarity": r["familiarity"],
            }
            for r in rel_rows
        }

        # MoveEngine（PlaceManager 依存のためここで生成）
        self._move_engine = MoveEngine(self._place_manager)

        # WorldClock 構築（last_sim_time がある場合は再開位置から）
        last_sim_time: str | None = story.get("last_sim_time")
        self.world_clock = WorldClock(
            season_start=story["season_start"],
            turn_minutes=story["turn_minutes"],
            event_calendar=None,
            initial_datetime=last_sim_time if last_sim_time else None,
        )

        # Config が渡されている場合、未注入のマネージャーを自動構築する
        if self._config is not None:
            provider = self._effective_llm_provider
            model = self._effective_llm_model
            if self._story_memory_manager is None:
                self._story_memory_manager = StoryMemoryManager(
                    self.story_id, self.db, self.llm_router,
                    self._config.story_memory,
                    llm_provider=provider, llm_model=model,
                )
            if self._narration_engine is None:
                self._narration_engine = NarrationEngine(
                    self.story_id, self.db, self.llm_router,
                    self._config.narration,
                    llm_provider=provider, llm_model=model,
                )
            if self._story_director is None:
                self._story_director = StoryDirector(
                    self._config.story_director, self.db, self.llm_router,
                    llm_provider=provider, llm_model=model,
                    intent_profile=self._intent_profile,
                )
            if self._interaction_pattern_engine is None:
                self._interaction_pattern_engine = InteractionPatternEngine(
                    self.db,
                    self.story_id,
                    analysis_interval_rounds=max(1, self._config.story_director.analysis_interval_rounds),
                )
            if self._relationship_mode_engine is None:
                self._relationship_mode_engine = RelationshipModeEngine(
                    self.db,
                    self.story_id,
                )
            if self._emergent_canonizer is None:
                self._emergent_canonizer = EmergentCanonizer(
                    self.db,
                    self.story_id,
                    intent_profile=self._intent_profile,
                )
            if self._canon_reignition_engine is None:
                self._canon_reignition_engine = CanonReignitionEngine(
                    self.db,
                    self.story_id,
                )
            if self._dramatic_pressure_evaluator is None and self._config.dramatic_pressure.enabled:
                self._dramatic_pressure_evaluator = DramaticPressureEvaluator(
                    self.db,
                    self.story_id,
                    intent_profile=self._intent_profile,
                    recent_window_turns=self._config.dramatic_pressure.recent_window_turns,
                    min_pressure_score=self._config.dramatic_pressure.min_pressure_score,
                    stale_rounds_before_resolve=self._config.dramatic_pressure.stale_rounds_before_resolve,
                    max_active_pressures=self._config.dramatic_pressure.max_active_pressures,
                )
            if self._episode_planner is None and self._config.episode_planner.enabled:
                self._episode_planner = EpisodePlanner(
                    self.story_id,
                    self.db,
                    min_turns_per_episode=self._config.episode_planner.min_turns_per_episode,
                    max_turns_per_episode=self._config.episode_planner.max_turns_per_episode,
                    carry_hook_limit=self._config.episode_planner.carry_hook_limit,
                    stale_rounds_before_close=self._config.episode_planner.stale_rounds_before_close,
                    episode_tempo=self._intent_profile.episode_tempo,
                    dramatic_pressure_evaluator=self._dramatic_pressure_evaluator,
                )
            if self._chapter_manager is None and self._config.chapter.enabled:
                self._chapter_manager = ChapterManager(
                    self.story_id,
                    self.db,
                    beat_check_interval_rounds=self._config.chapter.beat_check_interval_rounds,
                    beat_timeout_turns=self._config.chapter.beat_timeout_turns,
                    auto_event_injection=self._config.chapter.auto_event_injection,
                )
            if self._director_persona is None and self._config.director_persona.enabled:
                self._director_persona = DirectorPersona(
                    self.story_id,
                    self.db,
                    evaluation_interval_rounds=self._config.director_persona.evaluation_interval_rounds,
                    steering_strength=self._config.director_persona.steering_strength,
                    satisfaction_decay=self._config.director_persona.satisfaction_decay,
                    allow_mid_chapter_swap=self._config.director_persona.allow_mid_chapter_swap,
                    swap_cooldown_turns=self._config.director_persona.swap_cooldown_turns,
                )
            if self._character_evolution_manager is None:
                self._character_evolution_manager = CharacterEvolutionManager(
                    self.story_id, self.db, self.llm_router,
                    self._config.character_evolution,
                    llm_provider=provider, llm_model=model,
                )
            if self._growth_engine is None and self._config.growth_engine.enabled:
                self._growth_engine = GrowthEngine(
                    self.story_id,
                    self.db,
                    self.llm_router,
                    self._config.growth_engine,
                    llm_provider=provider,
                    llm_model=model,
                )
            if self._scene_hook_analyzer is None and self._config.story_hooks.enabled:
                self._scene_hook_analyzer = SceneHookAnalyzer(
                    self.story_id,
                    self.db,
                    self.llm_router,
                    llm_provider=provider,
                    llm_model=model,
                )
            if self._quality_guard is None and self._config.quality_guard.enabled:
                # per-story の発話長設定（文字数・文数）を取得して QualityGuardConfig に反映
                _max_chars = 180
                _max_sentences = 3
                try:
                    _utterance = await self.db.get_utterance_settings(self.story_id)
                    if isinstance(_utterance, dict):
                        _max_chars = int(_utterance.get("max_chars", 180))
                        _max_sentences = int(_utterance.get("max_sentences", 3))
                except Exception:
                    logger.warning(
                        "utterance_settings: failed to load, using defaults",
                        extra={"story_id": self.story_id},
                    )
                self._story_utterance_max_chars = _max_chars
                self._story_utterance_max_sentences = _max_sentences
                import dataclasses as _dc
                _per_story_qg = _dc.replace(
                    self._config.quality_guard,
                    max_monologue_chars=_max_chars,
                    max_reply_chars=_max_chars,
                    max_group_chars=_max_chars,
                    max_current_affairs_chars=_max_chars,
                    max_sentences=_max_sentences,
                )
                self._quality_guard = QualityGuard(_per_story_qg)
            # per-story の時事モードタグフィルタをロード（未設定なら全記事）
            try:
                self._news_tag_filter = await self.db.get_news_tag_filter(self.story_id)
            except Exception:
                logger.warning(
                    "news_tag_filter: failed to load, using no filter",
                    extra={"story_id": self.story_id},
                )
                self._news_tag_filter = []
            if self._canon_profile_writeback_engine is None:
                self._canon_profile_writeback_engine = CanonProfileWritebackEngine(
                    self.db,
                    self.story_id,
                )
            if self._novel_generator is None:
                from engine.novel_generator import NovelGenerator as _NG
                self._novel_generator = _NG(
                    self.story_id, self._config.novel_generator,
                    self.db, self.llm_router,
                    llm_provider=provider, llm_model=model,
                )
            if self._ambient_context_manager is None:
                self._ambient_context_manager = AmbientContextManager(
                    self.story_id,
                    self.db,
                    self.llm_router,
                    self._config.ambient_context,
                    llm_provider=provider,
                    llm_model=model,
                )

        if self._chapter_manager is not None:
            await self._chapter_manager.initialize()
            # Phase 3 optional: LLM beat judgment 注入
            if self._config is not None and self._config.chapter.enable_llm_beat_judgment:
                self._chapter_manager._llm_router = self.llm_router
                self._chapter_manager._llm_provider = self._effective_llm_provider
                self._chapter_manager._llm_model = self._effective_llm_model
                self._chapter_manager._enable_llm_beat_judgment = True
                self._chapter_manager._llm_beat_min_elapsed = (
                    self._config.chapter.llm_beat_min_elapsed_turns
                )

        if self._director_persona is not None:
            await self._director_persona.initialize()
            # v2 upgrade: StoryDirector が既に構築済みなら director_persona を注入
            if self._story_director is not None:
                self._story_director._director_persona = self._director_persona
            # v2 upgrade Phase 3: chapter_manager に director_persona を後注入
            if self._chapter_manager is not None:
                self._chapter_manager._director_persona = self._director_persona
            # Phase 3 optional: LLM satisfaction evaluation 注入
            if self._config is not None and self._config.director_persona.enable_llm_evaluation:
                self._director_persona._llm_router = self.llm_router
                self._director_persona._llm_provider = self._effective_llm_provider
                self._director_persona._llm_model = self._effective_llm_model
                self._director_persona._enable_llm_eval = True
                self._director_persona._llm_eval_interval = (
                    self._config.director_persona.llm_eval_interval_rounds
                )

        # Phase 4: ChapterGenerator 初期化
        if self._config is not None and self._config.chapter_generator.enabled:
            from engine.chapter_generator import ChapterGenerator
            self._chapter_generator = ChapterGenerator(
                self.story_id,
                self.db,
                self.llm_router,
                self._effective_llm_provider,
                self._effective_llm_model,
                default_beat_timeout=self._config.chapter_generator.default_beat_timeout,
                include_conflict_seeds=self._config.chapter_generator.include_conflict_seeds,
                max_events_per_beat=self._config.chapter_generator.max_events_per_beat,
            )
            logger.info(
                "ChapterGenerator initialized",
                extra={"story_id": self.story_id},
            )

        # NarrationEngine の再起動時初期化（既存 closed arc 数を同期）
        if self._narration_engine is not None:
            await self._narration_engine.initialize()

        # PreTurnSceneEngine の初期化（enabled 時のみ）
        if (
            self._config is not None
            and self._config.scene_script.enabled
            and self._pre_turn_scene_engine is None
        ):
            self._pre_turn_scene_engine = PreTurnSceneEngine(
                self.story_id,
                self.db,
                self.llm_router,
                self._config.scene_script,
                llm_provider=self._effective_llm_provider,
                llm_model=self._effective_llm_model,
            )
        if self._pre_turn_scene_engine is not None:
            await self._pre_turn_scene_engine.initialize()

        # config が initialize() 時点で確定しているので Scheduler を再生成してレートを適用
        self._scheduler = self._make_scheduler()

        # 時事モード: NewsArticleSelector の初期化
        if (
            self._config is not None
            and self._config.news_mode.enabled
            and self._news_selector is None
        ):
            try:
                from db.news_store import NewsArticleStore
                from engine.news_selector import NewsArticleSelector
                if self._news_store is None:
                    self._news_store = NewsArticleStore()
                    await self._news_store.initialize()
                    self._owns_news_store = True
                self._news_selector = NewsArticleSelector(
                    router=self.llm_router,
                    store=self._news_store,
                    llm_provider=self._effective_llm_provider,
                    llm_model=self._effective_llm_model,
                    candidates_for_llm=self._config.news_mode.candidates_for_llm,
                )
                logger.info(
                    "NewsArticleSelector initialized",
                    extra={"story_id": self.story_id},
                )
            except Exception as exc:
                logger.warning(
                    "NewsArticleSelector init failed (時事モード無効で続行): %s", exc,
                    extra={"story_id": self.story_id},
                )

        self._initialized = True
        logger.info("StoryEngine initialized", extra={"story_id": self.story_id})

    async def run_one_turn(self) -> None:
        """1キャラ分の14ステップターンを処理する（LLM呼び出し → DB保存）。

        全キャラ1周完了時に WorldClock.tick() と last_sim_time 更新を行う。
        """
        assert self.world_clock is not None, "Call initialize() before run_one_turn()"
        assert self._place_manager is not None
        assert self._move_engine is not None

        # ── Step 0: シーンスクリプト生成（per_round はラウンド先頭のみ）──
        if self._pre_turn_scene_engine is not None:
            is_round_start = (
                (self._planned_turn_queue and self._planned_turn_index == 0)
                or (not self._planned_turn_queue and self._char_index % len(self._characters) == 0)
            )
            if is_round_start or self._pre_turn_scene_engine._config.generation_mode == "per_turn":
                scene_ctx = await self._build_scene_script_context()
                self._round_scene_script = await self._pre_turn_scene_engine.generate_for_turn(
                    self.world_clock.turn_number, scene_ctx
                )
                self._round_scene_payload = self._extract_scene_payload_from_metadata(
                    self._pre_turn_scene_engine.get_cached_generation_metadata()
                )
            elif self._round_scene_script is None:
                self._round_scene_script = self._pre_turn_scene_engine.get_cached_script()
                self._round_scene_payload = self._extract_scene_payload_from_metadata(
                    self._pre_turn_scene_engine.get_cached_generation_metadata()
                )

        # ── Step 1: キャラ選択 ──────────────────────────────────────────
        planned_turn = self._planned_turn_queue[self._planned_turn_index] if self._planned_turn_queue else None
        if planned_turn is not None:
            char = next(
                candidate for candidate in self._characters
                if candidate["id"] == planned_turn["char_id"]
            )
        else:
            char = self._characters[self._char_index % len(self._characters)]
        char_id = char["id"]

        # ── Step 2: 最新状態取得 ────────────────────────────────────────
        state = await self.db.get_latest_character_state(self.story_id, char_id)
        if state is None:
            state = self._make_initial_state(char)

        emotions: dict[str, float] = {
            "stress":     float(state["stress"]),
            "motivation": float(state["motivation"]),
            "loneliness": float(state["loneliness"]),
            "excitement": float(state["excitement"]),
        }
        current_place: str = self._runtime_place_snapshot.get(char_id, state["current_place"])
        previous_place: str | None = state.get("previous_place")

        # ── Step 3: 感情減衰 ────────────────────────────────────────────
        emotions = self._emotion_manager.apply_decay(emotions)

        # ラウンド開始時の感情を記録（emotion_delta 計算用、ラウンド最初のみ）
        if char_id not in self._round_start_emotions:
            self._round_start_emotions[char_id] = dict(emotions)

        # ── Step 4: 推奨場所算出 ────────────────────────────────────────
        recommended_place = self._place_manager.get_recommended_place(
            self._time_schedules,
            self.world_clock.sim_datetime,
            self.world_clock.weekday_type,
        )

        # ── Step 5 & 6: 移動判定・実行 ──────────────────────────────────
        move_reason: str | None = None
        pre_move_places = await self._load_current_places(char_id, current_place)
        pre_move_same_place_chars = self._get_same_place_chars(
            char_id, current_place, pre_move_places
        )
        social_target_place = await self._select_social_target_place(
            char_id=char_id,
            current_place=current_place,
            current_places=pre_move_places,
        )
        current_place_pull_strength = await self._current_place_pull_strength(
            char_id=char_id,
            current_place=current_place,
            current_places=pre_move_places,
        )
        social_pull_strength = 1.0 if emotions.get("loneliness", 0.0) >= 0.6 else 0.5
        if self._move_engine.should_move(
            emotions,
            current_place,
            recommended_place,
            same_place_count=len(pre_move_same_place_chars),
            current_place_pull_strength=current_place_pull_strength,
            social_target_place=social_target_place,
            social_pull_strength=social_pull_strength,
        ):
            destination = self._move_engine.decide_destination(
                current_place,
                recommended_place,
                social_target_place=social_target_place,
            )
            # 隣接場所がない場合 decide_destination は現在地を返すため、実際に移動した場合のみ処理する
            if destination != current_place:
                move_reason = self._move_engine.generate_move_reason(
                    current_place, destination, emotions, recommended_place
                )
                previous_place = current_place
                current_place = destination
                emotions = self._emotion_manager.apply_trigger(emotions, "moved")
                logger.debug(
                    "char moved",
                    extra={"story_id": self.story_id, "char_id": char_id},
                )

        # ── Step 7: 同一場所キャラ収集 ──────────────────────────────────
        current_places = await self._load_current_places(char_id, current_place)
        same_place_chars = self._get_same_place_chars(
            char_id, current_place, current_places
        )
        same_place_char_names = [
            next((c["name_ja"] for c in self._characters if c["id"] == cid), cid)
            for cid in same_place_chars
        ]

        # ── Step 8: 異常検知 ────────────────────────────────────────────
        anomaly = self._place_manager.detect_anomaly(
            current_place,
            self.world_clock.sim_datetime,
            self._anomaly_rules,
            alone=len(same_place_chars) == 0,
            same_place_chars=same_place_chars,
            char_stress=emotions["stress"],
        )

        # ── Step 9: 感情トリガー適用 ────────────────────────────────────
        if anomaly:
            emotions = self._emotion_manager.apply_trigger(emotions, "anomaly_detected")
        if len(same_place_chars) == 0:
            emotions = self._emotion_manager.apply_trigger(emotions, "alone_long")
        else:
            emotions = self._emotion_manager.apply_trigger(emotions, "talked_to")

        ambient_context = AmbientTurnContext()
        if self._ambient_context_manager is not None:
            ambient_context = await self._ambient_context_manager.resolve_turn(
                turn_number=self.world_clock.turn_number,
                sim_datetime=self.world_clock.sim_datetime,
                char_id=char_id,
                place_id=current_place,
                place_name=self._places.get(current_place, current_place),
                same_place_char_ids=same_place_chars,
                emotions=emotions,
                move_reason=move_reason,
                anomaly=anomaly,
            )
            for key, delta in ambient_context.emotion_delta.items():
                if key in emotions:
                    emotions[key] += delta
            emotions = self._emotion_manager.clamp_emotions(emotions)

        # ── Step 10: 秘密開示判定 ───────────────────────────────────────
        secret_result = self._secret_manager.check_reveal(
            char, same_place_chars, self._relationships
        )
        secret_hint = secret_result["hint_text"] if secret_result else None

        # ── Step 11: 発言スケジューリング ───────────────────────────────
        scene_id = planned_turn["scene_id"] if planned_turn is not None else None
        speaker_intent = planned_turn["speaker_intent"] if planned_turn is not None else None
        has_recent_dialogue = await self._has_recent_dialogue_context(
            char_id=char_id,
            current_place=current_place,
            current_places=current_places,
        )
        schedule = self._scheduler.schedule_utterance(
            char_id,
            same_place_chars,
            self._relationships,
            anomaly,
            has_recent_dialogue=has_recent_dialogue,
            in_active_scene_or_session=bool(scene_id) or bool(same_place_chars),
            speaker_intent=speaker_intent,
        )
        conversation = await self._resolve_conversation_turn(
            char_id=char_id,
            current_place=current_place,
            current_places=current_places,
            schedule=schedule,
        )
        msg_type: str = conversation["msg_type"]
        target_char_id = conversation["target_char_id"]

        # ── 時事モード injection ──────────────────────────────────────────
        # reply（直接 1:1 返答）だけ除外。それ以外（monologue/group/initiation）は注入可能。
        # has_recent_dialogue は 8 人全員が会話中だとほぼ常に True になるため使わない。
        _news_article_for_turn: dict[str, Any] | None = None
        if (
            msg_type != "reply"
            and self._news_selector is not None
        ):
            try:
                _news_article_for_turn = await self._try_current_affairs_injection(char_id)
            except Exception:
                logger.warning(
                    "news_mode injection failed; skipping",
                    extra={"story_id": self.story_id, "char_id": char_id},
                )
            if _news_article_for_turn is not None:
                msg_type = "current_affairs"
                # 50% で同席キャラへの話題振り
                if same_place_chars and __import__("random").random() < 0.5:
                    target_char_id = same_place_chars[0]

        # ── Step 12: 表情判定 ────────────────────────────────────────────
        expression = self._emotion_manager.judge_expression(emotions)

        # ── Step 13: プロンプト構築 + LLM呼び出し ───────────────────────
        place_name = self._places.get(current_place, current_place)

        # ── 追加コンテキスト: 内的反応ブリッジ要素の計算 ─────────────────
        target_char_id_str = target_char_id if isinstance(target_char_id, str) else None
        scene_script_payload = await self._get_current_scene_script_payload()
        character_directive_text = self._build_character_directive_text(
            scene_script_payload,
            char_id,
        )
        scene_brief = self._build_scene_brief_from_payload(scene_script_payload)
        speaker_objective = (
            character_directive_text
            or _extract_character_objective(self._round_scene_script or "", char["name_ja"])
        )
        target_char_personality_hint: str | None = None
        if target_char_id_str:
            tchar = next((c for c in self._characters if c["id"] == target_char_id_str), None)
            if tchar:
                target_char_personality_hint = str(tchar.get("personality_core") or "")[:80]
        relationship_frame: str | None = None
        if target_char_id_str:
            rel = self._relationships.get((char_id, target_char_id_str)) or {}
            trust = float(rel.get("trust", 0.5))
            if trust > 0.65:
                relationship_frame = "信頼している"
            elif trust < 0.35:
                relationship_frame = "距離を置いている"
            else:
                relationship_frame = "複雑な間柄"
        reaction_frame = _derive_reaction_frame(
            emotions=emotions,
            trust_level=float(
                (self._relationships.get((char_id, target_char_id_str)) or {}).get("trust", 0.5)
            ) if target_char_id_str else 0.5,
            target_name=conversation.get("target_char_name"),
        )
        is_initiation = (
            speaker_intent == "advance"
            and not has_recent_dialogue
            and bool(same_place_chars)
        )

        # v2 upgrade: Chapter System world_injection 取得
        chapter_world_injection: str | None = None
        if self._chapter_manager is not None:
            chapter_world_injection = await self._chapter_manager.get_current_world_injection()

        # v2 upgrade: Director Persona tone hints 取得
        director_tone_hints: list[str] = []
        if self._director_persona is not None:
            director_tone_hints = self._director_persona.get_steering_signals().tone_hints

        # Phase B-10: ストーリーメモリ・evolution overlay・介入を取得
        story_memory_texts: list[str] = []
        if self._story_memory_manager is not None:
            for mem in await self._story_memory_manager.get_relevant_memories(char_id):
                story_memory_texts.append(str(mem["summary"]))

        evolution_overlay = await self._get_effective_profile_overlay(char_id)

        intervention_texts: list[str] = []
        if self._story_director is not None:
            interventions = await self.db.get_active_interventions(
                self.story_id, self.world_clock.turn_number
            )
            for iv in interventions:
                scope = iv.get("scope", "all")
                if _intervention_applies_to_character(
                    scope, char_id=char_id, current_place=current_place
                ):
                    intervention_texts.append(str(iv["prompt_injection"]))
                    if iv.get("status") == "active":
                        await self.db.update_intervention(
                            int(iv["id"]),
                            {"status": "acknowledged"},
                        )

        active_hook_texts = await self._get_active_hook_texts(char_id, current_place)
        conversation_motif_text = conversation.get("conversation_motif_text")
        if conversation_motif_text:
            active_hook_texts.append(str(conversation_motif_text))
        relationship_counterparts = list(same_place_chars)
        if isinstance(target_char_id, str) and target_char_id not in relationship_counterparts:
            relationship_counterparts.append(target_char_id)
        relationship_summary_texts = await self._get_relationship_summary_texts(
            char_id,
            relationship_counterparts,
        )
        relationship_mode_texts = await self._select_relationship_mode_prompt_texts(
            char_id=char_id,
            target_char_id=target_char_id if isinstance(target_char_id, str) else None,
            same_place_char_ids=same_place_chars,
        )
        canon_bit_texts = await self._select_canon_bit_prompt_texts(
            char_id=char_id,
            target_char_id=target_char_id if isinstance(target_char_id, str) else None,
            current_place_id=current_place,
        )
        dramatic_pressure_texts = await self._select_dramatic_pressure_prompt_texts(
            char_id=char_id,
            target_char_id=target_char_id if isinstance(target_char_id, str) else None,
            current_place_id=current_place,
            same_place_char_ids=same_place_chars,
            msg_type=msg_type,
        )
        scene_objective_text = await self._get_current_scene_objective_text(current_place)
        dominant_signal_text = self._select_dominant_prompt_signal(
            msg_type=msg_type,
            relationship_mode_texts=relationship_mode_texts,
            canon_bit_texts=canon_bit_texts,
            dramatic_pressure_texts=dramatic_pressure_texts,
            scene_objective_text=scene_objective_text,
        )
        target_last_utterance_excerpt = self._extract_target_last_utterance_excerpt(
            reply_to_log_id=conversation.get("reply_to_log_id"),
            recent_logs=conversation.get("recent_logs") or [],
        )
        reply_focus_text = self._select_reply_focus_text(
            target_excerpt=target_last_utterance_excerpt,
            recent_dialogue_lines=conversation["recent_dialogue_lines"],
        )
        speech_payload = json.loads(char["speech"])
        voice_anchor_text = self._build_voice_anchor_text(
            tone=str(speech_payload.get("tone", speech_payload.get("speech_style", ""))),
            speech_examples=list(speech_payload.get("examples", [])),
        )
        (
            relationship_mode_texts,
            canon_bit_texts,
            dramatic_pressure_texts,
            scene_objective_text,
        ) = self._refine_prompt_signal_payload(
            msg_type=msg_type,
            relationship_mode_texts=relationship_mode_texts,
            canon_bit_texts=canon_bit_texts,
            dramatic_pressure_texts=dramatic_pressure_texts,
            scene_objective_text=scene_objective_text,
            reply_focus_text=reply_focus_text,
            handoff_target_name=conversation.get("handoff_target_name"),
        )
        dominant_signal_text = self._select_dominant_prompt_signal(
            msg_type=msg_type,
            relationship_mode_texts=relationship_mode_texts,
            canon_bit_texts=canon_bit_texts,
            dramatic_pressure_texts=dramatic_pressure_texts,
            scene_objective_text=scene_objective_text,
        )
        story_pressure_cue = self._build_story_pressure_cue(
            msg_type=msg_type,
            dominant_signal_text=dominant_signal_text,
            scene_objective_text=scene_objective_text,
            relationship_mode_texts=relationship_mode_texts,
            dramatic_pressure_texts=dramatic_pressure_texts,
        )

        place_dialogue_lines = await self._build_place_dialogue_lines(char_id, current_place)

        ctx = self._build_context(
            char,
            state,
            place_name,
            emotions=emotions,
            expression=expression,
            move_reason=move_reason,
            anomaly=anomaly,
            same_place_char_names=same_place_char_names,
            secret_hint=secret_hint,
            target_char_name=conversation["target_char_name"],
            conversation_partner_names=conversation["conversation_partner_names"],
            recent_dialogue_lines=conversation["recent_dialogue_lines"],
            story_memory_texts=story_memory_texts,
            evolution_overlay=evolution_overlay,
            intervention_texts=intervention_texts,
            active_hook_texts=active_hook_texts,
            relationship_summary_texts=relationship_summary_texts,
            relationship_mode_texts=relationship_mode_texts,
            canon_bit_texts=canon_bit_texts,
            dramatic_pressure_texts=dramatic_pressure_texts,
            dominant_signal_text=dominant_signal_text,
            scene_objective_text=scene_objective_text,
            story_pressure_cue=story_pressure_cue,
            target_last_utterance_excerpt=target_last_utterance_excerpt,
            reply_focus_text=reply_focus_text,
            voice_anchor_text=voice_anchor_text,
            body_state_texts=ambient_context.body_state_texts,
            ambient_texts=ambient_context.ambient_texts,
            chapter_world_injection=chapter_world_injection,
            director_tone_hints=director_tone_hints,
            handoff_target_name=conversation.get("handoff_target_name"),
            scene_frame_text=scene_brief["scene_frame"],
            scene_turn_goal_text=scene_brief["turn_goal"],
            scene_turn_shift_text=scene_brief["turn_shift"],
            scene_banned_surface_patterns=scene_brief["banned_surface_patterns"],
            speaker_initiative=(speaker_intent == "advance"),
            is_presence_monologue=(
                msg_type == "monologue"
                and "内省モノローグ注入" in (schedule.get("reason") or "")
            ),
            speaker_objective=speaker_objective,
            character_directive_text=character_directive_text,
            reaction_frame=reaction_frame,
            target_char_personality_hint=target_char_personality_hint,
            relationship_frame=relationship_frame,
            is_initiation=is_initiation,
            place_dialogue_lines=place_dialogue_lines,
        )
        if msg_type == "current_affairs" and _news_article_for_turn is not None:
            # 時事感想ターン（1-pass、外部刺激が既にある）
            system_prompt, user_prompt = build_current_affairs_prompt(ctx, _news_article_for_turn)
        elif msg_type in ("reply", "group") or ctx.is_initiation:
            # 2段生成: STEP A 内的モノローグ → STEP B 発話
            inner_system, inner_user = build_inner_thought_prompt(ctx)
            inner_response = await self._generate_llm_response(
                char_id=char_id,
                system_prompt=inner_system,
                user_prompt=inner_user,
            )
            inner_thought = self._extract_inner_thought_text(inner_response)
            logger.debug(
                "内的モノローグ生成完了",
                extra={"char_id": char_id, "inner_thought": inner_thought},
            )

            if ctx.is_initiation:
                system_prompt, user_prompt = build_voiced_initiation_prompt(ctx, inner_thought)
            elif msg_type == "reply":
                system_prompt, user_prompt = build_voiced_reply_prompt(ctx, inner_thought)
            else:
                system_prompt, user_prompt = build_voiced_group_prompt(ctx, inner_thought)
        else:
            system_prompt, user_prompt = build_monologue_prompt(ctx)

        response = await self._generate_llm_response(
            char_id=char_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if response is None:
            self._runtime_place_snapshot[char_id] = current_place
            await self._advance_turn_cursor()
            return

        response_chars = len(response.final_text)
        logger.info(
            "LLM生成完了",
            extra={
                "story_id": self.story_id,
                "char_id": char_id,
                "turn": self.world_clock.turn_number,
                "llm_provider": response.provider,
                "llm_model": response.model,
                "llm_latency_ms": response.latency_ms,
                "llm_response_chars": response_chars,
                "llm_thinking_chars": response.reasoning_chars,
                "llm_done_reason": response.done_reason,
                "llm_completion_status": response.completion_status,
            },
        )

        response_text = self._sanitize_generated_text(response.final_text)
        quality_score: float | None = None
        quality_flags: list[str] = []
        pending_quality_issue_payloads: list[dict[str, Any]] = []
        use_llm_quality_cycle = isinstance(self._quality_guard, QualityGuard)
        if use_llm_quality_cycle:
            attempts: list[dict[str, Any]] = []
            repair_instruction = ""
            accepted_issue_payloads: list[dict[str, Any]] = []
            candidate_response = response
            candidate_user_prompt = user_prompt
            quality_result: QualityGuardResult | None = None
            for attempt_index in range(1, 4):
                if attempt_index > 1:
                    candidate_response = await self._generate_llm_response(
                        char_id=char_id,
                        system_prompt=system_prompt,
                        user_prompt=self._build_dialogue_regeneration_user_prompt(
                            candidate_user_prompt,
                            msg_type=msg_type,
                            previous_attempts=attempts,
                            repair_instruction=repair_instruction,
                        ),
                    )
                    if candidate_response is None:
                        attempts.append(
                            {
                                "attempt": attempt_index,
                                "phase": "generation",
                                "candidate_preview": "",
                                "hard_failures": ["empty_output"],
                                "critic_verdict": None,
                                "critic_reason_code": "llm_empty",
                                "repair_instruction": "発話本文だけを短く返す",
                            }
                        )
                        repair_instruction = "発話本文だけを短く返す"
                        continue
                    response = candidate_response

                raw_candidate_text = str(candidate_response.final_text or "")
                sanitized_candidate_text = self._sanitize_generated_text(raw_candidate_text)
                hard_result = self._quality_guard.evaluate_hard(
                    raw_candidate_text,
                    msg_type=msg_type,
                    sanitized_text=sanitized_candidate_text,
                    recent_dialogue_lines=conversation["recent_dialogue_lines"],
                )
                hard_failures = [str(issue.get("issue_type") or "") for issue in hard_result.issues]
                attempt_record: dict[str, Any] = {
                    "attempt": attempt_index,
                    "phase": "hard_guard",
                    "candidate_preview": sanitized_candidate_text[:120],
                    "hard_failures": hard_failures,
                    "critic_verdict": None,
                    "critic_reason_code": None,
                    "repair_instruction": "",
                }
                if hard_result.action != "accept":
                    attempts.append(attempt_record)
                    repair_instruction = self._repair_instruction_from_hard_failures(hard_failures)
                    quality_result = hard_result
                    continue

                critic_result = await self._evaluate_dialogue_with_llm_critic(
                    char_id=char_id,
                    msg_type=msg_type,
                    candidate_text=hard_result.text,
                    character_name=str(char.get("name_ja") or char_id),
                    target_char_name=conversation["target_char_name"],
                    recent_dialogue_lines=conversation["recent_dialogue_lines"],
                    scene_frame_text=scene_brief["scene_frame"],
                    scene_turn_goal_text=scene_brief["turn_goal"],
                    scene_turn_shift_text=scene_brief["turn_shift"],
                    character_directive_text=character_directive_text,
                    reply_focus_text=reply_focus_text,
                    dominant_signal_text=dominant_signal_text,
                    scene_objective_text=scene_objective_text,
                )
                attempt_record["phase"] = "critic"
                attempt_record["critic_verdict"] = critic_result["verdict"]
                attempt_record["critic_reason_code"] = critic_result["reason_code"]
                attempt_record["repair_instruction"] = critic_result["repair_instruction"]
                if critic_result["verdict"] == "accept":
                    response_text = hard_result.text
                    quality_result = QualityGuardResult(
                        text=response_text,
                        action="accept",
                        score=1.0,
                        issues=[],
                    )
                    break
                if critic_result["verdict"] == "unavailable":
                    response_text = hard_result.text
                    accepted_issue_payloads.append(
                        {
                            "issue_type": "dialogue_critic_unavailable",
                            "severity": "warning",
                            "details": {
                                "attempt": attempt_index,
                                "critic_raw_preview": critic_result["raw_preview"],
                                "attempts": [*attempts, attempt_record],
                            },
                        }
                    )
                    quality_result = QualityGuardResult(
                        text=response_text,
                        action="accept",
                        score=0.8,
                        issues=accepted_issue_payloads,
                    )
                    break
                attempts.append(attempt_record)
                repair_instruction = critic_result["repair_instruction"]
                quality_result = QualityGuardResult(
                    text=hard_result.text,
                    action="retry",
                    score=0.4,
                    issues=[
                        {
                            "issue_type": "dialogue_critic_reject",
                            "severity": "warning",
                            "details": dict(attempt_record),
                        }
                    ],
                )

            if quality_result is None or quality_result.action == "retry":
                response_text = "・・・・・・"
                quality_result = QualityGuardResult(
                    text=response_text,
                    action="silence",
                    score=0.0,
                    issues=[
                        {
                            "issue_type": "dialogue_silence_fallback",
                            "severity": "warning",
                            "details": {
                                "msg_type": msg_type,
                                "final_action": "silence",
                                "attempts": attempts,
                            },
                        }
                    ],
                )
            response_text = quality_result.text
            quality_score = quality_result.score
            quality_flags = [
                "silence_fallback"
                if issue["issue_type"] == "dialogue_silence_fallback"
                else str(issue["issue_type"])
                for issue in quality_result.issues
            ]
            pending_quality_issue_payloads = quality_result.issues

        legacy_quality_guard = None if use_llm_quality_cycle else self._quality_guard
        if legacy_quality_guard is not None:
            recent_self_logs = await self.db.get_recent_chat_logs(self.story_id, limit=12)
            recent_self_messages = [
                str(log.get("message") or "").strip()
                for log in recent_self_logs
                if str(log.get("char_id") or "") == char_id and str(log.get("message") or "").strip()
            ][:3]
            recent_scene_messages = [
                str(log.get("message") or "").strip()
                for log in recent_self_logs
                if str(log.get("msg_type") or "") in {"reply", "group"}
                and str(log.get("message") or "").strip()
            ][:6]
            quality_leniency = (
                self._director_persona.get_steering_signals().quality_leniency
                if self._director_persona is not None
                else None
            )
            reply_focus_family = self._reply_focus_family(reply_focus_text)
            reply_focus_contract = self._build_reply_focus_contract(reply_focus_text)
            reply_signal_contract = self._build_reply_signal_contract(
                dominant_signal_text=dominant_signal_text,
                scene_objective_text=scene_objective_text,
            )
            reply_surface_contract = self._build_reply_surface_contract(
                reply_focus_text=reply_focus_text,
                voice_anchor_text=voice_anchor_text,
                recent_self_messages=recent_self_messages,
            )
            reply_variety_contract = self._build_reply_variety_contract(
                reply_focus_text=reply_focus_text,
                recent_self_messages=recent_self_messages,
            )
            reply_dramatic_contract = self._build_reply_dramatic_contract(
                reply_focus_text=reply_focus_text,
                reply_signal_contract=reply_signal_contract,
                reply_variety_contract=reply_variety_contract,
            )
            reply_blandness_contract = self._build_reply_blandness_contract(
                reply_focus_text=reply_focus_text,
                reply_variety_contract=reply_variety_contract,
                reply_dramatic_contract=reply_dramatic_contract,
                recent_self_messages=recent_self_messages,
            )
            reply_shape_contract = self._build_reply_shape_contract(
                reply_focus_text=reply_focus_text,
                reply_signal_contract=reply_signal_contract,
                reply_variety_contract=reply_variety_contract,
                reply_dramatic_contract=reply_dramatic_contract,
                recent_self_messages=recent_self_messages,
                dominant_signal_text=dominant_signal_text,
                scene_objective_text=scene_objective_text,
                relationship_mode_texts=relationship_mode_texts,
                dramatic_pressure_texts=dramatic_pressure_texts,
            )
            reply_quality_contract = self._build_reply_quality_contract(
                dominant_signal_text=dominant_signal_text,
                scene_objective_text=scene_objective_text,
                reply_shape_contract=reply_shape_contract,
                reply_variety_contract=reply_variety_contract,
            )
            reply_story_quality_contract = self._build_reply_story_quality_contract(
                dominant_signal_text=dominant_signal_text,
                scene_objective_text=scene_objective_text,
                reply_quality_contract=reply_quality_contract,
                story_pressure_cue=story_pressure_cue,
            )
            reply_residual_quality_contract = self._build_reply_residual_quality_contract(
                reply_story_quality_contract=reply_story_quality_contract,
            )
            recorded_issues: list[dict[str, Any]] = []
            retry_kept_without_fallback = False
            fallback_policy = ""
            quality_result = self._quality_guard.evaluate(
                response_text,
                msg_type=msg_type,
                style_profile=self._style_profile,
                target_char_name=conversation["target_char_name"],
                recent_dialogue_lines=conversation["recent_dialogue_lines"],
                scene_objective_text=scene_objective_text,
                dominant_signal_text=dominant_signal_text,
                reply_focus_text=reply_focus_text,
                reply_focus_contract=reply_focus_contract,
                reply_signal_contract=reply_signal_contract,
                reply_surface_contract=reply_surface_contract,
                reply_variety_contract=reply_variety_contract,
                reply_dramatic_contract=reply_dramatic_contract,
                reply_blandness_contract=reply_blandness_contract,
                reply_shape_contract=reply_shape_contract,
                reply_quality_contract=reply_quality_contract,
                reply_story_quality_contract=reply_story_quality_contract,
                reply_residual_quality_contract=reply_residual_quality_contract,
                voice_anchor_text=voice_anchor_text,
                quality_leniency=quality_leniency,
            )
            if quality_result.action == "retry":
                recorded_issues.extend(quality_result.issues)
                retry_response = await self._generate_llm_response(
                    char_id=char_id,
                    system_prompt=system_prompt,
                    user_prompt=self._build_retry_user_prompt(
                        user_prompt,
                        msg_type=msg_type,
                        issues=quality_result.issues,
                        target_last_utterance_excerpt=target_last_utterance_excerpt,
                        reply_focus_text=reply_focus_text,
                        voice_anchor_text=voice_anchor_text,
                        scene_objective_text=scene_objective_text,
                        dominant_signal_text=dominant_signal_text,
                        reply_focus_contract=reply_focus_contract,
                        reply_signal_contract=reply_signal_contract,
                        reply_surface_contract=reply_surface_contract,
                        reply_variety_contract=reply_variety_contract,
                        reply_dramatic_contract=reply_dramatic_contract,
                        reply_blandness_contract=reply_blandness_contract,
                        reply_shape_contract=reply_shape_contract,
                        reply_quality_contract=reply_quality_contract,
                        reply_story_quality_contract=reply_story_quality_contract,
                        reply_residual_quality_contract=reply_residual_quality_contract,
                        story_pressure_cue=story_pressure_cue,
                        handoff_target_name=conversation.get("handoff_target_name"),
                    ),
                )
                if retry_response is not None:
                    response_text = self._sanitize_generated_text(retry_response.final_text)
                    if msg_type == "reply":
                        response_text = self._compress_reply_to_two_sentences(
                            response_text,
                            reply_shape_contract=reply_shape_contract,
                        )
                    quality_result = self._quality_guard.evaluate(
                        response_text,
                        msg_type=msg_type,
                        style_profile=self._style_profile,
                        target_char_name=conversation["target_char_name"],
                        recent_dialogue_lines=conversation["recent_dialogue_lines"],
                        scene_objective_text=scene_objective_text,
                        dominant_signal_text=dominant_signal_text,
                        reply_focus_text=reply_focus_text,
                        reply_focus_contract=reply_focus_contract,
                        reply_signal_contract=reply_signal_contract,
                        reply_surface_contract=reply_surface_contract,
                        reply_variety_contract=reply_variety_contract,
                        reply_dramatic_contract=reply_dramatic_contract,
                        reply_blandness_contract=reply_blandness_contract,
                        reply_shape_contract=reply_shape_contract,
                        reply_quality_contract=reply_quality_contract,
                        reply_story_quality_contract=reply_story_quality_contract,
                        reply_residual_quality_contract=reply_residual_quality_contract,
                        voice_anchor_text=voice_anchor_text,
                        quality_leniency=quality_leniency,
                    )
                    if self._should_keep_retry_output_without_fallback(
                        msg_type=msg_type,
                        text=quality_result.text,
                        issues=quality_result.issues,
                    ):
                        retry_kept_without_fallback = True
                        fallback_policy = "kept_retry"
                        quality_result = QualityGuardResult(
                            text=quality_result.text,
                            action="normalize",
                            score=quality_result.score,
                            issues=quality_result.issues,
                        )
                if quality_result.action == "retry":
                    fallback_root_issue = self._select_reply_fallback_root_issue(quality_result.issues)
                    fallback_text = self._build_deterministic_fallback_text(
                        ctx=ctx,
                        msg_type=msg_type,
                        target_char_name=conversation["target_char_name"],
                        recent_self_messages=recent_self_messages,
                        recent_scene_messages=recent_scene_messages,
                        reply_focus_contract=reply_focus_contract,
                        fallback_root_issue=fallback_root_issue,
                        reply_signal_contract=reply_signal_contract,
                        reply_variety_contract=reply_variety_contract,
                        reply_dramatic_contract=reply_dramatic_contract,
                        reply_blandness_contract=reply_blandness_contract,
                        reply_shape_contract=reply_shape_contract,
                        reply_quality_contract=reply_quality_contract,
                        reply_story_quality_contract=reply_story_quality_contract,
                        reply_residual_quality_contract=reply_residual_quality_contract,
                    )
                    if fallback_text:
                        fallback_policy = "extractive_fallback" if msg_type == "reply" else "fallback"
                        response_text = fallback_text
                        quality_result = QualityGuardResult(
                            text=fallback_text,
                            action="fallback",
                            score=0.6,
                            issues=quality_result.issues,
                        )
                    else:
                        fallback_policy = "anchorless_drop" if msg_type == "reply" else "drop"
                        quality_result = QualityGuardResult(
                            text=quality_result.text,
                            action="drop",
                            score=quality_result.score,
                            issues=quality_result.issues,
                        )
            root_cause_issues = recorded_issues + quality_result.issues
            final_issues = root_cause_issues if quality_result.action == "drop" else quality_result.issues
            response_text = quality_result.text
            quality_score = quality_result.score
            quality_flags = [
                str(issue["issue_type"])
                for issue in final_issues
                if not self._should_omit_issue_from_quality_flags(
                    issue,
                    quality_action=quality_result.action,
                    response_text=response_text,
                )
            ]
            issue_payloads: list[dict[str, Any]] = []
            fallback_root_issue = self._select_reply_fallback_root_issue(root_cause_issues)
            fallback_issue_set = self._reply_fallback_issue_set(root_cause_issues)
            flat_issue_family = self._select_reply_flat_issue_family(final_issues)
            reply_variety_shape = (
                str(reply_variety_contract.get("response_shape") or "")
                if reply_variety_contract
                else ""
            )
            reply_variety_second_beat = (
                str(reply_variety_contract.get("second_beat_mode") or "")
                if reply_variety_contract
                else ""
            )
            reply_dramatic_move_mode = (
                str(reply_dramatic_contract.get("move_mode") or "")
                if reply_dramatic_contract
                else ""
            )
            reply_shape_mode = (
                str(reply_shape_contract.get("primary_shape") or "")
                if reply_shape_contract
                else ""
            )
            reply_quality_shape = (
                str(reply_quality_contract.get("primary_shape") or "")
                if reply_quality_contract
                else ""
            )
            reply_quality_second_beat = (
                str(reply_quality_contract.get("second_beat_mode") or "")
                if reply_quality_contract
                else ""
            )
            reply_story_flavor_role = (
                str(reply_quality_contract.get("required_story_flavor_role") or "")
                if reply_quality_contract
                else ""
            )
            reply_story_quality_shape = (
                str(reply_story_quality_contract.get("primary_shape") or "")
                if reply_story_quality_contract
                else ""
            )
            reply_story_quality_second_beat = (
                str(reply_story_quality_contract.get("second_beat_mode") or "")
                if reply_story_quality_contract
                else ""
            )
            reply_story_quality_role = (
                str(reply_story_quality_contract.get("required_story_flavor_role") or "")
                if reply_story_quality_contract
                else ""
            )
            reply_residual_shape = (
                str(reply_residual_quality_contract.get("primary_shape") or "")
                if reply_residual_quality_contract
                else ""
            )
            reply_residual_second_beat = (
                str(reply_residual_quality_contract.get("second_beat_mode") or "")
                if reply_residual_quality_contract
                else ""
            )
            reply_residual_story_flavor_role = (
                str(reply_residual_quality_contract.get("required_story_flavor_role") or "")
                if reply_residual_quality_contract
                else ""
            )
            reply_visibility_mode = (
                str(reply_signal_contract.get("visibility_mode") or "")
                if reply_signal_contract
                else ""
            )
            signal_anchor_tokens = (
                [
                    str(token).strip()
                    for token in list(reply_signal_contract.get("signal_anchor_tokens") or [])
                    if str(token).strip()
                ]
                if reply_signal_contract
                else []
            )
            objective_anchor_tokens = (
                [
                    str(token).strip()
                    for token in list(reply_signal_contract.get("objective_anchor_tokens") or [])
                    if str(token).strip()
                ]
                if reply_signal_contract
                else []
            )
            recent_opening_reused = self._reply_flat_recent_opening_reused(final_issues)
            recent_self_tail_reused = self._reply_flat_recent_tail_reused(final_issues)
            recent_second_beat_reused = self._reply_flat_recent_second_beat_reused(final_issues)
            for issue in final_issues:
                issue_copy = dict(issue)
                details = dict(issue_copy.get("details", {}))
                if (
                    issue_copy.get("issue_type") == "voice_flat_reply"
                    and not bool(details.get("voice_flat_blocking"))
                    and quality_result.action in {"normalize", "shorten", "accept"}
                ):
                    continue
                if self._should_omit_issue_from_quality_flags(
                    issue_copy,
                    quality_action=quality_result.action,
                    response_text=response_text,
                ):
                    continue
                if reply_focus_family and issue_copy.get("issue_type") in {
                    "reply_focus_missing",
                    "reply_without_direct_reaction",
                    "voice_flat_reply",
                }:
                    details["reply_focus_family"] = reply_focus_family
                if fallback_policy:
                    details.setdefault("fallback_policy", fallback_policy)
                if reply_surface_contract and issue_copy.get("issue_type") in {
                    "generic_reply_tail",
                    "voice_flat_reply",
                }:
                    details.setdefault(
                        "reply_surface_mode",
                        str(reply_surface_contract.get("surface_mode") or "").strip(),
                    )
                if reply_variety_contract and issue_copy.get("issue_type") in {
                    "generic_reply_tail",
                    "voice_flat_reply",
                }:
                    details.setdefault("reply_variety_shape", reply_variety_shape)
                    details.setdefault("reply_variety_second_beat", reply_variety_second_beat)
                if reply_dramatic_contract and issue_copy.get("issue_type") in {
                    "voice_flat_reply",
                    "generic_reply_tail",
                }:
                    details.setdefault("reply_dramatic_move_mode", reply_dramatic_move_mode)
                if reply_shape_contract and issue_copy.get("issue_type") in {
                    "voice_flat_reply",
                    "generic_reply_tail",
                }:
                    details.setdefault("reply_shape_mode", reply_shape_mode)
                    details.setdefault(
                        "reply_story_pressure_seen",
                        any(
                            bool(dict(item.get("details", {})).get("reply_story_pressure_seen"))
                            for item in final_issues
                        ),
                    )
                if reply_quality_contract and issue_copy.get("issue_type") in {
                    "voice_flat_reply",
                    "generic_reply_tail",
                }:
                    details.setdefault("reply_quality_shape", reply_quality_shape)
                    details.setdefault("reply_quality_second_beat", reply_quality_second_beat)
                    details.setdefault("reply_story_flavor_role", reply_story_flavor_role)
                    details.setdefault(
                        "reply_story_flavor_seen",
                        any(
                            bool(dict(item.get("details", {})).get("reply_story_flavor_seen"))
                            for item in final_issues
                        ),
                    )
                if reply_story_quality_contract and issue_copy.get("issue_type") in {
                    "voice_flat_reply",
                    "generic_reply_tail",
                }:
                    details.setdefault("reply_story_quality_shape", reply_story_quality_shape)
                    details.setdefault(
                        "reply_story_quality_second_beat",
                        reply_story_quality_second_beat,
                    )
                    details.setdefault("reply_story_quality_role", reply_story_quality_role)
                    details.setdefault(
                        "reply_story_quality_seen",
                        any(
                            bool(dict(item.get("details", {})).get("reply_story_quality_seen"))
                            for item in final_issues
                        ),
                    )
                if reply_residual_quality_contract and issue_copy.get("issue_type") in {
                    "voice_flat_reply",
                    "generic_reply_tail",
                }:
                    details.setdefault("reply_residual_shape", reply_residual_shape)
                    details.setdefault("reply_residual_second_beat", reply_residual_second_beat)
                    details.setdefault(
                        "reply_residual_story_flavor_role",
                        reply_residual_story_flavor_role,
                    )
                    details.setdefault(
                        "reply_residual_story_flavor_seen",
                        any(
                            bool(dict(item.get("details", {})).get("reply_residual_story_flavor_seen"))
                            for item in final_issues
                        ),
                    )
                if issue_copy.get("issue_type") in {
                    "signal_visibility_missing",
                    "scene_objective_visibility_missing",
                }:
                    details.setdefault("reply_visibility_mode", reply_visibility_mode)
                    details.setdefault("signal_anchor_tokens", signal_anchor_tokens)
                    details.setdefault("objective_anchor_tokens", objective_anchor_tokens)
                issue_copy["details"] = details
                issue_payloads.append(issue_copy)
            if quality_result.action in {"normalize", "shorten"}:
                issue_payloads.append(
                    {
                        "issue_type": "quality_output_normalized",
                        "severity": "info",
                        "details": {
                            "issue_count": len(final_issues),
                            "msg_type": msg_type,
                            "retry_kept_without_fallback": retry_kept_without_fallback,
                            "fallback_policy": fallback_policy,
                            "fallback_root_issue": fallback_root_issue,
                            "fallback_issue_set": fallback_issue_set,
                            "reply_surface_mode": (
                                str(reply_surface_contract.get("surface_mode") or "")
                                if reply_surface_contract
                                else ""
                            ),
                            "reply_variety_shape": reply_variety_shape,
                            "reply_variety_second_beat": reply_variety_second_beat,
                            "reply_dramatic_move_mode": reply_dramatic_move_mode,
                            "reply_shape_mode": reply_shape_mode,
                            "reply_quality_shape": reply_quality_shape,
                            "reply_quality_second_beat": reply_quality_second_beat,
                            "reply_story_flavor_role": reply_story_flavor_role,
                            "reply_story_quality_shape": reply_story_quality_shape,
                            "reply_story_quality_second_beat": reply_story_quality_second_beat,
                            "reply_story_quality_role": reply_story_quality_role,
                            "reply_residual_shape": reply_residual_shape,
                            "reply_residual_second_beat": reply_residual_second_beat,
                            "reply_residual_story_flavor_role": reply_residual_story_flavor_role,
                            "reply_visibility_mode": reply_visibility_mode,
                            "signal_anchor_tokens": signal_anchor_tokens,
                            "objective_anchor_tokens": objective_anchor_tokens,
                            "fallback_anchor_source": (
                                "reply_signal_contract"
                                if signal_anchor_tokens or objective_anchor_tokens
                                else "none"
                            ),
                            "fallback_second_tail_signature": self._fallback_move_signature(response_text),
                            "flat_issue_family": flat_issue_family,
                            "recent_opening_reused": recent_opening_reused,
                            "recent_self_tail_reused": recent_self_tail_reused,
                            "recent_ending_reused": recent_self_tail_reused,
                            "recent_second_beat_reused": recent_second_beat_reused,
                            "reply_second_beat_reused": recent_second_beat_reused,
                            "reply_dramatic_move_seen": any(
                                bool(dict(item.get("details", {})).get("reply_dramatic_move_seen"))
                                for item in final_issues
                            ),
                            "reply_soft_landing_used": any(
                                bool(dict(item.get("details", {})).get("reply_soft_landing_used"))
                                for item in final_issues
                            ),
                            "reply_shape_reused_recently": any(
                                bool(dict(item.get("details", {})).get("reply_shape_reused_recently"))
                                for item in final_issues
                            ),
                            "reply_shape_reused": any(
                                bool(dict(item.get("details", {})).get("reply_shape_reused"))
                                for item in final_issues
                            ),
                            "reply_pressure_shift_seen": any(
                                bool(dict(item.get("details", {})).get("reply_pressure_shift_seen"))
                                for item in final_issues
                            ),
                            "reply_story_pressure_seen": any(
                                bool(dict(item.get("details", {})).get("reply_story_pressure_seen"))
                                for item in final_issues
                            ),
                            "reply_story_flavor_seen": any(
                                bool(dict(item.get("details", {})).get("reply_story_flavor_seen"))
                                for item in final_issues
                            ),
                            "reply_story_quality_seen": any(
                                bool(dict(item.get("details", {})).get("reply_story_quality_seen"))
                                for item in final_issues
                            ),
                            "reply_residual_story_flavor_seen": any(
                                bool(dict(item.get("details", {})).get("reply_residual_story_flavor_seen"))
                                for item in final_issues
                            ),
                            "story_flavor_weak": any(
                                bool(dict(item.get("details", {})).get("story_flavor_weak"))
                                for item in final_issues
                            ),
                            "reply_quality_contract_missed": any(
                                bool(dict(item.get("details", {})).get("reply_quality_contract_missed"))
                                for item in final_issues
                            ),
                            "reply_story_quality_contract_missed": any(
                                bool(dict(item.get("details", {})).get("reply_story_quality_contract_missed"))
                                for item in final_issues
                            ),
                            "reply_residual_contract_missed": any(
                                bool(dict(item.get("details", {})).get("reply_residual_contract_missed"))
                                for item in final_issues
                            ),
                            **({"reply_focus_family": reply_focus_family} if reply_focus_family else {}),
                        },
                    }
                )
            elif quality_result.action == "fallback":
                issue_payloads.append(
                    {
                        "issue_type": "quality_output_fallback",
                        "severity": "warning",
                        "details": {
                            "issue_count": len(final_issues),
                            "msg_type": msg_type,
                            "retry_kept_without_fallback": retry_kept_without_fallback,
                            "fallback_policy": fallback_policy,
                            "fallback_root_issue": fallback_root_issue,
                            "fallback_issue_set": fallback_issue_set,
                            "reply_surface_mode": (
                                str(reply_surface_contract.get("surface_mode") or "")
                                if reply_surface_contract
                                else ""
                            ),
                            "reply_variety_shape": reply_variety_shape,
                            "reply_variety_second_beat": reply_variety_second_beat,
                            "reply_dramatic_move_mode": reply_dramatic_move_mode,
                            "reply_shape_mode": reply_shape_mode,
                            "reply_quality_shape": reply_quality_shape,
                            "reply_quality_second_beat": reply_quality_second_beat,
                            "reply_story_flavor_role": reply_story_flavor_role,
                            "reply_story_quality_shape": reply_story_quality_shape,
                            "reply_story_quality_second_beat": reply_story_quality_second_beat,
                            "reply_story_quality_role": reply_story_quality_role,
                            "reply_visibility_mode": reply_visibility_mode,
                            "signal_anchor_tokens": signal_anchor_tokens,
                            "objective_anchor_tokens": objective_anchor_tokens,
                            "fallback_anchor_source": (
                                "reply_signal_contract"
                                if signal_anchor_tokens or objective_anchor_tokens
                                else "none"
                            ),
                            "fallback_second_tail_signature": self._fallback_move_signature(response_text),
                            "flat_issue_family": flat_issue_family,
                            "recent_opening_reused": recent_opening_reused,
                            "recent_self_tail_reused": recent_self_tail_reused,
                            "recent_ending_reused": recent_self_tail_reused,
                            "recent_second_beat_reused": recent_second_beat_reused,
                            "reply_second_beat_reused": recent_second_beat_reused,
                            "reply_dramatic_move_seen": any(
                                bool(dict(item.get("details", {})).get("reply_dramatic_move_seen"))
                                for item in final_issues
                            ),
                            "reply_soft_landing_used": any(
                                bool(dict(item.get("details", {})).get("reply_soft_landing_used"))
                                for item in final_issues
                            ),
                            "reply_shape_reused_recently": any(
                                bool(dict(item.get("details", {})).get("reply_shape_reused_recently"))
                                for item in final_issues
                            ),
                            "reply_shape_reused": any(
                                bool(dict(item.get("details", {})).get("reply_shape_reused"))
                                for item in final_issues
                            ),
                            "reply_pressure_shift_seen": any(
                                bool(dict(item.get("details", {})).get("reply_pressure_shift_seen"))
                                for item in final_issues
                            ),
                            "reply_story_pressure_seen": any(
                                bool(dict(item.get("details", {})).get("reply_story_pressure_seen"))
                                for item in final_issues
                            ),
                            "reply_story_flavor_seen": any(
                                bool(dict(item.get("details", {})).get("reply_story_flavor_seen"))
                                for item in final_issues
                            ),
                            "reply_story_quality_seen": any(
                                bool(dict(item.get("details", {})).get("reply_story_quality_seen"))
                                for item in final_issues
                            ),
                            "story_flavor_weak": any(
                                bool(dict(item.get("details", {})).get("story_flavor_weak"))
                                for item in final_issues
                            ),
                            "reply_quality_contract_missed": any(
                                bool(dict(item.get("details", {})).get("reply_quality_contract_missed"))
                                for item in final_issues
                            ),
                            "reply_story_quality_contract_missed": any(
                                bool(dict(item.get("details", {})).get("reply_story_quality_contract_missed"))
                                for item in final_issues
                            ),
                            **({"reply_focus_family": reply_focus_family} if reply_focus_family else {}),
                        },
                    }
                )
            if quality_result.action == "drop":
                for issue in issue_payloads:
                    await self.db.insert_generation_quality_issue(
                        self.story_id,
                        {
                            "issue_type": issue["issue_type"],
                            "severity": issue.get("severity", "warning"),
                            "details": issue.get("details", {}),
                            "auto_action": quality_result.action,
                            "created_turn": self.world_clock.turn_number,
                            "scene_id": scene_id,
                            "log_id": None,
                        },
                    )
                self._runtime_place_snapshot[char_id] = current_place
                await self._advance_turn_cursor()
                return
            pending_quality_issue_payloads = issue_payloads

        if msg_type == "current_affairs" and _news_article_for_turn is not None:
            response_text = self._build_current_affairs_message(
                response_text,
                str(_news_article_for_turn.get("title", "")),
                str(_news_article_for_turn.get("url", "")),
            )

        # ── Step 14: DB保存 ──────────────────────────────────────────────
        emotion_snapshot = json.dumps(emotions)

        # target_char_id: list の場合は JSON 文字列化
        target_id_for_db: str | None
        if isinstance(target_char_id, list):
            target_id_for_db = json.dumps(target_char_id)
        else:
            target_id_for_db = target_char_id

        new_log_id = await self.db.insert_chat_log(
            self.story_id,
            {
                "sim_datetime":   self.world_clock.sim_datetime,
                "turn_number":    self.world_clock.turn_number,
                "char_id":        char_id,
                "msg_type":       msg_type,
                "target_char_id": target_id_for_db,
                "place_id":       current_place,
                "expression":     expression,
                "message":        response_text,
                "conversation_session_id": conversation["conversation_session_id"],
                "reply_to_log_id": conversation["reply_to_log_id"],
                "emotion_snapshot": emotion_snapshot,
                "llm_provider":   response.provider,
                "llm_model":      response.model,
                "posted_to_web":  0,
                "scene_id":       scene_id,
                "speaker_intent": speaker_intent,
                "quality_score":  quality_score,
                "quality_flags":  quality_flags,
            },
        )
        for issue in pending_quality_issue_payloads:
            await self.db.insert_generation_quality_issue(
                self.story_id,
                {
                    "issue_type": issue["issue_type"],
                    "severity": issue.get("severity", "warning"),
                    "details": issue.get("details", {}),
                    "auto_action": (
                        "accept"
                        if self._quality_guard is None
                        else quality_result.action
                    ),
                    "created_turn": self.world_clock.turn_number,
                    "scene_id": scene_id,
                    "log_id": new_log_id,
                },
            )
        if scene_id is not None:
            await self.db.record_scene_participant_turn(
                scene_id, char_id, turn_number=self.world_clock.turn_number
            )
        if conversation["conversation_session_id"] is not None:
            await self.db.update_conversation_session(
                conversation["conversation_session_id"],
                {
                    "participant_ids": conversation["participant_ids"],
                    "last_speaker_id": char_id,
                    "last_log_id": new_log_id,
                    "last_activity_sim_datetime": self.world_clock.sim_datetime,
                    "last_turn_number": self.world_clock.turn_number,
                    "scene_id": scene_id,
                },
            )

        # 時事モード: コメント履歴記録
        if msg_type == "current_affairs" and _news_article_for_turn is not None:
            article_url = str(_news_article_for_turn.get("url", ""))
            if article_url:
                await self.db.record_news_commentary(
                    self.story_id, char_id, article_url, self.world_clock.turn_number
                )
                self._last_news_turn_per_char[char_id] = self.world_clock.turn_number

        await self._process_story_hooks(
            log_id=new_log_id,
            char_id=char_id,
            target_char_id=target_char_id,
            scene_id=scene_id,
            place_id=current_place,
            message=response_text,
            msg_type=msg_type,
        )
        await self._process_relationship_dynamics(
            log_id=new_log_id,
            char_id=char_id,
            target_char_id=target_char_id,
            scene_id=scene_id,
            message=response_text,
            msg_type=msg_type,
        )

        await self.db.insert_character_state(
            self.story_id,
            {
                "char_id":           char_id,
                "sim_datetime":      self.world_clock.sim_datetime,
                "turn_number":       self.world_clock.turn_number,
                "current_place":     current_place,
                "previous_place":    previous_place,
                "move_reason":       move_reason,
                "current_action":    msg_type,
                "current_expression": expression,
                "stress":      emotions["stress"],
                "motivation":  emotions["motivation"],
                "loneliness":  emotions["loneliness"],
                "excitement":  emotions["excitement"],
            },
        )

        # in-memory 場所トラッキングを更新
        self._runtime_place_snapshot[char_id] = current_place
        self._last_known_place[char_id] = current_place
        # 最新感情を記録（emotion_delta 計算用）
        self._prev_emotion_snapshots[char_id] = dict(emotions)

        await self._advance_turn_cursor()

    async def run(self) -> None:
        """シミュレーションのメインループ。stop() が呼ばれるまで実行し続ける。"""
        self._running = True
        try:
            if not self._initialized:
                await self.initialize()
            logger.info("StoryEngine started", extra={"story_id": self.story_id})
            while self._running:
                place_snapshot = await self._build_place_snapshot()
                if self._scene_path_enabled():
                    self._planned_turn_queue = await self._build_round_turn_plan(place_snapshot)
                    self._planned_turn_index = 0
                else:
                    self._planned_turn_queue = []
                    self._planned_turn_index = 0
                turn_count = len(self._planned_turn_queue) if self._planned_turn_queue else len(self._characters)
                for _ in range(turn_count):
                    if not self._running:
                        break
                    await self.run_one_turn()
                if self._running:
                    current_places = await self._build_place_snapshot()
                    character_moved = current_places != place_snapshot
                    await self._run_post_round_hooks(
                        character_moved,
                        current_places=current_places,
                    )
                    await asyncio.sleep(self._story["turn_interval_sec"])
        finally:
            self._running = False
            await self._finalize_session()
            await self._close_owned_news_store()
            logger.info("StoryEngine stopped", extra={"story_id": self.story_id})

    async def _run_single_round(self) -> dict[str, Any]:
        """probe/test 用に 1 round を進め、post-round の要約を返す。"""
        assert self.world_clock is not None
        place_snapshot = await self._build_place_snapshot()
        if self._scene_path_enabled():
            self._planned_turn_queue = await self._build_round_turn_plan(place_snapshot)
            self._planned_turn_index = 0
        else:
            self._planned_turn_queue = []
            self._planned_turn_index = 0
        turn_count = len(self._planned_turn_queue) if self._planned_turn_queue else len(self._characters)
        start_turn_number = self.world_clock.turn_number
        for _ in range(turn_count):
            await self.run_one_turn()
        current_places = await self._build_place_snapshot()
        character_moved = current_places != place_snapshot
        processed_closed_scene_ids = list(self._closed_story_scene_ids)
        await self._run_post_round_hooks(
            character_moved,
            current_places=current_places,
        )
        growth_results: dict[str, dict[str, Any]] = {}
        if self._growth_engine is not None:
            growth_results = self._growth_engine.get_last_round_results()
        return {
            "start_turn_number": start_turn_number,
            "end_turn_number": self.world_clock.turn_number,
            "planned_turns": turn_count,
            "processed_closed_scene_ids": processed_closed_scene_ids,
            "growth_results": growth_results,
        }

    async def _run_post_round_hooks(
        self,
        character_moved: bool,
        *,
        current_places: dict[str, str] | None = None,
    ) -> None:
        """ラウンド完了後フック。StoryMemory 要約 + NarrationEngine 呼び出し。"""
        try:
            await self._run_post_round_hooks_inner(character_moved, current_places=current_places)
        except Exception:
            logger.exception(
                "Post-round hook raised unhandled exception; continuing to next round",
                extra={"story_id": self.story_id},
            )

    async def _run_post_round_hooks_inner(
        self,
        character_moved: bool,
        *,
        current_places: dict[str, str] | None = None,
    ) -> None:
        """_run_post_round_hooks の実装本体。例外は呼び出し元で catch される。"""
        assert self.world_clock is not None
        turn_number = self.world_clock.turn_number
        effective_places = current_places or await self._build_place_snapshot()

        if self._director_persona is not None:
            await self._director_persona.reload_active_persona()

        # ラウンドレベルで会話セッションを同期（全キャラ分散時の stale session 検知）
        await self._sync_active_conversation_sessions(effective_places)

        # emotion_delta 計算
        max_delta = 0.0
        for cid, start_emo in self._round_start_emotions.items():
            current_emo = self._prev_emotion_snapshots.get(cid, start_emo)
            for key in ("stress", "motivation", "loneliness", "excitement"):
                delta = abs(current_emo.get(key, 0.0) - start_emo.get(key, 0.0))
                max_delta = max(max_delta, delta)
        self._round_start_emotions.clear()

        if self._story_memory_manager is not None:
            await self._story_memory_manager.process_round(turn_number)

        chapter_break = False
        if self._narration_engine is not None:
            ctx = await self._build_narration_context(
                turn_number,
                character_moved,
                emotion_delta=max_delta,
                current_places=effective_places,
            )
            chapter_break = await self._narration_engine.process_round(turn_number, ctx)

        if self._growth_engine is not None and self._config is not None and self._config.growth_engine.enabled:
            await self._growth_engine.process_round(turn_number, self._characters)
        elif self._character_evolution_manager is not None:
            await self._character_evolution_manager.process_round(
                turn_number, self._characters
            )

        scene_close_candidate_ids: list[int] = []
        if self._closed_story_scene_ids or self._novel_generator is not None:
            scene_close_candidate_ids = await self._process_closed_story_scenes(turn_number)

        await self._conversation_motif_engine.process_hooks(turn_number)

        if self._interaction_pattern_engine is not None:
            await self._interaction_pattern_engine.process_round(turn_number)

        if self._relationship_mode_engine is not None:
            await self._relationship_mode_engine.process_round(turn_number)

        if self._emergent_canonizer is not None:
            await self._emergent_canonizer.process_round(turn_number)

        if self._canon_reignition_engine is not None:
            await self._canon_reignition_engine.process_round(turn_number)

        if self._canon_profile_writeback_engine is not None:
            await self._canon_profile_writeback_engine.process_round(turn_number)

        if self._dramatic_pressure_evaluator is not None:
            await self._dramatic_pressure_evaluator.process_round(turn_number)

        active_chapter: dict[str, Any] | None = None
        if self._chapter_manager is not None:
            raw_chapter_round_result = await self._chapter_manager.process_round(turn_number)
            chapter_round_result = (
                raw_chapter_round_result if isinstance(raw_chapter_round_result, dict) else {}
            )
            raw_active_chapter = chapter_round_result.get("activated_chapter")
            active_chapter = raw_active_chapter if isinstance(raw_active_chapter, dict) else None
            if active_chapter is None:
                raw_active_chapter = await self._chapter_manager.get_active_chapter()
                active_chapter = raw_active_chapter if isinstance(raw_active_chapter, dict) else None

        episode_round_result: dict[str, Any] = {}
        active_episode: dict[str, Any] | None = None
        if self._episode_planner is not None:
            raw_episode_round_result = await self._episode_planner.process_round(
                turn_number,
                active_chapter=active_chapter,
            )
            episode_round_result = (
                raw_episode_round_result if isinstance(raw_episode_round_result, dict) else {}
            )
            raw_active_episode = episode_round_result.get("active_episode")
            active_episode = raw_active_episode if isinstance(raw_active_episode, dict) else None
            if active_episode is None:
                raw_active_episode = await self._episode_planner.get_active_episode()
                active_episode = raw_active_episode if isinstance(raw_active_episode, dict) else None

        if self._director_persona is not None:
            await self._director_persona.evaluate_satisfaction(turn_number)

        if self._story_director is not None:
            await self.db.expire_interventions_before_turn(
                self.story_id,
                turn_number=turn_number,
            )
            recent_memory_texts: list[str] = []
            if self._story_memory_manager is not None:
                seen_ids: set[int] = set()
                for char in self._characters:
                    for mem in await self._story_memory_manager.get_relevant_memories(char["id"]):
                        mem_id = int(mem["id"])
                        if mem_id not in seen_ids:
                            seen_ids.add(mem_id)
                            recent_memory_texts.append(str(mem["summary"]))
            analysis_window = 3
            if self._config is not None:
                analysis_window = max(1, self._config.story_director.analysis_interval_rounds)
            since_turn = max(0, turn_number - analysis_window + 1)
            recent_scene_outcomes = await self.db.get_closed_story_scene_summaries(
                self.story_id,
                since_turn=since_turn,
                limit=5,
            )
            open_hook_summaries: list[str] = []
            open_hooks = await self.db.get_open_story_hooks(self.story_id)
            for hook in open_hooks[:5]:
                title = str(hook.get("title") or "").strip()
                description = str(hook.get("description") or "").strip()
                if title and description:
                    open_hook_summaries.append(f"{title}: {description}")
                elif title:
                    open_hook_summaries.append(title)
                elif description:
                    open_hook_summaries.append(description)
            recent_relationship_event_summaries = [
                str(event.get("summary") or "").strip()
                for event in await self.db.get_recent_relationship_events(
                    self.story_id,
                    since_turn=since_turn,
                    limit=5,
                )
                if str(event.get("summary") or "").strip()
            ]
            carry_over_hook_summaries: list[str] = []
            if active_episode is not None:
                carry_hook_ids = {
                    int(hook_id)
                    for hook_id in list(active_episode.get("carry_over_hook_ids") or [])
                }
                for hook in open_hooks:
                    try:
                        hook_id = int(hook.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if hook_id not in carry_hook_ids:
                        continue
                    title = str(hook.get("title") or "").strip()
                    description = str(hook.get("description") or "").strip()
                    if title and description:
                        carry_over_hook_summaries.append(f"{title}: {description}")
                    elif title:
                        carry_over_hook_summaries.append(title)
                    elif description:
                        carry_over_hook_summaries.append(description)
            director_ctx = StoryDirectorContext(
                story_id=self.story_id,
                turn_number=turn_number,
                world_rules=str(self._story.get("world_rules", "")),
                recent_memory_texts=recent_memory_texts,
                recent_scene_outcomes=recent_scene_outcomes,
                open_hook_summaries=open_hook_summaries,
                recent_relationship_event_summaries=recent_relationship_event_summaries,
                active_episode_id=(
                    int(active_episode["id"])
                    if active_episode is not None and active_episode.get("id") is not None
                    else None
                ),
                active_episode_type=(
                    str(active_episode.get("episode_type") or "") or None
                    if active_episode is not None
                    else None
                ),
                active_episode_goal=(
                    str(active_episode.get("goal") or "") or None
                    if active_episode is not None
                    else None
                ),
                active_episode_stakes=(
                    str(active_episode.get("stakes") or "") or None
                    if active_episode is not None
                    else None
                ),
                active_episode_pattern_type=(
                    str(active_episode.get("active_pattern_type") or "") or None
                    if active_episode is not None
                    else None
                ),
                carry_over_hook_summaries=carry_over_hook_summaries,
                episode_age_turns=(
                    turn_number - int(active_episode.get("opened_turn") or turn_number) + 1
                    if active_episode is not None
                    else 0
                ),
            )
            await self._story_director.process_round(turn_number, director_ctx)

        if self._novel_generator is not None and scene_close_candidate_ids:
            for scene_id in scene_close_candidate_ids:
                await self._invoke_novel_generator_for_scene(turn_number, scene_id)

        if self._novel_generator is not None and chapter_break:
            await self._invoke_novel_generator(turn_number, trigger="chapter_break")

        # 閉じた会話セッションごとに session_end prose を生成
        if self._novel_generator is not None and self._closed_conversation_session_ids:
            for session_id in self._closed_conversation_session_ids:
                await self._invoke_novel_generator_for_session(turn_number, session_id)
        raw_closed_episode = episode_round_result.get("closed_episode")
        closed_episode = raw_closed_episode if isinstance(raw_closed_episode, dict) else None
        if self._novel_generator is not None and closed_episode is not None:
            await self._invoke_novel_generator_for_episode(turn_number, closed_episode)
        self._closed_conversation_session_ids.clear()
        self._closed_story_scene_ids.clear()

    async def _scene_close_artifact_status(self, scene_id: int) -> str:
        scene = await self.db.get_story_scene(scene_id)
        if scene is None or scene.get("scene_type") != "conversation" or scene.get("status") != "closed":
            return "skip"
        scene_arc = await self.db.get_arc_by_source_scene_id(self.story_id, scene_id)
        if scene_arc is None:
            return "missing_arc"
        scene_outputs = await self.db.get_novel_outputs(self.story_id, int(scene_arc["id"]))
        if scene_outputs:
            return "complete"
        return "missing_novel_output"

    async def _collect_scene_close_handoff_candidate_ids(self, turn_number: int) -> list[int]:
        candidate_ids: list[int] = []
        seen: set[int] = set()
        for scene_id in self._closed_story_scene_ids:
            sid = int(scene_id)
            if sid not in seen:
                seen.add(sid)
                candidate_ids.append(sid)
        since_turn = max(0, turn_number - _SCENE_CLOSE_BACKLOG_LOOKBACK_TURNS + 1)
        missing_arc_ids: list[int] = []
        missing_novel_ids: list[int] = []
        backlog_batches = [
            (since_turn, _SCENE_CLOSE_BACKLOG_LIMIT),
            (0, _SCENE_CLOSE_BACKLOG_LIMIT * 3),
        ]
        for batch_since_turn, batch_limit in backlog_batches:
            if len(missing_arc_ids) + len(missing_novel_ids) >= _SCENE_CLOSE_BACKLOG_LIMIT:
                break
            backlog_scenes = await self.db.get_recent_closed_story_scenes(
                self.story_id,
                since_turn=batch_since_turn,
                limit=batch_limit,
                scene_type="conversation",
            )
            for scene in backlog_scenes:
                if len(missing_arc_ids) + len(missing_novel_ids) >= _SCENE_CLOSE_BACKLOG_LIMIT:
                    break
                scene_id = int(scene["id"])
                if scene_id in seen:
                    continue
                artifact_status = await self._scene_close_artifact_status(scene_id)
                if artifact_status == "missing_arc":
                    seen.add(scene_id)
                    missing_arc_ids.append(scene_id)
                elif artifact_status == "missing_novel_output":
                    seen.add(scene_id)
                    missing_novel_ids.append(scene_id)
        candidate_ids.extend(missing_arc_ids)
        candidate_ids.extend(missing_novel_ids)
        return candidate_ids

    async def _process_closed_story_scenes(self, turn_number: int) -> list[int]:
        """close 済み story scene の hook consolidation と artifact 候補収集を行う。"""
        candidate_scene_ids = await self._collect_scene_close_handoff_candidate_ids(turn_number)
        if (
            self._scene_hook_analyzer is None
            or self._config is None
            or not self._config.story_hooks.enabled
        ):
            return candidate_scene_ids

        for scene_id in candidate_scene_ids:
            scene = await self.db.get_story_scene(scene_id)
            if scene is None or scene.get("scene_type") != "conversation":
                continue
            result = await self._scene_hook_analyzer.analyze_closed_scene(scene_id)
            if result is None:
                continue

            refined_summary = str(result.get("refined_outcome_summary") or "").strip()
            if refined_summary:
                await self.db.update_story_scene(
                    scene_id,
                    {"outcome_summary": refined_summary},
                )

            for hook_id in result.get("resolve_hook_ids", []):
                await self.db.resolve_story_hook(
                    int(hook_id),
                    resolution_log_id=None,
                    resolved_turn=turn_number,
                    summary=refined_summary or str(scene.get("outcome_summary") or ""),
                )

            existing_hooks = await self.db.get_open_story_hooks_for_scene(self.story_id, scene_id)
            existing_keys = {
                (
                    str(hook.get("hook_type") or ""),
                    _normalize_hook_text(str(hook.get("description") or "")),
                )
                for hook in existing_hooks
            }
            for merged_hook in result.get("merged_hooks", []):
                hook_key = (
                    str(merged_hook.get("hook_type") or ""),
                    _normalize_hook_text(str(merged_hook.get("description") or "")),
                )
                if hook_key in existing_keys:
                    continue
                await self.db.insert_story_hook(
                    self.story_id,
                    {
                        "hook_type": merged_hook["hook_type"],
                        "status": "open",
                        "owner_char_id": merged_hook.get("owner_char_id"),
                        "target_char_id": merged_hook.get("target_char_id"),
                        "title": merged_hook["title"],
                        "description": merged_hook["description"],
                        "priority": merged_hook.get("priority", 0.7),
                        "source_scene_id": scene_id,
                    },
                )
                existing_keys.add(hook_key)
        return candidate_scene_ids

    async def _invoke_novel_generator(
        self, turn_number: int, trigger: str,
    ) -> None:
        """NovelGenerator を呼び出す共通ロジック。"""
        assert self._novel_generator is not None
        from engine.novel_generator import NovelGeneratorContext
        recent_logs = await self.db.get_recent_chat_logs(self.story_id, limit=20)
        char_name_map = {c["id"]: c["name_ja"] for c in self._characters}
        ng_log_lines = [
            f"{char_name_map.get(log['char_id'], log['char_id'])}: {log['message']}"
            for log in recent_logs
            if log["char_id"] != "_narrator"
        ]
        ng_source_ids = [
            int(log["id"]) for log in recent_logs
            if log["char_id"] != "_narrator"
        ]
        ng_memory_texts: list[str] = []
        if self._story_memory_manager is not None:
            seen_ng: set[int] = set()
            for char in self._characters:
                for mem in await self._story_memory_manager.get_relevant_memories(char["id"]):
                    mid = int(mem["id"])
                    if mid not in seen_ng:
                        seen_ng.add(mid)
                        ng_memory_texts.append(str(mem["summary"]))
        ng_ctx = NovelGeneratorContext(
            world_rules=str(self._story.get("world_rules", "")),
            recent_log_lines=ng_log_lines,
            story_memory_texts=ng_memory_texts,
            trigger=trigger,
            source_log_ids=ng_source_ids,
        )
        await self._novel_generator.process_round(turn_number, ng_ctx)

    async def _invoke_novel_generator_for_session(
        self, turn_number: int, conversation_session_id: int,
    ) -> None:
        """閉じた会話セッション固有のログで NovelGenerator を呼び出す。"""
        assert self._novel_generator is not None
        from engine.novel_generator import NovelGeneratorContext
        session_logs = await self.db.get_recent_conversation_logs(
            self.story_id, conversation_session_id, limit=50
        )
        if any(log.get("scene_id") is not None for log in session_logs):
            return
        char_name_map = {c["id"]: c["name_ja"] for c in self._characters}
        ng_log_lines = [
            f"{char_name_map.get(log['char_id'], log['char_id'])}: {log['message']}"
            for log in session_logs
            if log["char_id"] != "_narrator"
        ]
        ng_source_ids = [
            int(log["id"]) for log in session_logs
            if log["char_id"] != "_narrator"
        ]
        ng_memory_texts: list[str] = []
        if self._story_memory_manager is not None:
            seen_ng: set[int] = set()
            for char in self._characters:
                for mem in await self._story_memory_manager.get_relevant_memories(char["id"]):
                    mid = int(mem["id"])
                    if mid not in seen_ng:
                        seen_ng.add(mid)
                        ng_memory_texts.append(str(mem["summary"]))
        ng_ctx = NovelGeneratorContext(
            world_rules=str(self._story.get("world_rules", "")),
            recent_log_lines=ng_log_lines,
            story_memory_texts=ng_memory_texts,
            trigger="session_end",
            source_log_ids=ng_source_ids,
        )
        await self._novel_generator.process_round(turn_number, ng_ctx)

    async def _invoke_novel_generator_for_scene(
        self,
        turn_number: int,
        scene_id: int,
    ) -> None:
        """閉じた conversation scene から scene_close prose を生成する。"""
        assert self._novel_generator is not None
        from engine.novel_generator import NovelGeneratorContext

        scene = await self.db.get_story_scene(scene_id)
        if scene is None or scene.get("scene_type") != "conversation" or scene.get("status") != "closed":
            return

        existing_scene_arc = await self.db.get_arc_by_source_scene_id(self.story_id, scene_id)
        if existing_scene_arc is not None:
            existing_outputs = await self.db.get_novel_outputs(
                self.story_id,
                int(existing_scene_arc["id"]),
            )
            if existing_outputs:
                return

        scene_logs = await self.db.get_scene_logs(scene_id, limit=50)
        ng_ctx = await self._build_scene_close_novel_context(
            scene_id=scene_id,
            scene=scene,
            scene_logs=scene_logs,
        )
        await self._novel_generator.process_round(turn_number, ng_ctx)

    async def _build_scene_close_novel_context(
        self,
        *,
        scene_id: int,
        scene: dict[str, Any],
        scene_logs: list[dict[str, Any]],
    ) -> Any:
        from engine.novel_generator import NovelGeneratorContext

        char_name_map = {c["id"]: c["name_ja"] for c in self._characters}
        existing_scene_arc = await self.db.get_arc_by_source_scene_id(self.story_id, scene_id)
        ng_log_lines = [
            f"{char_name_map.get(log['char_id'], log['char_id'])}: {log['message']}"
            for log in scene_logs
            if log["char_id"] != "_narrator"
        ]
        participant_names = list(dict.fromkeys(
            char_name_map.get(log["char_id"], log["char_id"])
            for log in scene_logs
            if log["char_id"] != "_narrator"
        ))
        if not participant_names:
            scene_participants = await self.db.get_scene_participants(scene_id)
            participant_names = list(
                dict.fromkeys(
                    char_name_map.get(str(row.get("char_id") or ""), str(row.get("char_id") or ""))
                    for row in scene_participants
                    if str(row.get("char_id") or "").strip()
                )
            )

        hook_summaries: list[str] = []
        for hook in await self.db.get_open_story_hooks_for_scene(self.story_id, scene_id):
            title = str(hook.get("title") or "").strip()
            description = str(hook.get("description") or "").strip()
            if title and description:
                hook_summaries.append(f"{title}: {description}")
            elif title:
                hook_summaries.append(title)
            elif description:
                hook_summaries.append(description)

        tension_summaries: list[str] = []
        for tension in await self.db.get_active_tensions(self.story_id):
            tension_type = str(tension.get("tension_type") or "").strip()
            description = str(tension.get("description") or "").strip()
            if tension_type and description:
                tension_summaries.append(f"{tension_type}: {description}")
            elif description:
                tension_summaries.append(description)

        return NovelGeneratorContext(
            world_rules=str(self._story.get("world_rules", "")),
            recent_log_lines=ng_log_lines,
            trigger="scene_close",
            source_log_ids=[int(log["id"]) for log in scene_logs if log["char_id"] != "_narrator"],
            scene_id=scene_id,
            scene_type=str(scene.get("scene_type") or "") or None,
            scene_outcome_summary=(
                str(existing_scene_arc.get("summary") or "").strip()
                if existing_scene_arc is not None and str(existing_scene_arc.get("summary") or "").strip()
                else self._fallback_scene_close_outcome_summary(
                    scene=scene,
                    scene_logs=scene_logs,
                    participant_names=participant_names,
                )
            ),
            hook_summaries=hook_summaries,
            tension_summaries=tension_summaries,
            place_id=str(scene.get("place_id") or "") or None,
            participant_names=participant_names,
        )

    def _fallback_scene_close_outcome_summary(
        self,
        *,
        scene: dict[str, Any],
        scene_logs: list[dict[str, Any]],
        participant_names: list[str],
    ) -> str | None:
        explicit_summary = str(scene.get("outcome_summary") or "").strip()
        if explicit_summary:
            return explicit_summary

        message_excerpts = [
            str(log.get("message") or "").strip()
            for log in scene_logs
            if log.get("char_id") != "_narrator" and str(log.get("message") or "").strip()
        ]
        if message_excerpts:
            excerpt = " ".join(message_excerpts[-2:])[:100]
            place_id = str(scene.get("place_id") or "").strip()
            if place_id:
                return f"{place_id}では「{excerpt}」というやり取りが次へ持ち越された。"
            return f"「{excerpt}」というやり取りが次へ持ち越された。"

        place_id = str(scene.get("place_id") or "").strip()
        if participant_names and place_id:
            return f"{'、'.join(participant_names)}のやり取りが{place_id}に残った。"
        if participant_names:
            return f"{'、'.join(participant_names)}のやり取りが次へ持ち越された。"
        if place_id:
            return f"{place_id}の場面が次へ持ち越された。"
        return None

    async def _invoke_novel_generator_for_episode(
        self,
        turn_number: int,
        episode: dict[str, Any],
    ) -> None:
        """閉じた episode から episode_close prose を生成する。"""
        assert self._novel_generator is not None
        from engine.novel_generator import NovelGeneratorContext

        recent_logs = await self.db.get_recent_chat_logs(self.story_id, limit=20)
        char_name_map = {c["id"]: c["name_ja"] for c in self._characters}
        focus_char_ids = {str(char_id) for char_id in list(episode.get("focus_char_ids") or [])}
        filtered_logs = [
            log
            for log in recent_logs
            if log["char_id"] != "_narrator"
            and (not focus_char_ids or str(log["char_id"]) in focus_char_ids)
        ]
        source_logs = filtered_logs if filtered_logs else [log for log in recent_logs if log["char_id"] != "_narrator"]
        ng_ctx = NovelGeneratorContext(
            world_rules=str(self._story.get("world_rules", "")),
            recent_log_lines=[
                f"{char_name_map.get(log['char_id'], log['char_id'])}: {log['message']}"
                for log in source_logs[-8:]
            ],
            trigger="episode_close",
            source_log_ids=[int(log["id"]) for log in source_logs],
            episode_id=int(episode["id"]) if episode.get("id") is not None else None,
            episode_type=str(episode.get("episode_type") or "") or None,
            episode_goal=str(episode.get("goal") or "") or None,
            episode_summary=str(episode.get("summary") or "") or None,
        )
        await self._novel_generator.process_round(turn_number, ng_ctx)

    async def _finalize_session(self) -> None:
        """エンジン停止時に残存 active session を閉じ、未処理分の prose を生成する。"""
        assert self.world_clock is not None
        # 残存 active session を全て閉じる
        active_sessions = await self.db.get_active_conversation_sessions(self.story_id)
        for session in active_sessions:
            await self.db.close_conversation_session(
                session["id"],
                last_activity_sim_datetime=self.world_clock.sim_datetime,
                last_turn_number=self.world_clock.turn_number,
            )
            self._closed_conversation_session_ids.append(session["id"])
            logger.info(
                "Closing active session on engine shutdown",
                extra={"story_id": self.story_id, "session_id": session["id"]},
            )
        # 未処理の closed session から prose を生成
        if self._novel_generator is not None:
            for session_id in self._closed_conversation_session_ids:
                await self._invoke_novel_generator_for_session(
                    self.world_clock.turn_number, session_id
                )
        self._closed_conversation_session_ids.clear()

    async def _build_narration_context(
        self, turn_number: int, character_moved: bool,
        emotion_delta: float = 0.0,
        current_places: dict[str, str] | None = None,
    ) -> NarrationContext:
        """NarrationEngine 用コンテキストを組み立てる。"""
        assert self.world_clock is not None

        # 直近ログを取得（_narrator 行を除外して発言行のみ使用）
        recent_logs = await self.db.get_recent_chat_logs(self.story_id, limit=10)
        char_name_map = {c["id"]: c["name_ja"] for c in self._characters}
        recent_log_lines = [
            f"{char_name_map.get(log['char_id'], log['char_id'])}: {log['message']}"
            for log in recent_logs
            if log["char_id"] != "_narrator"
        ]

        # 代表場所: ラウンド最後のキャラの場所
        effective_places = current_places or await self._build_place_snapshot()
        if effective_places:
            place_id = list(effective_places.values())[-1]
        elif self._places:
            place_id = next(iter(self._places))
        else:
            place_id = "unknown"
        place_name = self._places.get(place_id, place_id)

        # StoryMemory テキスト（利用可能なら全キャラ分をまとめて注入）
        story_memory_texts: list[str] = []
        if self._story_memory_manager is not None:
            seen_ids: set[int] = set()
            for char in self._characters:
                for mem in await self._story_memory_manager.get_relevant_memories(char["id"]):
                    mem_id = int(mem["id"])
                    if mem_id not in seen_ids:
                        seen_ids.add(mem_id)
                        story_memory_texts.append(str(mem["summary"]))

        return NarrationContext(
            sim_datetime=self.world_clock.sim_datetime,
            place_id=place_id,
            place_name=place_name,
            world_rules=str(self._story.get("world_rules", "")),
            recent_log_lines=recent_log_lines,
            emotion_delta=emotion_delta,
            character_moved=character_moved,
            story_memory_texts=story_memory_texts,
        )

    def _make_scheduler(self) -> "Scheduler":
        """現在の config を使って Scheduler を生成する。"""
        return _make_scheduler_from_config(self._config)

    async def _try_current_affairs_injection(
        self, char_id: str
    ) -> "dict[str, Any] | None":
        """時事モード injection を試みる。発火すれば選択記事 dict を返す。"""
        if self._news_selector is None or self._config is None:
            return None
        if not self._config.news_mode.enabled:
            logger.debug(
                "news_mode: disabled in global config char_id=%s", char_id,
                extra={"story_id": self.story_id},
            )
            return None

        # per-story の時事モード設定を取得
        settings = await self.db.get_news_mode_settings(self.story_id)
        if not settings.get("enabled", False):
            logger.debug(
                "news_mode: per-story disabled char_id=%s", char_id,
                extra={"story_id": self.story_id},
            )
            return None
        intensity_name: str = str(settings.get("intensity", "low"))
        preset = self._config.news_mode.get_preset(intensity_name)
        if preset.rate <= 0.0:
            logger.debug(
                "news_mode: intensity=off char_id=%s", char_id,
                extra={"story_id": self.story_id},
            )
            return None

        # クールダウンチェック
        assert self.world_clock is not None
        current_turn = self.world_clock.turn_number
        last_turn = self._last_news_turn_per_char.get(char_id, -9999)
        if (current_turn - last_turn) < preset.cooldown_turns:
            logger.debug(
                "news_mode: cooldown char_id=%s turns_left=%d",
                char_id,
                preset.cooldown_turns - (current_turn - last_turn),
                extra={"story_id": self.story_id},
            )
            return None

        # 確率判定
        import random as _random
        roll = _random.random()
        if roll >= preset.rate:
            logger.debug(
                "news_mode: probability miss char_id=%s rate=%.3f roll=%.3f",
                char_id, preset.rate, roll,
                extra={"story_id": self.story_id},
            )
            return None

        # 記事選択
        char = next((c for c in self._characters if c["id"] == char_id), {})
        exclude_urls = await self.db.get_commented_article_urls(self.story_id, char_id)
        article = await self._news_selector.select_for_character(
            char=char,
            story_id=self.story_id,
            exclude_urls=exclude_urls,
            tag_filter=self._news_tag_filter or None,
        )
        if article is None:
            logger.info(
                "news_mode: no article available char_id=%s", char_id,
                extra={"story_id": self.story_id},
            )
            return None
        logger.info(
            "news_mode: injection fired char_id=%s title=%r",
            char_id, str(article.get("title", ""))[:50],
            extra={"story_id": self.story_id},
        )
        return article

    async def _build_scene_script_context(self) -> SceneScriptContext:
        """PreTurnSceneEngine 用コンテキストを組み立てる。"""
        assert self.world_clock is not None

        recent_logs = await self.db.get_recent_chat_logs(self.story_id, limit=10)
        char_name_map = {c["id"]: c["name_ja"] for c in self._characters}
        recent_dialogue_lines = [
            f"{char_name_map.get(log['char_id'], log['char_id'])}: {log['message']}"
            for log in recent_logs
            if log["char_id"] != "_narrator"
        ]

        current_places = await self._build_place_snapshot()
        places_by_name: dict[str, list[str]] = {}
        for char_id, place_id in current_places.items():
            place_label = self._places.get(place_id, place_id)
            name = char_name_map.get(char_id, char_id)
            places_by_name.setdefault(place_label, []).append(name)

        active_character_states: list[dict[str, Any]] = []
        for char in self._characters:
            state = await self.db.get_latest_character_state(self.story_id, char["id"])
            if state is not None:
                place_id = current_places.get(char["id"], state.get("current_place", ""))
                active_character_states.append({
                    "char_id": char["id"],
                    "name": char["name_ja"],
                    "place": self._places.get(place_id, place_id),
                    "current_goal": str(char.get("current_goal") or ""),
                    "current_worry": str(char.get("current_worry") or ""),
                    "dominant_emotion": _pick_dominant_emotion(state),
                })

        chapter_world_injection: str | None = None
        chapter_beat_goal: str | None = None
        chapter_beat_phase: str | None = None
        chapter_id: str | None = None
        if self._chapter_manager is not None:
            chapter_world_injection = await self._chapter_manager.get_current_world_injection()
            beat = await self._chapter_manager.get_current_beat()
            if beat is not None:
                chapter_beat_goal = beat.get("goal")
                chapter_beat_phase = beat.get("phase")
            active_ch = await self.db.get_active_chapter(self.story_id)
            if active_ch is not None:
                chapter_id = str(active_ch.get("chapter_id", ""))

        director_persona_name: str | None = None
        director_persona_values: list[str] = []
        director_persona_traits: list[str] = []
        director_persona_id: str | None = None
        if self._director_persona is not None:
            persona = await self.db.get_active_director_persona(self.story_id)
            if persona is not None:
                director_persona_name = str(persona.get("name", ""))
                director_persona_values = list(persona.get("values_json") or [])
                director_persona_traits = list(persona.get("traits_json") or [])
                director_persona_id = str(persona.get("persona_id", ""))

        return SceneScriptContext(
            sim_datetime=self.world_clock.sim_datetime,
            world_rules=str(self._story.get("world_rules", "")),
            recent_dialogue_lines=recent_dialogue_lines,
            active_character_states=active_character_states,
            required_character_ids=self._scene_script_required_character_ids(),
            current_places=places_by_name,
            chapter_world_injection=chapter_world_injection,
            chapter_beat_goal=chapter_beat_goal,
            chapter_beat_phase=chapter_beat_phase,
            chapter_id=chapter_id,
            director_persona_name=director_persona_name,
            director_persona_values=director_persona_values,
            director_persona_traits=director_persona_traits,
            director_persona_id=director_persona_id,
            round_number=self.world_clock.turn_number,
        )

    async def stop(self) -> None:
        """グレースフル停止フラグを立てる。"""
        was_running = self._running
        self._running = False
        if not was_running:
            await self._close_owned_news_store()
        logger.info("StoryEngine stop requested", extra={"story_id": self.story_id})

    async def _close_owned_news_store(self) -> None:
        if self._owns_news_store and self._news_store is not None:
            await self._news_store.close()
            self._news_store = None
            self._owns_news_store = False

    @staticmethod
    def _build_current_affairs_message(reaction: str, title: str, url: str) -> str:
        """Construct current_affairs message: Markdown-linked title in quotes + character reaction.

        Stored format: 「[article_title](article_url)」{reaction}
        The web viewer's linkify() converts [text](url) → <a href="url">text</a>.
        """
        # Strip any URL accidentally included by LLM
        clean = re.sub(r"https?://\S+", "", str(reaction or ""), flags=re.IGNORECASE).strip()
        # Strip accidental leading 「 or 『 the LLM might prepend
        clean = re.sub(r"^[「『]", "", clean).strip()

        title = str(title or "").strip()
        url = str(url or "").strip()

        if not title or not url:
            return clean

        linked = f"[{title}]({url})"
        if clean:
            return f"「{linked}」{clean}"
        return f"「{linked}」"

    async def _load_current_places(
        self, char_id: str, current_place: str
    ) -> dict[str, str]:
        """runtime snapshot を優先して各キャラの現在地 snapshot を組み立てる。"""
        return await self._build_place_snapshot({char_id: current_place})

    def _get_same_place_chars(
        self,
        char_id: str,
        current_place: str,
        current_places: dict[str, str] | None = None,
    ) -> list[str]:
        """in-memory トラッキングを使い、同一場所の他キャラ ID リストを返す。

        Phase 2 では前ターンの場所を近似値として使用。Phase 3 で DB クエリに改善予定。
        """
        place_map = current_places or {
            **self._last_known_place,
            **self._runtime_place_snapshot,
        }
        return [
            cid for cid, place in place_map.items()
            if cid != char_id and place == current_place
        ]

    async def _build_place_snapshot(
        self,
        seed_places: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """現在プロセスで有効な各キャラの場所 snapshot を組み立てる。"""
        current_places = dict(self._last_known_place)
        current_places.update(self._runtime_place_snapshot)
        if seed_places is not None:
            current_places.update(seed_places)

        for character in self._characters:
            other_id = character["id"]
            if other_id in current_places:
                continue
            latest_state = await self.db.get_latest_character_state(
                self.story_id, other_id
            )
            if latest_state is None:
                latest_state = self._make_initial_state(character)
            current_places[other_id] = latest_state["current_place"]

        return current_places

    def _character_name(self, char_id: str) -> str:
        """キャラクター ID を表示名へ変換する。"""
        return next(
            (char["name_ja"] for char in self._characters if char["id"] == char_id),
            char_id,
        )

    def _prompt_char_name(self, char_id: str) -> str:
        """prompt 補助文向けに、未知 ID でも読みやすい表示名を返す。"""
        resolved = self._character_name(char_id)
        if resolved != char_id:
            return resolved
        if char_id.startswith("char_"):
            suffix = char_id.removeprefix("char_")
            if suffix.isdigit():
                return f"キャラ{suffix}"
        return resolved

    def _ordered_place_participants(
        self, current_places: dict[str, str], place_id: str
    ) -> list[str]:
        """place_id にいる参加者をキャラ定義順で返す。"""
        return [
            char["id"]
            for char in self._characters
            if current_places.get(char["id"]) == place_id
        ]

    def _scene_path_enabled(self) -> bool:
        if self._config is None:
            return False
        return (
            self._config.scene_management.enabled
            and self._config.participation_planner.enabled
        )

    async def _collect_scene_signal_state(
        self,
        char_id: str,
        *,
        active_scenes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if active_scenes is None:
            raw_active_scenes = await self.db.get_active_story_scenes(self.story_id)
            active_scenes = raw_active_scenes if isinstance(raw_active_scenes, list) else []
        active_patterns = await self.db.get_active_interaction_patterns(self.story_id)
        active_relationship_modes = await self.db.get_active_relationship_modes(self.story_id)
        active_canon_bits = await self.db.get_active_story_canon_bits(self.story_id)
        active_pressures = await self.db.get_active_story_dramatic_pressures(self.story_id)
        raw_active_episode = (
            await self._episode_planner.get_active_episode()
            if self._episode_planner is not None
            else None
        )
        active_episode = raw_active_episode if isinstance(raw_active_episode, dict) else None
        return {
            "active_scenes": active_scenes,
            "active_patterns": active_patterns if isinstance(active_patterns, list) else [],
            "active_relationship_modes": (
                active_relationship_modes if isinstance(active_relationship_modes, list) else []
            ),
            "active_canon_bits": active_canon_bits if isinstance(active_canon_bits, list) else [],
            "active_pressures": active_pressures if isinstance(active_pressures, list) else [],
            "active_episode": active_episode,
            "counterpart_ids": await self._collect_social_counterpart_ids(char_id),
            "relationship_mode_counterparts": await self._collect_relationship_mode_counterparts(char_id),
        }

    def _score_place_scene_opportunity(
        self,
        *,
        char_id: str,
        place_id: str,
        occupants: list[str],
        signal_state: dict[str, Any],
    ) -> tuple[int, float, float, float]:
        active_conversation_places = {
            str(scene.get("place_id"))
            for scene in signal_state.get("active_scenes", [])
            if scene.get("scene_type") == "conversation" and scene.get("status") == "active"
        }
        active_episode = signal_state.get("active_episode")
        episode_focus_chars = {
            str(counterpart_id)
            for counterpart_id in list(active_episode.get("focus_char_ids") or [])
        } if isinstance(active_episode, dict) else set()
        episode_focus_place = (
            str(active_episode.get("focus_place_id") or "")
            if isinstance(active_episode, dict)
            else ""
        )
        counterpart_ids = set(signal_state.get("counterpart_ids", set()))
        relationship_mode_counterparts = dict(signal_state.get("relationship_mode_counterparts", {}))
        active_canon_bits = list(signal_state.get("active_canon_bits", []))
        active_pressures = list(signal_state.get("active_pressures", []))
        active_patterns = list(signal_state.get("active_patterns", []))

        score = 0
        pressure_bonus = 0.0
        mode_bonus = 0.0
        canon_bonus = 0.0
        if place_id in active_conversation_places:
            score += 4
        if len(occupants) >= 2:
            score += 2
        if counterpart_ids and any(occupant in counterpart_ids for occupant in occupants):
            score += 1
        if episode_focus_chars and any(occupant in episode_focus_chars for occupant in occupants):
            score += 2
        if episode_focus_place and place_id == episode_focus_place:
            score += 3
        if relationship_mode_counterparts:
            mode_bonus = max(
                (
                    float(relationship_mode_counterparts.get(occupant) or 0.0)
                    for occupant in occupants
                ),
                default=0.0,
            )
        for pressure in active_pressures:
            focus_char_ids = {
                str(focus_char_id).strip()
                for focus_char_id in list(pressure.get("focus_char_ids") or [])
                if str(focus_char_id).strip()
            }
            if str(pressure.get("focus_place_id") or "") == place_id:
                score += 4
                pressure_bonus = max(pressure_bonus, float(pressure.get("score") or 0.0))
            if focus_char_ids and any(occupant in focus_char_ids for occupant in occupants):
                score += 2
                pressure_bonus = max(pressure_bonus, float(pressure.get("score") or 0.0))
            if char_id in focus_char_ids:
                pressure_bonus = max(pressure_bonus, float(pressure.get("score") or 0.0))
        for pattern in active_patterns:
            involved_chars = {
                str(counterpart_id).strip()
                for counterpart_id in list(pattern.get("involved_chars") or [])
                if str(counterpart_id).strip()
            }
            if involved_chars and any(occupant in involved_chars for occupant in occupants):
                score += 1
        for bit in active_canon_bits:
            if str(bit.get("focus_place_id") or "") == place_id:
                score += 2
                canon_bonus = max(
                    canon_bonus,
                    self._canon_level_weight(str(bit.get("canon_level") or "")) + 0.05,
                )
            bit_chars = {
                str(counterpart_id).strip()
                for counterpart_id in list(bit.get("focus_char_ids") or [])
                if str(counterpart_id).strip()
            }
            if bit_chars and any(occupant in bit_chars for occupant in occupants):
                score += 1
                canon_bonus = max(
                    canon_bonus,
                    self._canon_level_weight(str(bit.get("canon_level") or "")),
                )
        return score, pressure_bonus, mode_bonus, canon_bonus

    async def _current_place_pull_strength(
        self,
        *,
        char_id: str,
        current_place: str,
        current_places: dict[str, str],
    ) -> float:
        signal_state = await self._collect_scene_signal_state(char_id)
        occupants = self._ordered_place_participants(current_places, current_place)
        score, pressure_bonus, mode_bonus, canon_bonus = self._score_place_scene_opportunity(
            char_id=char_id,
            place_id=current_place,
            occupants=occupants,
            signal_state=signal_state,
        )
        return min(1.0, (score / 8.0) + pressure_bonus + mode_bonus + canon_bonus)

    def _objective_from_signal_type(self, signal_type: str) -> str:
        return {
            "misunderstanding": "誰かの認識のズレが明らかになりそうだ",
            "near_reveal": "誰かが隠していたことが見えてきた",
            "status_clash": "二人の思いがぶつかりそうだ",
            "status_flashpoint": "二人の思いが今ぶつかっている",
            "showoff_flashpoint": "誰かが本気を出す瞬間が来た",
            "role_reversal": "場の流れが変わろうとしている",
            "role_reversal_ready": "場の流れが変わりそうだ",
            "small_win_loss": "小さなことで何かが決まろうとしている",
            "chaos_partner": "場がざわついている",
            "bluff_or_showoff": "誰かが何かを見せようとしている",
            "payoff_ready": "これまでのことが今ここで形になる",
            "stall_risk": "何か言わないと場が止まりそうだ",
        }.get(signal_type, "自然発生の会話")

    def _select_scene_focus_and_objective(
        self,
        *,
        place_id: str,
        participants: list[str],
        signal_state: dict[str, Any],
    ) -> tuple[list[str], str]:
        best_score = -1.0
        best_focus_char_ids = participants[:1]
        best_objective = "自然発生の会話"

        for pressure in list(signal_state.get("active_pressures", [])):
            focus_char_ids = [
                participant
                for participant in participants
                if participant in list(pressure.get("focus_char_ids") or [])
            ]
            place_match = str(pressure.get("focus_place_id") or "") == place_id
            overlap = len(focus_char_ids)
            if not place_match and overlap == 0:
                continue
            score = float(pressure.get("score") or 0.0) + (0.2 if place_match else 0.0) + (0.1 * overlap)
            if score > best_score:
                best_score = score
                best_focus_char_ids = focus_char_ids[:2] or participants[:1]
                best_objective = self._objective_from_signal_type(str(pressure.get("pressure_type") or ""))

        for pattern in list(signal_state.get("active_patterns", [])):
            involved = [
                participant
                for participant in participants
                if participant in list(pattern.get("involved_chars") or [])
            ]
            if len(involved) < 2:
                continue
            score = 0.55 + (0.05 * float(pattern.get("intensity") or 0.0))
            if score > best_score:
                best_score = score
                best_focus_char_ids = involved[:2]
                best_objective = self._objective_from_signal_type(str(pattern.get("pattern_type") or ""))

        for mode in list(signal_state.get("active_relationship_modes", [])):
            pair = [
                participant
                for participant in participants
                if participant in {
                    str(mode.get("char_id_from") or ""),
                    str(mode.get("char_id_to") or ""),
                }
            ]
            if len(pair) < 2:
                continue
            score = 0.45 + (0.10 * float(mode.get("intensity") or 0.0))
            if score > best_score:
                best_score = score
                best_focus_char_ids = pair[:2]
                best_objective = self._objective_from_signal_type(str(mode.get("mode_type") or ""))

        for bit in list(signal_state.get("active_canon_bits", [])):
            bit_focus = [
                participant
                for participant in participants
                if participant in list(bit.get("focus_char_ids") or [])
            ]
            place_match = str(bit.get("focus_place_id") or "") == place_id
            if len(bit_focus) < 2 and not place_match:
                continue
            score = self._canon_level_weight(str(bit.get("canon_level") or "")) + (0.20 if place_match else 0.0)
            if score > best_score:
                best_score = score
                best_focus_char_ids = bit_focus[:2] or participants[:1]
                best_objective = self._objective_from_signal_type(str(bit.get("motif_key") or ""))

        active_episode = signal_state.get("active_episode")
        if isinstance(active_episode, dict):
            episode_focus = [
                participant
                for participant in participants
                if participant in list(active_episode.get("focus_char_ids") or [])
            ]
            place_match = str(active_episode.get("focus_place_id") or "") == place_id
            if episode_focus or place_match:
                score = 0.50 + (0.15 if place_match else 0.0)
                if score > best_score:
                    best_focus_char_ids = episode_focus[:2] or participants[:1]
                    best_objective = self._objective_from_signal_type(
                        str(active_episode.get("episode_type") or "")
                    )

        return best_focus_char_ids[:2], best_objective

    def _get_character_display_name(self, char_id: str) -> str:
        for character in self._characters:
            if str(character.get("id") or "") == char_id:
                return str(character.get("name_ja") or char_id)
        return char_id

    @staticmethod
    def _compact_scene_message_excerpt(text: str, *, limit: int = 18) -> str:
        cleaned = " ".join(str(text or "").split())
        return cleaned[:limit].rstrip()

    def _build_conversation_scene_close_summary(
        self,
        *,
        scene: dict[str, Any],
        outcome_type: str,
        scene_logs: list[dict[str, Any]],
        active_participants: list[str],
    ) -> str:
        objective = str(scene.get("objective") or "").strip()
        focus_char_ids = [
            str(char_id)
            for char_id in list(scene.get("focus_char_ids") or [])
            if str(char_id).strip()
        ]
        if not focus_char_ids:
            focus_char_ids = active_participants[:2]
        focus_names = [self._get_character_display_name(char_id) for char_id in focus_char_ids[:2]]
        focus_label = "と".join(focus_names)
        recent_messages = [
            self._compact_scene_message_excerpt(str(log.get("message") or ""))
            for log in scene_logs[-2:]
            if str(log.get("message") or "").strip()
        ]
        recent_fragment = " / ".join(fragment for fragment in recent_messages if fragment)
        if objective == "自然発生の会話":
            objective = ""

        if outcome_type == "participants_dispersed":
            if objective and focus_label:
                return f"{focus_label}の{objective}だけが残ったまま、その場は途切れた。"
            if recent_fragment:
                return f"「{recent_fragment}」という言葉だけが残ったまま、その場は途切れた。"
            return "参加者が分散したため場面を終了した。"

        if outcome_type == "objective_saturated":
            if objective and focus_label:
                return f"{focus_label}の{objective}がひとまず表に出た。"
            if recent_fragment:
                return f"「{recent_fragment}」と応酬したところで、この場の争点が見えた。"
            return "この場の争点がひとまず表に出た。"

        if outcome_type == "stalled_conversation":
            if objective and recent_fragment:
                return f"{objective}は決めきれず、「{recent_fragment}」だけを残して持ち越された。"
            if objective:
                return f"{objective}は決めきれないまま、この場から持ち越された。"
            if recent_fragment:
                return f"「{recent_fragment}」という保留だけを残して、この場はいったん切れた。"
            return "結論は薄いまま、この場の流れだけが次へ持ち越された。"

        return "場面の流れが次へ持ち越された。"

    async def _close_story_scene_and_queue(
        self,
        *,
        scene_id: int,
        outcome_type: str,
        outcome_summary: str,
    ) -> None:
        assert self.world_clock is not None
        await self.db.close_story_scene(
            scene_id,
            outcome_type=outcome_type,
            outcome_summary=outcome_summary,
            closed_turn=self.world_clock.turn_number,
        )
        if scene_id not in self._closed_story_scene_ids:
            self._closed_story_scene_ids.append(int(scene_id))

    async def _determine_conversation_scene_close(
        self,
        *,
        scene: dict[str, Any],
        participants: list[str],
    ) -> tuple[str, str] | None:
        scene_id = int(scene["id"])
        scene_logs = await self.db.get_scene_logs(scene_id, limit=_SCENE_CLOSE_LOG_LIMIT)
        if len(participants) < 2:
            return (
                "participants_dispersed",
                self._build_conversation_scene_close_summary(
                    scene=scene,
                    outcome_type="participants_dispersed",
                    scene_logs=scene_logs,
                    active_participants=participants,
                ),
            )

        focus_char_ids = [
            str(char_id)
            for char_id in list(scene.get("focus_char_ids") or [])
            if str(char_id).strip()
        ]
        if not focus_char_ids:
            focus_char_ids = participants[:2]
        focus_set = set(focus_char_ids[:2])
        focus_hits = {
            str(log.get("char_id") or "")
            for log in scene_logs
            if str(log.get("char_id") or "") in focus_set
        }
        objective = str(scene.get("objective") or "").strip()

        if (
            objective
            and objective != "自然発生の会話"
            and len(scene_logs) >= _OBJECTIVE_SATURATED_MIN_LOGS
            and len(focus_hits) >= min(2, len(focus_set))
        ):
            return (
                "objective_saturated",
                self._build_conversation_scene_close_summary(
                    scene=scene,
                    outcome_type="objective_saturated",
                    scene_logs=scene_logs,
                    active_participants=participants,
                ),
            )

        opened_turn = int(scene.get("opened_turn") or 0)
        assert self.world_clock is not None
        scene_age = max(0, self.world_clock.turn_number - opened_turn)
        if (
            scene_age >= _STALLED_CONVERSATION_MIN_AGE
            and len(scene_logs) >= 2
            and len(focus_hits) >= min(2, len(focus_set))
        ):
            return (
                "stalled_conversation",
                self._build_conversation_scene_close_summary(
                    scene=scene,
                    outcome_type="stalled_conversation",
                    scene_logs=scene_logs,
                    active_participants=participants,
                ),
            )

        return None

    async def _sync_story_scenes(
        self, current_places: dict[str, str]
    ) -> list[dict[str, Any]]:
        """現在の場所 snapshot に合わせて active story scenes を更新する。"""
        assert self.world_clock is not None
        active_scenes = await self.db.get_active_story_scenes(self.story_id)
        active_by_place = {scene["place_id"]: scene for scene in active_scenes if scene.get("place_id")}

        for scene in list(active_scenes):
            place_id = scene.get("place_id")
            participants = self._ordered_place_participants(current_places, place_id) if place_id else []
            if scene["scene_type"] == "conversation":
                close_result = await self._determine_conversation_scene_close(
                    scene=scene,
                    participants=participants,
                )
                if close_result is not None:
                    outcome_type, outcome_summary = close_result
                    await self._close_story_scene_and_queue(
                        scene_id=int(scene["id"]),
                        outcome_type=outcome_type,
                        outcome_summary=outcome_summary,
                    )
                    active_by_place.pop(place_id, None)
            elif scene["scene_type"] == "solo" and len(participants) != 1:
                await self._close_story_scene_and_queue(
                    scene_id=int(scene["id"]),
                    outcome_type="solo_interrupted",
                    outcome_summary="単独場面の条件が崩れたため終了した。",
                )
                active_by_place.pop(place_id, None)

        refreshed = await self.db.get_active_story_scenes(self.story_id)
        active_by_place = {scene["place_id"]: scene for scene in refreshed if scene.get("place_id")}
        solo_created = 0
        scene_signal_state = await self._collect_scene_signal_state(
            "",
            active_scenes=refreshed,
        )

        for place_id in dict.fromkeys(current_places.values()):
            participants = self._ordered_place_participants(current_places, place_id)
            scene = active_by_place.get(place_id)
            if len(participants) >= 2:
                focus_char_ids, objective = self._select_scene_focus_and_objective(
                    place_id=place_id,
                    participants=participants,
                    signal_state=scene_signal_state,
                )
                if scene is None:
                    scene_id = await self.db.insert_story_scene(
                        self.story_id,
                        {
                            "scene_type": "conversation",
                            "status": "active",
                            "place_id": place_id,
                            "focus_char_ids": focus_char_ids,
                            "objective": objective,
                            "opened_turn": self.world_clock.turn_number,
                        },
                    )
                    scene = {
                        "id": scene_id,
                        "scene_type": "conversation",
                        "status": "active",
                        "place_id": place_id,
                    }
                    active_by_place[place_id] = scene
                elif objective != "自然発生の会話" and (
                    str(scene.get("objective") or "") in {"", "自然発生の会話"}
                    or list(scene.get("focus_char_ids") or []) != focus_char_ids
                ):
                    await self.db.update_story_scene(
                        int(scene["id"]),
                        {
                            "focus_char_ids": focus_char_ids,
                            "objective": objective,
                        },
                    )
                await self.db.replace_scene_participants(
                    scene["id"],
                    [
                        {
                            "char_id": char_id,
                            "role": "observer",
                            "join_turn": self.world_clock.turn_number,
                        }
                        for char_id in participants
                    ],
                )
            elif len(participants) == 1 and scene is None and solo_created < 1:
                char_id = participants[0]
                if await self._should_open_solo_scene(char_id):
                    scene_id = await self.db.insert_story_scene(
                        self.story_id,
                        {
                            "scene_type": "solo",
                            "status": "active",
                            "place_id": place_id,
                            "focus_char_ids": [char_id],
                            "objective": "単独の心情場面",
                            "opened_turn": self.world_clock.turn_number,
                        },
                    )
                    await self.db.replace_scene_participants(
                        scene_id,
                        [
                            {
                                "char_id": char_id,
                                "role": "focus",
                                "join_turn": self.world_clock.turn_number,
                                "speak_budget": 1,
                            }
                        ],
                    )
                    solo_created += 1

        return await self.db.get_active_story_scenes(self.story_id)

    async def _should_open_solo_scene(self, char_id: str) -> bool:
        """rare solo scene の条件を判定する。"""
        assert self.world_clock is not None
        state = await self.db.get_latest_character_state(self.story_id, char_id)
        if state is None:
            char = next(character for character in self._characters if character["id"] == char_id)
            state = self._make_initial_state(char)
        if state.get("current_action") == "monologue" and state.get("turn_number") == self.world_clock.turn_number - 1:
            return False
        stress = float(state["stress"])
        loneliness = float(state["loneliness"])
        motivation = float(state["motivation"])
        emotion_gate = stress >= 0.50 or loneliness >= 0.50 or motivation >= 0.75
        if not emotion_gate:
            return False
        last_turn = int(state.get("turn_number") or self.world_clock.turn_number)
        alone_turn_gap = max(0, self.world_clock.turn_number - last_turn)
        active_tensions = await self.db.get_active_tensions(self.story_id)
        relevant_tension = any(
            char_id in list(tension.get("involved_chars") or [])
            for tension in active_tensions
        )
        relevant_hooks = await self.db.get_relevant_story_hooks(self.story_id, char_id, "", limit=1)
        if not relevant_tension and not relevant_hooks:
            current_place = str(state.get("current_place") or "")
            if current_place and self._place_manager is not None:
                current_places = await self._load_current_places(char_id, current_place)
                social_target_place = await self._select_social_target_place(
                    char_id=char_id,
                    current_place=current_place,
                    current_places=current_places,
                )
                if social_target_place is not None:
                    signal_state = await self._collect_scene_signal_state(char_id)
                    occupants = self._ordered_place_participants(current_places, social_target_place)
                    score, pressure_bonus, mode_bonus, canon_bonus = self._score_place_scene_opportunity(
                        char_id=char_id,
                        place_id=social_target_place,
                        occupants=occupants,
                        signal_state=signal_state,
                    )
                    if (score / 8.0) + pressure_bonus + mode_bonus + canon_bonus >= 0.7:
                        return False
        if alone_turn_gap >= 2:
            return True
        return bool(relevant_tension or relevant_hooks)

    async def _select_social_target_place(
        self,
        *,
        char_id: str,
        current_place: str,
        current_places: dict[str, str],
    ) -> str | None:
        assert self._place_manager is not None
        adjacent_places = self._place_manager.get_adjacent_places(current_place)
        if not adjacent_places:
            return None

        signal_state = await self._collect_scene_signal_state(char_id)
        best_place: str | None = None
        best_score: tuple[int, float, float, float, int, str] | None = None
        for place_id in adjacent_places:
            occupants = self._ordered_place_participants(current_places, place_id)
            if not occupants:
                continue
            score, pressure_bonus, mode_bonus, canon_bonus = self._score_place_scene_opportunity(
                char_id=char_id,
                place_id=place_id,
                occupants=occupants,
                signal_state=signal_state,
            )
            ranking = (score, pressure_bonus, mode_bonus, canon_bonus, len(occupants), place_id)
            if best_score is None or ranking > best_score:
                best_score = ranking
                best_place = place_id
        return (
            best_place
            if best_score is not None and (
                best_score[0] > 0
                or best_score[1] > 0.0
                or best_score[2] > 0.0
                or best_score[3] > 0.0
            )
            else None
        )

    async def _collect_relationship_mode_counterparts(self, char_id: str) -> dict[str, float]:
        counterparts: dict[str, float] = {}
        for mode in await self.db.get_active_relationship_modes(self.story_id):
            if str(mode.get("char_id_from") or "") != char_id:
                continue
            target_char_id = str(mode.get("char_id_to") or "").strip()
            if not target_char_id:
                continue
            counterparts[target_char_id] = max(
                counterparts.get(target_char_id, 0.0),
                float(mode.get("intensity") or 0.0),
            )
        return counterparts

    @staticmethod
    def _canon_level_weight(level: str) -> float:
        return {
            "momentary_bit": 0.05,
            "recurring_bit": 0.10,
            "proto_canon": 0.15,
            "canon": 0.20,
        }.get(level, 0.0)

    async def _select_relationship_mode_prompt_texts(
        self,
        *,
        char_id: str,
        target_char_id: str | None,
        same_place_char_ids: list[str],
    ) -> list[str]:
        same_place_set = set(same_place_char_ids)
        ranked_texts: list[tuple[tuple[int, int, float, float], str]] = []
        for mode in await self.db.get_active_relationship_modes(self.story_id):
            if str(mode.get("char_id_from") or "") != char_id:
                continue
            counterpart_id = str(mode.get("char_id_to") or "").strip()
            if not counterpart_id:
                continue
            text = self._format_relationship_mode_prompt_text(mode)
            if text is None:
                continue
            priority = (
                1 if target_char_id is not None and counterpart_id == target_char_id else 0,
                1 if counterpart_id in same_place_set else 0,
                float(mode.get("intensity") or 0.0),
                float(mode.get("confidence") or 0.0),
            )
            ranked_texts.append((priority, text))
        ranked_texts.sort(key=lambda item: item[0], reverse=True)
        return [text for _, text in ranked_texts[:2]]

    def _format_relationship_mode_prompt_text(self, mode: dict[str, Any]) -> str | None:
        counterpart_id = str(mode.get("char_id_to") or "").strip()
        if not counterpart_id:
            return None
        counterpart_name = self._prompt_char_name(counterpart_id)
        templates = {
            "irritated_respect": f"{counterpart_name} には認めつつも張り合いがち。",
            "unsafe_confidant": f"{counterpart_name} には危うい本音を漏らしやすい。",
            "chaos_partner": f"{counterpart_name} と組むと悪ノリしやすい。",
            "cannot_ignore": f"{counterpart_name} の動きは放っておけない。",
        }
        return templates.get(str(mode.get("mode_type") or "").strip())

    async def _select_canon_bit_prompt_texts(
        self,
        *,
        char_id: str,
        target_char_id: str | None,
        current_place_id: str,
    ) -> list[str]:
        ranked_texts: list[tuple[tuple[int, int, int, float, float], str]] = []
        for bit in await self.db.get_active_story_canon_bits(self.story_id):
            focus_char_ids = {
                str(focus_char_id).strip()
                for focus_char_id in list(bit.get("focus_char_ids") or [])
                if str(focus_char_id).strip()
            }
            text = self._format_canon_bit_prompt_text(
                bit,
                char_id=char_id,
                target_char_id=target_char_id,
                current_place_id=current_place_id,
            )
            if text is None:
                continue
            priority = (
                1 if char_id in focus_char_ids else 0,
                1 if target_char_id is not None and target_char_id in focus_char_ids else 0,
                1 if str(bit.get("focus_place_id") or "") == current_place_id else 0,
                self._canon_level_weight(str(bit.get("canon_level") or "")),
                float(bit.get("intent_alignment") or 0.0),
            )
            ranked_texts.append((priority, text))
        ranked_texts.sort(key=lambda item: item[0], reverse=True)
        return [text for _, text in ranked_texts[:2]]

    def _format_canon_bit_prompt_text(
        self,
        bit: dict[str, Any],
        *,
        char_id: str,
        target_char_id: str | None,
        current_place_id: str,
    ) -> str | None:
        motif_key = str(bit.get("motif_key") or "").strip()
        if not motif_key:
            return None
        bit_type = str(bit.get("bit_type") or "").strip()
        focus_char_ids = [
            str(focus_char_id).strip()
            for focus_char_id in list(bit.get("focus_char_ids") or [])
            if str(focus_char_id).strip()
        ]
        focus_place_id = str(bit.get("focus_place_id") or "").strip()
        if bit_type == "pair_dynamic":
            if target_char_id is not None and target_char_id in focus_char_ids:
                names = f"{self._prompt_char_name(char_id)}と{self._prompt_char_name(target_char_id)}"
            else:
                names = "と".join(self._prompt_char_name(cid) for cid in focus_char_ids[:2]) or "この組み合わせ"
            return f"{names} は {motif_key} に戻りやすい。"
        if bit_type == "character_tendency":
            return f"自分は {motif_key} の流れを繰り返しやすい。"
        if bit_type == "group_routine":
            names = "・".join(self._prompt_char_name(cid) for cid in focus_char_ids[:3]) or "この組み合わせ"
            return f"{names} が揃うと {motif_key} が起きやすい。"
        if bit_type == "place_motif":
            place_id = focus_place_id or current_place_id
            place_name = self._places.get(place_id, place_id)
            return f"{place_name} では {motif_key} が起きやすい。"
        return None

    async def _select_dramatic_pressure_prompt_texts(
        self,
        *,
        char_id: str,
        target_char_id: str | None,
        current_place_id: str,
        same_place_char_ids: list[str],
        msg_type: str,
    ) -> list[str]:
        max_items = 1 if msg_type == "monologue" else 2
        same_place_set = set(same_place_char_ids)
        ranked_texts: list[tuple[tuple[int, int, int, int, float], str]] = []
        for pressure in await self.db.get_active_story_dramatic_pressures(self.story_id):
            focus_char_ids = {
                str(focus_char_id).strip()
                for focus_char_id in list(pressure.get("focus_char_ids") or [])
                if str(focus_char_id).strip()
            }
            if msg_type == "monologue" and target_char_id is None:
                if not (
                    char_id in focus_char_ids
                    or str(pressure.get("focus_place_id") or "") == current_place_id
                ):
                    continue
            text = self._format_dramatic_pressure_prompt_text(pressure)
            if text is None:
                continue
            priority = (
                1 if target_char_id is not None and target_char_id in focus_char_ids else 0,
                1 if char_id in focus_char_ids else 0,
                1 if same_place_set and bool(focus_char_ids & same_place_set) else 0,
                1 if str(pressure.get("focus_place_id") or "") == current_place_id else 0,
                float(pressure.get("score") or 0.0),
            )
            ranked_texts.append((priority, text))
        ranked_texts.sort(key=lambda item: item[0], reverse=True)
        return [text for _, text in ranked_texts[:max_items]]

    @staticmethod
    def _format_dramatic_pressure_prompt_text(pressure: dict[str, Any]) -> str | None:
        templates = {
            "status_flashpoint": "誰かが何かを言えば、場が動くかもしれない。",
            "showoff_flashpoint": "誰かが本気を出す瞬間が来ている。",
            "near_reveal": "誰かが言いそうで言わないことが空気の中にある。",
            "role_reversal_ready": "場の流れが変わりそうな瞬間がある。",
            "payoff_ready": "積み重なってきたものが形になりそうな場面だ。",
            "stall_risk": "このまま止まると、何かを失いそうな気がする。",
        }
        return templates.get(str(pressure.get("pressure_type") or "").strip())

    @staticmethod
    def _describe_reply_move_mode(mode: str) -> str:
        return {
            "counter": "反論か差し返し",
            "condition": "条件を示す返し",
            "probe": "問い返し",
            "claim": "立場を言い切る返し",
            "press": "一歩踏み込む返し",
            "redirect": "向きを変える返し",
        }.get(str(mode or "").strip(), "別の具体的な反応")

    @staticmethod
    def _describe_reply_shape(shape: str) -> str:
        return {
            "answer_then_counter": "答えてから押し返す形",
            "answer_then_condition": "答えてから条件を示す形",
            "answer_then_probe": "答えてから問い返す形",
            "answer_then_press": "答えてから一歩踏み込む形",
            "answer_then_redirect": "答えてから話の向きを変える形",
        }.get(str(shape or "").strip(), "答えてから具体的に返す形")

    @staticmethod
    def _describe_story_flavor_role(role: str) -> str:
        return {
            "pressure": "場で気になっていること",
            "stakes": "失いたくないもの",
            "positioning": "自分の立場",
        }.get(str(role or "").strip(), "")

    async def _collect_social_counterpart_ids(self, char_id: str) -> set[str]:
        counterpart_ids: set[str] = set()
        for hook in await self.db.get_relevant_story_hooks(self.story_id, char_id, "", limit=5):
            target_char_id = hook.get("target_char_id")
            if isinstance(target_char_id, str) and target_char_id != char_id:
                counterpart_ids.add(target_char_id)
        for tension in await self.db.get_active_tensions(self.story_id):
            involved = list(tension.get("involved_chars") or [])
            if char_id not in involved:
                continue
            for counterpart_id in involved:
                if counterpart_id != char_id:
                    counterpart_ids.add(str(counterpart_id))
        return counterpart_ids

    async def _build_round_turn_plan(
        self, current_places: dict[str, str]
    ) -> list[dict[str, Any]]:
        """scene path 用に、今ラウンドの speaker queue を構築する。"""
        scenes = await self._sync_story_scenes(current_places)
        raw_active_episode = (
            await self._episode_planner.get_active_episode()
            if self._episode_planner is not None
            else None
        )
        active_episode = raw_active_episode if isinstance(raw_active_episode, dict) else None
        episode_focus_char_ids = {
            str(char_id)
            for char_id in list(active_episode.get("focus_char_ids") or [])
        } if active_episode is not None else set()
        plan: list[dict[str, Any]] = []
        planned_ids: set[str] = set()
        for scene in scenes:
            participants = await self.db.get_scene_participants(scene["id"])
            selectable = [p for p in participants if p.get("role") != "observer"]
            if not selectable:
                selectable = participants
            ordered = sorted(
                selectable,
                key=lambda row: (
                    row.get("char_id") not in episode_focus_char_ids,
                    row.get("last_spoken_turn") is not None,
                    row.get("last_spoken_turn") if row.get("last_spoken_turn") is not None else -1,
                    row.get("char_id", ""),
                ),
            )
            if scene["scene_type"] == "solo":
                if ordered:
                    plan.append(
                        {
                            "char_id": ordered[0]["char_id"],
                            "scene_id": scene["id"],
                            "speaker_intent": "solo",
                        }
                    )
                    planned_ids.add(ordered[0]["char_id"])
                continue

            limited = ordered[:2]
            limited_ids = {participant["char_id"] for participant in limited}
            if len(ordered) > 2:
                open_hooks = await self.db.get_open_story_hooks_for_scene(
                    self.story_id,
                    scene["id"],
                )
                for hook in open_hooks:
                    target_char_id = hook.get("target_char_id")
                    if not isinstance(target_char_id, str):
                        continue
                    if target_char_id in limited_ids:
                        continue
                    extra_participant = next(
                        (
                            participant
                            for participant in ordered[2:]
                            if participant["char_id"] == target_char_id
                        ),
                        None,
                    )
                    if extra_participant is not None:
                        limited.append(extra_participant)
                        limited_ids.add(extra_participant["char_id"])
                        break
            for index, participant in enumerate(limited):
                plan.append(
                    {
                        "char_id": participant["char_id"],
                        "scene_id": scene["id"],
                        "speaker_intent": "advance" if index == 0 else "react",
                    }
                )
                planned_ids.add(participant["char_id"])
        if plan:
            plan.extend(await self._build_starvation_rescue_turns(planned_ids))
        return plan

    async def _build_starvation_rescue_turns(
        self,
        planned_ids: set[str],
    ) -> list[dict[str, Any]]:
        """scene から漏れた stale キャラを fallback turn として末尾へ足す。"""
        assert self.world_clock is not None
        stale_candidates: list[tuple[int, int, str]] = []
        for index, character in enumerate(self._characters):
            char_id = str(character["id"])
            if char_id in planned_ids:
                continue
            latest_state = await self.db.get_latest_character_state(self.story_id, char_id)
            if latest_state is None:
                turn_gap = _SCENE_TURN_STARVATION_GAP
            else:
                last_turn_raw = latest_state.get("turn_number")
                last_turn = (
                    int(last_turn_raw)
                    if last_turn_raw is not None
                    else self.world_clock.turn_number
                )
                turn_gap = self.world_clock.turn_number - last_turn
            if turn_gap >= _SCENE_TURN_STARVATION_GAP:
                stale_candidates.append((turn_gap, index, char_id))
        stale_candidates.sort(key=lambda item: (-item[0], item[1]))
        return [
            {"char_id": char_id, "scene_id": None, "speaker_intent": "catch_up"}
            for _gap, _index, char_id in stale_candidates
        ]

    def _format_conversation_lines(
        self, logs: list[dict[str, Any]]
    ) -> list[str]:
        """直近会話ログを prompt 用の1行文字列へ整形する。"""
        return [
            f"{self._character_name(log['char_id'])}: {log['message']}"
            for log in logs
            if log.get("message")
        ]

    def _extract_inner_thought_text(self, response: Any | None) -> str:
        """内的モノローグ LLM 応答から（）内心テキストを抽出して返す。

        失敗・空の場合は空文字を返す。発話生成は inner_thought 無しで続行される。
        """
        if response is None:
            return ""
        text = (response.final_text or "").strip()
        # （）でくくられた部分を優先して抽出
        match = _RE_INNER_THOUGHT.search(text)
        if match:
            return f"（{match.group(1)}）"
        # くくられていない場合は先頭 60 字を括弧で包んで返す
        if text:
            return f"（{text[:60]}）"
        return ""

    async def _build_place_dialogue_lines(
        self, char_id: str, current_place: str
    ) -> list[str]:
        """自キャラが current_place に入場してから現在までの発話ログを返す。

        character_states から入場 turn を特定し、chat_logs から同 place の発話を取得。
        """
        if not current_place:
            return []
        entry_turn = await self.db.get_character_entry_turn(
            self.story_id, char_id, current_place
        ) or 1
        limit = (
            self._config.participation_planner.max_place_dialogue_lines
            if self._config is not None
            else 30
        )
        logs = await self.db.get_place_dialogue_since_turn(
            self.story_id, current_place, entry_turn, limit=limit
        )
        return self._format_conversation_lines(logs)

    def _find_reply_target(
        self, char_id: str, logs: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """直近ログから返答先に使う他キャラ発言を返す。"""
        for log in reversed(logs):
            if log.get("char_id") != char_id:
                return log
        return None

    def _fallback_reply_target_id(
        self,
        *,
        char_id: str,
        participant_ids: list[str],
        scheduled_target: str | list[str] | None,
    ) -> str | None:
        """直近ログが無いときの reply fallback 先を返す。"""
        if isinstance(scheduled_target, str) and scheduled_target != char_id:
            return scheduled_target
        if isinstance(scheduled_target, list):
            for target_id in scheduled_target:
                if target_id != char_id:
                    return target_id
        for participant_id in participant_ids:
            if participant_id != char_id:
                return participant_id
        return None

    def _resolve_group_hook_target(
        self,
        *,
        char_id: str,
        participant_ids: list[str],
        message: str,
    ) -> str | None:
        """group 発話内で明示的に名指しされた相手が1人だけなら返す。"""
        candidates: list[str] = []
        for participant_id in participant_ids:
            if participant_id == char_id:
                continue
            name = self._character_name(participant_id)
            if name and name in message:
                candidates.append(participant_id)
        unique_candidates = list(dict.fromkeys(candidates))
        if len(unique_candidates) == 1:
            return unique_candidates[0]
        return None

    async def _sync_active_conversation_sessions(
        self,
        current_places: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        """現在の配置に合わせて active conversation session を更新・終了する。"""
        assert self.world_clock is not None
        sessions_by_place: dict[str, dict[str, Any]] = {}
        active_sessions = await self.db.get_active_conversation_sessions(self.story_id)

        for session in active_sessions:
            participants = self._ordered_place_participants(
                current_places, session["place_id"]
            )
            if len(participants) < 2:
                await self.db.close_conversation_session(
                    session["id"],
                    last_activity_sim_datetime=self.world_clock.sim_datetime,
                    last_turn_number=self.world_clock.turn_number,
                )
                self._closed_conversation_session_ids.append(session["id"])
                continue
            if participants != session["participant_ids"]:
                await self.db.update_conversation_session(
                    session["id"],
                    {"participant_ids": participants},
                )
                session["participant_ids"] = participants
            sessions_by_place[session["place_id"]] = session

        return sessions_by_place

    async def _resolve_conversation_turn(
        self,
        *,
        char_id: str,
        current_place: str,
        current_places: dict[str, str],
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        """現在ターンの会話 session / prompt 文脈 / log metadata を決定する。"""
        assert self.world_clock is not None

        participant_ids = self._ordered_place_participants(current_places, current_place)
        if len(participant_ids) < 2:
            return {
                "conversation_session_id": None,
                "participant_ids": [],
                "msg_type": schedule["msg_type"],
                "target_char_id": schedule["target_char_id"],
                "reply_to_log_id": None,
                "target_char_name": None,
                "handoff_target_name": None,
                "conversation_partner_names": [],
                "recent_dialogue_lines": [],
                "conversation_motif_text": None,
            }

        sessions_by_place = await self._sync_active_conversation_sessions(current_places)
        session = sessions_by_place.get(current_place)
        recent_logs: list[dict[str, Any]] = []
        if session is None:
            session_id = await self.db.insert_conversation_session(
                self.story_id,
                {
                    "place_id": current_place,
                    "participant_ids": participant_ids,
                    "status": "active",
                    "started_at_sim_datetime": self.world_clock.sim_datetime,
                    "last_activity_sim_datetime": self.world_clock.sim_datetime,
                    "last_turn_number": self.world_clock.turn_number,
                },
            )
            session = {
                "id": session_id,
                "place_id": current_place,
                "participant_ids": participant_ids,
            }
            seed_logs = await self.db.get_conversation_seed_logs(
                self.story_id,
                current_place,
                self.world_clock.sim_datetime,
                self.world_clock.turn_number,
                participant_ids,
            )
            if seed_logs:
                await self.db.attach_logs_to_conversation_session(
                    [log["id"] for log in seed_logs],
                    session_id,
                )
                recent_logs = seed_logs

        if not recent_logs:
            recent_logs = await self.db.get_recent_conversation_logs(
                self.story_id,
                session["id"],
                limit=5,
            )
        recent_dialogue_lines = self._format_conversation_lines(recent_logs)
        conversation_partner_names = [
            self._character_name(cid)
            for cid in participant_ids
            if cid != char_id
        ]

        msg_type = schedule["msg_type"]
        target_char_id = schedule["target_char_id"]
        reply_to_log_id: int | None = None
        target_char_name: str | None = None
        handoff_target_name: str | None = None

        if len(participant_ids) >= 3:
            if schedule.get("speaker_intent") == "react" or recent_logs:
                reply_target = self._find_reply_target(char_id, recent_logs)
                if reply_target is not None:
                    msg_type = "reply"
                    target_char_id = reply_target["char_id"]
                    reply_to_log_id = reply_target["id"]
                    target_char_name = self._character_name(reply_target["char_id"])
                else:
                    fallback_target_id = self._fallback_reply_target_id(
                        char_id=char_id,
                        participant_ids=participant_ids,
                        scheduled_target=target_char_id,
                    )
                    if fallback_target_id is not None:
                        msg_type = "reply"
                        target_char_id = fallback_target_id
                        target_char_name = self._character_name(fallback_target_id)
                    else:
                        msg_type = "group"
                        target_char_id = [cid for cid in participant_ids if cid != char_id]
            else:
                msg_type = "group"
                target_char_id = [cid for cid in participant_ids if cid != char_id]
                handoff_target_id = self._fallback_reply_target_id(
                    char_id=char_id,
                    participant_ids=participant_ids,
                    scheduled_target=target_char_id,
                )
                if handoff_target_id is not None:
                    handoff_target_name = self._character_name(handoff_target_id)
        else:
            reply_target = self._find_reply_target(char_id, recent_logs)
            if reply_target is not None:
                msg_type = "reply"
                target_char_id = reply_target["char_id"]
                reply_to_log_id = reply_target["id"]
                target_char_name = self._character_name(reply_target["char_id"])
            elif isinstance(target_char_id, str):
                target_char_name = self._character_name(target_char_id)

        motif_context = await self._conversation_motif_engine.apply_turn_context(
            char_id=char_id,
            place_id=current_place,
            participant_ids=participant_ids,
            turn_number=self.world_clock.turn_number,
        )
        if motif_context is not None:
            msg_type = str(motif_context["msg_type"])
            target_char_id = motif_context["target_char_id"]
            reply_to_log_id = motif_context.get("reply_to_log_id")
            if isinstance(target_char_id, str):
                target_char_name = self._character_name(target_char_id)

        return {
            "conversation_session_id": session["id"],
            "participant_ids": participant_ids,
            "msg_type": msg_type,
            "target_char_id": target_char_id,
            "reply_to_log_id": reply_to_log_id,
            "target_char_name": target_char_name,
            "handoff_target_name": handoff_target_name,
            "conversation_partner_names": conversation_partner_names,
            "recent_dialogue_lines": recent_dialogue_lines,
            "recent_logs": recent_logs,
            "conversation_motif_text": (
                motif_context.get("motif_text") if motif_context is not None else None
            ),
        }

    async def _has_recent_dialogue_context(
        self,
        *,
        char_id: str,
        current_place: str,
        current_places: dict[str, str],
    ) -> bool:
        participant_ids = self._ordered_place_participants(current_places, current_place)
        if len(participant_ids) < 2:
            return False
        sessions_by_place = await self._sync_active_conversation_sessions(current_places)
        session = sessions_by_place.get(current_place)
        if session is None:
            return False
        recent_logs = await self.db.get_recent_conversation_logs(
            self.story_id,
            session["id"],
            limit=3,
        )
        return any(log.get("char_id") != char_id for log in recent_logs)

    def _build_context(
        self,
        char: dict[str, Any],
        state: dict[str, Any],
        place_name: str,
        *,
        emotions: dict[str, float] | None = None,
        expression: str = "neutral",
        move_reason: str | None = None,
        anomaly: dict[str, Any] | None = None,
        same_place_char_names: list[str] | None = None,
        secret_hint: str | None = None,
        target_char_name: str | None = None,
        conversation_partner_names: list[str] | None = None,
        recent_dialogue_lines: list[str] | None = None,
        story_memory_texts: list[str] | None = None,
        evolution_overlay: dict[str, str] | None = None,
        intervention_texts: list[str] | None = None,
        active_hook_texts: list[str] | None = None,
        relationship_summary_texts: list[str] | None = None,
        relationship_mode_texts: list[str] | None = None,
        canon_bit_texts: list[str] | None = None,
        dramatic_pressure_texts: list[str] | None = None,
        dominant_signal_text: str | None = None,
        scene_objective_text: str | None = None,
        story_pressure_cue: str | None = None,
        target_last_utterance_excerpt: str | None = None,
        reply_focus_text: str | None = None,
        voice_anchor_text: str | None = None,
        body_state_texts: list[str] | None = None,
        ambient_texts: list[str] | None = None,
        chapter_world_injection: str | None = None,
        director_tone_hints: list[str] | None = None,
        handoff_target_name: str | None = None,
        scene_frame_text: str | None = None,
        scene_turn_goal_text: str | None = None,
        scene_turn_shift_text: str | None = None,
        scene_banned_surface_patterns: list[str] | None = None,
        speaker_initiative: bool = False,
        is_presence_monologue: bool = False,
        speaker_objective: str | None = None,
        character_directive_text: str | None = None,
        reaction_frame: str | None = None,
        target_char_personality_hint: str | None = None,
        relationship_frame: str | None = None,
        is_initiation: bool = False,
        place_dialogue_lines: list[str] | None = None,
    ) -> CharacterContext:
        """キャラ行・状態行から CharacterContext を構築する（Phase 2 版）。"""
        assert self.world_clock is not None
        speech = json.loads(char["speech"])
        overlay = evolution_overlay or {}
        return CharacterContext(
            char_id=char["id"],
            name_ja=char["name_ja"],
            personality_core=overlay.get("personality_core") or char["personality_core"],
            first_person=speech["first_person"],
            tone=speech.get("tone", speech.get("speech_style", "")),
            speech_examples=speech.get("examples", []),
            never_say=speech.get("never_say", []),
            current_goal=overlay.get("current_goal") or char["current_goal"],
            current_worry=overlay.get("current_worry") or char["current_worry"],
            sim_datetime=self.world_clock.sim_datetime,
            current_place_name=place_name,
            # Phase 2 フィールド
            emotions=emotions or {},
            current_expression=expression,
            move_reason=move_reason,
            anomaly=anomaly,
            same_place_char_names=same_place_char_names or [],
            secret_hint=secret_hint,
            target_char_name=target_char_name,
            conversation_partner_names=conversation_partner_names or [],
            recent_dialogue_lines=recent_dialogue_lines or [],
            style_profile=self._style_profile,
            story_memory_texts=story_memory_texts or [],
            intervention_texts=intervention_texts or [],
            active_hook_texts=active_hook_texts or [],
            relationship_summary_texts=relationship_summary_texts or [],
            relationship_mode_texts=relationship_mode_texts or [],
            canon_bit_texts=canon_bit_texts or [],
            dramatic_pressure_texts=dramatic_pressure_texts or [],
            dominant_signal_text=dominant_signal_text,
            scene_objective_text=scene_objective_text,
            story_pressure_cue=story_pressure_cue,
            target_last_utterance_excerpt=target_last_utterance_excerpt,
            reply_focus_text=reply_focus_text,
            voice_anchor_text=voice_anchor_text,
            body_state_texts=body_state_texts or [],
            ambient_texts=ambient_texts or [],
            chapter_world_injection=chapter_world_injection,
            director_tone_hints=director_tone_hints or [],
            handoff_target_name=handoff_target_name,
            pre_turn_scene_script=self._round_scene_script,
            character_directive_text=character_directive_text,
            scene_frame_text=scene_frame_text,
            scene_turn_goal_text=scene_turn_goal_text,
            scene_turn_shift_text=scene_turn_shift_text,
            scene_banned_surface_patterns=scene_banned_surface_patterns or [],
            speaker_initiative=speaker_initiative,
            is_presence_monologue=is_presence_monologue,
            speaker_objective=speaker_objective,
            reaction_frame=reaction_frame,
            target_char_personality_hint=target_char_personality_hint,
            relationship_frame=relationship_frame,
            is_initiation=is_initiation,
            place_dialogue_lines=place_dialogue_lines or [],
            max_utterance_chars=self._story_utterance_max_chars,
            max_sentences=self._story_utterance_max_sentences,
        )

    async def _get_effective_profile_overlay(self, char_id: str) -> dict[str, str]:
        """growth/canon overlay を merge した prompt 用 profile overlay を返す。"""
        growth_overlay: dict[str, str] = {}
        if self._growth_engine is not None and self._config is not None and self._config.growth_engine.enabled:
            growth_overlay = await self._growth_engine.get_profile_overlay(char_id)
        elif self._character_evolution_manager is not None:
            growth_overlay = await self._character_evolution_manager.get_evolution_overlay(char_id)

        canon_overlay_row = await self.db.get_character_canon_overlay(self.story_id, char_id)
        canon_overlay = (
            dict(canon_overlay_row.get("overlay_json", {}))
            if canon_overlay_row is not None
            else {}
        )
        effective_overlay = dict(canon_overlay)
        effective_overlay.update(growth_overlay)
        return effective_overlay

    def _make_initial_state(self, char: dict[str, Any]) -> dict[str, Any]:
        """character_states がまだない場合に emotion_default から初期状態を生成する。"""
        emotion_default = json.loads(char["emotion_default"])
        favorite_places = json.loads(char["favorite_places"])
        return {
            "current_place":     favorite_places[0] if favorite_places else "classroom",
            "previous_place":    None,
            "current_expression": "neutral",
            "stress":      emotion_default.get("stress",     0.3),
            "motivation":  emotion_default.get("motivation", 0.7),
            "loneliness":  emotion_default.get("loneliness", 0.2),
            "excitement":  emotion_default.get("excitement", 0.5),
        }

    def _build_retry_user_prompt(
        self,
        original_user_prompt: str,
        *,
        msg_type: str,
        issues: list[dict[str, Any]] | None = None,
        target_last_utterance_excerpt: str | None = None,
        reply_focus_text: str | None = None,
        reply_focus_contract: dict[str, Any] | None = None,
        reply_signal_contract: dict[str, Any] | None = None,
        reply_surface_contract: dict[str, Any] | None = None,
        reply_variety_contract: dict[str, Any] | None = None,
        reply_dramatic_contract: dict[str, Any] | None = None,
        reply_blandness_contract: dict[str, Any] | None = None,
        reply_shape_contract: dict[str, Any] | None = None,
        reply_quality_contract: dict[str, Any] | None = None,
        reply_story_quality_contract: dict[str, Any] | None = None,
        reply_residual_quality_contract: dict[str, Any] | None = None,
        voice_anchor_text: str | None = None,
        scene_objective_text: str | None = None,
        dominant_signal_text: str | None = None,
        story_pressure_cue: str | None = None,
        handoff_target_name: str | None = None,
    ) -> str:
        """文体を軽くするための 1 回限りの再試行指示を追加する。"""
        raw_issue_types = [
            str(issue.get("issue_type") or "")
            for issue in (issues or [])
            if str(issue.get("issue_type") or "")
        ]
        issue_types = self._prioritized_retry_issue_types(raw_issue_types)
        retry_instruction = (
            "短く、具体的な相手・物・行動を使って言い直してください。"
            "比喩や地の文は使わず、会話として自然な本文だけを返してください。"
        )
        if msg_type == "reply":
            retry_instruction += " 最大2文までにしてください。3文目は書かないでください。"
            retry_instruction += " 1文目は相手の言った一点への直接反応にしてください。"
            if reply_focus_contract and reply_focus_contract.get("must_answer_in_first_sentence"):
                retry_instruction += " 1文目で焦点への答えを必ず出してください。"
                retry_instruction += " 1文目の最初の短い切り出しで答えを出してください。"
            if "reply_without_direct_reaction" in issue_types and target_last_utterance_excerpt:
                retry_instruction += (
                    f" 相手の発言の要点を先頭で拾ってください: 「{target_last_utterance_excerpt}」。"
                )
                if reply_focus_contract:
                    focus_family = str(reply_focus_contract.get("focus_family") or "").strip()
                    if focus_family == "yes_no":
                        retry_instruction += " 1文目で帰る/帰らない/残る/行くを返してください。"
                    elif focus_family == "timing":
                        retry_instruction += " 1文目でいつ動くかを自分の言葉で返してください。"
                    elif focus_family == "decision_owner":
                        retry_instruction += " 1文目で誰が決めるかを返してください。"
                    elif focus_family == "basis":
                        retry_instruction += " 1文目で基準を返してください。"
                    elif focus_family == "premise":
                        retry_instruction += " 1文目で前提か条件を返してください。"
                    elif focus_family == "proposal":
                        retry_instruction += " 1文目で自分の言葉で自然に返してください。"
            else:
                retry_instruction += " 相手の名前か、相手の言葉を先頭で拾ってください。"
            retry_instruction += " 抽象的な感情説明ではなく、今のやりとりに返してください。"
            if reply_focus_text and not _contains_backend_meta_vocabulary(reply_focus_text):
                retry_instruction += f" 今返す一点は「{reply_focus_text}」です。"
                focus_family = self._reply_focus_family(reply_focus_text)
                if focus_family == "decision_owner":
                    retry_instruction += " 誰が何をするか、1文目で自然に返してください。"
                elif focus_family == "basis":
                    retry_instruction += " 自分が思う答えを1文目で返してください。"
                elif focus_family == "timing":
                    retry_instruction += " いつ動くかを1文目で返してください。"
                    retry_instruction += " 問い返しではなく、自分の立場を短く言い切ってください。"
                elif focus_family == "yes_no":
                    retry_instruction += " 先頭で可否を返してください。"
                elif focus_family == "premise":
                    retry_instruction += " 相手の前提を一つ拾って返してください。"
                elif focus_family == "proposal":
                    retry_instruction += " 1文目で自分の考えを短く出してください。"
            if "reply_focus_missing" in issue_types and reply_focus_contract:
                focus_family = str(reply_focus_contract.get("focus_family") or "").strip()
                focus_anchor_tokens = [
                    str(token).strip()
                    for token in list(reply_focus_contract.get("focus_anchor_tokens") or [])
                    if str(token).strip()
                ]
                if focus_family == "timing" and focus_anchor_tokens:
                    retry_instruction += " 1文目でいつ動くかを自分の言葉で出してください。"
                    retry_instruction += " 問い返しではなく、自分の気持ちを短く言い切ってください。"
                elif focus_family == "yes_no" and focus_anchor_tokens:
                    retry_instruction += " 1文目で帰る/帰らない/残る/行くのどれかを出してください。"
                elif focus_family == "decision_owner":
                    retry_instruction += " 1文目で誰が何をするか、短く出してください。"
                elif focus_family == "basis":
                    retry_instruction += " 1文目で自分が思う答えを短く出してください。"
                elif focus_family == "premise":
                    retry_instruction += " 1文目で前提か条件を一つ拾ってください。"
                elif focus_family == "proposal":
                    retry_instruction += " 1文目で自分の考えを短く出してください。"
            if reply_focus_contract:
                preferred_action = str(reply_focus_contract.get("preferred_action") or "").strip()
                if preferred_action == "confirm":
                    retry_instruction += " 確認や決定の答えを先に出してください。"
                elif preferred_action == "reject":
                    retry_instruction += " 反論なら最初に異議をはっきり出してください。"
                elif preferred_action == "propose":
                    retry_instruction += " 提案なら最初に次の一手を出してください。"
                elif preferred_action == "defer":
                    retry_instruction += " 保留なら時期や条件を先に言ってください。"
            if {"scene_objective_drift", "signal_override"} & issue_types:
                signal_tokens = [
                    str(token).strip()
                    for token in list((reply_signal_contract or {}).get("signal_anchor_tokens") or [])
                    if str(token).strip()
                ]
                objective_tokens = [
                    str(token).strip()
                    for token in list((reply_signal_contract or {}).get("objective_anchor_tokens") or [])
                    if str(token).strip()
                ]
                signal_text = scene_objective_text or dominant_signal_text
                if objective_tokens:
                    retry_instruction += " 今この場で大切な一点に触れてください。"
                elif signal_tokens:
                    retry_instruction += " 今の場の流れに自然に触れてください。"
                elif signal_text:
                    retry_instruction += " 今この場で自分が言うべき一点に触れてください。"
            if reply_signal_contract and (
                {"signal_visibility_missing", "scene_objective_visibility_missing"} & issue_types
            ):
                visibility_mode = str(reply_signal_contract.get("visibility_mode") or "").strip()
                signal_tokens = [
                    str(token).strip()
                    for token in list(reply_signal_contract.get("signal_anchor_tokens") or [])
                    if str(token).strip()
                ]
                objective_tokens = [
                    str(token).strip()
                    for token in list(reply_signal_contract.get("objective_anchor_tokens") or [])
                    if str(token).strip()
                ]
                if visibility_mode == "signal_first" and signal_tokens:
                    retry_instruction += (
                        " 返答の直後に "
                        + signal_tokens[0]
                        + " をいま気にしていることとして1回だけ明示してください。"
                    )
                elif visibility_mode == "objective_first" and objective_tokens:
                    retry_instruction += (
                        " 返答の直後に "
                        + objective_tokens[0]
                        + " をこの場で決めることとして明示してください。"
                    )
                elif signal_tokens and objective_tokens:
                    objective_phrase = self._clean_scene_objective_text(scene_objective_text) or objective_tokens[0]
                    retry_instruction += (
                        " 1-2文の中で "
                        + signal_tokens[0]
                        + " と "
                        + objective_phrase
                        + " の両方を別の役割で出してください。"
                    )
            if voice_anchor_text:
                retry_instruction += f" 返し方は「{voice_anchor_text}」を意識してください。"
            if "generic_reply_tail" in issue_types:
                retry_instruction += " 2文目は汎用的な締めではなく、その場を押し返す一手だけにしてください。汎用締めは使わないでください。"
            if "voice_flat_reply" in issue_types:
                retry_instruction += " 2文目が説明口調だけで終わらないよう、口調の芯になる短い語を1つ混ぜてください。"
            if "sentence_overflow" in issue_types:
                retry_instruction += " 1文目を答え、2文目を押し返しに固定して圧縮してください。"
            if reply_surface_contract and reply_surface_contract.get("must_vary_from_recent_self"):
                retry_instruction += " 直近の自分の返しと同じ締め方を避けてください。"
            preferred_voice_cues = [
                str(token).strip()
                for token in list((reply_surface_contract or {}).get("preferred_voice_cues") or [])
                if str(token).strip()
            ]
            if preferred_voice_cues:
                retry_instruction += (
                    " 口調の芯として "
                    + " / ".join(preferred_voice_cues[:2])
                    + " のどちらかを短く混ぜてください。"
                )
            if reply_variety_contract:
                second_beat_mode = str(reply_variety_contract.get("second_beat_mode") or "").strip()
                preferred_move_tokens = [
                    str(token).strip()
                    for token in list(reply_variety_contract.get("preferred_move_tokens") or [])
                    if str(token).strip()
                ]
                if second_beat_mode:
                    retry_instruction += (
                        f" 2文目は {self._describe_reply_move_mode(second_beat_mode)} にしてください。"
                    )
                if reply_variety_contract.get("forbidden_recent_openings"):
                    retry_instruction += " 直近と同じ切り出しも避けてください。"
                if reply_variety_contract.get("forbidden_recent_second_beats"):
                    retry_instruction += " 直近と同じ運び方も避けてください。"
                if preferred_move_tokens:
                    retry_instruction += " 次の押し方の芯は " + " / ".join(preferred_move_tokens[:2]) + " を優先してください。"
            if reply_dramatic_contract:
                move_mode = str(reply_dramatic_contract.get("move_mode") or "").strip()
                semantic_family = str(reply_dramatic_contract.get("semantic_move_family") or "").strip()
                required_move_tokens = [
                    str(token).strip()
                    for token in list(reply_dramatic_contract.get("required_move_tokens") or [])
                    if str(token).strip()
                ]
                required_semantic_cues = [
                    str(token).strip()
                    for token in list(reply_dramatic_contract.get("required_semantic_cues") or [])
                    if str(token).strip()
                ]
                pressure_anchor_tokens = [
                    str(token).strip()
                    for token in list(reply_dramatic_contract.get("pressure_anchor_tokens") or [])
                    if str(token).strip()
                ]
                if move_mode:
                    retry_instruction += (
                        f" 2文目は {self._describe_reply_move_mode(move_mode)} で別の反応にしてください。"
                    )
                if required_move_tokens:
                    retry_instruction += " 返し方の芯は " + " / ".join(required_move_tokens[:3]) + " です。"
                if pressure_anchor_tokens:
                    retry_instruction += f" 2文目で{pressure_anchor_tokens[0]}を拾って押し返してください。"
                if semantic_family == "counter":
                    retry_instruction += " 相手の運びを差し戻すか奪い返す形にしてください。"
                elif semantic_family == "condition":
                    retry_instruction += " 2文目で条件か順序を固定してください。"
                elif semantic_family == "probe":
                    retry_instruction += " 2文目で相手が避けている一点を問い返してください。"
                elif semantic_family == "claim":
                    retry_instruction += " 2文目で立場を言い切ってください。"
                if required_semantic_cues:
                    retry_instruction += " 反応の芯は " + " / ".join(required_semantic_cues[:3]) + " を優先してください。"
                if reply_dramatic_contract.get("must_change_pressure_in_second_sentence"):
                    retry_instruction += " 2文目は無難に閉じず、押し返す・条件を付ける・問い返す・言い切るのどれかにしてください。"
                if reply_dramatic_contract.get("forbidden_soft_landings"):
                    retry_instruction += " 無難な着地だけで閉じないでください。"
            if reply_blandness_contract:
                primary_shape = str(reply_blandness_contract.get("primary_shape") or "").strip()
                if primary_shape:
                    retry_instruction += f" 返しの型は {self._describe_reply_shape(primary_shape)} を優先してください。"
                if reply_blandness_contract.get("must_shift_pressure_in_second_sentence"):
                    retry_instruction += " 2文目で別の具体的な反応にしてください。"
                forbidden_soft_landings = [
                    str(token).strip()
                    for token in list(reply_blandness_contract.get("forbidden_soft_landings") or [])
                    if str(token).strip()
                ]
                if forbidden_soft_landings:
                    retry_instruction += " 薄い締めとして " + " / ".join(forbidden_soft_landings[:2]) + " は避けてください。"
            if reply_shape_contract:
                primary_shape = str(reply_shape_contract.get("primary_shape") or "").strip()
                if primary_shape:
                    retry_instruction += f" 返しの型は {self._describe_reply_shape(primary_shape)} を優先してください。"
                if reply_shape_contract.get("must_shift_pressure_in_second_sentence"):
                    retry_instruction += " 2文目で別の具体的な反応にしてください。"
                story_pressure_tokens = [
                    str(token).strip()
                    for token in list(reply_shape_contract.get("story_pressure_tokens") or [])
                    if str(token).strip()
                ]
                if story_pressure_tokens:
                    retry_instruction += " 2文目で " + " / ".join(story_pressure_tokens[:2]) + " のどちらかを実語で出してください。"
            if reply_quality_contract:
                if reply_quality_contract.get("second_beat_mode"):
                    retry_instruction += " 2文目で別の具体的な反応にしてください。"
                quality_story_tokens = [
                    str(token).strip()
                    for token in list(reply_quality_contract.get("story_pressure_tokens") or [])
                    if str(token).strip()
                ]
                if quality_story_tokens:
                    retry_instruction += " 2文目で " + " / ".join(quality_story_tokens[:2]) + " のどちらかを実語で出してください。"
                required_story_flavor_role = str(
                    reply_quality_contract.get("required_story_flavor_role") or ""
                ).strip()
                story_flavor_role = self._describe_story_flavor_role(required_story_flavor_role)
                if story_flavor_role:
                    retry_instruction += f" {story_flavor_role} が伝わる返しにしてください。"
                if reply_quality_contract.get("forbidden_soft_landings"):
                    retry_instruction += " 無難な着地だけで閉じないでください。"
            if reply_story_quality_contract:
                story_quality_tokens = [
                    str(token).strip()
                    for token in list(reply_story_quality_contract.get("story_pressure_tokens") or [])
                    if str(token).strip()
                ]
                if story_quality_tokens:
                    retry_instruction += " 2文目で " + " / ".join(story_quality_tokens[:2]) + " のどちらかを実語で出してください。"
                story_quality_role = str(
                    reply_story_quality_contract.get("required_story_flavor_role") or ""
                ).strip()
                story_quality_role_text = self._describe_story_flavor_role(story_quality_role)
                if story_quality_role_text:
                    retry_instruction += f" {story_quality_role_text} が伝わる返しにしてください。"
                story_quality_cue = str(
                    reply_story_quality_contract.get("story_pressure_cue_text") or ""
                ).strip()
                if reply_story_quality_contract.get("forbidden_soft_landings"):
                    retry_instruction += " 無難な着地だけで閉じないでください。"
            if reply_residual_quality_contract:
                residual_tokens = [
                    str(token).strip()
                    for token in list(reply_residual_quality_contract.get("story_pressure_tokens") or [])
                    if str(token).strip()
                ]
                if residual_tokens:
                    retry_instruction += " 2文目で " + " / ".join(residual_tokens[:2]) + " のどちらかを実語で出してください。"
                residual_role = str(
                    reply_residual_quality_contract.get("required_story_flavor_role") or ""
                ).strip()
                residual_role_text = self._describe_story_flavor_role(residual_role)
                if residual_role_text:
                    retry_instruction += f" {residual_role_text} が伝わる返しにしてください。"
                if reply_residual_quality_contract.get("second_beat_mode"):
                    retry_instruction += " 2文目は同じ運びを繰り返さず、別の具体的な反応にしてください。"
                if reply_residual_quality_contract.get("forbidden_soft_landings"):
                    retry_instruction += " 無難な着地だけで閉じないでください。"
        elif msg_type == "group":
            retry_instruction += " 周囲の誰かの発言を一つ拾って、自分の言葉で返してください。"
            if handoff_target_name:
                retry_instruction += f" 2文目は {handoff_target_name} に返しやすい形へ寄せてください。"
        elif msg_type == "monologue":
            retry_instruction += " 1〜2文で、目の前の物か次の行動を必ず含めてください。"
        return (
            f"{original_user_prompt}\n\n"
            "前回の出力は抽象的・詩的すぎました。"
            f"{retry_instruction}"
        )

    @staticmethod
    def _prioritized_retry_issue_types(issue_types: list[str]) -> set[str]:
        """retry 指示は root cause に近い最大2種類へ圧縮する。"""
        priority = (
            "reply_without_direct_reaction",
            "scene_objective_drift",
            "signal_override",
            "reply_focus_missing",
            "generic_reply_tail",
            "signal_visibility_missing",
            "scene_objective_visibility_missing",
            "voice_flat_reply",
            "sentence_overflow",
            "paragraph_break_output",
            "markdown_formatting",
        )
        ordered: list[str] = []
        seen: set[str] = set()
        for issue_type in [*priority, *issue_types]:
            if issue_type not in issue_types or issue_type in seen:
                continue
            ordered.append(issue_type)
            seen.add(issue_type)
            if len(ordered) >= 2:
                break
        return set(ordered)

    @staticmethod
    def _should_keep_retry_output_without_fallback(
        *,
        msg_type: str,
        text: str,
        issues: list[dict[str, Any]],
    ) -> bool:
        if msg_type != "reply" or not issues:
            return False
        issue_types = {
            str(issue.get("issue_type") or "")
            for issue in issues
            if str(issue.get("issue_type") or "")
        }
        if any(
            str(issue.get("issue_type") or "") == "voice_flat_reply"
            and StoryEngine._is_blocking_voice_flat_issue(dict(issue.get("details", {})))
            for issue in issues
        ):
            return False
        if any(str(issue.get("issue_type") or "") == "director_cue_surface_leak" for issue in issues):
            return False
        if any(
            str(issue.get("issue_type") or "") == "generic_reply_tail"
            and bool(dict(issue.get("details", {})).get("generic_reply_tail_blocking"))
            for issue in issues
        ):
            return False
        if any(
            str(issue.get("issue_type") or "") in {"signal_visibility_missing", "scene_objective_visibility_missing"}
            and StoryEngine._is_blocking_visibility_issue(dict(issue.get("details", {})))
            for issue in issues
        ):
            return False
        blocking_issue_types = {
            "reply_focus_missing",
            "reply_without_direct_reaction",
            "scene_objective_drift",
            "signal_override",
            "abstract_opening",
            "low_concreteness",
        }
        if issue_types & blocking_issue_types:
            return False
        keep_issue_types = {
            "reply_direct_reaction_soft",
            "voice_flat_reply",
            "signal_visibility_missing",
            "scene_objective_visibility_missing",
            "generic_reply_tail",
            "reply_surface_normalized",
            "paragraph_break_output",
            "sentence_overflow",
            "overlength",
            "markdown_formatting",
        }
        return bool(text.strip()) and issue_types.issubset(keep_issue_types)

    @staticmethod
    def _is_blocking_voice_flat_issue(details: dict[str, Any]) -> bool:
        explicit = details.get("voice_flat_blocking")
        if explicit is not None:
            return bool(explicit)
        return any(
            bool(details.get(flag))
            for flag in (
                "surface_contract_missed",
                "variety_contract_missed",
                "dramatic_contract_missed",
                "blandness_contract_missed",
                "shape_contract_missed",
                "reply_quality_contract_missed",
                "reply_story_quality_contract_missed",
                "reply_residual_contract_missed",
            )
        )

    @staticmethod
    def _is_blocking_visibility_issue(details: dict[str, Any]) -> bool:
        explicit = details.get("visibility_blocking")
        if explicit is not None:
            return bool(explicit)
        return True

    @staticmethod
    def _should_omit_issue_from_quality_flags(
        issue: dict[str, Any],
        *,
        quality_action: str,
        response_text: str,
    ) -> bool:
        if quality_action not in {"accept", "normalize", "shorten"}:
            return False
        issue_type = str(issue.get("issue_type") or "")
        details = dict(issue.get("details", {}))
        if issue_type == "voice_flat_reply" and not StoryEngine._is_blocking_voice_flat_issue(details):
            return True
        if (
            issue_type == "sentence_overflow"
            and quality_action == "shorten"
            and len(StoryEngine._split_reply_sentences(response_text)) <= 2
        ):
            return True
        if issue_type in {"signal_visibility_missing", "scene_objective_visibility_missing"} and not (
            StoryEngine._is_blocking_visibility_issue(details)
        ):
            return True
        return False

    @staticmethod
    def _select_reply_fallback_root_issue(issues: list[dict[str, Any]]) -> str | None:
        priority = (
            "reply_focus_missing",
            "reply_without_direct_reaction",
            "signal_visibility_missing",
            "scene_objective_visibility_missing",
            "scene_objective_drift",
            "signal_override",
            "abstract_opening",
            "low_concreteness",
            "voice_flat_reply",
            "generic_reply_tail",
        )
        issue_types = [
            str(issue.get("issue_type") or "").strip()
            for issue in issues
            if str(issue.get("issue_type") or "").strip()
        ]
        for candidate in priority:
            if candidate in issue_types:
                return candidate
        return issue_types[0] if issue_types else None

    @staticmethod
    def _split_reply_sentences(text: str) -> list[str]:
        parts = [part.strip() for part in re.split(r"([。！？!?])", str(text).strip()) if part.strip()]
        sentences: list[str] = []
        current = ""
        for part in parts:
            current += part
            if part in "。！？!?":
                sentences.append(current.strip())
                current = ""
        if current.strip():
            sentences.append(current.strip())
        return sentences

    @classmethod
    def _compress_reply_to_two_sentences(
        cls,
        text: str,
        *,
        reply_shape_contract: dict[str, Any] | None,
    ) -> str:
        sentences = cls._split_reply_sentences(text)
        if len(sentences) <= 2:
            return str(text).strip()
        first_sentence = sentences[0].rstrip("。！？!?")
        second_sentence = " ".join(
            sentence.rstrip("。！？!?")
            for sentence in sentences[1:]
            if sentence.rstrip("。！？!?")
        ).strip()
        if not first_sentence:
            return "".join(sentences[:2]).strip()
        compressed = f"{first_sentence}。{second_sentence}" if second_sentence else f"{first_sentence}。"
        compressed = cls._apply_reply_shape_to_fallback(
            compressed,
            reply_shape_contract=reply_shape_contract,
        )
        final_sentences = cls._split_reply_sentences(compressed)
        if len(final_sentences) > 2:
            return "".join(final_sentences[:2]).strip()
        return compressed.strip()

    @staticmethod
    def _reply_fallback_issue_set(issues: list[dict[str, Any]]) -> list[str]:
        return sorted(
            {
                str(issue.get("issue_type") or "").strip()
                for issue in issues
                if str(issue.get("issue_type") or "").strip()
            }
        )

    @staticmethod
    def _select_reply_flat_issue_family(issues: list[dict[str, Any]]) -> str | None:
        for issue in issues:
            details = dict(issue.get("details", {}))
            family = str(details.get("flat_issue_family") or "").strip()
            if family:
                return family
        return None

    @staticmethod
    def _reply_flat_recent_tail_reused(issues: list[dict[str, Any]]) -> bool:
        return any(bool(dict(issue.get("details", {})).get("recent_self_tail_reused")) for issue in issues)

    @staticmethod
    def _reply_flat_recent_opening_reused(issues: list[dict[str, Any]]) -> bool:
        return any(bool(dict(issue.get("details", {})).get("recent_opening_reused")) for issue in issues)

    @staticmethod
    def _reply_flat_recent_second_beat_reused(issues: list[dict[str, Any]]) -> bool:
        return any(bool(dict(issue.get("details", {})).get("recent_second_beat_reused")) for issue in issues)

    def _build_deterministic_fallback_text(
        self,
        *,
        ctx: CharacterContext,
        msg_type: str,
        target_char_name: str | None,
        recent_self_messages: list[str] | None = None,
        recent_scene_messages: list[str] | None = None,
        reply_focus_contract: dict[str, Any] | None = None,
        fallback_root_issue: str | None = None,
        reply_signal_contract: dict[str, Any] | None = None,
        reply_variety_contract: dict[str, Any] | None = None,
        reply_dramatic_contract: dict[str, Any] | None = None,
        reply_blandness_contract: dict[str, Any] | None = None,
        reply_shape_contract: dict[str, Any] | None = None,
        reply_quality_contract: dict[str, Any] | None = None,
        reply_story_quality_contract: dict[str, Any] | None = None,
        reply_residual_quality_contract: dict[str, Any] | None = None,
    ) -> str:
        signal_text = ctx.dominant_signal_text or ctx.scene_objective_text or ""
        intent = self._fallback_signal_intent(signal_text)
        objective_text = str(ctx.scene_objective_text or "").replace("この場の争点:", "").strip()
        voice = self._infer_fallback_voice_profile(ctx)
        if msg_type == "reply":
            return self._build_extractive_reply_fallback(
                ctx=ctx,
                target_char_name=target_char_name,
                voice=voice,
                reply_signal_contract=reply_signal_contract,
                reply_dramatic_contract=reply_dramatic_contract,
                reply_shape_contract=reply_shape_contract,
                reply_quality_contract=reply_quality_contract,
                reply_story_quality_contract=reply_story_quality_contract,
                reply_residual_quality_contract=reply_residual_quality_contract,
            )
        variants = self._build_fallback_variants(
            ctx=ctx,
            msg_type=msg_type,
            target_char_name=target_char_name,
            intent=intent,
            objective_text=objective_text,
            voice=voice,
        )
        if not variants:
            return ""
        reply_move_plan = self._build_reply_move_plan(
            ctx=ctx,
            reply_signal_contract=reply_signal_contract,
            reply_dramatic_contract=reply_dramatic_contract,
            reply_shape_contract=reply_shape_contract,
            recent_self_messages=recent_self_messages or [],
            recent_scene_messages=recent_scene_messages or [],
        )
        key = "|".join(
            [
                ctx.char_id,
                msg_type,
                intent,
                target_char_name or "",
                objective_text,
                ctx.sim_datetime,
            ]
        )
        index = sum(ord(ch) for ch in key) % len(variants)
        recent_set = {
            str(message).strip()
            for message in (recent_self_messages or [])
            if str(message).strip()
        }
        recent_second_sentences = {
            second
            for second in (
                self._extract_fallback_second_sentence(message)
                for message in [*(recent_self_messages or []), *(recent_scene_messages or [])]
            )
            if second
        }
        forbidden_recent_openings = {
            str(token).strip()
            for token in list((reply_variety_contract or {}).get("forbidden_recent_openings") or [])
            if str(token).strip()
        }
        forbidden_recent_second_beats = {
            str(token).strip()
            for token in list((reply_variety_contract or {}).get("forbidden_recent_second_beats") or [])
            if str(token).strip()
        }
        quality_story_pressure_tokens = [
            str(token).strip()
            for token in list((reply_quality_contract or {}).get("story_pressure_tokens") or [])
            if str(token).strip()
        ]
        story_quality_pressure_tokens = [
            str(token).strip()
            for token in list((reply_story_quality_contract or {}).get("story_pressure_tokens") or [])
            if str(token).strip()
        ]
        dramatic_pressure_anchor_tokens = [
            str(token).strip()
            for token in list((reply_dramatic_contract or {}).get("pressure_anchor_tokens") or [])
            if str(token).strip()
        ]
        dramatic_semantic_family = str(
            (reply_dramatic_contract or {}).get("semantic_move_family") or ""
        ).strip()
        dramatic_required_semantic_cues = [
            str(token).strip()
            for token in list((reply_dramatic_contract or {}).get("required_semantic_cues") or [])
            if str(token).strip()
        ]
        dramatic_forbidden_semantic_drifts = {
            str(token).strip()
            for token in list((reply_dramatic_contract or {}).get("forbidden_semantic_drifts") or [])
            if str(token).strip()
        }
        focus_anchor_tokens = [
            str(token).strip()
            for token in list((reply_focus_contract or {}).get("focus_anchor_tokens") or [])
            if str(token).strip()
        ]
        residual_pressure_tokens = [
            str(token).strip()
            for token in list((reply_residual_quality_contract or {}).get("story_pressure_tokens") or [])
            if str(token).strip()
        ]
        residual_forbidden_soft_landings = {
            str(token).strip()
            for token in list((reply_residual_quality_contract or {}).get("forbidden_soft_landings") or [])
            if str(token).strip()
        }
        require_focus_anchor_first = bool(
            focus_anchor_tokens
            and fallback_root_issue in {"reply_focus_missing", "reply_without_direct_reaction", None}
        )
        for anchor_required in ([True, False] if require_focus_anchor_first else [False]):
            for offset in range(len(variants)):
                candidate = variants[(index + offset) % len(variants)].strip()
                candidate_first = self._extract_fallback_first_sentence(candidate)
                candidate_second = self._extract_fallback_second_sentence(candidate)
                candidate_second_beat = self._infer_reply_second_beat_mode(candidate_second)
                if anchor_required and (
                    not candidate_first
                    or not any(token in candidate_first for token in focus_anchor_tokens)
                ):
                    continue
                if (
                    candidate
                    and candidate not in recent_set
                    and (not candidate_first or candidate_first not in forbidden_recent_openings)
                    and (
                        not candidate_first
                        or not reply_focus_contract
                        or fallback_root_issue not in {"reply_focus_missing", "reply_without_direct_reaction", None}
                        or self._fallback_matches_focus_contract(
                            candidate_first,
                            reply_focus_contract=reply_focus_contract,
                        )
                    )
                    and (not candidate_second or candidate_second not in recent_second_sentences)
                    and (
                        not candidate_second_beat
                        or candidate_second_beat not in forbidden_recent_second_beats
                    )
                    and (
                        not candidate_second
                        or not quality_story_pressure_tokens
                        or any(token in candidate_second for token in quality_story_pressure_tokens)
                    )
                    and (
                        not candidate_second
                        or not story_quality_pressure_tokens
                        or any(token in candidate_second for token in story_quality_pressure_tokens)
                    )
                    and (
                        not candidate_second
                        or not residual_pressure_tokens
                        or any(token in candidate_second for token in residual_pressure_tokens)
                    )
                    and (
                        not candidate_second
                        or not residual_forbidden_soft_landings
                        or all(token not in candidate_second for token in residual_forbidden_soft_landings)
                    )
                    and (
                        not candidate_second
                        or not dramatic_semantic_family
                        or self._fallback_matches_dramatic_semantic_family(
                            candidate_second,
                            semantic_family=dramatic_semantic_family,
                            required_semantic_cues=dramatic_required_semantic_cues,
                        )
                    )
                    and (
                        not candidate_second
                        or not dramatic_pressure_anchor_tokens
                        or any(token in candidate_second for token in dramatic_pressure_anchor_tokens)
                    )
                    and (
                        not candidate_second
                        or not dramatic_forbidden_semantic_drifts
                        or all(token not in candidate_second for token in dramatic_forbidden_semantic_drifts)
                    )
                ):
                    return self._apply_reply_move_plan_to_fallback(
                        self._apply_reply_signal_visibility_to_fallback(
                            self._apply_reply_shape_to_fallback(
                                self._apply_reply_blandness_to_fallback(
                                    self._apply_reply_dramatic_move_to_fallback(
                                        candidate,
                                        reply_dramatic_contract=reply_dramatic_contract,
                                    ),
                                    reply_blandness_contract=reply_blandness_contract,
                                ),
                                reply_shape_contract=reply_shape_contract,
                            ),
                            reply_signal_contract=reply_signal_contract,
                        ),
                        plan=reply_move_plan,
                    )
        return self._apply_reply_move_plan_to_fallback(
            self._apply_reply_signal_visibility_to_fallback(
                self._apply_reply_shape_to_fallback(
                    self._apply_reply_blandness_to_fallback(
                        self._apply_reply_dramatic_move_to_fallback(
                            variants[index].strip(),
                            reply_dramatic_contract=reply_dramatic_contract,
                        ),
                        reply_blandness_contract=reply_blandness_contract,
                    ),
                    reply_shape_contract=reply_shape_contract,
                ),
                reply_signal_contract=reply_signal_contract,
            ),
            plan=reply_move_plan,
        )

    @classmethod
    def _build_extractive_reply_fallback(
        cls,
        *,
        ctx: CharacterContext,
        target_char_name: str | None,
        voice: str,
        reply_signal_contract: dict[str, Any] | None,
        reply_dramatic_contract: dict[str, Any] | None,
        reply_shape_contract: dict[str, Any] | None,
        reply_quality_contract: dict[str, Any] | None,
        reply_story_quality_contract: dict[str, Any] | None,
        reply_residual_quality_contract: dict[str, Any] | None,
    ) -> str:
        anchor_terms = cls._extract_reply_fallback_anchor_terms(
            ctx=ctx,
            reply_signal_contract=reply_signal_contract,
            reply_dramatic_contract=reply_dramatic_contract,
            reply_shape_contract=reply_shape_contract,
            reply_quality_contract=reply_quality_contract,
            reply_story_quality_contract=reply_story_quality_contract,
            reply_residual_quality_contract=reply_residual_quality_contract,
        )
        if not anchor_terms:
            return ""
        anchor = anchor_terms[0]
        target = target_char_name or ctx.target_char_name
        prefix = f"{target}、" if target else ""
        contract_anchor_terms: list[str] = []
        for contract, key in (
            (reply_signal_contract, "objective_anchor_tokens"),
            (reply_signal_contract, "signal_anchor_tokens"),
            (reply_dramatic_contract, "pressure_anchor_tokens"),
            (reply_shape_contract, "story_pressure_tokens"),
            (reply_quality_contract, "story_pressure_tokens"),
            (reply_story_quality_contract, "story_pressure_tokens"),
            (reply_residual_quality_contract, "story_pressure_tokens"),
        ):
            if not contract:
                continue
            for token in list(contract.get(key) or []):
                cleaned = cls._clean_reply_fallback_anchor_term(str(token))
                if cleaned:
                    contract_anchor_terms.append(cleaned)
        has_contract_anchor = anchor in contract_anchor_terms
        source_text = " ".join(
            str(item or "")
            for item in (
                ctx.target_last_utterance_excerpt,
                ctx.reply_focus_text,
                ctx.scene_objective_text,
                ctx.dominant_signal_text,
            )
        )
        if "帰" in source_text and not has_contract_anchor:
            return f"{prefix}まだ帰らない"
        if "見せ" in source_text:
            return f"{prefix}{anchor}はまだ見せない"
        if voice == "polite":
            return f"{prefix}{anchor}は少し落ち着いて話しましょう"
        if voice == "hot_blooded":
            return f"{prefix}{anchor}から逃げない"
        if voice == "provocative":
            return f"{prefix}{anchor}を隠すなら見せてよ"
        if voice == "cool_observer":
            return f"{prefix}{anchor}の扱いは雑にできない"
        return f"{prefix}{anchor}のことなら返す"

    @classmethod
    def _extract_reply_fallback_anchor_terms(
        cls,
        *,
        ctx: CharacterContext,
        reply_signal_contract: dict[str, Any] | None,
        reply_dramatic_contract: dict[str, Any] | None,
        reply_shape_contract: dict[str, Any] | None,
        reply_quality_contract: dict[str, Any] | None,
        reply_story_quality_contract: dict[str, Any] | None,
        reply_residual_quality_contract: dict[str, Any] | None,
    ) -> list[str]:
        terms: list[str] = []

        def add(term: str) -> None:
            cleaned = cls._clean_reply_fallback_anchor_term(term)
            if cleaned and cleaned not in terms:
                terms.append(cleaned)

        for contract, key in (
            (reply_signal_contract, "objective_anchor_tokens"),
            (reply_signal_contract, "signal_anchor_tokens"),
            (reply_dramatic_contract, "pressure_anchor_tokens"),
            (reply_shape_contract, "story_pressure_tokens"),
            (reply_quality_contract, "story_pressure_tokens"),
            (reply_story_quality_contract, "story_pressure_tokens"),
            (reply_residual_quality_contract, "story_pressure_tokens"),
        ):
            if not contract:
                continue
            for token in list(contract.get(key) or []):
                add(str(token))

        for source in (
            ctx.target_last_utterance_excerpt,
            cls._target_utterance_from_recent_dialogue(ctx.recent_dialogue_lines, ctx.target_char_name),
            ctx.reply_focus_text,
            ctx.character_directive_text,
            ctx.story_pressure_cue,
            cls._clean_scene_objective_text(ctx.scene_objective_text),
            cls._clean_scene_objective_text(ctx.dominant_signal_text),
        ):
            for token in cls._extract_concrete_reply_fallback_terms(source):
                add(token)
        return terms[:3]

    @staticmethod
    def _target_utterance_from_recent_dialogue(
        recent_dialogue_lines: list[str],
        target_char_name: str | None,
    ) -> str:
        if not target_char_name:
            return ""
        target_prefix = f"{target_char_name}:"
        for line in reversed(recent_dialogue_lines or []):
            cleaned = str(line or "").strip()
            if not cleaned.startswith(target_prefix):
                continue
            return cleaned.split(":", maxsplit=1)[1].strip(" 「」")
        return ""

    @staticmethod
    def _extract_concrete_reply_fallback_terms(text: str | None) -> list[str]:
        cleaned = str(text or "").strip()
        if not cleaned:
            return []
        cleaned = cleaned.replace("……", " ").replace("...", " ")
        chunks = re.split(r"[、。,\s「」『』（）()！？!?]+|を|が|は|に|で|と|へ|から|より|だけ|ほど|まで|の", cleaned)
        terms: list[str] = []
        for chunk in chunks:
            candidate = StoryEngine._clean_reply_fallback_anchor_term(chunk)
            if candidate and candidate not in terms:
                terms.append(candidate)
        return terms[:4]

    @staticmethod
    def _clean_reply_fallback_anchor_term(term: str) -> str:
        candidate = str(term or "").strip(" 「」『』（）()、。,.!?！？")
        if not candidate:
            return ""
        candidate = re.sub(
            r"(する|して|した|してる|される|られる|れる|です|ます|ない|たい|よう|ろ|よ|か)$",
            "",
            candidate,
        ).strip()
        stop_terms = {
            "その話",
            "話",
            "会話",
            "相手",
            "主張",
            "返答",
            "確認",
            "自然発生",
            "ここ",
            "そこ",
            "これ",
            "それ",
            "こと",
            "もの",
            "誰か",
            "何か",
            "今",
            "一つ",
            "少し",
        }
        if len(candidate) < 2 or candidate in stop_terms:
            return ""
        if set(candidate) <= {"…", ".", "・", "-"}:
            return ""
        if re.fullmatch(r"[ぁ-んー]+", candidate) and len(candidate) <= 3:
            return ""
        return candidate[:12]

    @staticmethod
    def _fallback_matches_focus_contract(
        first_sentence: str,
        *,
        reply_focus_contract: dict[str, Any],
    ) -> bool:
        focus_window = str(first_sentence).strip()
        focus_family = str(reply_focus_contract.get("focus_family") or "").strip()
        preferred_action = str(reply_focus_contract.get("preferred_action") or "").strip()
        required_tokens = [
            str(token).strip()
            for token in list(reply_focus_contract.get("required_tokens") or [])
            if str(token).strip()
        ]
        required_focus_cues = [
            str(token).strip()
            for token in list(reply_focus_contract.get("required_focus_cues") or [])
            if str(token).strip()
        ]
        focus_anchor_tokens = [
            str(token).strip()
            for token in list(reply_focus_contract.get("focus_anchor_tokens") or [])
            if str(token).strip()
        ]
        forbidden_focus_drift_tokens = {
            str(token).strip()
            for token in list(reply_focus_contract.get("forbidden_focus_drift_tokens") or [])
            if str(token).strip()
        }
        family_cue_seen = False
        if focus_family == "timing":
            family_cue_seen = any(
                token in focus_window
                for token in ("今", "先に", "あとで", "後で", "いつ", "時間", "タイミング")
            ) or any(
                phrase in focus_window
                for phrase in (
                    "始まる前",
                    "その前",
                    "ここで固定",
                    "ここで切る",
                    "ここで区切る",
                    "先に固定",
                    "先に区切る",
                    "あとで決める",
                    "先に通す",
                    "動く前",
                    "今のうち",
                    "一回止める",
                    "いったん止める",
                    "いったん区切る",
                    "順番は先に決め",
                    "順番を先に決め",
                    "ここで曖昧にしない",
                    "まだ濁す",
                    "タイミングだけ",
                )
            )
        elif focus_family == "decision_owner":
            family_cue_seen = any(
                token in focus_window
                for token in ("誰", "私が", "お前が", "任せ", "役", "決める役", "仕切", "こっち", "肩書", "権利", "主導権")
            )
        elif focus_family == "basis":
            family_cue_seen = any(
                token in focus_window for token in ("基準", "理由", "何で", "何の", "どの", "判断", "定義")
            ) or any(
                phrase in focus_window
                for phrase in ("何で測る", "何で決める", "どの基準", "何の基準", "何を基準", "判断基準", "どう定義")
            )
        elif focus_family == "proposal" or preferred_action == "propose":
            family_cue_seen = any(token in focus_window for token in ("なら", "じゃあ", "どう", "提案")) or any(
                phrase in focus_window
                for phrase in ("代わりに", "その前に", "順番を変", "順番を入れ替", "先にコード", "先にこれ", "まずこっち")
            )
        token_seen = not required_tokens or any(token in focus_window for token in required_tokens)
        cue_seen = not required_focus_cues or any(token in focus_window for token in required_focus_cues)
        anchor_seen = not focus_anchor_tokens or any(token in focus_window for token in focus_anchor_tokens)
        drift_seen = any(token in focus_window for token in forbidden_focus_drift_tokens)
        if focus_anchor_tokens and not anchor_seen and not family_cue_seen:
            return False
        if required_focus_cues and not cue_seen and not token_seen and not family_cue_seen:
            return False
        if drift_seen and not anchor_seen and not family_cue_seen:
            return False
        return token_seen or cue_seen or anchor_seen or family_cue_seen

    @staticmethod
    def _extract_fallback_first_sentence(text: str) -> str | None:
        sentences = [part.strip() for part in str(text).split("。") if part.strip()]
        if not sentences:
            return None
        return sentences[0]

    @staticmethod
    def _extract_fallback_second_sentence(text: str) -> str | None:
        sentences = [part.strip() for part in str(text).split("。") if part.strip()]
        if len(sentences) < 2:
            return None
        return sentences[1]

    @staticmethod
    def _fallback_signal_intent(signal_text: str) -> str:
        text = signal_text or ""
        if "張り合い" in text or "勝" in text or "負" in text:
            return "status_clash"
        if "食い違い" in text or "違" in text or "噛み合" in text:
            return "misunderstanding"
        if "本音" in text or "言い切られていない" in text or "曖昧" in text:
            return "near_reveal"
        if "回収" in text or "決め" in text or "片づ" in text:
            return "payoff_ready"
        if "見せ" in text or "自慢" in text or "優劣" in text or "ドヤ" in text:
            return "showoff"
        return "push_back"

    @staticmethod
    def _infer_fallback_voice_profile(ctx: CharacterContext) -> str:
        text = " ".join([ctx.tone, *ctx.speech_examples[:2]])
        if any(token in text for token in ("熱血", "直球", "だろ", "ぶつか", "足りない")):
            return "hot_blooded"
        if any(token in text for token in ("挑発", "嫌い", "煽", "悪ノリ", "主役")):
            return "provocative"
        if any(token in text for token in ("冷たい", "観察", "設計", "平然", "整理")):
            return "cool_observer"
        if any(token in text for token in ("丁寧", "落ち着", "ください", "と思います", "です")):
            return "polite"
        return "plain"

    def _build_fallback_variants(
        self,
        *,
        ctx: CharacterContext,
        msg_type: str,
        target_char_name: str | None,
        intent: str,
        objective_text: str,
        voice: str,
    ) -> list[str]:
        target = target_char_name or ctx.target_char_name or "その話"
        handoff_target = ctx.handoff_target_name or target_char_name or ""
        scene_fragment = self._fallback_scene_fragment(objective_text, intent)
        push_variants = self._fallback_push_variants(intent=intent, voice=voice)

        if msg_type == "reply":
            reactions = self._fallback_reply_reactions(
                target,
                voice,
                intent,
                target_last_utterance_excerpt=ctx.target_last_utterance_excerpt,
                reply_focus_text=ctx.reply_focus_text,
            )
            return [
                f"{reaction}。{push}"
                for reaction in reactions
                for push in push_variants[:2]
            ]

        if msg_type == "group":
            openers = self._fallback_group_openers(scene_fragment, voice)
            if handoff_target:
                handoffs = self._fallback_group_handoffs(handoff_target, voice, intent)
                return [
                    f"{opener}。{handoff}"
                    for opener in openers[:2]
                    for handoff in handoffs[:2]
                ]
            return [f"{opener}。{push}" for opener in openers[:2] for push in push_variants[:2]]

        observations = self._fallback_monologue_observations(ctx.current_place_name, voice)
        closers = self._fallback_monologue_closers(intent=intent, voice=voice)
        return [
            f"{observation}。{closer}"
            for observation in observations[:2]
            for closer in closers[:2]
        ]

    @staticmethod
    def _fallback_scene_fragment(objective_text: str, intent: str) -> str:
        if objective_text:
            return objective_text
        if intent == "status_clash":
            return "この場の空気が張ってきた"
        if intent == "misunderstanding":
            return "話がかみ合っていない"
        if intent == "near_reveal":
            return "何か言いかけてやめた感じがした"
        if intent == "payoff_ready":
            return "ここで流れが変わりそうだ"
        if intent == "showoff":
            return "見ていてほしい空気がある"
        return "この場の流れをつかんでいきたい"

    @staticmethod
    def _fallback_reply_reactions(
        target: str,
        voice: str,
        intent: str,
        *,
        target_last_utterance_excerpt: str | None = None,
        reply_focus_text: str | None = None,
    ) -> list[str]:
        focus_text = " ".join(
            part for part in [target_last_utterance_excerpt, reply_focus_text] if part
        )
        if "帰" in focus_text:
            if voice == "polite":
                return [f"{target}、まだ帰りません", f"{target}、もう少しいてください"]
            if voice == "hot_blooded":
                return [f"{target}、まだ帰らない", f"{target}、行かないで"]
            if voice == "provocative":
                return [f"{target}、まだ帰る気でいるの？", f"{target}、逃げないでよ"]
            if voice == "cool_observer":
                return [f"{target}、まだ帰らない", f"{target}、今じゃない"]
            return [f"{target}、まだ帰らない", f"{target}、待ってほしい"]
        if voice == "polite":
            if intent == "misunderstanding":
                return [f"{target}、それは違います", f"{target}、少し話がずれているかもしれません"]
            if intent == "status_clash":
                return [f"{target}、少し落ち着いてください", f"{target}、焦点を一つ示してください"]
            return [f"{target}、少し待ってください", f"{target}、もう少し具体的に言ってください"]
        if voice == "hot_blooded":
            if intent == "status_clash":
                return [f"{target}、そこで引く気はない", f"{target}、もっとちゃんと向き合ってくれ"]
            if intent == "showoff":
                return [f"{target}、見せるなら今だ", f"{target}、そのまま終わる気はない"]
            return [f"{target}、まだ終わってない", f"{target}、目をそらさず言ってくれ"]
        if voice == "provocative":
            if intent == "showoff":
                return [f"{target}、それで終わりの顔しないで", f"{target}、もっと見せてよ"]
            return [f"{target}、それで押し切れると思った？", f"{target}、まだ隠してる所を出してよ"]
        if voice == "cool_observer":
            if intent == "misunderstanding":
                return [f"{target}、話が食い違ってる", f"{target}、ずれた所を一度見たい"]
            return [f"{target}、その流れは雑だ", f"{target}、もう少し整理してほしい"]
        return [f"{target}、その話は聞いた", f"{target}、話を続けよう"]

    @staticmethod
    def _fallback_push_variants(*, intent: str, voice: str) -> list[str]:
        if intent == "status_clash":
            if voice == "polite":
                return ["少し話をしませんか", "伝えたいことがあります"]
            if voice == "hot_blooded":
                return ["ここで引けない", "もっとぶつかってきてほしい"]
            if voice == "provocative":
                return ["どっちか見せてよ", "引かないなら最後まで付き合うよ"]
            if voice == "cool_observer":
                return ["ここで話をしよう", "落ち着いて話しませんか"]
            return ["ここで向き合おう", "もう少し話そう"]
        if intent == "misunderstanding":
            if voice == "polite":
                return ["もう一度確認させてください", "少し話が噛み合っていないかもしれません"]
            if voice == "hot_blooded":
                return ["話が違うなら今ここで合わせる", "噛み合ってないなら向き合おう"]
            if voice == "provocative":
                return ["噛み合ってないなら、今ばらして", "ずれたまま終わるのはもったいないよ"]
            if voice == "cool_observer":
                return ["少し立ち止まって考えよう", "ちょっとずれてる気がする"]
            return ["もう少し話そう", "ここで立ち止まりたい"]
        if intent == "near_reveal":
            if voice == "polite":
                return ["もう少し残っている所を出してください", "まだ話せることがあれば言葉にしてください"]
            if voice == "hot_blooded":
                return ["もっと言ってほしい", "全部話してくれ"]
            if voice == "provocative":
                return ["その顔、まだ何かあるでしょ", "そこまで来たなら、濁さず言って"]
            if voice == "cool_observer":
                return ["残りを見れば分かる気がする", "もう少しだけ言葉にして"]
            return ["残っている所を出して", "続きを話してほしい"]
        if intent == "payoff_ready":
            if voice == "polite":
                return ["そろそろ話をまとめませんか", "ここで一度片づけましょう"]
            if voice == "hot_blooded":
                return ["ここで決める", "今ここで終わらせる"]
            if voice == "provocative":
                return ["回収するなら今だよ", "引き延ばすより、ここで決めよう"]
            if voice == "cool_observer":
                return ["流れはここで閉じる", "結論を出す段階だ"]
            return ["ここで決める", "今片づける"]
        if intent == "showoff":
            if voice == "polite":
                return ["見せるなら中身でお願いします", "ちゃんと見ています"]
            if voice == "hot_blooded":
                return ["見せるなら本気で来い", "張るなら最後まで見せろ"]
            if voice == "provocative":
                return ["ほら、もっと派手に見せて", "その自信、ここで証明してよ"]
            if voice == "cool_observer":
                return ["見せるなら結果で示せ", "誇示するなら最後まで通せ"]
            return ["それなら今ここで見せる", "自慢するなら結果を出す"]
        if voice == "polite":
            return ["少し話を続けませんか", "もう少しだけ言葉にしてください"]
        if voice == "hot_blooded":
            return ["このままでは終われない", "ここで火を消す気はない"]
        if voice == "provocative":
            return ["そのまま流すには惜しいよ", "ここで黙るには早いでしょ"]
        if voice == "cool_observer":
            return ["流れに乗る前に一度止まろう", "雑に閉じるにはまだ早い"]
        return ["もう少し話そう", "このまま終わりにしない"]

    @staticmethod
    def _fallback_group_openers(scene_fragment: str, voice: str) -> list[str]:
        if voice == "polite":
            return [scene_fragment, "少し話しませんか"]
        if voice == "hot_blooded":
            return [scene_fragment, "ここで流れを止めるな"]
        if voice == "provocative":
            return [scene_fragment, "まだ面白くなるところでしょ"]
        if voice == "cool_observer":
            return [scene_fragment, "少し状況を整理したい"]
        return [scene_fragment, "ここで話を前に進める"]

    @staticmethod
    def _fallback_group_handoffs(target: str, voice: str, intent: str) -> list[str]:
        if voice == "polite":
            return [f"{target}、見えたことを一つ教えてください", f"{target}、その場で拾ったことを言ってください"]
        if voice == "hot_blooded":
            return [f"{target}、お前が返せ", f"{target}、次はお前の番だ"]
        if voice == "provocative":
            return [f"{target}、黙るには早いでしょ", f"{target}、隠してる顔を見せてよ"]
        if voice == "cool_observer":
            return [f"{target}、見えた点を一つ出して", f"{target}、引っかかった点を言って"]
        if intent == "near_reveal":
            return [f"{target}、隠した部分に触れて", f"{target}、言いかけた所を出して"]
        return [f"{target}、その場で一つ拾って", f"{target}、見えたことを言って"]

    @staticmethod
    def _fallback_monologue_observations(place_name: str, voice: str) -> list[str]:
        if voice == "polite":
            return [f"{place_name}の空気が少し張っている", f"{place_name}が妙に静かだ"]
        if voice == "hot_blooded":
            return [f"{place_name}の空気がまだ熱い", f"{place_name}の音が止まってない"]
        if voice == "provocative":
            return [f"{place_name}がまだ退屈しきっていない", f"{place_name}の空気が妙にぬるい"]
        if voice == "cool_observer":
            return [f"{place_name}の空気がまだ揺れている", f"{place_name}の気配が散りきっていない"]
        return [f"{place_name}の空気がまだ落ち着かない", f"{place_name}の流れがまだ残っている"]

    @staticmethod
    def _fallback_monologue_closers(*, intent: str, voice: str) -> list[str]:
        base = StoryEngine._fallback_push_variants(intent=intent, voice=voice)
        if voice == "polite":
            return [base[0], "次は落ち着いて確かめます"]
        if voice == "hot_blooded":
            return [base[0], "次は真正面から返す"]
        if voice == "provocative":
            return [base[0], "次はもっと揺らしてみる"]
        if voice == "cool_observer":
            return [base[0], "次はしっかり見届ける"]
        return [base[0], "次はちゃんと確かめる"]

    async def _get_current_scene_objective_text(self, current_place: str) -> str | None:
        active_scenes = await self.db.get_active_story_scenes(self.story_id)
        for scene in active_scenes:
            if (
                str(scene.get("scene_type") or "") == "conversation"
                and str(scene.get("place_id") or "") == current_place
            ):
                objective = str(scene.get("objective") or "").strip()
                if objective:
                    return f"この場の争点: {objective}"
        return None

    def _select_dominant_prompt_signal(
        self,
        *,
        msg_type: str,
        relationship_mode_texts: list[str],
        canon_bit_texts: list[str],
        dramatic_pressure_texts: list[str],
        scene_objective_text: str | None,
    ) -> str | None:
        if msg_type == "reply":
            candidates = (
                relationship_mode_texts
                + dramatic_pressure_texts
                + canon_bit_texts
                + ([scene_objective_text] if scene_objective_text and scene_objective_text != "自然発生の会話" else [])
            )
        elif msg_type == "group":
            candidates = (
                dramatic_pressure_texts
                + ([scene_objective_text] if scene_objective_text and scene_objective_text != "自然発生の会話" else [])
                + relationship_mode_texts
                + canon_bit_texts
            )
        else:
            candidates = (
                canon_bit_texts
                + dramatic_pressure_texts
                + ([scene_objective_text] if scene_objective_text and scene_objective_text != "自然発生の会話" else [])
            )
        return candidates[0] if candidates else None

    @staticmethod
    def _refine_prompt_signal_payload(
        *,
        msg_type: str,
        relationship_mode_texts: list[str],
        canon_bit_texts: list[str],
        dramatic_pressure_texts: list[str],
        scene_objective_text: str | None,
        reply_focus_text: str | None,
        handoff_target_name: str | None,
    ) -> tuple[list[str], list[str], list[str], str | None]:
        scene_objective = (
            scene_objective_text
            if scene_objective_text and scene_objective_text != "自然発生の会話"
            else None
        )
        relationship_texts = relationship_mode_texts[:1]
        pressure_texts = dramatic_pressure_texts[:1]
        canon_texts = canon_bit_texts[:1]

        if msg_type == "reply":
            if relationship_texts or pressure_texts:
                canon_texts = []
            return relationship_texts, canon_texts, pressure_texts, scene_objective

        if msg_type == "group":
            if scene_objective and handoff_target_name:
                canon_texts = []
            return relationship_texts, canon_texts, pressure_texts, scene_objective

        return relationship_texts, canon_texts, pressure_texts[:1], scene_objective

    @staticmethod
    def _extract_target_last_utterance_excerpt(
        *,
        reply_to_log_id: int | None,
        recent_logs: list[dict[str, Any]],
    ) -> str | None:
        if not recent_logs:
            return None
        selected: dict[str, Any] | None = None
        if reply_to_log_id is not None:
            for log in recent_logs:
                if int(log.get("id") or 0) == int(reply_to_log_id):
                    selected = log
                    break
        if selected is None:
            selected = recent_logs[-1]
        message = str(selected.get("message") or "").strip()
        if not message:
            return None
        stripped = message.strip("「」『』 ")
        sentence_match = re.match(r"^(.+?[。！？!?])(?:\s|$)", stripped)
        if sentence_match is not None:
            first_sentence = sentence_match.group(1).strip()
        else:
            first_sentence = stripped
        return first_sentence or None

    @staticmethod
    def _select_reply_focus_text(
        *,
        target_excerpt: str | None,
        recent_dialogue_lines: list[str],
    ) -> str | None:
        text = (target_excerpt or "").strip()
        if not text and recent_dialogue_lines:
            text = str(recent_dialogue_lines[-1]).strip()
        if not text:
            return None
        if "？" in text or "?" in text:
            if "帰" in text:
                return "帰るかどうかの確認"
            if "いつ" in text or "何時" in text:
                return "いつ動くかの確認"
            if "誰" in text and "決" in text:
                return "誰が決めるかの確認"
            if any(token in text for token in ("基準", "何の", "どの", "見せ場")):
                return "判断基準の確認"
            if any(token in text for token in ("前提", "譲歩", "済む話")):
                return "相手の前提の確認"
            return None
        if any(token in text for token in ("いつ", "何時", "先に決め", "どの時点", "今決め")):
            return "いつ動くかの確認"
        if any(token in text for token in ("決める役", "誰が決め", "仕切", "順番の前")):
            return "誰が決めるかの確認"
        if any(token in text for token in ("基準", "見せ場", "何で決め", "どの線")):
            return "判断基準の確認"
        if any(token in text for token in ("前提", "譲歩", "済む話", "残りを言", "言い切れ")):
            return "相手の前提の確認"
        if any(token in text for token in ("違", "違う", "違って", "いや", "でも")):
            return "食い違っている論点"
        if any(token in text for token in ("しよう", "する", "やる", "行く", "来る")):
            return "相手の提案への返答"
        return None

    @staticmethod
    def _reply_focus_family(reply_focus_text: str | None) -> str | None:
        if not reply_focus_text:
            return None
        if "帰るかどうか" in reply_focus_text:
            return "yes_no"
        if "誰が決める" in reply_focus_text:
            return "decision_owner"
        if "判断基準" in reply_focus_text:
            return "basis"
        if "前提" in reply_focus_text:
            return "premise"
        if "いつ動く" in reply_focus_text:
            return "timing"
        if "提案" in reply_focus_text:
            return "proposal"
        if "確認" in reply_focus_text:
            return "confirm"
        return "generic"

    @classmethod
    def _build_reply_focus_contract(cls, reply_focus_text: str | None) -> dict[str, Any] | None:
        family = cls._reply_focus_family(reply_focus_text)
        if family in {None, "generic"}:
            return None
        contract: dict[str, Any] = {
            "focus_family": family,
            "required_tokens": [],
            "required_focus_cues": cls._reply_focus_required_cues(family),
            "focus_anchor_tokens": cls._reply_focus_anchor_tokens(family),
            "forbidden_focus_drift_tokens": cls._reply_focus_drift_tokens(family),
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        }
        if family == "yes_no":
            contract["required_tokens"] = ["帰", "残", "行"]
        elif family == "timing":
            contract["required_tokens"] = ["いつ", "今", "後", "あと", "明日", "決め"]
        elif family == "decision_owner":
            contract["required_tokens"] = ["誰", "決め", "役", "肩書", "主導権", "順番"]
        elif family == "basis":
            contract["required_tokens"] = ["基準", "何", "どの", "理由"]
        elif family == "premise":
            contract["required_tokens"] = ["前提", "済む", "残り", "譲歩"]
        elif family == "proposal":
            contract["required_tokens"] = ["なら", "じゃあ", "どう", "先に"]
            contract["preferred_action"] = "propose"
        elif family == "confirm":
            contract["required_tokens"] = ["確認", "本当", "つまり", "なの", "誰", "どの"]
        else:
            contract["required_tokens"] = []
            contract["must_answer_in_first_sentence"] = False
        return contract

    @staticmethod
    def _reply_focus_required_cues(family: str) -> list[str]:
        if family == "yes_no":
            return ["帰る", "帰らない", "残る", "行く", "行かない"]
        if family == "timing":
            return [
                "今",
                "先に",
                "あとで",
                "いつ",
                "そのあと",
                "タイミング",
                "タイミングだけ",
                "動く前",
                "今のうち",
            ]
        if family == "decision_owner":
            return [
                "誰が",
                "こっち",
                "私が",
                "お前が",
                "任せる",
                "決める役",
                "仕切る",
                "役を固定",
                "肩書き",
                "誰の肩書き",
                "権利",
                "主導権",
                "入れ替わり",
                "順番を通す",
            ]
        if family == "basis":
            return [
                "基準",
                "理由",
                "何で",
                "どこを見る",
                "何の",
                "どの",
                "何で測る",
                "どの基準",
                "何の基準",
                "何を基準",
                "判断基準",
                "判断",
                "定義",
                "どう定義",
            ]
        if family == "premise":
            return ["なら", "その前に", "まず", "前提"]
        if family == "proposal":
            return ["なら", "じゃあ", "先に", "どう"]
        if family == "confirm":
            return ["確認", "本当", "つまり"]
        return []

    @staticmethod
    def _reply_focus_anchor_tokens(family: str) -> list[str]:
        if family == "yes_no":
            return ["帰る", "帰らない", "残る", "行く"]
        if family == "timing":
            return ["今", "あとで", "先に", "タイミング", "タイミングだけ", "動く前", "今のうち"]
        if family == "decision_owner":
            return [
                "誰が",
                "こっち",
                "私が",
                "お前が",
                "決める役",
                "仕切る",
                "肩書き",
                "誰の肩書き",
                "権利",
                "主導権",
                "入れ替わり",
                "順番を通す",
            ]
        if family == "basis":
            return [
                "基準",
                "理由",
                "何で",
                "何の",
                "どの",
                "どの基準",
                "何の基準",
                "何を基準",
                "何で測る",
                "判断基準",
                "判断",
                "定義",
                "どう定義",
            ]
        if family == "premise":
            return ["前提", "まず", "なら"]
        if family == "proposal":
            return ["なら", "じゃあ"]
        if family == "confirm":
            return ["確認", "本当"]
        return []

    @staticmethod
    def _reply_focus_drift_tokens(family: str) -> list[str]:
        if family == "yes_no":
            return ["基準", "誰が", "前提"]
        if family == "timing":
            return ["基準", "誰が", "見せ場"]
        if family == "decision_owner":
            return ["基準", "見せ場", "前提"]
        if family == "basis":
            return ["誰が", "帰る", "前提"]
        if family == "premise":
            return ["基準", "誰が", "見せ場"]
        return []

    @classmethod
    def _build_reply_signal_contract(
        cls,
        *,
        dominant_signal_text: str | None,
        scene_objective_text: str | None,
    ) -> dict[str, Any] | None:
        signal_tokens = cls._extract_reply_signal_anchor_tokens(dominant_signal_text)
        objective_tokens = cls._extract_reply_signal_anchor_tokens(
            cls._clean_scene_objective_text(scene_objective_text)
        )
        if not signal_tokens and not objective_tokens:
            return None
        if signal_tokens and objective_tokens and signal_tokens == objective_tokens:
            signal_tokens = []
            visibility_mode = "objective_first"
        elif signal_tokens and objective_tokens:
            visibility_mode = "paired"
        elif signal_tokens:
            visibility_mode = "signal_first"
        else:
            visibility_mode = "objective_first"
        return {
            "signal_anchor_tokens": signal_tokens,
            "objective_anchor_tokens": objective_tokens,
            "visibility_mode": visibility_mode,
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話", "今の件", "そこ", "これ"],
        }

    @staticmethod
    def _clean_scene_objective_text(scene_objective_text: str | None) -> str:
        text = str(scene_objective_text or "").strip()
        if text.startswith("この場の争点:"):
            text = text.replace("この場の争点:", "", 1).strip()
        if text == "自然発生の会話":
            return ""
        return text

    @staticmethod
    def _extract_reply_signal_anchor_tokens(text: str | None) -> list[str]:
        cleaned = str(text or "").strip()
        if not cleaned:
            return []
        cleaned = StoryEngine._clean_scene_objective_text(cleaned)
        if not cleaned:
            return []
        cue_tokens = [
            "張り合い",
            "勝ち負け",
            "順番",
            "主導権",
            "決める",
            "決着",
            "見せ場",
            "勝負",
            "本音",
            "秘密",
            "基準",
            "前提",
            "役",
            "時間",
            "保留",
            "対立",
            "食い違い",
            "疑い",
            "流れ",
            "回収",
            "入れ替わり",
        ]
        found = [token for token in cue_tokens if token in cleaned]
        if found:
            return found[:3]
        # 説明テンプレや人物紹介風の文から壊れた anchor を作らない。
        weak_signal_patterns = (
            "場が動きやすい",
            "起きやすい",
            "しやすい",
            "触れると",
            "言い切られていない",
        )
        if any(pattern in cleaned for pattern in weak_signal_patterns):
            return []
        derived_chunks: list[str] = []
        stop_chunks = {
            "この場",
            "争点",
            "その場",
            "ここ",
            "こと",
            "もの",
            "ため",
            "相手",
            "誰か",
        }
        semantic_stems = (
            "張り合",
            "勝ち負け",
            "順番",
            "主導権",
            "決め",
            "決着",
            "見せ場",
            "勝負",
            "本音",
            "秘密",
            "基準",
            "前提",
            "役",
            "時間",
            "保留",
            "対立",
            "食い違",
            "疑",
            "流れ",
            "回収",
            "入れ替",
        )
        raw_chunks = re.split(r"[、。,\s]+|を|が|は|に|で|と|へ|から|より|だけ|ほど|まで|の", cleaned)
        for chunk in raw_chunks:
            candidate = chunk.strip()
            if not candidate:
                continue
            candidate = re.sub(
                r"(する|して|した|してる|される|られる|れる|です|ます|ない|たい|よう)$",
                "",
                candidate,
            )
            candidate = candidate.strip()
            if len(candidate) < 2 or candidate in stop_chunks:
                continue
            if not any(stem in candidate for stem in semantic_stems):
                continue
            if candidate not in derived_chunks:
                derived_chunks.append(candidate)
        if derived_chunks:
            return derived_chunks[:3]
        return []

    @classmethod
    def _build_reply_surface_contract(
        cls,
        *,
        reply_focus_text: str | None,
        voice_anchor_text: str | None,
        recent_self_messages: list[str] | None,
    ) -> dict[str, Any] | None:
        family = cls._reply_focus_family(reply_focus_text)
        if family is None and not voice_anchor_text:
            return None
        surface_mode = "answer_first"
        if family in {"basis", "premise"}:
            surface_mode = "question_push"
        elif family == "proposal":
            surface_mode = "counter_proposal"
        return {
            "focus_family": family or "generic",
            "surface_mode": surface_mode,
            "forbidden_tail_patterns": [
                "今の流れをここで返す",
                "今ここでちゃんと返す",
                "今の流れをそのまま流したくない",
                "確認します",
                "続けます",
                "返します",
            ],
            "preferred_voice_cues": cls._preferred_voice_cues(voice_anchor_text),
            "must_vary_from_recent_self": True,
            "recent_self_endings": cls._recent_reply_endings(recent_self_messages or []),
        }

    @staticmethod
    def _build_reply_variety_contract(
        *,
        reply_focus_text: str | None,
        recent_self_messages: list[str] | None,
    ) -> dict[str, Any] | None:
        family = StoryEngine._reply_focus_family(reply_focus_text) or "generic"
        second_beat_mode = "press"
        if family in {"basis", "premise"}:
            second_beat_mode = "condition"
        elif family in {"proposal", "generic"}:
            second_beat_mode = "redirect"
        response_shape = f"answer_then_{second_beat_mode}"
        recent_second_beat_history = StoryEngine._recent_reply_second_beat_modes(recent_self_messages or [])
        forbidden_recent_second_beats = [
            mode
            for mode in dict.fromkeys(recent_second_beat_history)
            if mode and recent_second_beat_history.count(mode) >= 2
        ]
        return {
            "focus_family": family,
            "response_shape": response_shape,
            "second_beat_mode": second_beat_mode,
            "forbidden_recent_endings": StoryEngine._recent_reply_endings(recent_self_messages or []),
            "forbidden_recent_openings": StoryEngine._recent_reply_openings(recent_self_messages or []),
            "recent_second_beat_history": recent_second_beat_history,
            "forbidden_recent_second_beats": forbidden_recent_second_beats,
            "preferred_move_tokens": StoryEngine._preferred_variety_move_tokens(second_beat_mode),
        }

    @staticmethod
    def _build_reply_dramatic_contract(
        *,
        reply_focus_text: str | None,
        reply_signal_contract: dict[str, Any] | None,
        reply_variety_contract: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        family = StoryEngine._reply_focus_family(reply_focus_text) or "generic"
        if family == "yes_no":
            move_mode = "counter"
        elif family in {"decision_owner", "basis"}:
            move_mode = "condition"
        elif family in {"timing", "confirm"}:
            move_mode = "probe"
        elif family == "premise":
            move_mode = "claim"
        elif family == "proposal":
            move_mode = "counter"
        else:
            move_mode = "claim"
        signal_tokens = [
            str(token).strip()
            for token in list((reply_signal_contract or {}).get("signal_anchor_tokens") or [])
            if str(token).strip()
        ]
        objective_tokens = [
            str(token).strip()
            for token in list((reply_signal_contract or {}).get("objective_anchor_tokens") or [])
            if str(token).strip()
        ]
        pressure_anchor = (objective_tokens or signal_tokens or ["そこ"])[0]
        return {
            "move_mode": move_mode,
            "semantic_move_family": move_mode,
            "pressure_anchor_tokens": [pressure_anchor],
            "allowed_secondary_modes": StoryEngine._allowed_dramatic_secondary_modes(move_mode),
            "required_move_tokens": StoryEngine._required_dramatic_move_tokens(
                move_mode,
                reply_variety_contract=reply_variety_contract,
            ),
            "required_semantic_cues": StoryEngine._required_dramatic_semantic_cues(move_mode),
            "forbidden_semantic_drifts": StoryEngine._forbidden_dramatic_semantic_drifts(move_mode),
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": StoryEngine._forbidden_soft_landings(pressure_anchor),
        }

    @staticmethod
    def _build_reply_blandness_contract(
        *,
        reply_focus_text: str | None,
        reply_variety_contract: dict[str, Any] | None,
        reply_dramatic_contract: dict[str, Any] | None,
        recent_self_messages: list[str] | None,
    ) -> dict[str, Any] | None:
        family = StoryEngine._reply_focus_family(reply_focus_text) or "generic"
        if family in {"yes_no", "decision_owner", "proposal"}:
            primary_shape = "answer_then_counter"
        elif family in {"basis", "premise"}:
            primary_shape = "answer_then_condition"
        else:
            primary_shape = "answer_then_probe"
        secondary_shape = StoryEngine._reply_blandness_secondary_shape(
            primary_shape=primary_shape,
            reply_dramatic_contract=reply_dramatic_contract,
        )
        response_shape = str((reply_variety_contract or {}).get("response_shape") or "").strip()
        recent_shape_history = [response_shape] * len(recent_self_messages or []) if response_shape else []
        return {
            "primary_shape": primary_shape,
            "secondary_shape": secondary_shape,
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": [
                str(token).strip()
                for token in list((reply_dramatic_contract or {}).get("forbidden_soft_landings") or [])
                if str(token).strip()
            ],
            "recent_shape_history": recent_shape_history,
        }

    def _build_reply_shape_contract(
        self,
        *,
        reply_focus_text: str | None,
        reply_signal_contract: dict[str, Any] | None,
        reply_variety_contract: dict[str, Any] | None,
        reply_dramatic_contract: dict[str, Any] | None,
        recent_self_messages: list[str] | None,
        dominant_signal_text: str | None,
        scene_objective_text: str | None,
        relationship_mode_texts: list[str] | None,
        dramatic_pressure_texts: list[str] | None,
    ) -> dict[str, Any] | None:
        family = self._reply_focus_family(reply_focus_text) or "generic"
        if family in {"yes_no", "decision_owner", "proposal"}:
            primary_shape = "answer_then_counter"
        elif family in {"basis", "premise"}:
            primary_shape = "answer_then_condition"
        else:
            primary_shape = "answer_then_probe"
        secondary_shape = self._reply_blandness_secondary_shape(
            primary_shape=primary_shape,
            reply_dramatic_contract=reply_dramatic_contract,
        )
        response_shape = str((reply_variety_contract or {}).get("response_shape") or "").strip()
        recent_shape_history = [response_shape] * len(recent_self_messages or []) if response_shape else []
        story_pressure_tokens = self._extract_story_pressure_tokens(
            dominant_signal_text=dominant_signal_text,
            scene_objective_text=scene_objective_text,
            relationship_mode_texts=relationship_mode_texts or [],
            dramatic_pressure_texts=dramatic_pressure_texts or [],
        )
        return {
            "primary_shape": primary_shape,
            "secondary_shape": secondary_shape,
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": [
                str(token).strip()
                for token in list((reply_dramatic_contract or {}).get("forbidden_soft_landings") or [])
                if str(token).strip()
            ],
            "recent_shape_history": recent_shape_history,
            "story_pressure_tokens": story_pressure_tokens,
        }

    @staticmethod
    def _build_reply_quality_contract(
        *,
        dominant_signal_text: str | None,
        scene_objective_text: str | None,
        reply_shape_contract: dict[str, Any] | None,
        reply_variety_contract: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not reply_shape_contract and not reply_variety_contract:
            return None
        if dominant_signal_text:
            required_story_flavor_role = "pressure"
        elif scene_objective_text:
            required_story_flavor_role = "stakes"
        else:
            required_story_flavor_role = "positioning"
        return {
            "primary_shape": str((reply_shape_contract or {}).get("primary_shape") or "").strip(),
            "second_beat_mode": str((reply_variety_contract or {}).get("second_beat_mode") or "").strip(),
            "story_pressure_tokens": [
                str(token).strip()
                for token in list((reply_shape_contract or {}).get("story_pressure_tokens") or [])
                if str(token).strip()
            ],
            "forbidden_soft_landings": [
                str(token).strip()
                for token in list((reply_shape_contract or {}).get("forbidden_soft_landings") or [])
                if str(token).strip()
            ],
            "recent_shape_history": [
                str(token).strip()
                for token in list((reply_shape_contract or {}).get("recent_shape_history") or [])
                if str(token).strip()
            ],
            "recent_second_beat_history": [
                str(token).strip()
                for token in list((reply_variety_contract or {}).get("recent_second_beat_history") or [])
                if str(token).strip()
            ],
            "required_story_flavor_role": required_story_flavor_role,
        }

    @staticmethod
    def _build_reply_story_quality_contract(
        *,
        dominant_signal_text: str | None,
        scene_objective_text: str | None,
        reply_quality_contract: dict[str, Any] | None,
        story_pressure_cue: str | None,
    ) -> dict[str, Any] | None:
        if not reply_quality_contract and not story_pressure_cue:
            return None
        required_story_flavor_role = ""
        if dominant_signal_text:
            required_story_flavor_role = "pressure"
        elif scene_objective_text:
            required_story_flavor_role = "stakes"
        elif reply_quality_contract:
            required_story_flavor_role = str(
                reply_quality_contract.get("required_story_flavor_role") or ""
            ).strip()
        return {
            "primary_shape": str((reply_quality_contract or {}).get("primary_shape") or "").strip(),
            "second_beat_mode": str((reply_quality_contract or {}).get("second_beat_mode") or "").strip(),
            "story_pressure_tokens": [
                str(token).strip()
                for token in list((reply_quality_contract or {}).get("story_pressure_tokens") or [])
                if str(token).strip()
            ],
            "forbidden_soft_landings": [
                str(token).strip()
                for token in list((reply_quality_contract or {}).get("forbidden_soft_landings") or [])
                if str(token).strip()
            ],
            "recent_shape_history": [
                str(token).strip()
                for token in list((reply_quality_contract or {}).get("recent_shape_history") or [])
                if str(token).strip()
            ],
            "recent_second_beat_history": [
                str(token).strip()
                for token in list((reply_quality_contract or {}).get("recent_second_beat_history") or [])
                if str(token).strip()
            ],
            "required_story_flavor_role": required_story_flavor_role,
            "story_pressure_cue_text": str(story_pressure_cue or "").strip(),
        }

    @staticmethod
    def _build_reply_residual_quality_contract(
        *,
        reply_story_quality_contract: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not reply_story_quality_contract:
            return None
        return {
            "primary_shape": str((reply_story_quality_contract or {}).get("primary_shape") or "").strip(),
            "second_beat_mode": str((reply_story_quality_contract or {}).get("second_beat_mode") or "").strip(),
            "story_pressure_tokens": [
                str(token).strip()
                for token in list((reply_story_quality_contract or {}).get("story_pressure_tokens") or [])
                if str(token).strip()
            ],
            "forbidden_soft_landings": [
                str(token).strip()
                for token in list((reply_story_quality_contract or {}).get("forbidden_soft_landings") or [])
                if str(token).strip()
            ],
            "recent_shape_history": [
                str(token).strip()
                for token in list((reply_story_quality_contract or {}).get("recent_shape_history") or [])
                if str(token).strip()
            ],
            "recent_second_beat_history": [
                str(token).strip()
                for token in list((reply_story_quality_contract or {}).get("recent_second_beat_history") or [])
                if str(token).strip()
            ],
            "required_story_flavor_role": str(
                (reply_story_quality_contract or {}).get("required_story_flavor_role") or ""
            ).strip(),
            "story_pressure_cue_text": str(
                (reply_story_quality_contract or {}).get("story_pressure_cue_text") or ""
            ).strip(),
            "residual_block_threshold": "shape+second_beat+pressure_shift_or_story_flavor",
        }

    def _build_story_pressure_cue(
        self,
        *,
        msg_type: str,
        dominant_signal_text: str | None,
        scene_objective_text: str | None,
        relationship_mode_texts: list[str],
        dramatic_pressure_texts: list[str],
    ) -> str | None:
        if msg_type != "reply":
            return None
        tokens = self._extract_story_pressure_tokens(
            dominant_signal_text=dominant_signal_text,
            scene_objective_text=scene_objective_text,
            relationship_mode_texts=relationship_mode_texts,
            dramatic_pressure_texts=dramatic_pressure_texts,
        )
        if not tokens:
            return None
        if self.story_id == "ankoku_gakuen":
            # 「秘密・本音」はナラティブ語彙として LLM へ渡してよい
            if any(token in tokens for token in ("秘密", "本音")):
                return "誰が何を知っていて、何を隠しているか、自然に触れる"
            # 「主導権・順番・見せ場」等のバックエンド制御語彙は LLM に渡さない
            return None
        # 汎用 fallback もメタ語彙を使わない
        return None

    def _extract_story_pressure_tokens(
        self,
        *,
        dominant_signal_text: str | None,
        scene_objective_text: str | None,
        relationship_mode_texts: list[str],
        dramatic_pressure_texts: list[str],
    ) -> list[str]:
        candidates: list[str] = []
        candidates.extend(self._extract_reply_signal_anchor_tokens(dominant_signal_text))
        candidates.extend(
            self._extract_reply_signal_anchor_tokens(
                self._clean_scene_objective_text(scene_objective_text)
            )
        )
        cue_tokens = (
            "張り合い",
            "見せ場",
            "勝負",
            "優劣",
            "本音",
            "秘密",
            "対立",
            "役",
            "時間",
        )
        source_text = " ".join([*relationship_mode_texts, *dramatic_pressure_texts])
        for token in cue_tokens:
            if token in source_text:
                candidates.append(token)
        for pattern_type in self._intent_profile.preferred_pattern_types:
            if pattern_type == "status_clash":
                candidates.extend(["張り合い"])
            elif pattern_type == "small_win_loss":
                candidates.append("勝ち負け")
            elif pattern_type == "bluff_or_showoff":
                candidates.append("見せ場")
        unique_tokens = list(dict.fromkeys(token for token in candidates if token))
        return unique_tokens[:3]

    @staticmethod
    def _preferred_variety_move_tokens(second_beat_mode: str) -> list[str]:
        if second_beat_mode == "condition":
            return ["なら", "そのあと", "その後"]
        if second_beat_mode == "redirect":
            return ["じゃあ", "でも", "それより"]
        return ["もっと", "まだ", "だから"]

    @staticmethod
    def _allowed_dramatic_secondary_modes(move_mode: str) -> list[str]:
        if move_mode == "counter":
            return ["probe"]
        if move_mode == "condition":
            return ["claim"]
        if move_mode == "probe":
            return ["claim"]
        if move_mode == "claim":
            return ["condition"]
        return []

    @staticmethod
    def _reply_blandness_secondary_shape(
        *,
        primary_shape: str,
        reply_dramatic_contract: dict[str, Any] | None,
    ) -> str:
        mode_to_shape = {
            "counter": "answer_then_counter",
            "condition": "answer_then_condition",
            "probe": "answer_then_probe",
            "claim": "answer_then_condition",
        }
        secondary_modes = [
            str(token).strip()
            for token in list((reply_dramatic_contract or {}).get("allowed_secondary_modes") or [])
            if str(token).strip()
        ]
        for mode in secondary_modes:
            shape = mode_to_shape.get(mode)
            if shape and shape != primary_shape:
                return shape
        if primary_shape != "answer_then_condition":
            return "answer_then_condition"
        return "answer_then_probe"

    @staticmethod
    def _required_dramatic_move_tokens(
        move_mode: str,
        *,
        reply_variety_contract: dict[str, Any] | None,
    ) -> list[str]:
        preferred = [
            str(token).strip()
            for token in list((reply_variety_contract or {}).get("preferred_move_tokens") or [])
            if str(token).strip()
        ]
        if move_mode == "condition":
            return preferred or ["なら", "そのあと", "条件"]
        if move_mode == "probe":
            return ["誰が", "何で", "どこに"]
        if move_mode == "claim":
            return ["今", "決める", "通す"]
        return preferred or ["先に", "言って", "出せ"]

    @staticmethod
    def _required_dramatic_semantic_cues(move_mode: str) -> list[str]:
        if move_mode == "counter":
            return ["違う", "先に", "まず"]
        if move_mode == "condition":
            return ["なら", "条件", "そのあと"]
        if move_mode == "probe":
            return ["誰が", "何で", "どこに"]
        if move_mode == "claim":
            return ["今決める", "譲らない", "私がやる"]
        return ["先に", "決める"]

    @staticmethod
    def _forbidden_dramatic_semantic_drifts(move_mode: str) -> list[str]:
        if move_mode == "counter":
            return ["見ておく", "そのあとでいい", "あとで片づける"]
        if move_mode == "condition":
            return ["任せる", "見ておく", "あとでいい"]
        if move_mode == "probe":
            return ["見ておく", "そのうち", "あとでいい"]
        if move_mode == "claim":
            return ["誰かが決める", "見ておく", "そのうち"]
        return ["見ておく", "あとでいい"]

    @staticmethod
    def _forbidden_soft_landings(pressure_anchor: str) -> list[str]:
        return [
            "順番はそのあとでいい",
            "あとで片づける",
            "曖昧にしない",
            f"{pressure_anchor}はそのあとでいい",
        ]

    @staticmethod
    def _fallback_matches_dramatic_semantic_family(
        second_sentence: str,
        *,
        semantic_family: str,
        required_semantic_cues: list[str],
    ) -> bool:
        if required_semantic_cues and any(token in second_sentence for token in required_semantic_cues):
            return True
        if semantic_family == "counter":
            return any(token in second_sentence for token in ("違う", "先に", "まず", "出せ"))
        if semantic_family == "condition":
            return any(token in second_sentence for token in ("なら", "条件", "そのあと"))
        if semantic_family == "probe":
            return any(
                token in second_sentence
                for token in (
                    "なぜ",
                    "どこで",
                    "どこに",
                    "何を",
                    "何で",
                    "誰が",
                    "いつ",
                    "どこまで",
                    "見る",
                    "見て",
                    "確かめ",
                    "拾う",
                    "触れる",
                )
            )
        if semantic_family == "claim":
            return any(
                token in second_sentence
                for token in ("今決める", "譲らない", "私がやる", "通す", "曖昧にしない", "終わらせない", "流さない")
            )
        return True

    @staticmethod
    def _story_pressure_move_phrase(
        pressure_token: str,
        *,
        move_mode: str,
    ) -> str:
        token = str(pressure_token or "").strip() or "そこ"
        if move_mode == "counter":
            mapping = {
                "主導権": "こっちで進める",
                "順番": "こっちから話す",
                "見せ場": "ここで言う",
                "基準": "自分で決める",
                "回収": "今ここで動く",
                "決着": "今ここでつける",
                "勝負": "今ここでつける",
                "勝ち負け": "今ここで決める",
                "優劣": "今ここで決める",
                "本音": "ここで言う",
                "秘密": "ここでは言わない",
                "役": "自分が動く",
                "時間": "今ここで決める",
                "対立": "ここで言う",
                "食い違い": "ここで話す",
                "流れ": "ここで変える",
            }
            return mapping.get(token, "ここで言う")
        if move_mode == "condition":
            mapping = {
                "主導権": "進めるなら今ここで動く",
                "順番": "今ここで決める",
                "見せ場": "今ここで見せる",
                "基準": "まず動いてみる",
                "回収": "今動く",
                "決着": "今ここでつける",
                "勝負": "今ここでつける",
                "勝ち負け": "今ここで決める",
                "優劣": "今ここで動く",
                "本音": "ここで言う",
                "秘密": "ここでは隠さない",
                "役": "自分が動く",
                "時間": "今動く",
                "対立": "今ここで話す",
                "食い違い": "今ここから話す",
                "流れ": "切り替えるなら今ここで",
            }
            return mapping.get(token, "今ここで決める")
        if move_mode == "probe":
            mapping = {
                "主導権": "こっちから動く",
                "順番": "こっちから話す",
                "見せ場": "今の声を逃さない",
                "基準": "理由をはっきりさせる",
                "回収": "さっきの話に戻す",
                "決着": "ここで終わらせない",
                "勝負": "ここで引かない",
                "勝ち負け": "ここで引かない",
                "優劣": "その線引きには乗らない",
                "本音": "まだ隠していることがある",
                "秘密": "その話はまだ伏せておく",
                "役": "こっちから動く",
                "時間": "今は逃がさない",
                "対立": "そのままにはしない",
                "食い違い": "そこは話がずれている",
                "流れ": "さっきの話に戻す",
            }
            if token in mapping:
                return mapping[token]
            if token and token not in {"そこ", "それ", "unknown"}:
                return f"{token}はまだ終わらせない"
            return "話を少し戻したい"
        mapping = {
            "主導権": "こっちで進める",
            "順番": "こっちから話す",
            "見せ場": "今ここで言う",
            "基準": "今ここで動く",
            "回収": "今ここで進める",
            "決着": "今ここでつける",
            "勝負": "今ここでつける",
            "勝ち負け": "今ここで決める",
            "優劣": "今ここで決める",
            "本音": "本音はここで隠さない",
            "秘密": "秘密はここで伏せない",
            "役": "誰が動くかここで決める",
            "時間": "今ここで切る",
            "対立": "ここで話す",
            "食い違い": "ここで話す",
            "流れ": "ここで変える",
        }
        return mapping.get(token, "今ここで動く")

    @staticmethod
    def _apply_reply_signal_visibility_to_fallback(
        text: str,
        *,
        reply_signal_contract: dict[str, Any] | None,
    ) -> str:
        if not reply_signal_contract:
            return text
        sentences = [part.strip() for part in str(text).split("。") if part.strip()]
        if not sentences:
            return text
        first_sentence = sentences[0]
        signal_tokens = [
            str(token).strip()
            for token in list(reply_signal_contract.get("signal_anchor_tokens") or [])
            if str(token).strip()
        ]
        objective_tokens = [
            str(token).strip()
            for token in list(reply_signal_contract.get("objective_anchor_tokens") or [])
            if str(token).strip()
        ]
        visibility_mode = str(reply_signal_contract.get("visibility_mode") or "").strip()
        body = " ".join(sentences[:2])
        signal_visible = any(token in body for token in signal_tokens)
        objective_visible = any(token in body for token in objective_tokens)
        second_sentence = sentences[1] if len(sentences) > 1 else ""
        if visibility_mode == "signal_first" and signal_tokens and not signal_visible:
            merged = StoryEngine._merge_signal_tokens_into_fallback_second_sentence(
                second_sentence,
                lead=f"{signal_tokens[0]}は",
                fallback=f"{signal_tokens[0]}をもう少し具体的に聞きたい",
            )
            return f"{first_sentence}。{merged}"
        if visibility_mode == "objective_first" and objective_tokens and not objective_visible:
            merged = StoryEngine._merge_signal_tokens_into_fallback_second_sentence(
                second_sentence,
                lead=f"{objective_tokens[0]}は",
                fallback=f"{objective_tokens[0]}から話を戻したい",
            )
            return f"{first_sentence}。{merged}"
        if visibility_mode == "paired" and signal_tokens and objective_tokens and (not signal_visible or not objective_visible):
            merged = StoryEngine._merge_signal_tokens_into_fallback_second_sentence(
                second_sentence,
                lead=f"{signal_tokens[0]}も{objective_tokens[0]}も",
                fallback=f"{signal_tokens[0]}も{objective_tokens[0]}も具体的に返したい",
            )
            return f"{first_sentence}。{merged}"
        return text

    @staticmethod
    def _build_reply_move_plan(
        *,
        ctx: CharacterContext,
        reply_signal_contract: dict[str, Any] | None,
        reply_dramatic_contract: dict[str, Any] | None,
        reply_shape_contract: dict[str, Any] | None,
        recent_self_messages: list[str],
        recent_scene_messages: list[str] | None = None,
    ) -> ReplyMovePlan:
        anchor_terms: list[str] = []
        for contract, key in (
            (reply_signal_contract, "objective_anchor_tokens"),
            (reply_signal_contract, "signal_anchor_tokens"),
            (reply_shape_contract, "story_pressure_tokens"),
        ):
            if not contract:
                continue
            for token in list(contract.get(key) or []):
                normalized = str(token).strip()
                if normalized and normalized not in anchor_terms:
                    anchor_terms.append(normalized)
        progression_goal = str((reply_dramatic_contract or {}).get("move_mode") or "").strip() or "probe"
        reaction_goal = StoryEngine._reply_focus_family(ctx.reply_focus_text) or "direct_reply"
        recent_signatures = tuple(
            signature
            for signature in (
                StoryEngine._fallback_move_signature(message)
                for message in [*recent_self_messages, *(recent_scene_messages or [])]
            )
            if signature
        )
        return ReplyMovePlan(
            reaction_goal=reaction_goal,
            progression_goal=progression_goal,
            scene_anchor_terms=tuple(anchor_terms),
            allow_second_sentence=bool(anchor_terms),
            recent_move_signatures=recent_signatures,
        )

    @staticmethod
    def _fallback_move_signature(text: str) -> str | None:
        second_sentence = StoryEngine._extract_fallback_second_sentence(text)
        if not second_sentence:
            return None
        if any(token in second_sentence for token in ("流さない", "曖昧にしない")):
            return "generic_refusal"
        if any(
            token in second_sentence
            for token in ("話を少し戻したい", "話を戻したい", "さっきの話に戻す")
        ):
            return "generic_recenter"
        if any(token in second_sentence for token in ("なぜ", "どこ", "何", "誰", "いつ", "聞きたい")):
            return "probe"
        if any(token in second_sentence for token in ("なら", "条件", "先に")):
            return "condition"
        if any(token in second_sentence for token in ("違う", "決める", "返す")):
            return "counter"
        return "claim"

    @staticmethod
    def _apply_reply_move_plan_to_fallback(text: str, *, plan: ReplyMovePlan) -> str:
        sentences = [part.strip() for part in str(text).split("。") if part.strip()]
        if not sentences:
            return text
        first_sentence = sentences[0]
        if len(sentences) < 2:
            return first_sentence
        second_sentence = sentences[1]
        signature = StoryEngine._fallback_move_signature(f"{first_sentence}。{second_sentence}")
        needs_rewrite = (
            signature in {"generic_refusal", "generic_recenter"}
            or (signature is not None and signature in plan.recent_move_signatures)
        )
        if not needs_rewrite:
            return f"{first_sentence}。{second_sentence}"
        if not plan.allow_second_sentence:
            return first_sentence
        replacement = StoryEngine._reply_move_plan_second_sentence(plan)
        if replacement is None:
            return first_sentence
        return f"{first_sentence}。{replacement}"

    @staticmethod
    def _reply_move_plan_second_sentence(plan: ReplyMovePlan) -> str | None:
        if not plan.scene_anchor_terms:
            return None
        anchor = plan.scene_anchor_terms[0]
        if plan.progression_goal == "condition":
            return f"{anchor}を先に決めたい"
        if plan.progression_goal == "counter":
            return f"{anchor}から返す"
        if plan.progression_goal == "claim":
            return f"{anchor}を今の話に戻す"
        return f"{anchor}をもう少し聞きたい"

    @staticmethod
    def _merge_signal_tokens_into_fallback_second_sentence(
        second_sentence: str,
        *,
        lead: str,
        fallback: str,
    ) -> str:
        cleaned = str(second_sentence).strip().rstrip("。")
        if not cleaned:
            return fallback
        if cleaned.startswith(lead):
            return cleaned
        if len(cleaned) <= 12 and any(token in cleaned for token in ("先に", "言って", "出せ", "なら", "なぜ", "どう", "？", "?")):
            return f"{lead}{cleaned}"
        return fallback

    @staticmethod
    def _apply_reply_dramatic_move_to_fallback(
        text: str,
        *,
        reply_dramatic_contract: dict[str, Any] | None,
    ) -> str:
        if not reply_dramatic_contract:
            return text
        sentences = [part.strip() for part in str(text).split("。") if part.strip()]
        if not sentences:
            return text
        first_sentence = sentences[0]
        second_sentence = sentences[1] if len(sentences) > 1 else ""
        forbidden_soft_landings = [
            str(token).strip()
            for token in list(reply_dramatic_contract.get("forbidden_soft_landings") or [])
            if str(token).strip()
        ]
        required_move_tokens = [
            str(token).strip()
            for token in list(reply_dramatic_contract.get("required_move_tokens") or [])
            if str(token).strip()
        ]
        required_semantic_cues = [
            str(token).strip()
            for token in list(reply_dramatic_contract.get("required_semantic_cues") or [])
            if str(token).strip()
        ]
        pressure_anchor_tokens = [
            str(token).strip()
            for token in list(reply_dramatic_contract.get("pressure_anchor_tokens") or [])
            if str(token).strip()
        ]
        forbidden_semantic_drifts = [
            str(token).strip()
            for token in list(reply_dramatic_contract.get("forbidden_semantic_drifts") or [])
            if str(token).strip()
        ]
        move_mode = str(reply_dramatic_contract.get("move_mode") or "").strip()
        if (
            second_sentence
            and not any(token in second_sentence for token in forbidden_soft_landings)
            and not any(token in second_sentence for token in forbidden_semantic_drifts)
            and any(token in second_sentence for token in required_move_tokens)
            and any(token in second_sentence for token in required_semantic_cues)
            and (not pressure_anchor_tokens or any(token in second_sentence for token in pressure_anchor_tokens))
        ):
            return text
        replacement = StoryEngine._fallback_dramatic_second_sentence(
            move_mode,
            required_move_tokens=required_move_tokens,
            pressure_anchor_tokens=pressure_anchor_tokens,
        )
        if replacement is None:
            return first_sentence
        return f"{first_sentence}。{replacement}"

    @staticmethod
    def _apply_reply_blandness_to_fallback(
        text: str,
        *,
        reply_blandness_contract: dict[str, Any] | None,
    ) -> str:
        if not reply_blandness_contract:
            return text
        sentences = [part.strip() for part in str(text).split("。") if part.strip()]
        if not sentences:
            return text
        first_sentence = sentences[0]
        second_sentence = sentences[1] if len(sentences) > 1 else ""
        primary_shape = str(reply_blandness_contract.get("primary_shape") or "").strip()
        secondary_shape = str(reply_blandness_contract.get("secondary_shape") or "").strip()
        recent_shape_history = {
            str(token).strip()
            for token in list(reply_blandness_contract.get("recent_shape_history") or [])
            if str(token).strip()
        }
        preferred_shape = secondary_shape if primary_shape and primary_shape in recent_shape_history else primary_shape
        if second_sentence and StoryEngine._fallback_matches_blandness_shape(second_sentence, preferred_shape):
            return text
        replacement = StoryEngine._fallback_blandness_second_sentence(
            preferred_shape or primary_shape,
        )
        if replacement is None:
            return text
        return f"{first_sentence}。{replacement}"

    @staticmethod
    def _apply_reply_shape_to_fallback(
        text: str,
        *,
        reply_shape_contract: dict[str, Any] | None,
    ) -> str:
        if not reply_shape_contract:
            return text
        sentences = [part.strip() for part in str(text).split("。") if part.strip()]
        if not sentences:
            return text
        first_sentence = sentences[0]
        second_sentence = sentences[1] if len(sentences) > 1 else ""
        primary_shape = str(reply_shape_contract.get("primary_shape") or "").strip()
        secondary_shape = str(reply_shape_contract.get("secondary_shape") or "").strip()
        recent_shape_history = {
            str(token).strip()
            for token in list(reply_shape_contract.get("recent_shape_history") or [])
            if str(token).strip()
        }
        story_pressure_tokens = [
            str(token).strip()
            for token in list(reply_shape_contract.get("story_pressure_tokens") or [])
            if str(token).strip()
        ]
        preferred_shape = secondary_shape if primary_shape and primary_shape in recent_shape_history else primary_shape
        if (
            second_sentence
            and StoryEngine._fallback_matches_blandness_shape(second_sentence, preferred_shape)
            and (
                not story_pressure_tokens
                or any(token in second_sentence for token in story_pressure_tokens)
            )
        ):
            return text
        replacement = StoryEngine._fallback_shape_second_sentence(
            preferred_shape or primary_shape,
            story_pressure_tokens=story_pressure_tokens,
        )
        if replacement is None:
            return first_sentence
        return f"{first_sentence}。{replacement}"

    @staticmethod
    def _fallback_matches_blandness_shape(second_sentence: str, shape: str) -> bool:
        if shape == "answer_then_counter":
            return any(token in second_sentence for token in ("先に", "出せ", "言え", "見せ", "違う"))
        if shape == "answer_then_condition":
            return any(token in second_sentence for token in ("なら", "そのあと", "条件", "先なら"))
        if shape == "answer_then_probe":
            return any(
                token in second_sentence
                for token in (
                    "なぜ",
                    "どこで",
                    "どこに",
                    "何を",
                    "何で",
                    "誰が",
                    "いつ",
                    "どこまで",
                    "見る",
                    "確かめ",
                    "拾う",
                    "触れる",
                    "まだ",
                )
            )
        return False

    @staticmethod
    def _fallback_blandness_second_sentence(shape: str) -> str | None:
        if shape == "answer_then_counter":
            return "それは違う"
        if shape == "answer_then_condition":
            return "なら今ここで話そう"
        return None

    @staticmethod
    def _fallback_shape_second_sentence(
        shape: str,
        *,
        story_pressure_tokens: list[str],
    ) -> str | None:
        if not story_pressure_tokens:
            return None
        pressure_token = story_pressure_tokens[0]
        if shape == "answer_then_counter":
            return StoryEngine._story_pressure_move_phrase(pressure_token, move_mode="counter")
        if shape == "answer_then_condition":
            return StoryEngine._story_pressure_move_phrase(pressure_token, move_mode="condition")
        return StoryEngine._story_pressure_move_phrase(pressure_token, move_mode="probe")

    @staticmethod
    def _build_character_directive_text(payload: dict[str, Any], char_id: str) -> str | None:
        if not isinstance(payload, dict):
            return None
        characters = payload.get("characters")
        if not isinstance(characters, dict):
            return None
        directive = characters.get(char_id)
        if not isinstance(directive, dict):
            return None
        lines: list[str] = []
        role = str(directive.get("role") or "").strip()
        next_move = str(directive.get("next_move") or "").strip()
        speech_task = str(directive.get("speech_task") or "").strip()
        behavior = str(directive.get("actable_behavior") or "").strip()
        if role:
            lines.append(f"役割: {role}")
        if next_move:
            lines.append(f"次の動き: {next_move}")
        if speech_task:
            lines.append(f"発話タスク: {speech_task}")
        if behavior:
            lines.append(f"所作: {behavior}")
        avoid = directive.get("avoid")
        if isinstance(avoid, list):
            compact_avoid = " / ".join(str(item).strip() for item in avoid if str(item).strip())
            if compact_avoid:
                lines.append(f"避ける: {compact_avoid}")
        return "\n".join(lines) if lines else None

    def _scene_script_required_character_ids(self) -> list[str]:
        cap = 10
        if self._config is not None:
            cap = max(1, int(self._config.scene_script.max_directive_characters or 10))
        if self._planned_turn_queue:
            source = [str(item.get("char_id") or "").strip() for item in self._planned_turn_queue]
        else:
            if not self._characters:
                return []
            start = self._char_index % len(self._characters)
            ordered = self._characters[start:] + self._characters[:start]
            source = [str(character.get("id") or "").strip() for character in ordered]
        result: list[str] = []
        seen: set[str] = set()
        for char_id in source:
            if not char_id or char_id in seen:
                continue
            result.append(char_id)
            seen.add(char_id)
            if len(result) >= cap:
                break
        return result

    async def _get_current_scene_script_payload(self) -> dict[str, Any]:
        if isinstance(self._round_scene_payload, dict):
            return self._round_scene_payload
        if self._pre_turn_scene_engine is not None:
            metadata = self._pre_turn_scene_engine.get_cached_generation_metadata()
            payload = self._extract_scene_payload_from_metadata(metadata)
            if payload:
                self._round_scene_payload = payload
                return payload
        latest_scene_script = await self.db.get_latest_scene_script(self.story_id)
        if not isinstance(latest_scene_script, dict):
            return {}
        metadata = latest_scene_script.get("generation_metadata")
        payload = self._extract_scene_payload_from_metadata(metadata)
        if payload:
            self._round_scene_payload = payload
        return payload

    @staticmethod
    def _extract_scene_payload_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            return {}
        payload = metadata.get("director_payload")
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _build_scene_brief_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {
                "scene_frame": None,
                "turn_goal": None,
                "turn_shift": None,
                "banned_surface_patterns": [],
            }
        banned = payload.get("banned_surface_patterns")
        banned_patterns = [
            str(item).strip()
            for item in (banned if isinstance(banned, list) else [])
            if str(item).strip()
        ]
        return {
            "scene_frame": str(payload.get("scene_frame") or "").strip() or None,
            "turn_goal": str(payload.get("turn_goal") or "").strip() or None,
            "turn_shift": str(payload.get("turn_shift") or "").strip() or None,
            "banned_surface_patterns": banned_patterns,
        }

    @staticmethod
    def _fallback_dramatic_second_sentence(
        move_mode: str,
        *,
        required_move_tokens: list[str],
        pressure_anchor_tokens: list[str],
    ) -> str | None:
        if not pressure_anchor_tokens:
            return None
        anchor = pressure_anchor_tokens[0]
        if move_mode == "condition":
            return StoryEngine._story_pressure_move_phrase(anchor, move_mode="condition")
        if move_mode == "probe":
            return StoryEngine._story_pressure_move_phrase(anchor, move_mode="probe")
        if move_mode == "claim":
            return StoryEngine._story_pressure_move_phrase(anchor, move_mode="claim")
        return StoryEngine._story_pressure_move_phrase(anchor, move_mode="counter")

    @staticmethod
    def _recent_reply_openings(messages: list[str]) -> list[str]:
        openings: list[str] = []
        for message in messages:
            sentences = [part.strip() for part in str(message).split("。") if part.strip()]
            if sentences:
                openings.append(sentences[0])
        return openings

    @staticmethod
    def _preferred_voice_cues(voice_anchor_text: str | None) -> list[str]:
        text = str(voice_anchor_text or "")
        if "熱血" in text or "直球" in text:
            return ["言う", "動く", "ここで"]
        if "挑発" in text:
            return ["見せ", "だろ", "かよ", "もっと"]
        if "丁寧" in text:
            return ["ください", "ましょう", "ません"]
        if "静かな確認" in text:
            return ["合わせ", "確かめ", "見る"]
        return []

    @staticmethod
    def _recent_reply_endings(messages: list[str]) -> list[str]:
        endings: list[str] = []
        for message in messages:
            sentences = [part.strip() for part in str(message).split("。") if part.strip()]
            if len(sentences) >= 2:
                endings.append(sentences[1])
        return endings

    @classmethod
    def _recent_reply_second_beat_modes(cls, messages: list[str]) -> list[str]:
        modes: list[str] = []
        for message in messages:
            second_sentence = cls._extract_fallback_second_sentence(message)
            mode = cls._infer_reply_second_beat_mode(second_sentence)
            if mode:
                modes.append(mode)
        return modes

    @staticmethod
    def _infer_reply_second_beat_mode(second_sentence: str | None) -> str | None:
        text = str(second_sentence or "").strip()
        if not text:
            return None
        if any(token in text for token in ("じゃあ", "その前に", "まず", "代わりに", "別の")):
            return "redirect"
        if any(token in text for token in ("なら", "そのあと", "先なら", "条件", "済んだら")):
            return "condition"
        if any(token in text for token in ("先に", "言って", "出して", "今", "決め", "返して", "出せ")):
            return "press"
        return None

    @staticmethod
    def _build_voice_anchor_text(*, tone: str, speech_examples: list[str]) -> str | None:
        text = " ".join([tone, *(speech_examples[:2])]).strip()
        if not text:
            return None
        if any(token in text for token in ("熱血", "直球", "拳", "ぶつか", "真っ向")):
            return "熱血直球で短く返す"
        if any(token in text for token in ("挑発", "煽", "嫌い", "主役", "もっと見せ")):
            return "挑発混じりに短く押す"
        if any(token in text for token in ("冷たい", "観察", "設計", "整理", "静か")):
            return "静かな確認で短く返す"
        if any(token in text for token in ("丁寧", "落ち着", "ください", "と思います")):
            return "丁寧に短く返す"
        return "短く断定して返す"

    async def _evaluate_dialogue_with_llm_critic(
        self,
        *,
        char_id: str,
        msg_type: str,
        candidate_text: str,
        character_name: str,
        target_char_name: str | None,
        recent_dialogue_lines: list[str],
        scene_frame_text: str,
        scene_turn_goal_text: str,
        scene_turn_shift_text: str,
        character_directive_text: str,
        reply_focus_text: str | None,
        dominant_signal_text: str | None,
        scene_objective_text: str | None,
    ) -> dict[str, str]:
        system_prompt = (
            "あなたは会話品質の検査係です。セリフを作らず、JSONだけを返してください。"
            "判定は accept または reject。カンペ文、場面無視、相手への無反応、"
            "同じ位置での足踏みを reject してください。"
        )
        user_prompt = self._build_dialogue_critic_user_prompt(
            msg_type=msg_type,
            candidate_text=candidate_text,
            character_name=character_name,
            target_char_name=target_char_name,
            recent_dialogue_lines=recent_dialogue_lines,
            scene_frame_text=scene_frame_text,
            scene_turn_goal_text=scene_turn_goal_text,
            scene_turn_shift_text=scene_turn_shift_text,
            character_directive_text=character_directive_text,
            reply_focus_text=reply_focus_text,
            dominant_signal_text=dominant_signal_text,
            scene_objective_text=scene_objective_text,
        )
        response = await self._generate_llm_response(
            char_id=char_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if response is None:
            return {
                "verdict": "unavailable",
                "reason_code": "critic_empty",
                "repair_instruction": "",
                "raw_preview": "",
            }
        raw_text = self._sanitize_generated_text(str(response.final_text or ""))
        payload = self._parse_dialogue_critic_payload(raw_text)
        if payload is None:
            return {
                "verdict": "unavailable",
                "reason_code": "critic_invalid_json",
                "repair_instruction": "",
                "raw_preview": raw_text[:120],
            }
        verdict = str(payload.get("verdict") or "").strip().lower()
        if verdict not in {"accept", "reject"}:
            return {
                "verdict": "unavailable",
                "reason_code": "critic_invalid_verdict",
                "repair_instruction": "",
                "raw_preview": raw_text[:120],
            }
        reason_code = str(payload.get("reason_code") or ("accepted" if verdict == "accept" else "other")).strip()
        repair_instruction = str(payload.get("repair_instruction") or "").strip()
        return {
            "verdict": verdict,
            "reason_code": reason_code,
            "repair_instruction": repair_instruction,
            "raw_preview": raw_text[:120],
        }

    @staticmethod
    def _build_dialogue_critic_user_prompt(
        *,
        msg_type: str,
        candidate_text: str,
        character_name: str,
        target_char_name: str | None,
        recent_dialogue_lines: list[str],
        scene_frame_text: str,
        scene_turn_goal_text: str,
        scene_turn_shift_text: str,
        character_directive_text: str,
        reply_focus_text: str | None,
        dominant_signal_text: str | None,
        scene_objective_text: str | None,
    ) -> str:
        recent_text = "\n".join(recent_dialogue_lines[-6:]) if recent_dialogue_lines else "なし"
        return (
            "次の候補発話を検査してください。\n"
            "返答は必ず JSON 1個だけです。\n"
            "形式: {\"verdict\":\"accept|reject\",\"reason_code\":\"...\","
            "\"repair_instruction\":\"...\"}\n"
            "reject の代表 reason_code: cue_like_dialogue, not_reacting, scene_ignored, "
            "voice_mismatch, loop_like, other\n\n"
            f"msg_type: {msg_type}\n"
            f"speaker: {character_name}\n"
            f"target: {target_char_name or 'なし'}\n"
            f"candidate: {candidate_text}\n"
            f"recent_dialogue:\n{recent_text}\n"
            f"scene_frame: {scene_frame_text or 'なし'}\n"
            f"turn_goal: {scene_turn_goal_text or scene_objective_text or 'なし'}\n"
            f"turn_shift: {scene_turn_shift_text or 'なし'}\n"
            f"character_role: {character_directive_text or 'なし'}\n"
            f"reply_focus: {reply_focus_text or 'なし'}\n"
            f"dominant_signal: {dominant_signal_text or 'なし'}\n\n"
            "判断基準:\n"
            "- 候補がキャラの実際のセリフ・内面として読めるなら accept。\n"
            "- 演出指示、作業指示、抽象的なカンペ文なら reject。\n"
            "- reply は相手の直前発話に最低限反応していなければ reject。\n"
            "- scene_frame/turn_goal/turn_shift と明らかに無関係なら reject。\n"
            "- repair_instruction は次回生成に渡せる短い日本語にしてください。"
        )

    @staticmethod
    def _parse_dialogue_critic_payload(text: str) -> dict[str, Any] | None:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if match is None:
                return None
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _repair_instruction_from_hard_failures(issue_types: list[str]) -> str:
        if "empty_output" in issue_types:
            return "発話本文だけを短く返す"
        if "json_or_code_output" in issue_types or "markdown_formatting" in issue_types:
            return "JSONやMarkdownを使わず、セリフ本文だけを返す"
        if "thinking_tag_output" in issue_types:
            return "thinkingや内部推論を出さず、本文だけを返す"
        if "sentence_overflow" in issue_types:
            return "本文は1〜2文に収める。感嘆だけの短文を分けず、ひと続きの自然な台詞にする"
        if "overlength" in issue_types:
            return "もっと短くする"
        if "director_cue_surface_leak" in issue_types:
            return "演出指示ではなく、キャラ本人の言葉にする"
        return "発話本文だけを自然な日本語で短く返す"

    @staticmethod
    def _build_dialogue_regeneration_user_prompt(
        base_user_prompt: str,
        *,
        msg_type: str,
        previous_attempts: list[dict[str, Any]],
        repair_instruction: str,
    ) -> str:
        last_attempt = previous_attempts[-1] if previous_attempts else {}
        reason = (
            str(last_attempt.get("critic_reason_code") or "")
            or " / ".join(str(item) for item in last_attempt.get("hard_failures", []) if item)
            or "quality_retry"
        )
        return (
            f"{base_user_prompt}\n\n"
            "【再生成】\n"
            f"前回候補は採用不可でした。理由: {reason}\n"
            f"修正指示: {repair_instruction or 'キャラ本人の自然な発話だけを短く返す'}\n"
            f"msg_type={msg_type} の本文だけを返してください。"
        )

    async def _generate_llm_response(
        self,
        *,
        char_id: str,
        system_prompt: str,
        user_prompt: str,
    ) -> Any | None:
        """モデル profile を適用し、必要なら 1 回だけ recovery する。"""
        provider = self._effective_llm_provider
        model = self._effective_llm_model
        profile = self._resolve_model_profile(provider, model)

        primary_kwargs = self._build_generation_kwargs(profile, recovery=False)
        primary_kwargs["request_tag"] = self._build_generation_request_tag(
            char_id=char_id,
            recovery=False,
        )
        response = await self.llm_router.generate(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            **primary_kwargs,
        )
        if self._has_persistable_text(self._sanitize_generated_text(response.final_text)):
            return response

        logger.warning(
            "LLM returned empty final content; attempting recovery",
            extra={
                "story_id": self.story_id,
                "char_id": char_id,
                "turn": self.world_clock.turn_number if self.world_clock else None,
                "llm_provider": provider,
                "llm_model": model,
                "llm_response_chars": len(response.final_text),
                "llm_thinking_chars": response.reasoning_chars,
                "llm_done_reason": response.done_reason,
                "llm_completion_status": response.completion_status,
                "llm_recovery_attempted": True,
            },
        )

        recovery_kwargs = self._build_generation_kwargs(profile, recovery=True)
        recovery_kwargs["request_tag"] = self._build_generation_request_tag(
            char_id=char_id,
            recovery=True,
        )
        recovery_kwargs = self._tighten_empty_final_recovery_kwargs(
            recovery_kwargs,
            provider=provider,
            model=model,
            response=response,
        )
        if recovery_kwargs == primary_kwargs:
            logger.error(
                "LLM empty final content and no recovery variant available; skipping chat log",
                extra={
                    "story_id": self.story_id,
                    "char_id": char_id,
                    "turn": self.world_clock.turn_number if self.world_clock else None,
                    "llm_provider": provider,
                    "llm_model": model,
                    "llm_response_chars": len(response.final_text),
                    "llm_thinking_chars": response.reasoning_chars,
                    "llm_done_reason": response.done_reason,
                    "llm_completion_status": response.completion_status,
                    "llm_recovery_attempted": False,
                },
            )
            return None

        recovered = await self.llm_router.generate(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            **recovery_kwargs,
        )
        if self._has_persistable_text(self._sanitize_generated_text(recovered.final_text)):
            return recovered

        logger.error(
            "LLM recovery exhausted; skipping chat log",
            extra={
                "story_id": self.story_id,
                "char_id": char_id,
                "turn": self.world_clock.turn_number if self.world_clock else None,
                "llm_provider": provider,
                "llm_model": model,
                "llm_response_chars": len(recovered.final_text),
                "llm_thinking_chars": recovered.reasoning_chars,
                "llm_done_reason": recovered.done_reason,
                "llm_completion_status": recovered.completion_status,
                "llm_recovery_attempted": True,
            },
        )
        return None

    @staticmethod
    def _build_generation_request_tag(*, char_id: str, recovery: bool) -> str:
        phase = "recovery" if recovery else "primary"
        return f"story_engine:reply:{char_id}:{phase}"

    @staticmethod
    def _tighten_empty_final_recovery_kwargs(
        recovery_kwargs: dict[str, Any],
        *,
        provider: str,
        model: str,
        response: Any,
    ) -> dict[str, Any]:
        adjusted = dict(recovery_kwargs)
        if (
            provider == "ollama"
            and model.lower().startswith("gemma4")
            and getattr(response, "completion_status", "") == "empty_final"
        ):
            current = adjusted.get("max_tokens")
            if isinstance(current, int):
                adjusted["max_tokens"] = min(current, 520)
            adjusted.setdefault("temperature", 0.0)
            adjusted["reasoning_mode"] = "off"
        return adjusted

    @staticmethod
    def _has_persistable_text(text: str) -> bool:
        """空白-only を含む空本文を永続化対象から除外する。"""
        return text.strip() != ""

    @staticmethod
    def _sanitize_generated_text(text: str) -> str:
        """本文先頭に漏れた thinking / reasoning residue を除去する。"""
        cleaned = text.replace("\r\n", "\n")
        cleaned = re.sub(
            r"<think(?:ing)?>.*?</think(?:ing)?>",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )
        previous = None
        while cleaned != previous:
            previous = cleaned
            cleaned = re.sub(
                r"^\s*```(?:thinking|reasoning|analysis)?\s*.*?```\s*",
                "",
                cleaned,
                flags=re.IGNORECASE | re.DOTALL,
            )
            cleaned = re.sub(
                r"^\s*(?:thinking|reasoning|analysis|internal)\s*[:：]\s*[^\n]*(?:\n+|$)",
                "",
                cleaned,
                flags=re.IGNORECASE,
            )
            cleaned = re.sub(r"^\s*\n+", "", cleaned)
        return cleaned.strip()

    def _story_hooks_enabled(self) -> bool:
        return self._config is not None and self._config.story_hooks.enabled

    def _relationship_dynamics_enabled(self) -> bool:
        return self._config is not None and self._config.relationship_dynamics.enabled

    async def _get_active_hook_texts(self, char_id: str, place_id: str) -> list[str]:
        if not self._story_hooks_enabled():
            return []
        max_hooks = 3
        if self._config is not None:
            max_hooks = min(3, self._config.story_hooks.max_open_hooks)
        hooks = await self.db.get_relevant_story_hooks(
            self.story_id,
            char_id,
            place_id,
            max_hooks,
        )
        hooks = [
            hook
            for hook in hooks
            if not (
                hook.get("hook_type") == "question"
                and hook.get("target_char_id") is None
            )
        ]
        return [self._format_hook_text(hook) for hook in hooks]

    async def _get_relationship_summary_texts(
        self,
        char_id: str,
        counterpart_ids: list[str],
    ) -> list[str]:
        if not self._relationship_dynamics_enabled():
            return []
        unique_ids = list(dict.fromkeys(counterpart_ids))
        if not unique_ids:
            return []
        return await self.db.get_relationship_summary(
            self.story_id,
            char_id,
            unique_ids,
            limit=2,
        )

    @staticmethod
    def _format_hook_text(hook: dict[str, Any]) -> str:
        title = str(hook.get("title") or hook.get("hook_type") or "未解決事項")
        description = str(hook.get("description") or "").strip()
        return f"{title}: {description}" if description else title

    async def _process_story_hooks(
        self,
        *,
        log_id: int,
        char_id: str,
        target_char_id: str | list[str] | None,
        scene_id: int | None,
        place_id: str,
        message: str,
        msg_type: str,
    ) -> None:
        if msg_type == "current_affairs":
            return
        if not self._story_hooks_enabled():
            return

        participant_ids: list[str] = []
        if isinstance(target_char_id, str):
            participant_ids = [char_id, target_char_id]
        elif isinstance(target_char_id, list):
            participant_ids = [char_id, *target_char_id]

        named_group_target: str | None = None
        resolved_target_char_id = target_char_id
        if msg_type == "group":
            named_group_target = self._resolve_group_hook_target(
                char_id=char_id,
                participant_ids=participant_ids,
                message=message,
            )
            resolved_target_char_id = named_group_target

        if msg_type == "reply" and isinstance(target_char_id, str):
            relevant_hooks = await self.db.get_relevant_story_hooks(
                self.story_id,
                char_id,
                place_id,
                limit=10,
            )
            for hook in relevant_hooks:
                if hook.get("hook_type") != "question":
                    continue
                if hook.get("status") != "open":
                    continue
                if hook.get("owner_char_id") != target_char_id:
                    continue
                if hook.get("target_char_id") != char_id:
                    continue
                if scene_id is not None and hook.get("source_scene_id") not in (None, scene_id):
                    continue
                await self.db.resolve_story_hook(
                    int(hook["id"]),
                    resolution_log_id=log_id,
                    resolved_turn=self.world_clock.turn_number if self.world_clock else None,
                    summary=f"{char_id} が返答して問いかけに応じた。",
                )

        hook_candidates: list[dict[str, Any]] = []
        normalized_message = _normalize_hook_text(message)
        if msg_type == "monologue" and len(normalized_message) >= 8:
            hook_candidates.append(
                {
                    "hook_type": "solo_seed",
                    "title": "聞き逃せない独り言",
                    "description": normalized_message,
                    "priority": 0.7,
                }
            )
        if msg_type in {"reply", "group"} and _looks_like_question(message):
            if msg_type == "group" and named_group_target is None:
                pass
            else:
                hook_candidates.append(
                    {
                        "hook_type": "question",
                        "title": "返答待ちの問いかけ",
                        "description": normalized_message,
                        "priority": 0.75,
                    }
                )
        if _contains_any_cue(message, _HOOK_PROMISE_CUES):
            hook_candidates.append(
                {
                    "hook_type": "promise",
                    "title": "未完了の約束",
                    "description": normalized_message,
                    "priority": 0.8,
                }
            )
        if _contains_any_cue(message, _HOOK_CONFLICT_CUES):
            hook_candidates.append(
                {
                    "hook_type": "conflict",
                    "title": "くすぶる対立",
                    "description": normalized_message,
                    "priority": 0.85,
                }
            )

        if not hook_candidates:
            return

        open_hooks = await self.db.get_open_story_hooks(self.story_id)
        for candidate in hook_candidates:
            candidate_target = resolved_target_char_id
            if msg_type == "group" and candidate["hook_type"] in {"promise", "conflict"}:
                candidate_target = named_group_target
            duplicate_found = False
            for existing in open_hooks:
                if existing.get("hook_type") != candidate["hook_type"]:
                    continue
                if existing.get("owner_char_id") != char_id:
                    continue
                if existing.get("target_char_id") != candidate_target:
                    continue
                existing_source_log = existing.get("source_log_id")
                if existing_source_log is not None and abs(log_id - int(existing_source_log)) > 10:
                    continue
                existing_norm = _normalize_hook_text(str(existing.get("description") or ""))
                if existing_norm == candidate["description"]:
                    duplicate_found = True
                    break
            if duplicate_found:
                continue
            await self.db.insert_story_hook(
                self.story_id,
                {
                    "hook_type": candidate["hook_type"],
                    "status": "open",
                    "owner_char_id": char_id,
                    "target_char_id": candidate_target,
                    "title": candidate["title"],
                    "description": candidate["description"],
                    "priority": candidate["priority"],
                    "source_scene_id": scene_id,
                    "source_log_id": log_id,
                },
            )

    async def _process_relationship_dynamics(
        self,
        *,
        log_id: int,
        char_id: str,
        target_char_id: str | list[str] | None,
        scene_id: int | None,
        message: str,
        msg_type: str,
    ) -> None:
        if (
            not self._relationship_dynamics_enabled()
            or msg_type != "reply"
            or not isinstance(target_char_id, str)
        ):
            return

        supportive = _contains_any_cue(message, _HOOK_SUPPORTIVE_CUES)
        promise = _contains_any_cue(message, _HOOK_PROMISE_CUES)
        conflict = _contains_any_cue(message, _HOOK_CONFLICT_CUES)
        question = _looks_like_question(message)

        if question and not supportive and not promise and not conflict:
            return

        delta_trust = 0.02
        delta_tension = 0.0
        event_type = "reply"
        if supportive:
            delta_trust += 0.05
            delta_tension -= 0.03
            event_type = "support"
        if promise:
            delta_trust += 0.04
            delta_tension -= 0.02
            if event_type == "reply":
                event_type = "promise"
        if conflict:
            delta_trust -= 0.05
            delta_tension += 0.08
            event_type = "conflict"

        scale = (
            self._config.relationship_dynamics.trust_delta_scale
            if self._config is not None
            else 1.0
        )
        delta_trust *= scale
        delta_tension *= scale

        current_snapshot = self._relationships.get(
            (char_id, target_char_id),
            {"trust": 0.5, "tension": 0.0, "affinity": 0.5, "familiarity": 0.5},
        )
        new_trust = _clamp_unit(float(current_snapshot.get("trust", 0.5)) + delta_trust)
        new_tension = _clamp_unit(float(current_snapshot.get("tension", 0.0)) + delta_tension)
        summary = (
            f"{char_id} から {target_char_id} への {event_type} により "
            f"trust {delta_trust:+.2f}, tension {delta_tension:+.2f}"
        )
        patch = {
            "trust": new_trust,
            "tension": new_tension,
            "affinity": float(current_snapshot.get("affinity", 0.5)),
            "familiarity": float(current_snapshot.get("familiarity", 0.5)),
            "last_event_turn": self.world_clock.turn_number if self.world_clock else None,
            "last_event_summary": summary,
        }
        await self.db.update_relationship_snapshot(
            self.story_id,
            char_id,
            target_char_id,
            patch,
        )
        await self.db.insert_relationship_event(
            self.story_id,
            {
                "char_id_from": char_id,
                "char_id_to": target_char_id,
                "event_type": event_type,
                "delta_trust": delta_trust,
                "delta_tension": delta_tension,
                "summary": summary,
                "source_log_id": log_id,
                "scene_id": scene_id,
                "turn_number": self.world_clock.turn_number if self.world_clock else None,
            },
        )
        self._relationships[(char_id, target_char_id)] = {
            **current_snapshot,
            "trust": new_trust,
            "tension": new_tension,
        }

    def _resolve_model_profile(self, provider: str, model: str) -> ModelProfileConfig:
        if isinstance(self.llm_router, LLMRouter):
            return self.llm_router.get_model_profile(provider, model)
        if provider == "ollama" and model.lower().startswith("qwen3"):
            return ModelProfileConfig(
                reasoning_mode="auto",
                max_tokens=1000,
                recovery_max_tokens=1400,
                recovery_reasoning_mode="off",
            )
        if provider == "ollama" and model.lower().startswith("gemma4"):
            return ModelProfileConfig(
                reasoning_mode="off",
                max_tokens=900,
                recovery_max_tokens=1100,
                recovery_reasoning_mode="off",
            )
        return ModelProfileConfig()

    @staticmethod
    def _build_generation_kwargs(
        profile: ModelProfileConfig,
        *,
        recovery: bool,
    ) -> dict[str, Any]:
        reasoning_mode = profile.reasoning_mode
        temperature = profile.temperature
        max_tokens = profile.max_tokens
        if recovery:
            reasoning_mode = profile.recovery_reasoning_mode or reasoning_mode
            temperature = (
                profile.recovery_temperature
                if profile.recovery_temperature is not None
                else temperature
            )
            max_tokens = (
                profile.recovery_max_tokens
                if profile.recovery_max_tokens is not None
                else max_tokens
            )

        kwargs: dict[str, Any] = {"reasoning_mode": reasoning_mode}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return kwargs

    async def _advance_turn_cursor(self) -> None:
        assert self.world_clock is not None
        if self._planned_turn_queue:
            self._planned_turn_index += 1
            if self._planned_turn_index >= len(self._planned_turn_queue):
                self.world_clock.tick()
                await self.db.update_last_sim_time(
                    self.story_id, self.world_clock.sim_datetime
                )
                self._planned_turn_queue = []
                self._planned_turn_index = 0
            return
        self._char_index += 1
        if self._char_index % len(self._characters) == 0:
            self.world_clock.tick()
            await self.db.update_last_sim_time(
                self.story_id, self.world_clock.sim_datetime
            )


def _extract_character_objective(scene_script: str, char_name: str) -> str | None:
    """シーンスクリプトの〔各キャラの今ここ〕セクションから char_name の行を抽出する。"""
    if not scene_script or not char_name:
        return None
    for marker in ("〔各キャラの今ここ〕", "〔各キャラの実行カード〕"):
        start = scene_script.find(marker)
        if start == -1:
            continue
        next_sec = scene_script.find("〔", start + 1)
        section = scene_script[start:next_sec] if next_sec != -1 else scene_script[start:]
        for line in section.splitlines():
            if char_name in line:
                for sep in ("：", ":"):
                    pos = line.find(sep, line.find(char_name))
                    if pos != -1:
                        result = line[pos + 1:].strip()
                        return result[:120] if len(result) > 120 else result
    return None


def _derive_reaction_frame(
    emotions: dict[str, float],
    trust_level: float,
    target_name: str | None,
) -> str | None:
    """感情値と信頼度から内的反応フレームを決定論的に導出する。"""
    if not target_name:
        return None
    stress = emotions.get("stress", 0.5)
    loneliness = emotions.get("loneliness", 0.5)
    motivation = emotions.get("motivation", 0.5)
    if stress > 0.65 and trust_level < 0.4:
        return f"{target_name}の言葉に対して内心で身構えている。防衛的だが表面は落ち着いている。"
    if stress > 0.65:
        return f"プレッシャーを感じながら{target_name}に向き合っている。"
    if loneliness > 0.65:
        return f"心のどこかで{target_name}とつながりたい気持ちがある。"
    if motivation > 0.75 and trust_level > 0.5:
        return f"{target_name}の言葉に乗り気で応じたい。積極的だがまだ手を見せない。"
    if trust_level < 0.35:
        return f"{target_name}の意図を測ろうとしている。慎重に、表面は穏やかに。"
    return None


def _make_scheduler_from_config(config: Any) -> Any:
    """Config から Scheduler を生成するファクトリ（StoryEngine._make_scheduler のヘルパー）。"""
    from engine.scheduler import Scheduler
    if config is None:
        return Scheduler()
    pp = config.participation_planner
    return Scheduler(
        monologue_solo_rate=pp.monologue_solo_rate,
        monologue_group_rate=pp.monologue_group_rate,
        advance_monologue_bonus=pp.advance_monologue_bonus,
    )


def _pick_dominant_emotion(state: dict[str, Any]) -> str:
    """キャラクターの最新ステートから最も強い感情ラベルを返す。"""
    import json
    emotion_raw = state.get("emotion_state") or state.get("emotion_default") or {}
    if isinstance(emotion_raw, str):
        try:
            emotion_raw = json.loads(emotion_raw)
        except (ValueError, TypeError):
            emotion_raw = {}
    candidates = {
        "ストレス": float(emotion_raw.get("stress", 0)),
        "孤独感": float(emotion_raw.get("loneliness", 0)),
        "高揚感": float(emotion_raw.get("excitement", 0)),
        "やる気": float(emotion_raw.get("motivation", 0)),
    }
    dominant = max(candidates, key=lambda k: candidates[k])
    return dominant if candidates[dominant] > 0.3 else "平静"
