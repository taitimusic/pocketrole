"""
engine/director_persona.py — Director Persona（監修機能）

満足度評価（deterministic + optional LLM）とステアリングシグナルの算出を担う。
LLM 評価は enable_llm_evaluation=true + story_engine からの後注入で有効化。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from db.db_manager import DatabaseManager

if TYPE_CHECKING:
    from engine.llm.router import LLMRouter

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# SteeringSignals
# ------------------------------------------------------------------

@dataclass
class SteeringSignals:
    """各層向けのバイアス情報。

    - tension_type_weights:       > 1.0 = 選好, < 1.0 = 忌避
    - intervention_type_weights:  同上
    - pattern_type_weights:       Phase 3 で使用（今は空）
    - tone_hints:                 短い演出指示テキスト（日本語）
    """
    tension_type_weights: dict[str, float] = field(default_factory=dict)
    intervention_type_weights: dict[str, float] = field(default_factory=dict)
    pattern_type_weights: dict[str, float] = field(default_factory=dict)
    preferred_event_flavors: list[str] = field(default_factory=list)
    quality_leniency: dict[str, float] = field(default_factory=dict)
    tone_hints: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "SteeringSignals":
        return cls()


# ------------------------------------------------------------------
# DirectorPersona
# ------------------------------------------------------------------

class DirectorPersona:
    """監督ペルソナ管理クラス。

    - N round ごとの満足度評価（deterministic）
    - 満足度に基づく SteeringSignals の算出
    - ペルソナ swap ロジック
    """

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        *,
        evaluation_interval_rounds: int = 3,
        steering_strength: str = "moderate",
        satisfaction_decay: float = 0.1,
        swap_cooldown_turns: int = 0,
        allow_mid_chapter_swap: bool = True,
    ) -> None:
        self._story_id = story_id
        self._db = db
        self._eval_interval = evaluation_interval_rounds
        self._steering_strength = steering_strength
        self._decay = satisfaction_decay
        self._swap_cooldown = swap_cooldown_turns
        self._allow_mid_chapter_swap = allow_mid_chapter_swap

        self._active_persona: dict[str, Any] | None = None
        self._steering: SteeringSignals = SteeringSignals.empty()
        self._last_eval_turn: int = -self._eval_interval  # 初回は即評価可
        self._last_overall: float = 0.0
        self._last_swap_turn: int = -self._swap_cooldown  # 初回 swap は即可

        # Phase 3 optional: LLM-assisted 満足度評価（story_engine.initialize() が後注入）
        self._llm_router: "LLMRouter | None" = None
        self._llm_provider: str = ""
        self._llm_model: str = ""
        self._enable_llm_eval: bool = False
        self._llm_eval_interval: int = 3
        self._llm_eval_count: int = 0        # deterministic 評価の累計回数
        self._last_llm_suggestion: str = ""

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """DB から active persona を読み込み、ステアリングシグナルを初期化する。"""
        self._active_persona = await self._db.get_active_director_persona(self._story_id)
        if self._active_persona is None:
            logger.info(
                "DirectorPersona: no active persona found",
                extra={"story_id": self._story_id},
            )
            return

        self._update_steering_signals()

        # 前回の満足度履歴から last_eval_turn / last_overall を復元
        persona_id = self._active_persona["persona_id"]
        latest = await self._db.get_latest_director_satisfaction(self._story_id, persona_id)
        if latest is not None:
            self._last_eval_turn = int(latest.get("turn_number") or -self._eval_interval)
            self._last_overall = float(latest.get("overall") or 0.0)

        logger.info(
            "DirectorPersona initialized",
            extra={
                "story_id": self._story_id,
                "persona_id": persona_id,
                "last_eval_turn": self._last_eval_turn,
            },
        )

    async def evaluate_satisfaction(self, turn_number: int) -> dict[str, Any]:
        """現在の story 状態を評価し、満足度を DB に保存する。

        Returns:
            satisfaction dict（スキップ時は空 dict）
        """
        if self._active_persona is None:
            return {}
        if (turn_number - self._last_eval_turn) < self._eval_interval:
            return {}

        persona_id = self._active_persona["persona_id"]
        sat = await self._compute_satisfaction(turn_number)

        # Phase 3 optional: LLM による character_depth_sat / dialogue_sat の評価
        self._llm_eval_count += 1
        if (
            self._enable_llm_eval
            and self._llm_router is not None
            and self._llm_eval_count % self._llm_eval_interval == 0
        ):
            sat = await self._llm_evaluate_satisfaction(sat)

        overall = float(sat["overall"])
        trend = self._compute_trend(overall)

        sat_dict: dict[str, Any] = {
            "persona_id": persona_id,
            "turn_number": turn_number,
            "overall": overall,
            "tension_sat": sat["tension_sat"],
            "character_depth_sat": sat["character_depth_sat"],
            "pacing_sat": sat["pacing_sat"],
            "surprise_sat": sat["surprise_sat"],
            "dialogue_sat": sat["dialogue_sat"],
            "atmosphere_sat": sat["atmosphere_sat"],
            "trend": trend,
            "details_json": sat.get("details", {}),
        }

        await self._db.insert_director_satisfaction(self._story_id, sat_dict)
        self._last_eval_turn = turn_number
        self._last_overall = overall

        logger.info(
            "DirectorPersona satisfaction evaluated",
            extra={
                "story_id": self._story_id,
                "persona_id": persona_id,
                "turn_number": turn_number,
                "overall": round(overall, 3),
                "trend": trend,
            },
        )
        return sat_dict

    def get_steering_signals(self) -> SteeringSignals:
        """各層向けのバイアス情報を返す（同期）。"""
        return self._steering

    async def reload_active_persona(self) -> bool:
        """DB 上の active persona 変更を取り込み、steering を更新する。"""
        latest_persona = await self._db.get_active_director_persona(self._story_id)
        previous_id = (
            str(self._active_persona.get("persona_id"))
            if self._active_persona is not None
            else None
        )
        latest_id = (
            str(latest_persona.get("persona_id"))
            if latest_persona is not None
            else None
        )
        if previous_id == latest_id:
            return False

        self._active_persona = latest_persona
        self._last_llm_suggestion = ""
        self._last_eval_turn = -self._eval_interval
        self._last_overall = 0.0
        if self._active_persona is not None:
            latest_sat = await self._db.get_latest_director_satisfaction(self._story_id, latest_id)
            if latest_sat is not None:
                self._last_eval_turn = int(latest_sat.get("turn_number") or -self._eval_interval)
                self._last_overall = float(latest_sat.get("overall") or 0.0)
        self._update_steering_signals()
        return True

    async def swap_active_persona(
        self,
        new_persona_id: str,
        turn_number: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """active persona を切り替える。

        Args:
            new_persona_id: 新 persona の ID
            turn_number: 現在の turn 番号
            reason: 交代理由（任意）

        Returns:
            {"swapped": True, "from": old_id, "to": new_id}

        Raises:
            ValueError: 同じ persona への swap、または cooldown 期間中
        """
        if not self._allow_mid_chapter_swap:
            active_chapter = await self._db.get_active_chapter(self._story_id)
            if active_chapter is not None:
                raise ValueError("mid-chapter swap is disabled while a chapter is active")

        # cooldown チェック
        if self._swap_cooldown > 0:
            turns_since_swap = turn_number - self._last_swap_turn
            if turns_since_swap < self._swap_cooldown:
                raise ValueError(
                    f"swap_cooldown_turns={self._swap_cooldown} 未満のため swap 不可 "
                    f"(last_swap_turn={self._last_swap_turn}, current={turn_number})"
                )

        old_persona_id: str | None = None
        if self._active_persona is not None:
            old_persona_id = self._active_persona["persona_id"]
            if old_persona_id == new_persona_id:
                raise ValueError(f"既に active な persona への swap は不可: {new_persona_id!r}")

        # DB 更新: 旧 inactive → 新 active
        await self._db.set_persona_active(self._story_id, new_persona_id)

        # swap ログ記録
        await self._db.insert_director_swap_log(
            self._story_id,
            {
                "from_persona_id": old_persona_id,
                "to_persona_id": new_persona_id,
                "turn_number": turn_number,
                "reason": reason,
            },
        )

        # 新 persona を読み込んで内部状態を更新
        new_persona = await self._db.get_active_director_persona(self._story_id)
        self._active_persona = new_persona
        self._last_swap_turn = turn_number

        if self._active_persona is not None:
            self._update_steering_signals()
            # 初回満足度評価（前回 eval turn をリセットして即評価）
            self._last_eval_turn = -self._eval_interval
            await self.evaluate_satisfaction(turn_number)

        logger.info(
            "DirectorPersona swapped",
            extra={
                "story_id": self._story_id,
                "from": old_persona_id,
                "to": new_persona_id,
                "turn_number": turn_number,
                "reason": reason,
            },
        )
        return {"swapped": True, "from": old_persona_id, "to": new_persona_id}

    async def get_available_personas(self) -> list[dict[str, Any]]:
        """この story に定義されている全ペルソナを返す。"""
        return await self._db.get_all_director_personas(self._story_id)

    async def get_swap_history(self) -> list[dict[str, Any]]:
        """この story の swap 履歴を返す。"""
        return await self._db.get_director_swap_history(self._story_id)

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    async def _compute_satisfaction(self, turn_number: int) -> dict[str, Any]:
        """各満足度軸を 0.0〜1.0 で計算する。"""
        assert self._active_persona is not None
        aesthetic = dict(self._active_persona.get("aesthetic_json") or {})

        tension_pref = float(aesthetic.get("tension_preference", 0.5))
        depth_pref = float(aesthetic.get("character_depth", 0.5))
        action_pref = float(aesthetic.get("action_preference", 0.5))
        dialogue_wit = float(aesthetic.get("dialogue_wit", 0.5))
        atmo_pref = float(aesthetic.get("atmosphere_weight", 0.5))
        curiosity = float(aesthetic.get("curiosity", 0.5))
        since_turn = max(0, turn_number - max(3, self._eval_interval * 4) + 1)

        # -- tension_sat --------------------------------------------------
        tensions = await self._db.get_active_tensions(self._story_id)
        count_score = min(1.0, len(tensions) / 5.0)
        max_intensity = max(
            (float(t.get("intensity", 0.5)) for t in tensions), default=0.0
        )
        observed_tension = (count_score + max_intensity) / 2.0
        tension_sat = max(0.0, 1.0 - abs(tension_pref - observed_tension))

        # -- pacing_sat ---------------------------------------------------
        recent_logs = await self._db.get_recent_chat_logs(
            self._story_id, limit=max(12, self._eval_interval * 8)
        )
        recent_closed_scenes = await self._db.get_recent_closed_story_scenes(
            self._story_id,
            since_turn=since_turn,
            limit=10,
            scene_type="conversation",
        )
        scene_pacing_score = min(
            1.0,
            len(recent_closed_scenes) / max(1.0, self._eval_interval * 1.5),
        )
        chat_pacing_score = min(1.0, len(recent_logs) / max(1.0, self._eval_interval * 3.0))
        pacing_score = (scene_pacing_score + chat_pacing_score) / 2.0
        pacing_sat = max(0.0, 1.0 - abs(action_pref - pacing_score))

        # -- surprise_sat -------------------------------------------------
        active_patterns = await self._db.get_active_interaction_patterns(self._story_id)
        ivs = await self._db.get_active_interventions(self._story_id, turn_number)
        pattern_diversity = len(
            {
                str(pattern.get("pattern_type", "")).strip()
                for pattern in active_patterns
                if str(pattern.get("pattern_type", "")).strip()
            }
        )
        iv_score = min(1.0, len(ivs) / 5.0)
        pattern_score = min(1.0, pattern_diversity / 4.0)
        surprise_score = (iv_score + pattern_score) / 2.0
        surprise_sat = max(0.0, 1.0 - abs(curiosity - surprise_score))

        # -- character_depth_sat ------------------------------------------
        recent_events = await self._db.get_recent_relationship_events(
            self._story_id,
            since_turn=since_turn,
            limit=20,
        )
        recent_growth = await self._db.count_recent_character_evolutions(
            self._story_id,
            since_turn=since_turn,
        )
        depth_score = min(1.0, (recent_growth + len(recent_events)) / 4.0)
        char_depth_sat = max(0.0, 1.0 - abs(depth_pref - depth_score))

        # -- dialogue_sat -------------------------------------------------
        spoken_logs = [log for log in recent_logs if str(log.get("char_id")) != "_narrator"]
        reply_logs = [log for log in spoken_logs if log.get("msg_type") == "reply"]
        reply_ratio = (
            len(reply_logs) / len(spoken_logs)
            if spoken_logs
            else 0.0
        )
        fallback_count = await self._db.count_generation_quality_issues(
            self._story_id,
            since_turn=since_turn,
            auto_action="fallback",
        )
        fallback_rate = min(1.0, fallback_count / max(1.0, len(spoken_logs)))
        dialogue_score = max(
            0.0,
            min(1.0, (reply_ratio * 0.7) + ((1.0 - fallback_rate) * 0.3)),
        )
        dialogue_sat = max(0.0, 1.0 - abs(dialogue_wit - dialogue_score))

        # -- atmosphere_sat -----------------------------------------------
        narration_count = sum(
            1 for log in recent_logs if str(log.get("msg_type", "")).startswith("narration")
        )
        scene_prose_present = any(
            log.get("msg_type") == "narration_scene" for log in recent_logs
        )
        atmo_score = min(1.0, narration_count / max(1.0, self._eval_interval * 0.5))
        if scene_prose_present:
            atmo_score = min(1.0, atmo_score + 0.2)
        atmosphere_sat = max(0.0, 1.0 - abs(atmo_pref - atmo_score))

        # -- overall ------------------------------------------------------
        weights = {
            "tension_sat": max(0.1, tension_pref),
            "character_depth_sat": max(0.1, depth_pref),
            "pacing_sat": max(0.1, action_pref),
            "surprise_sat": max(0.1, curiosity),
            "dialogue_sat": max(0.1, dialogue_wit),
            "atmosphere_sat": max(0.1, atmo_pref),
        }
        raw_overall = (
            (tension_sat * weights["tension_sat"])
            + (char_depth_sat * weights["character_depth_sat"])
            + (pacing_sat * weights["pacing_sat"])
            + (surprise_sat * weights["surprise_sat"])
            + (dialogue_sat * weights["dialogue_sat"])
            + (atmosphere_sat * weights["atmosphere_sat"])
        ) / sum(weights.values())
        decayed_previous = max(0.0, min(1.0, self._last_overall - self._decay))
        overall = max(0.0, min(1.0, (raw_overall * 0.7) + (decayed_previous * 0.3)))

        return {
            "overall": overall,
            "tension_sat": tension_sat,
            "character_depth_sat": char_depth_sat,
            "pacing_sat": pacing_sat,
            "surprise_sat": surprise_sat,
            "dialogue_sat": dialogue_sat,
            "atmosphere_sat": atmosphere_sat,
            "details": {
                "active_tensions": len(tensions),
                "max_intensity": round(max_intensity, 3),
                "active_interventions": len(ivs),
                "active_patterns": len(active_patterns),
                "recent_growth": recent_growth,
                "recent_relationship_events": len(recent_events),
                "recent_logs": len(recent_logs),
                "reply_ratio": round(reply_ratio, 3),
                "fallback_count": fallback_count,
                "narration_count": narration_count,
            },
        }

    async def _llm_evaluate_satisfaction(
        self, sat: dict[str, Any]
    ) -> dict[str, Any]:
        """LLM による character_depth_sat / dialogue_sat の評価と suggestion 生成。

        失敗時は sat を変更せず返す。
        """
        if self._llm_router is None or not self._active_persona:
            return sat
        try:
            persona = self._active_persona
            details = sat.get("details", {})
            system_prompt = (
                f"あなたは {persona.get('name', '演出家')} のような映画監督・演出家です。"
                "提示された展開を読み、演出の観点から評価してください。"
            )
            user_prompt = (
                "【あなたの美学】\n"
                f"values: {persona.get('values_json', [])}\n"
                f"traits: {persona.get('traits_json', [])}\n\n"
                "【直近の展開】\n"
                f"active_tensions: {details.get('active_tensions', 0)}\n"
                f"active_interventions: {details.get('active_interventions', 0)}\n"
                f"recent_logs_count: {details.get('recent_logs', 0)}\n"
                f"narration_count: {details.get('narration_count', 0)}\n\n"
                "以下の JSON 形式のみで回答してください:\n"
                '{"character_depth_sat": 0.0から1.0, '
                '"dialogue_sat": 0.0から1.0, '
                '"overall_delta": -0.2から0.2, '
                '"suggestion": "次ターンへの演出指示（日本語、1から2文）"}'
            )
            response = await self._llm_router.generate(
                provider=self._llm_provider,
                model=self._llm_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=200,
                request_tag=f"director_persona:satisfaction:{persona.get('id', 'active')}",
            )
            data = _parse_json_response(response.text)
            if data is None:
                return sat
            sat = dict(sat)
            sat["character_depth_sat"] = max(0.0, min(1.0, float(
                data.get("character_depth_sat", sat["character_depth_sat"])
            )))
            sat["dialogue_sat"] = max(0.0, min(1.0, float(
                data.get("dialogue_sat", sat["dialogue_sat"])
            )))
            delta = float(data.get("overall_delta", 0.0))
            sat["overall"] = max(0.0, min(1.0, round(sat["overall"] + delta, 3)))
            suggestion = str(data.get("suggestion", "")).strip()
            if suggestion:
                self._last_llm_suggestion = suggestion
                details_copy = dict(sat.get("details", {}))
                details_copy["llm_suggestion"] = suggestion
                sat["details"] = details_copy
                # _update_steering_signals() が次回呼ばれた時に tone_hints に反映される
                self._update_steering_signals()
        except Exception as exc:
            logger.warning(
                "LLM satisfaction evaluation failed, using deterministic values",
                extra={"story_id": self._story_id, "error": str(exc)},
            )
        return sat

    def _compute_trend(self, overall: float) -> str:
        """前回の overall との差から trend を返す。"""
        delta = overall - self._last_overall
        if delta > 0.1:
            return "rising"
        if delta < -0.1:
            return "declining"
        return "flat"

    def _update_steering_signals(self) -> None:
        """active_persona の美学パラメータからステアリングシグナルを再計算する。"""
        if self._active_persona is None:
            self._steering = SteeringSignals.empty()
            return

        aesthetic = dict(self._active_persona.get("aesthetic_json") or {})
        tension_pref = float(aesthetic.get("tension_preference", 0.5))
        character_depth = float(aesthetic.get("character_depth", 0.5))
        action_pref = float(aesthetic.get("action_preference", 0.5))
        curiosity = float(aesthetic.get("curiosity", 0.5))
        atmo_weight = float(aesthetic.get("atmosphere_weight", 0.5))
        dialogue_wit = float(aesthetic.get("dialogue_wit", 0.5))

        def _w(pref: float) -> float:
            """選好度 0.0〜1.0 → steering_strength を反映した重みに変換する。"""
            strength_ranges = {
                "subtle": (0.75, 1.25),
                "moderate": (0.5, 1.5),
                "strong": (0.25, 1.75),
            }
            low, high = strength_ranges.get(self._steering_strength, strength_ranges["moderate"])
            return low + ((high - low) * pref)

        tension_type_weights: dict[str, float] = {
            "conflict": _w(tension_pref),
            "crisis": _w(tension_pref * 0.9),
            "rivalry": _w(tension_pref * 0.8),
            "mystery": _w(curiosity),
            "secret": _w(curiosity * 0.85),
            "romantic": _w(character_depth * 0.8),
            "moral_dilemma": _w(character_depth * 0.75),
            "betrayal": _w(max(tension_pref, character_depth) * 0.8),
        }
        # v2 upgrade Phase 3: VALID_INTERVENTION_TYPES に合わせたキーを使用
        # {"plot_twist", "mood", "relationship_catalyst", "revelation", "crisis", "opportunity"}
        intervention_type_weights: dict[str, float] = {
            "plot_twist": _w(curiosity),
            "revelation": _w(curiosity * 0.8),
            "relationship_catalyst": _w(character_depth),
            "mood": _w(atmo_weight),
            "crisis": _w(tension_pref),
            "opportunity": _w(action_pref),
        }
        pattern_type_weights: dict[str, float] = {
            "conflict": _w(tension_pref),
            "mystery": _w(curiosity),
            "near_reveal": _w(character_depth),
            "misunderstanding": _w(dialogue_wit * 0.8),
            "small_win_loss": _w(action_pref),
            "status_clash": _w(max(tension_pref, action_pref) * 0.8),
        }
        preferred_event_flavors = _build_preferred_event_flavors(
            values=list(self._active_persona.get("values_json") or []),
            traits=list(self._active_persona.get("traits_json") or []),
            aesthetic=aesthetic,
        )
        quality_leniency = {
            "poetic_abstraction": round(atmo_weight, 3),
            "multi_clause_heaviness": round(atmo_weight * 0.8, 3),
            "low_concreteness": round(atmo_weight * 0.8, 3),
            "abstract_opening": round(atmo_weight * 0.6, 3),
            "reply_without_direct_reaction": round(max(0.0, 1.0 - dialogue_wit), 3),
            "reply_focus_missing": round(max(0.0, 1.0 - dialogue_wit), 3),
        }

        values: list[str] = list(self._active_persona.get("values_json") or [])
        traits: list[str] = list(self._active_persona.get("traits_json") or [])
        tone_hints = _build_tone_hints(values, traits, aesthetic)

        # Phase 3 optional: LLM suggestion があれば tone_hints の末尾に追加
        if self._last_llm_suggestion:
            tone_hints = list(tone_hints)
            tone_hints.append(self._last_llm_suggestion)

        self._steering = SteeringSignals(
            tension_type_weights=tension_type_weights,
            intervention_type_weights=intervention_type_weights,
            pattern_type_weights=pattern_type_weights,
            preferred_event_flavors=preferred_event_flavors,
            quality_leniency=quality_leniency,
            tone_hints=tone_hints,
        )


# ------------------------------------------------------------------
# module-level helper
# ------------------------------------------------------------------

def _build_tone_hints(
    values: list[str],
    traits: list[str],
    aesthetic: dict[str, float],
) -> list[str]:
    """values / traits / aesthetic から演出指示テキストを最大 3 件生成する。"""
    hints: list[str] = []

    tension_pref    = float(aesthetic.get("tension_preference", 0.5))
    character_depth = float(aesthetic.get("character_depth", 0.5))
    dialogue_wit    = float(aesthetic.get("dialogue_wit", 0.5))
    atmo_weight     = float(aesthetic.get("atmosphere_weight", 0.5))

    # 美学パラメータ由来のヒント
    if tension_pref > 0.7:
        hints.append("重厚な緊張感のある展開を意識すること")
    if character_depth > 0.7:
        hints.append("登場人物の内面と感情の変化を丁寧に描くこと")
    if dialogue_wit > 0.7:
        hints.append("台詞の機知や掛け合いを活かすこと")
    if atmo_weight > 0.7:
        hints.append("情景描写と雰囲気の演出を重視すること")

    # traits 由来のヒント
    trait_hints: dict[str, str] = {
        "沈黙の演出": "沈黙や間を積極的に活用すること",
        "長回し好き": "場面を急がずじっくりと展開してよい",
        "長台詞好き": "じっくりと台詞を展開してよい",
        "食事描写重視": "食事や日常の細部を丁寧に描写すること",
    }
    for trait in traits:
        if trait in trait_hints and trait_hints[trait] not in hints:
            hints.append(trait_hints[trait])

    # 最大 3 件
    return hints[:3]


def _build_preferred_event_flavors(
    values: list[str],
    traits: list[str],
    aesthetic: dict[str, float],
) -> list[str]:
    """persona から chapter event 選好 flavor を構築する。"""
    flavors: list[str] = []
    if float(aesthetic.get("tension_preference", 0.5)) >= 0.65:
        flavors.extend(["conflict", "crisis"])
    if float(aesthetic.get("character_depth", 0.5)) >= 0.65:
        flavors.extend(["relationship", "emotion", "revelation"])
    if float(aesthetic.get("curiosity", 0.5)) >= 0.65:
        flavors.extend(["twist", "mystery", "revelation"])
    if float(aesthetic.get("action_preference", 0.5)) >= 0.65:
        flavors.extend(["action", "opportunity"])
    if float(aesthetic.get("atmosphere_weight", 0.5)) >= 0.65:
        flavors.extend(["atmosphere", "mood"])
    if any("葛藤" in value for value in values):
        flavors.append("conflict")
    if "沈黙の演出" in traits:
        flavors.append("mood")
    # 順序を保ったまま重複除去
    unique: list[str] = []
    for flavor in flavors:
        if flavor not in unique:
            unique.append(flavor)
    return unique


def _parse_json_response(text: str) -> dict[str, Any] | None:
    """LLM レスポンスから JSON オブジェクトを抽出してパースする。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    for pattern in (r"\{[^{}]*\}", r"\{.*\}"):
        m = re.search(pattern, cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None
