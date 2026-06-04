"""engine/story_director.py — ストーリー演出家。

「緊張検出（LLM）→ 状態遷移（pure DB）→ 介入生成（LLM）」の
3フェーズを毎 N ラウンドに実行し、
narrative_tensions / director_interventions テーブルを管理する。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager
    from engine.config import StoryDirectorConfig
    from engine.llm.router import LLMRouter
    from engine.story_intent import StoryIntentProfile
    from engine.director_persona import DirectorPersona

from engine.story_intent import DEFAULT_INTENT_PROFILE
from engine.interaction_patterns import PATTERN_TO_TENSION_PREFERENCES
from engine.relationship_modes import (
    RELATIONSHIP_MODE_TO_INTERVENTION_PREFERENCES,
    RELATIONSHIP_MODE_TO_TENSION_PREFERENCES,
)
from engine.emergent_canonizer import (
    CANON_TO_INTERVENTION_PREFERENCES,
    CANON_TO_TENSION_PREFERENCES,
)
from engine.dramatic_pressure import (
    PRESSURE_TO_INTERVENTION_PREFERENCES,
    PRESSURE_TO_TENSION_PREFERENCES,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 定数
# ------------------------------------------------------------------

def _safe_float(value: Any, default: float) -> float:
    """値を float に安全に変換する。失敗時は default を返す。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


VALID_TENSION_TYPES = frozenset({
    "romantic", "conflict", "secret", "rivalry",
    "crisis", "mystery", "moral_dilemma", "betrayal",
})

VALID_TENSION_STATUSES = frozenset({
    "simmering", "escalating", "climax", "resolving", "resolved",
})

VALID_INTERVENTION_TYPES = frozenset({
    "plot_twist", "mood", "relationship_catalyst",
    "revelation", "crisis", "opportunity",
})

_SEED_INTENSITY_BY_TYPE = {
    "conflict": 0.55,
    "mystery": 0.4,
    "crisis": 0.5,
    "rivalry": 0.5,
    "secret": 0.45,
}
_SECRET_OUTCOME_CUES = ("秘密", "隠", "言えない", "別の声", "demo")
_FALLBACK_INTERVENTION_BY_TENSION = {
    "conflict": "relationship_catalyst",
    "rivalry": "relationship_catalyst",
    "mystery": "revelation",
    "secret": "revelation",
    "crisis": "crisis",
    "moral_dilemma": "opportunity",
}


# ------------------------------------------------------------------
# コンテキスト
# ------------------------------------------------------------------

@dataclass
class StoryDirectorContext:
    """process_round に渡すコンテキスト情報。"""

    story_id: str
    turn_number: int
    world_rules: str
    recent_memory_texts: list[str] = field(default_factory=list)
    recent_scene_outcomes: list[str] = field(default_factory=list)
    open_hook_summaries: list[str] = field(default_factory=list)
    recent_relationship_event_summaries: list[str] = field(default_factory=list)
    active_episode_id: int | None = None
    active_episode_type: str | None = None
    active_episode_goal: str | None = None
    active_episode_stakes: str | None = None
    active_episode_pattern_type: str | None = None
    carry_over_hook_summaries: list[str] = field(default_factory=list)
    episode_age_turns: int = 0


# ------------------------------------------------------------------
# StoryDirector
# ------------------------------------------------------------------

class StoryDirector:
    """ストーリー演出家。

    毎 analysis_interval_rounds ごとに:
      1. 緊張検出（LLM）
      2. 緊張エスカレーション（DB）
      3. 介入生成（LLM）
    を実行する。
    """

    def __init__(
        self,
        config: StoryDirectorConfig,
        db: DatabaseManager,
        llm_router: LLMRouter,
        llm_provider: str = "",
        llm_model: str = "",
        intent_profile: StoryIntentProfile | None = None,
        director_persona: "DirectorPersona | None" = None,
    ) -> None:
        self._config = config
        self._db = db
        self._llm = llm_router
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._intent_profile = intent_profile or DEFAULT_INTENT_PROFILE
        self._director_persona = director_persona

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    async def process_round(
        self,
        turn_number: int,
        ctx: StoryDirectorContext,
    ) -> None:
        """ラウンド処理のエントリポイント。"""
        if not self._config.enabled:
            return
        if turn_number % self._config.analysis_interval_rounds != 0:
            return

        story_id = ctx.story_id

        # Phase 1: 既存状態の deterministic 更新
        active_tensions = await self._db.get_active_tensions(story_id)
        await self._escalate_tensions(turn_number, active_tensions)
        await self._seed_tensions(turn_number, ctx, active_tensions)
        active_tensions = await self._db.get_active_tensions(story_id)
        active_interventions = await self._db.get_active_interventions(story_id, turn_number)
        active_patterns = await self._db.get_active_interaction_patterns(story_id)
        active_relationship_modes = await self._db.get_active_relationship_modes(story_id)
        active_canon_bits = await self._db.get_active_story_canon_bits(story_id)
        active_dramatic_pressures = await self._db.get_active_story_dramatic_pressures(story_id)

        # Phase 2: director analysis
        should_analyze = (
            len(active_tensions) < self._config.max_active_tensions
            or bool(active_interventions)
            or bool(ctx.recent_scene_outcomes)
            or bool(ctx.open_hook_summaries)
            or bool(ctx.recent_relationship_event_summaries)
        )
        analysis = (
            await self._detect_tensions(ctx, active_tensions, active_interventions)
            if should_analyze
            else {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        )
        await self._apply_tension_updates(
            turn_number,
            active_tensions,
            analysis.get("tension_updates", []),
        )
        active_tensions = await self._db.get_active_tensions(story_id)
        merged_tension_map = await self._apply_merge_groups(
            turn_number,
            active_tensions,
            analysis.get("merge_groups", []),
        )
        if merged_tension_map:
            await self._resolve_linked_interventions(
                story_id,
                merged_tension_map,
            )
        await self._resolve_requested_interventions(
            analysis.get("resolve_intervention_ids", []),
            summary="resolved by director analysis",
        )
        active_tensions = await self._db.get_active_tensions(story_id)
        if len(active_tensions) < self._config.max_active_tensions:
            remaining_slots = self._config.max_active_tensions - len(active_tensions)
            new_tensions = sorted(
                analysis.get("new_tensions", []),
                key=lambda tension: (
                    -self._pattern_bonus_for_tension(tension, active_patterns),
                    -self._relationship_mode_bonus_for_tension(tension, active_relationship_modes),
                    -self._canon_bonus_for_tension(tension, active_canon_bits),
                    -self._pressure_bonus_for_tension(tension, active_dramatic_pressures),
                    self._tension_intent_rank(str(tension.get("tension_type", ""))),
                    str(tension.get("description", "")),
                ),
            )
            for t in new_tensions[:remaining_slots]:
                t["detected_turn"] = turn_number
                await self._db.insert_tension(story_id, t)
            if new_tensions:
                active_tensions = await self._db.get_active_tensions(story_id)

        # Phase 3: 介入生成
        active_interventions = await self._db.get_active_interventions(story_id, turn_number)
        inserted = await self._insert_analysis_interventions(
            story_id,
            turn_number,
            analysis.get("new_interventions", []),
            active_interventions,
            active_relationship_modes=active_relationship_modes,
            active_canon_bits=active_canon_bits,
            active_dramatic_pressures=active_dramatic_pressures,
        )
        active_interventions = await self._db.get_active_interventions(story_id, turn_number)
        if inserted == 0 and len(active_interventions) < self._config.max_active_interventions:
            await self._generate_interventions(ctx, active_tensions, active_interventions)
            active_interventions = await self._db.get_active_interventions(story_id, turn_number)
        if (
            inserted == 0
            and len(active_interventions) < self._config.max_active_interventions
        ):
            await self._insert_fallback_intervention(
                ctx,
                active_tensions,
                active_interventions,
                active_patterns=active_patterns,
                active_relationship_modes=active_relationship_modes,
                active_canon_bits=active_canon_bits,
                active_dramatic_pressures=active_dramatic_pressures,
            )

    async def _seed_tensions(
        self,
        turn_number: int,
        ctx: StoryDirectorContext,
        active_tensions: list[dict[str, Any]],
    ) -> int:
        """hook / scene / relationship から deterministic に tension を seed する。"""
        remaining_slots = self._config.max_active_tensions - len(active_tensions)
        if remaining_slots <= 0:
            return 0

        since_turn = max(0, turn_number - self._config.analysis_interval_rounds * 2)
        seed_limit = min(2, remaining_slots)
        existing_keys = {
            self._tension_seed_key(
                str(tension.get("tension_type", "")),
                list(tension.get("involved_chars", [])),
                str(tension.get("description", "")),
            )
            for tension in active_tensions
        }
        inserted = 0
        candidates: list[dict[str, Any]] = []

        open_hooks = await self._db.get_open_story_hooks(ctx.story_id)
        for hook in open_hooks:
            candidate = await self._build_hook_seed(turn_number, hook)
            if candidate is not None:
                candidates.append(candidate)

        recent_scenes = await self._db.get_recent_closed_story_scenes(
            ctx.story_id,
            since_turn=since_turn,
            limit=10,
            scene_type="conversation",
        )
        for scene in recent_scenes:
            candidate = await self._build_scene_outcome_seed(turn_number, scene)
            if candidate is not None:
                candidates.append(candidate)

        relationship_events = await self._db.get_recent_relationship_events(
            ctx.story_id,
            since_turn=since_turn,
            limit=20,
        )
        candidates.extend(self._build_relationship_seeds(turn_number, relationship_events))

        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (
                self._tension_intent_rank(str(candidate.get("tension_type", ""))),
                str(candidate.get("description", "")),
            ),
        )
        for candidate in sorted_candidates:
            key = self._tension_seed_key(
                candidate["tension_type"],
                candidate.get("involved_chars", []),
                candidate["description"],
            )
            if key in existing_keys:
                continue
            await self._db.insert_tension(ctx.story_id, candidate)
            existing_keys.add(key)
            inserted += 1
            if inserted >= seed_limit:
                break
        return inserted

    # ------------------------------------------------------------------
    # 内部メソッド — 緊張検出
    # ------------------------------------------------------------------

    async def _detect_tensions(
        self,
        ctx: StoryDirectorContext,
        active_tensions: list[dict[str, Any]],
        active_interventions: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]] | list[int]]:
        """LLM を使って緊張と介入の更新候補を検出する。"""
        if not any(
            (
                active_tensions,
                active_interventions,
                ctx.recent_memory_texts,
                ctx.recent_scene_outcomes,
                ctx.open_hook_summaries,
                ctx.recent_relationship_event_summaries,
            )
        ):
            return {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }

        compact_for_gemma = self._should_compact_for_gemma()
        existing_descriptions = [t.get("description", "") for t in active_tensions]
        existing_summary = self._compact_tension_summary(active_tensions, compact_for_gemma)
        recent_events = "\n".join(f"- {m}" for m in ctx.recent_memory_texts)
        recent_outcomes = (
            "\n".join(f"- {summary}" for summary in ctx.recent_scene_outcomes)
            if ctx.recent_scene_outcomes
            else "（なし）"
        )
        open_hooks = (
            "\n".join(f"- {summary}" for summary in ctx.open_hook_summaries)
            if ctx.open_hook_summaries
            else "（なし）"
        )
        relationship_events = (
            "\n".join(f"- {summary}" for summary in ctx.recent_relationship_event_summaries)
            if ctx.recent_relationship_event_summaries
            else "（なし）"
        )
        episode_section = (
            "現在のエピソード:\n"
            f"- type: {ctx.active_episode_type or '（なし）'}\n"
            f"- goal: {ctx.active_episode_goal or '（なし）'}\n"
            f"- stakes: {ctx.active_episode_stakes or '（なし）'}\n"
            f"- pattern: {ctx.active_episode_pattern_type or '（なし）'}\n"
            f"- age_turns: {ctx.episode_age_turns}\n"
            f"- carry_over_hooks:\n"
            f"{chr(10).join(f'- {summary}' for summary in ctx.carry_over_hook_summaries) or '（なし）'}\n\n"
        )
        existing_interventions = (
            "\n".join(
                f"- [#{iv['id']}] {iv.get('intervention_type')}: {iv.get('title', '')}"
                f" (status={iv.get('status')}, tension_id={iv.get('tension_id')})"
                for iv in active_interventions
            )
            if active_interventions
            else "（なし）"
        )
        stagnating_tensions = (
            "\n".join(
                f"- [#{t['id']}] {t.get('description', '')}"
                for t in active_tensions
                if self._config.stagnation_detection
                and t.get("status") in {"simmering", "escalating", "climax"}
                and ctx.turn_number - int(t.get("detected_turn", ctx.turn_number))
                >= self._config.analysis_interval_rounds * 2
                and not any(iv.get("tension_id") == t.get("id") for iv in active_interventions)
            )
            or "（なし）"
        )
        max_count = self._config.max_active_tensions - len(active_tensions)

        system_prompt = (
            "あなたはストーリー分析AIです。"
            "物語の緊張と介入の状態更新をJSONで出力してください。"
        )
        world_rules = self._compact_prompt_text(ctx.world_rules, 120 if compact_for_gemma else 600)
        existing_intervention_summary = self._compact_intervention_summary(
            active_interventions,
            compact_for_gemma,
        )
        stagnating_summary = self._compact_multiline(
            stagnating_tensions,
            2 if compact_for_gemma else 6,
            52 if compact_for_gemma else 180,
        )
        schema_example = (
            '{"new_tensions":[{"tension_type":"conflict","description":"..."}],'
            '"tension_updates":[{"tension_id":1,"new_status":"resolving"}],'
            '"merge_groups":[{"tension_ids":[1,2]}],'
            '"resolve_intervention_ids":[3],'
            '"new_interventions":[{"intervention_type":"crisis","title":"...","description":"..."}]}'
            if compact_for_gemma
            else
            '{"new_tensions": [{"tension_type": "conflict", "description": "...", '
            '"intensity": 0.5, "involved_chars": []}], '
            '"tension_updates": [{"tension_id": 1, "new_status": "resolving", '
            '"intensity": 0.3, "note": "..."}], '
            '"merge_groups": [{"tension_ids": [1, 2], "note": "..."}], '
            '"resolve_intervention_ids": [3], '
            '"new_interventions": [{"intervention_type": "crisis", "title": "...", '
            '"description": "...", "prompt_injection": "...", "scope": "all", '
            '"duration_rounds": 2, "tension_id": 1}]}'
        )
        if compact_for_gemma:
            compact_sections = [
                f"設定:{world_rules}",
                f"出来事:{self._compact_inline_items(ctx.recent_memory_texts, 2, 34)}",
                f"場面:{self._compact_inline_items(ctx.recent_scene_outcomes, 1, 36)}",
                f"フック:{self._compact_inline_items(ctx.open_hook_summaries, 1, 36)}",
                f"関係:{self._compact_inline_items(ctx.recent_relationship_event_summaries, 1, 36)}",
                f"章:{self._compact_episode_summary(ctx)}",
                f"緊張:{existing_summary}",
                f"介入:{existing_intervention_summary}",
                f"停滞:{stagnating_summary}",
            ]
            compact_sections = [
                section for section in compact_sections if not section.endswith("（なし）")
            ]
            compact_sections.append(
                "JSONのみ。各配列は最大1件。説明や note は短く。"
                f" type={ '|'.join(sorted(VALID_TENSION_TYPES)) }"
                f" status={ '|'.join(sorted(VALID_TENSION_STATUSES)) }"
                f" 例:{schema_example}"
            )
            user_prompt = "\n".join(compact_sections)
        else:
            user_prompt = (
                f"世界設定:\n{world_rules}\n\n"
                f"最近の出来事:\n{self._compact_multiline(recent_events, 3 if compact_for_gemma else 8, 56 if compact_for_gemma else 220)}\n\n"
                f"最近閉じた場面:\n{self._compact_multiline(recent_outcomes, 2 if compact_for_gemma else 6, 52 if compact_for_gemma else 200)}\n\n"
                f"未解決のフック:\n{self._compact_multiline(open_hooks, 2 if compact_for_gemma else 6, 52 if compact_for_gemma else 200)}\n\n"
                f"最近の関係変化:\n{self._compact_multiline(relationship_events, 2 if compact_for_gemma else 6, 52 if compact_for_gemma else 200)}\n\n"
                f"{self._compact_prompt_text(episode_section, 140 if compact_for_gemma else 900)}"
                f"既存の緊張:\n{existing_summary}\n\n"
                f"現在 live な介入:\n{existing_intervention_summary}\n\n"
                f"停滞候補の緊張:\n{stagnating_summary}\n\n"
                f"以下の JSON オブジェクトを出力してください。"
                f"new_tensions は最大{max_count}件、tension_typeは{sorted(VALID_TENSION_TYPES)}のいずれか。"
                f"tension_updates の new_status は {sorted(VALID_TENSION_STATUSES)} のいずれか。\n"
                f"例: {schema_example}"
            )

        response = await self._llm.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider=self._llm_provider,
            model=self._llm_model,
            request_tag=f"story_director:analysis:turn_{ctx.turn_number}",
            **self._generation_kwargs(max_tokens=320),
        )
        return self._parse_tension_analysis(
            response.text,
            max_count=max_count,
            valid_tension_ids={int(t["id"]) for t in active_tensions},
            valid_intervention_ids={int(iv["id"]) for iv in active_interventions},
            turn_number=ctx.turn_number,
        )

    def _parse_tension_analysis(
        self,
        text: str,
        *,
        max_count: int,
        valid_tension_ids: set[int],
        valid_intervention_ids: set[int],
        turn_number: int,
    ) -> dict[str, list[dict[str, Any]] | list[int]]:
        """LLM 応答テキストから緊張候補・更新候補・介入候補をパースする。"""
        cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
        data: Any = None
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            if cleaned.startswith("["):
                m = re.search(r"\[.*\]", cleaned, re.DOTALL)
            else:
                m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        if isinstance(data, list):
            return {
                "new_tensions": self._parse_tension_list(data, max_count),
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }
        if not isinstance(data, dict):
            return {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [],
            }

        if "intervention_type" in data:
            intervention = self._normalize_intervention_data(data, turn_number)
            return {
                "new_tensions": [],
                "tension_updates": [],
                "merge_groups": [],
                "resolve_intervention_ids": [],
                "new_interventions": [intervention] if intervention is not None else [],
            }

        raw_new_tensions = data.get("new_tensions", [])
        raw_updates = data.get("tension_updates", [])
        raw_merge_groups = data.get("merge_groups", [])
        raw_resolve_intervention_ids = data.get("resolve_intervention_ids", [])
        raw_new_interventions = data.get("new_interventions", [])
        result_updates: list[dict[str, Any]] = []
        if isinstance(raw_updates, list):
            for item in raw_updates:
                if not isinstance(item, dict):
                    continue
                tension_id = item.get("tension_id")
                try:
                    tension_id = int(tension_id)
                except (TypeError, ValueError):
                    continue
                if tension_id not in valid_tension_ids:
                    continue
                new_status = str(item.get("new_status", "")).strip()
                if new_status not in VALID_TENSION_STATUSES:
                    continue
                patch: dict[str, Any] = {"tension_id": tension_id, "new_status": new_status}
                if "intensity" in item:
                    patch["intensity"] = max(
                        0.0,
                        min(1.0, _safe_float(item.get("intensity"), 0.3)),
                    )
                note = str(item.get("note", "")).strip()
                if note:
                    patch["note"] = note
                result_updates.append(patch)

        result_merge_groups: list[dict[str, Any]] = []
        if isinstance(raw_merge_groups, list):
            for item in raw_merge_groups:
                if not isinstance(item, dict):
                    continue
                tension_ids = item.get("tension_ids", [])
                if not isinstance(tension_ids, list):
                    continue
                normalized_ids: list[int] = []
                for tension_id in tension_ids:
                    try:
                        normalized_id = int(tension_id)
                    except (TypeError, ValueError):
                        continue
                    if normalized_id in valid_tension_ids and normalized_id not in normalized_ids:
                        normalized_ids.append(normalized_id)
                if len(normalized_ids) < 2:
                    continue
                note = str(item.get("note", "")).strip()
                result_merge_groups.append(
                    {"tension_ids": normalized_ids, "note": note}
                )

        resolve_intervention_ids: list[int] = []
        if isinstance(raw_resolve_intervention_ids, list):
            for intervention_id in raw_resolve_intervention_ids:
                try:
                    normalized_id = int(intervention_id)
                except (TypeError, ValueError):
                    continue
                if normalized_id in valid_intervention_ids and normalized_id not in resolve_intervention_ids:
                    resolve_intervention_ids.append(normalized_id)

        new_interventions: list[dict[str, Any]] = []
        if isinstance(raw_new_interventions, list):
            for item in raw_new_interventions:
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize_intervention_data(item, turn_number)
                if normalized is not None:
                    new_interventions.append(normalized)

        raw_tensions = raw_new_tensions if isinstance(raw_new_tensions, list) else []
        return {
            "new_tensions": self._parse_tension_list(raw_tensions, max_count),
            "tension_updates": result_updates,
            "merge_groups": result_merge_groups,
            "resolve_intervention_ids": resolve_intervention_ids,
            "new_interventions": new_interventions,
        }

    def _parse_tension_list(
        self,
        items: list[Any],
        max_count: int,
    ) -> list[dict[str, Any]]:
        """JSON list から新規 tension 候補を取り出す。"""
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            t_type = item.get("tension_type", "")
            if t_type not in VALID_TENSION_TYPES:
                continue
            item["intensity"] = max(0.0, min(1.0, _safe_float(item.get("intensity", 0.3), 0.3)))
            item.setdefault("involved_chars", [])
            item["status"] = "simmering"
            result.append(item)
        return result

    async def _apply_tension_updates(
        self,
        turn_number: int,
        active_tensions: list[dict[str, Any]],
        updates: list[dict[str, Any]],
    ) -> None:
        """既存 tension への状態更新を適用する。"""
        active_by_id = {int(t["id"]): t for t in active_tensions}
        for update in updates:
            tension_id = int(update["tension_id"])
            if tension_id not in active_by_id:
                continue
            patch: dict[str, Any] = {"status": update["new_status"]}
            if "intensity" in update:
                patch["intensity"] = update["intensity"]
            if update.get("note"):
                patch["resolution_note"] = update["note"]
            if update["new_status"] == "resolved":
                patch["resolved_turn"] = turn_number
            await self._db.update_tension(tension_id, patch)

    async def _apply_merge_groups(
        self,
        turn_number: int,
        active_tensions: list[dict[str, Any]],
        merge_groups: list[dict[str, Any]],
    ) -> dict[int, int]:
        """duplicate tension を oldest survivor へ統合し、duplicate->survivor を返す。"""
        active_ids = {int(t["id"]) for t in active_tensions}
        resolved_ids: dict[int, int] = {}
        for group in merge_groups:
            tension_ids = [
                int(tension_id)
                for tension_id in group.get("tension_ids", [])
                if int(tension_id) in active_ids
            ]
            if len(tension_ids) < 2:
                continue
            survivor_id = min(tension_ids)
            note = str(group.get("note") or "").strip()
            resolution_note = note or f"Merged into tension #{survivor_id}."
            for duplicate_id in sorted(tension_ids):
                if duplicate_id == survivor_id:
                    continue
                await self._db.update_tension(
                    duplicate_id,
                    {
                        "status": "resolved",
                        "resolved_turn": turn_number,
                        "resolution_note": f"{resolution_note} survivor=#{survivor_id}",
                    },
                )
                resolved_ids[duplicate_id] = survivor_id
        return resolved_ids

    async def _insert_fallback_intervention(
        self,
        ctx: StoryDirectorContext,
        active_tensions: list[dict[str, Any]],
        active_interventions: list[dict[str, Any]],
        *,
        active_patterns: list[dict[str, Any]] | None = None,
        active_relationship_modes: list[dict[str, Any]] | None = None,
        active_canon_bits: list[dict[str, Any]] | None = None,
        active_dramatic_pressures: list[dict[str, Any]] | None = None,
    ) -> int:
        """analysis が介入を返さない場合に deterministic fallback を1件挿入する。"""
        existing_tension_ids = {
            int(tension_id)
            for tension_id in (
                intervention.get("tension_id") for intervention in active_interventions
            )
            if tension_id is not None
        }
        recent_interventions = await self._list_recent_interventions(ctx.story_id, ctx.turn_number)
        candidate = self._select_fallback_tension(
            ctx.turn_number,
            active_tensions,
            existing_tension_ids,
            recent_tension_ids=self._recent_tension_ids(recent_interventions),
            active_patterns=active_patterns,
            active_relationship_modes=active_relationship_modes,
            active_canon_bits=active_canon_bits,
            active_dramatic_pressures=active_dramatic_pressures,
        )
        if candidate is None:
            return 0

        intervention_type = _FALLBACK_INTERVENTION_BY_TENSION.get(
            str(candidate.get("tension_type", "")),
            "plot_twist",
        )
        intervention_type = self._preferred_relationship_mode_intervention_type(
            intervention_type,
            candidate,
            active_relationship_modes or [],
        )
        intervention_type = self._preferred_canon_intervention_type(
            intervention_type,
            candidate,
            active_canon_bits or [],
        )
        intervention_type = self._preferred_pressure_intervention_type(
            intervention_type,
            candidate,
            active_dramatic_pressures or [],
        )
        intervention_type = self._preferred_fallback_intervention_type(intervention_type)
        involved_chars = [
            char_id.strip()
            for char_id in candidate.get("involved_chars", [])
            if isinstance(char_id, str) and char_id.strip()
        ]
        scope = "all"
        if 2 <= len(involved_chars) <= 3:
            scope = f"char:{','.join(involved_chars)}"

        payload = {
            "intervention_type": intervention_type,
            "title": self._fallback_intervention_title(intervention_type, candidate),
            "description": self._fallback_intervention_description(intervention_type, candidate),
            "prompt_injection": self._fallback_prompt_injection(intervention_type, candidate),
            "scope": scope,
            "tension_id": int(candidate["id"]),
            "active_from_turn": ctx.turn_number,
            "active_until_turn": self._active_until_turn(
                ctx.turn_number,
                self._config.min_intervention_duration_rounds,
            ),
            "status": "active",
        }
        scope_counts = self._active_scope_counts(active_interventions)
        if not self._can_insert_intervention(payload, scope_counts, recent_interventions):
            return 0
        await self._db.insert_intervention(ctx.story_id, payload)
        return 1

    async def _resolve_requested_interventions(
        self,
        intervention_ids: list[int],
        *,
        summary: str,
    ) -> None:
        """analysis が指定した intervention を resolved にする。"""
        for intervention_id in intervention_ids:
            await self._db.update_intervention(
                intervention_id,
                {
                    "status": "resolved",
                    "resolution_summary": summary,
                },
            )

    async def _resolve_linked_interventions(
        self,
        story_id: str,
        merged_tension_map: dict[int, int],
    ) -> None:
        """closed/merged tension に紐づく live intervention を resolved にする。"""
        rows = await self._db.get_live_interventions_for_tension_ids(
            story_id,
            list(merged_tension_map),
        )
        for row in rows:
            tension_id = int(row.get("tension_id"))
            survivor_id = merged_tension_map.get(tension_id)
            await self._db.update_intervention(
                int(row["id"]),
                {
                    "status": "resolved",
                    "resolution_summary": f"merged into tension #{survivor_id}",
                },
            )

    async def _insert_analysis_interventions(
        self,
        story_id: str,
        turn_number: int,
        interventions: list[dict[str, Any]],
        active_interventions: list[dict[str, Any]],
        *,
        active_relationship_modes: list[dict[str, Any]] | None = None,
        active_canon_bits: list[dict[str, Any]] | None = None,
        active_dramatic_pressures: list[dict[str, Any]] | None = None,
    ) -> int:
        """analysis から返った intervention を上限まで挿入する。"""
        available_slots = self._config.max_active_interventions - len(active_interventions)
        if available_slots <= 0:
            return 0
        inserted = 0
        recent_interventions = await self._list_recent_interventions(story_id, turn_number)
        scope_counts = self._active_scope_counts(active_interventions)
        sorted_interventions = sorted(
            interventions,
            key=lambda intervention: (
                -self._relationship_mode_bonus_for_intervention(
                    intervention,
                    active_relationship_modes or [],
                ),
                -self._canon_bonus_for_intervention(
                    intervention,
                    active_canon_bits or [],
                ),
                -self._pressure_bonus_for_intervention(
                    intervention,
                    active_dramatic_pressures or [],
                ),
                self._intervention_intent_rank(str(intervention.get("intervention_type", ""))),
                str(intervention.get("title", "")),
            ),
        )
        for intervention in sorted_interventions[:available_slots]:
            payload = dict(intervention)
            payload.setdefault("active_from_turn", turn_number)
            if not self._can_insert_intervention(payload, scope_counts, recent_interventions):
                continue
            await self._db.insert_intervention(story_id, payload)
            self._register_inserted_intervention(payload, scope_counts, recent_interventions)
            inserted += 1
        return inserted

    # ------------------------------------------------------------------
    # 内部メソッド — エスカレーション
    # ------------------------------------------------------------------

    async def _escalate_tensions(
        self,
        turn_number: int,
        active_tensions: list[dict[str, Any]],
    ) -> None:
        """simmering で一定期間経過した緊張を escalating に遷移させる。"""
        for tension in active_tensions:
            if tension.get("status") != "simmering":
                continue
            detected_turn = tension.get("detected_turn", 0)
            elapsed = turn_number - detected_turn
            if elapsed >= self._config.tension_escalation_rounds:
                await self._db.update_tension(tension["id"], {"status": "escalating"})
                logger.info(
                    "緊張がエスカレーション: id=%d, elapsed=%d", tension["id"], elapsed
                )

    # ------------------------------------------------------------------
    # 内部メソッド — 介入生成
    # ------------------------------------------------------------------

    async def _generate_interventions(
        self,
        ctx: StoryDirectorContext,
        active_tensions: list[dict[str, Any]],
        active_interventions: list[dict[str, Any]],
    ) -> None:
        """escalating 緊張に対して介入を生成・挿入する。"""
        existing_tension_ids = {
            iv.get("tension_id")
            for iv in active_interventions
            if iv.get("tension_id") is not None
        }
        recent_interventions = await self._list_recent_interventions(ctx.story_id, ctx.turn_number)
        scope_counts = self._active_scope_counts(active_interventions)

        for tension in active_tensions:
            if tension.get("status") != "escalating":
                continue
            if tension["id"] in existing_tension_ids:
                continue

            # 上限再チェック
            current = await self._db.get_active_interventions(ctx.story_id, ctx.turn_number)
            if len(current) >= self._config.max_active_interventions:
                break

            intervention = await self._generate_intervention(ctx, tension)
            if intervention is None:
                continue

            intervention["tension_id"] = tension["id"]
            intervention["active_from_turn"] = ctx.turn_number
            if not self._can_insert_intervention(intervention, scope_counts, recent_interventions):
                continue
            await self._db.insert_intervention(ctx.story_id, intervention)
            self._register_inserted_intervention(intervention, scope_counts, recent_interventions)
            existing_tension_ids.add(tension["id"])
            logger.info("介入生成: tension_id=%d", tension["id"])

    async def _build_hook_seed(
        self,
        turn_number: int,
        hook: dict[str, Any],
    ) -> dict[str, Any] | None:
        """open hook から deterministic tension seed を組み立てる。"""
        hook_type = str(hook.get("hook_type", "")).strip()
        tension_type_map = {
            "conflict": "conflict",
            "question": "mystery",
            "promise": "crisis",
        }
        tension_type = tension_type_map.get(hook_type)
        if tension_type is None:
            return None

        owner_char_id = str(hook.get("owner_char_id") or "").strip()
        target_char_id = str(hook.get("target_char_id") or "").strip()
        involved_chars: list[str] = []
        if owner_char_id:
            involved_chars.append(owner_char_id)
        if target_char_id:
            involved_chars.append(target_char_id)
        if not target_char_id and hook.get("source_scene_id") is not None:
            participants = await self._db.get_scene_participants(int(hook["source_scene_id"]))
            involved_chars.extend(
                str(participant.get("char_id") or "").strip()
                for participant in participants
                if str(participant.get("char_id") or "").strip()
            )
        involved_chars = self._normalize_char_ids(involved_chars)
        if not involved_chars:
            return None

        description = self._hook_seed_description(
            hook_type,
            owner_char_id=owner_char_id,
            target_char_id=target_char_id or None,
            involved_chars=involved_chars,
        )
        return {
            "tension_type": tension_type,
            "description": description,
            "involved_chars": involved_chars,
            "intensity": _SEED_INTENSITY_BY_TYPE.get(tension_type, 0.4),
            "detected_turn": turn_number,
            "status": "simmering",
        }

    async def _build_scene_outcome_seed(
        self,
        turn_number: int,
        scene: dict[str, Any],
    ) -> dict[str, Any] | None:
        """closed scene outcome から secret tension を seed する。"""
        summary = str(scene.get("outcome_summary") or "").strip()
        if not summary or not any(cue in summary for cue in _SECRET_OUTCOME_CUES):
            return None
        participants = await self._db.get_scene_participants(int(scene["id"]))
        involved_chars = self._normalize_char_ids(
            [
                str(participant.get("char_id") or "").strip()
                for participant in participants
            ]
        )
        if not involved_chars:
            return None
        if len(involved_chars) == 1:
            description = f"{involved_chars[0]} が抱える秘密がまだ明かされていない。"
        else:
            description = (
                f"{' と '.join(involved_chars)} を含む場面で、秘密めいた違和感が未解決のまま残っている。"
            )
        return {
            "tension_type": "secret",
            "description": description,
            "involved_chars": involved_chars,
            "intensity": _SEED_INTENSITY_BY_TYPE["secret"],
            "detected_turn": turn_number,
            "status": "simmering",
        }

    def _build_relationship_seeds(
        self,
        turn_number: int,
        relationship_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """relationship event 群から rivalry seed を組み立てる。"""
        pair_counter: Counter[tuple[str, str]] = Counter()
        for event in relationship_events:
            delta_tension = _safe_float(event.get("delta_tension"), 0.0)
            if delta_tension <= 0.0:
                continue
            from_id = str(event.get("char_id_from") or "").strip()
            to_id = str(event.get("char_id_to") or "").strip()
            if not from_id or not to_id:
                continue
            pair_counter[tuple(sorted((from_id, to_id)))] += 1

        seeds: list[dict[str, Any]] = []
        for pair, count in pair_counter.items():
            if count < 2:
                continue
            description = f"{pair[0]} と {pair[1]} の張り合いが強まり、火種がくすぶっている。"
            seeds.append(
                {
                    "tension_type": "rivalry",
                    "description": description,
                    "involved_chars": list(pair),
                    "intensity": _SEED_INTENSITY_BY_TYPE["rivalry"],
                    "detected_turn": turn_number,
                    "status": "simmering",
                }
            )
        return seeds

    def _hook_seed_description(
        self,
        hook_type: str,
        *,
        owner_char_id: str,
        target_char_id: str | None,
        involved_chars: list[str],
    ) -> str:
        """hook seed 用の deterministic description を返す。"""
        if hook_type == "question":
            if target_char_id:
                return f"{owner_char_id} から {target_char_id} への問いかけが未解決のまま残っている。"
            return f"{' と '.join(involved_chars)} をまたぐ問いかけが未解決のまま残っている。"
        if hook_type == "promise":
            if target_char_id:
                return f"{owner_char_id} が {target_char_id} に向けた約束がまだ果たされていない。"
            return f"{' と '.join(involved_chars)} を巻き込む約束がまだ果たされていない。"
        if target_char_id:
            return f"{owner_char_id} と {target_char_id} の衝突が未解決のまま残っている。"
        return f"{' と '.join(involved_chars)} を含む場面で対立が未解決のまま残っている。"

    def _tension_seed_key(
        self,
        tension_type: str,
        involved_chars: list[str],
        description: str,
    ) -> tuple[str, tuple[str, ...], str]:
        """deterministic seed の重複判定キー。"""
        return (tension_type, tuple(sorted(self._normalize_char_ids(involved_chars))), description)

    def _normalize_char_ids(self, char_ids: list[str]) -> list[str]:
        """空要素除去 + 重複除去済み char id 配列を返す。"""
        normalized: list[str] = []
        for char_id in char_ids:
            cleaned = str(char_id).strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

    def _select_fallback_tension(
        self,
        turn_number: int,
        active_tensions: list[dict[str, Any]],
        existing_tension_ids: set[int],
        *,
        recent_tension_ids: set[int] | None = None,
        active_patterns: list[dict[str, Any]] | None = None,
        active_relationship_modes: list[dict[str, Any]] | None = None,
        active_canon_bits: list[dict[str, Any]] | None = None,
        active_dramatic_pressures: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """fallback intervention の対象 tension を選ぶ。"""
        priority_statuses = {"escalating", "climax"}
        patterns = active_patterns or []
        recent_ids = recent_tension_ids or set()
        sorted_tensions = sorted(
            active_tensions,
            key=lambda tension: (
                -self._pattern_bonus_for_tension(tension, patterns),
                -self._relationship_mode_bonus_for_tension(
                    tension,
                    active_relationship_modes or [],
                ),
                -self._canon_bonus_for_tension(tension, active_canon_bits or []),
                -self._pressure_bonus_for_tension(tension, active_dramatic_pressures or []),
                self._tension_intent_rank(str(tension.get("tension_type", ""))),
                int(tension.get("id", 0)),
            ),
        )
        for allow_recent in (False, True):
            for tension in sorted_tensions:
                tension_id = int(tension["id"])
                if tension_id in existing_tension_ids:
                    continue
                if not allow_recent and tension_id in recent_ids:
                    continue
                if tension.get("status") in priority_statuses:
                    return tension

        stagnation_threshold = self._config.analysis_interval_rounds * 2
        for allow_recent in (False, True):
            for tension in sorted_tensions:
                tension_id = int(tension["id"])
                if tension_id in existing_tension_ids:
                    continue
                if not allow_recent and tension_id in recent_ids:
                    continue
                if tension.get("status") != "simmering":
                    continue
                if turn_number - int(tension.get("detected_turn", turn_number)) >= stagnation_threshold:
                    return tension
        return None

    def _pattern_bonus_for_tension(
        self,
        tension: dict[str, Any],
        active_patterns: list[dict[str, Any]],
    ) -> int:
        """active pattern と噛み合う tension を優先する。"""
        tension_type = str(tension.get("tension_type") or "").strip()
        tension_id = tension.get("id")
        involved_chars = {
            str(char_id).strip()
            for char_id in tension.get("involved_chars", [])
            if str(char_id).strip()
        }
        best = 0
        for pattern in active_patterns:
            pattern_type = str(pattern.get("pattern_type") or "").strip()
            preferred_types = PATTERN_TO_TENSION_PREFERENCES.get(pattern_type, ())
            if tension_type not in preferred_types:
                continue
            if tension_id is not None and pattern.get("source_tension_id") == tension_id:
                best = max(best, 3)
                continue
            pattern_chars = {
                str(char_id).strip()
                for char_id in pattern.get("involved_chars", [])
                if str(char_id).strip()
            }
            if involved_chars and pattern_chars and involved_chars & pattern_chars:
                best = max(best, 2)
                continue
            best = max(best, 1)
        return best

    def _relationship_mode_bonus_for_tension(
        self,
        tension: dict[str, Any],
        active_relationship_modes: list[dict[str, Any]],
    ) -> int:
        """active relationship mode と噛み合う tension を優先する。"""
        tension_type = str(tension.get("tension_type") or "").strip()
        involved_chars = {
            str(char_id).strip()
            for char_id in tension.get("involved_chars", [])
            if str(char_id).strip()
        }
        cluster_best: dict[str, int] = {}
        for mode in active_relationship_modes:
            preferred_types = RELATIONSHIP_MODE_TO_TENSION_PREFERENCES.get(
                str(mode.get("mode_type") or "").strip(),
                (),
            )
            if tension_type not in preferred_types:
                continue
            mode_chars = {
                str(mode.get("char_id_from") or "").strip(),
                str(mode.get("char_id_to") or "").strip(),
            }
            cluster = "conflict" if str(mode.get("mode_type") or "") in {"irritated_respect", "chaos_partner"} else "attention"
            if involved_chars and mode_chars <= involved_chars:
                cluster_best[cluster] = max(cluster_best.get(cluster, 0), 2)
            elif involved_chars and involved_chars & mode_chars:
                cluster_best[cluster] = max(cluster_best.get(cluster, 0), 1)
        return min(3, sum(cluster_best.values()))

    def _relationship_mode_bonus_for_intervention(
        self,
        intervention: dict[str, Any],
        active_relationship_modes: list[dict[str, Any]],
    ) -> int:
        """active relationship mode と噛み合う intervention を優先する。"""
        intervention_type = str(intervention.get("intervention_type") or "").strip()
        scope = str(intervention.get("scope") or "").strip()
        scoped_chars: set[str] = set()
        if scope.startswith("char:"):
            scoped_chars = {
                char_id.strip()
                for char_id in scope[5:].split(",")
                if char_id.strip()
            }
        cluster_best: dict[str, int] = {}
        for mode in active_relationship_modes:
            preferred_types = RELATIONSHIP_MODE_TO_INTERVENTION_PREFERENCES.get(
                str(mode.get("mode_type") or "").strip(),
                (),
            )
            if intervention_type not in preferred_types:
                continue
            mode_chars = {
                str(mode.get("char_id_from") or "").strip(),
                str(mode.get("char_id_to") or "").strip(),
            }
            cluster = "conflict" if str(mode.get("mode_type") or "") in {"irritated_respect", "chaos_partner"} else "attention"
            if scoped_chars and mode_chars <= scoped_chars:
                cluster_best[cluster] = max(cluster_best.get(cluster, 0), 2)
            elif not scoped_chars:
                cluster_best[cluster] = max(cluster_best.get(cluster, 0), 1)
        return min(3, sum(cluster_best.values()))

    def _canon_bonus_for_tension(
        self,
        tension: dict[str, Any],
        active_canon_bits: list[dict[str, Any]],
    ) -> float:
        tension_type = str(tension.get("tension_type") or "").strip()
        involved_chars = {
            str(char_id).strip()
            for char_id in tension.get("involved_chars", [])
            if str(char_id).strip()
        }
        best = 0.0
        for bit in active_canon_bits:
            preferred_types = CANON_TO_TENSION_PREFERENCES.get(str(bit.get("motif_key") or "").strip(), ())
            if tension_type not in preferred_types:
                continue
            bit_chars = {
                str(char_id).strip()
                for char_id in list(bit.get("focus_char_ids") or [])
                if str(char_id).strip()
            }
            level_weight = self._canon_level_weight(str(bit.get("canon_level") or ""))
            if involved_chars and bit_chars and bit_chars <= involved_chars:
                best = max(best, level_weight + 0.05)
            elif involved_chars and involved_chars & bit_chars:
                best = max(best, level_weight)
        return best

    def _canon_bonus_for_intervention(
        self,
        intervention: dict[str, Any],
        active_canon_bits: list[dict[str, Any]],
    ) -> float:
        intervention_type = str(intervention.get("intervention_type") or "").strip()
        scope = str(intervention.get("scope") or "").strip()
        scoped_chars: set[str] = set()
        if scope.startswith("char:"):
            scoped_chars = {
                char_id.strip()
                for char_id in scope[5:].split(",")
                if char_id.strip()
            }
        best = 0.0
        for bit in active_canon_bits:
            preferred_types = CANON_TO_INTERVENTION_PREFERENCES.get(str(bit.get("motif_key") or "").strip(), ())
            if intervention_type not in preferred_types:
                continue
            bit_chars = {
                str(char_id).strip()
                for char_id in list(bit.get("focus_char_ids") or [])
                if str(char_id).strip()
            }
            level_weight = self._canon_level_weight(str(bit.get("canon_level") or ""))
            if scoped_chars and bit_chars and bit_chars <= scoped_chars:
                best = max(best, level_weight + 0.05)
            elif not scoped_chars:
                best = max(best, level_weight)
        return best

    def _pressure_bonus_for_tension(
        self,
        tension: dict[str, Any],
        active_dramatic_pressures: list[dict[str, Any]],
    ) -> float:
        tension_type = str(tension.get("tension_type") or "").strip()
        involved_chars = {
            str(char_id).strip()
            for char_id in tension.get("involved_chars", [])
            if str(char_id).strip()
        }
        best = 0.0
        for pressure in active_dramatic_pressures:
            preferred_types = PRESSURE_TO_TENSION_PREFERENCES.get(
                str(pressure.get("pressure_type") or "").strip(),
                (),
            )
            if tension_type not in preferred_types:
                continue
            pressure_chars = {
                str(char_id).strip()
                for char_id in list(pressure.get("focus_char_ids") or [])
                if str(char_id).strip()
            }
            score = float(pressure.get("score") or 0.0)
            if involved_chars and pressure_chars and pressure_chars <= involved_chars:
                best = max(best, score + 0.05)
            elif involved_chars and involved_chars & pressure_chars:
                best = max(best, score)
            elif not involved_chars:
                best = max(best, score * 0.5)
        return best

    def _pressure_bonus_for_intervention(
        self,
        intervention: dict[str, Any],
        active_dramatic_pressures: list[dict[str, Any]],
    ) -> float:
        intervention_type = str(intervention.get("intervention_type") or "").strip()
        scope = str(intervention.get("scope") or "").strip()
        scoped_chars: set[str] = set()
        if scope.startswith("char:"):
            scoped_chars = {
                char_id.strip()
                for char_id in scope[5:].split(",")
                if char_id.strip()
            }
        best = 0.0
        for pressure in active_dramatic_pressures:
            preferred_types = PRESSURE_TO_INTERVENTION_PREFERENCES.get(
                str(pressure.get("pressure_type") or "").strip(),
                (),
            )
            if intervention_type not in preferred_types:
                continue
            pressure_chars = {
                str(char_id).strip()
                for char_id in list(pressure.get("focus_char_ids") or [])
                if str(char_id).strip()
            }
            score = float(pressure.get("score") or 0.0)
            if scoped_chars and pressure_chars and pressure_chars <= scoped_chars:
                best = max(best, score + 0.05)
            elif not scoped_chars:
                best = max(best, score)
        return best

    def _preferred_relationship_mode_intervention_type(
        self,
        default_type: str,
        tension: dict[str, Any],
        active_relationship_modes: list[dict[str, Any]],
    ) -> str:
        """fallback intervention type を relationship mode に寄せる。"""
        involved_chars = {
            str(char_id).strip()
            for char_id in tension.get("involved_chars", [])
            if str(char_id).strip()
        }
        best_type = default_type
        best_bonus = 0
        for mode in active_relationship_modes:
            mode_chars = {
                str(mode.get("char_id_from") or "").strip(),
                str(mode.get("char_id_to") or "").strip(),
            }
            if not involved_chars or not (involved_chars & mode_chars):
                continue
            for intervention_type in RELATIONSHIP_MODE_TO_INTERVENTION_PREFERENCES.get(
                str(mode.get("mode_type") or "").strip(),
                (),
            ):
                bonus = 2 if mode_chars <= involved_chars else 1
                if bonus > best_bonus:
                    best_bonus = bonus
                    best_type = intervention_type
        return best_type

    def _preferred_canon_intervention_type(
        self,
        default_type: str,
        tension: dict[str, Any],
        active_canon_bits: list[dict[str, Any]],
    ) -> str:
        involved_chars = {
            str(char_id).strip()
            for char_id in tension.get("involved_chars", [])
            if str(char_id).strip()
        }
        best_type = default_type
        best_bonus = 0.0
        for bit in active_canon_bits:
            bit_chars = {
                str(char_id).strip()
                for char_id in list(bit.get("focus_char_ids") or [])
                if str(char_id).strip()
            }
            if involved_chars and bit_chars and not (involved_chars & bit_chars):
                continue
            for intervention_type in CANON_TO_INTERVENTION_PREFERENCES.get(
                str(bit.get("motif_key") or "").strip(),
                (),
            ):
                bonus = self._canon_level_weight(str(bit.get("canon_level") or ""))
                if bonus > best_bonus:
                    best_bonus = bonus
                    best_type = intervention_type
        return best_type

    def _preferred_pressure_intervention_type(
        self,
        default_type: str,
        tension: dict[str, Any],
        active_dramatic_pressures: list[dict[str, Any]],
    ) -> str:
        involved_chars = {
            str(char_id).strip()
            for char_id in tension.get("involved_chars", [])
            if str(char_id).strip()
        }
        best_type = default_type
        best_bonus = 0.0
        for pressure in active_dramatic_pressures:
            pressure_chars = {
                str(char_id).strip()
                for char_id in list(pressure.get("focus_char_ids") or [])
                if str(char_id).strip()
            }
            if involved_chars and pressure_chars and not (involved_chars & pressure_chars):
                continue
            for intervention_type in PRESSURE_TO_INTERVENTION_PREFERENCES.get(
                str(pressure.get("pressure_type") or "").strip(),
                (),
            ):
                bonus = float(pressure.get("score") or 0.0)
                if bonus > best_bonus:
                    best_bonus = bonus
                    best_type = intervention_type
        return best_type

    @staticmethod
    def _canon_level_weight(level: str) -> float:
        return {
            "momentary_bit": 0.05,
            "recurring_bit": 0.10,
            "proto_canon": 0.15,
            "canon": 0.20,
        }.get(level, 0.0)

    def _tension_intent_rank(self, tension_type: str) -> int:
        """story intent + director persona に基づく tension の優先順位を返す。"""
        if tension_type in self._intent_profile.preferred_tension_types:
            base = 0
        elif tension_type in self._intent_profile.deprioritized_tension_types:
            base = 2
        else:
            base = 1

        # v2 upgrade: director_persona の steering signals による調整
        if self._director_persona is not None:
            w = self._director_persona.get_steering_signals().tension_type_weights.get(
                tension_type, 1.0
            )
            if w >= 1.5:
                base = max(0, base - 1)
            elif w <= 0.5:
                base = min(2, base + 1)
        return base

    def _intervention_intent_rank(self, intervention_type: str) -> int:
        """story intent + director persona に基づく intervention の優先順位を返す。"""
        if intervention_type in self._intent_profile.preferred_intervention_types:
            base = 0
        elif intervention_type in self._intent_profile.deprioritized_intervention_types:
            base = 2
        else:
            base = 1

        # v2 upgrade: director_persona の steering signals による調整
        if self._director_persona is not None:
            w = self._director_persona.get_steering_signals().intervention_type_weights.get(
                intervention_type, 1.0
            )
            if w >= 1.5:
                base = max(0, base - 1)
            elif w <= 0.5:
                base = min(2, base + 1)
        return base

    def _preferred_fallback_intervention_type(self, default_type: str) -> str:
        """fallback intervention type を story intent に寄せる。"""
        if default_type not in self._intent_profile.deprioritized_intervention_types:
            return default_type
        for intervention_type in self._intent_profile.preferred_intervention_types:
            if intervention_type in VALID_INTERVENTION_TYPES:
                return intervention_type
        return default_type

    def _fallback_intervention_title(
        self,
        intervention_type: str,
        tension: dict[str, Any],
    ) -> str:
        """deterministic fallback intervention のタイトルを返す。"""
        titles = {
            "relationship_catalyst": "二人の火種が前に出る",
            "revelation": "隠れていた輪郭が見え始める",
            "crisis": "先延ばしにできない局面が来る",
            "opportunity": "選択を迫る瞬間が来る",
            "plot_twist": "流れが少しねじれる",
        }
        return titles.get(intervention_type, f"{tension.get('tension_type')} が前景化する")

    def _fallback_intervention_description(
        self,
        intervention_type: str,
        tension: dict[str, Any],
    ) -> str:
        """deterministic fallback intervention の説明文を返す。"""
        return (
            f"{tension.get('description', '')} を前に進めるため、"
            f"{intervention_type} 型の介入を入れる。"
        )

    def _fallback_prompt_injection(
        self,
        intervention_type: str,
        tension: dict[str, Any],
    ) -> str:
        """deterministic fallback 用 prompt_injection を返す。"""
        messages = {
            "relationship_catalyst": "今は相手との火種を避けず、短くても応答が返ってきやすい形でぶつけてください。",
            "revelation": "今は隠してきたことの輪郭が少し見える方向へ会話を進めてください。",
            "crisis": "今は先送りにできない決断や返答を意識して会話を進めてください。",
            "opportunity": "今は迷いを含んでも、何を選ぶかが見える方向へ会話を進めてください。",
            "plot_twist": "今は場面が少しずれるような一押しを意識してください。",
        }
        return messages.get(
            intervention_type,
            f"今は {tension.get('description', '')} が少し前に進む方向で会話してください。",
        )

    async def _generate_intervention(
        self,
        ctx: StoryDirectorContext,
        tension: dict[str, Any],
    ) -> dict[str, Any] | None:
        """LLM を使って1件の介入を生成する。"""
        system_prompt = (
            "あなたはストーリー演出AIです。"
            "物語の緊張に対する介入をJSONで出力してください。"
        )
        compact_for_gemma = self._should_compact_for_gemma()
        world_rules = self._compact_prompt_text(ctx.world_rules, 120 if compact_for_gemma else 600)
        user_prompt = (
            f"世界設定:\n{world_rules}\n\n"
            f"介入強度: {self._config.intervention_strength}\n\n"
            f"現在のエピソード:\n"
            f"  type: {ctx.active_episode_type or '（なし）'}\n"
            f"  goal: {ctx.active_episode_goal or '（なし）'}\n"
            f"  stakes: {ctx.active_episode_stakes or '（なし）'}\n\n"
            f"緊張情報:\n"
            f"  type: {tension.get('tension_type')}\n"
            f"  description: {tension.get('description')}\n"
            f"  intensity: {tension.get('intensity')}\n\n"
            f"最近閉じた場面:\n"
            f"{self._compact_items(ctx.recent_scene_outcomes, 2 if compact_for_gemma else 5, 64 if compact_for_gemma else 180)}\n\n"
            f"未解決のフック:\n"
            f"{self._compact_items(ctx.open_hook_summaries, 2 if compact_for_gemma else 5, 64 if compact_for_gemma else 180)}\n\n"
            f"以下の形式のJSONを1件出力してください。"
            f"intervention_typeは{sorted(VALID_INTERVENTION_TYPES)}のいずれか。\n"
            '例: {"intervention_type": "plot_twist", "title": "...", '
            '"description": "...", "prompt_injection": "...", '
            '"scope": "all | place:<place_id> | char:<id1>,<id2>", '
            f'"duration_rounds": {self._config.min_intervention_duration_rounds}}}'
        )

        response = await self._llm.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider=self._llm_provider,
            model=self._llm_model,
            request_tag=f"story_director:intervention:{tension.get('id', 'new')}",
            **self._generation_kwargs(max_tokens=220),
        )
        return self._parse_intervention(response.text, ctx.turn_number)

    def _generation_kwargs(self, *, max_tokens: int) -> dict[str, int | float | str]:
        if not self._should_compact_for_gemma():
            return {}
        return {
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "reasoning_mode": "off",
        }

    def _should_compact_for_gemma(self) -> bool:
        return self._llm_provider == "ollama" and self._llm_model.lower().startswith("gemma4")

    @staticmethod
    def _compact_prompt_text(text: Any, max_chars: int) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max_chars - 1].rstrip() + "…"

    def _compact_items(self, items: list[str], limit: int, max_chars: int) -> str:
        if not items:
            return "（なし）"
        return "\n".join(
            f"- {self._compact_prompt_text(item, max_chars)}"
            for item in items[:limit]
        )

    def _compact_inline_items(self, items: list[str], limit: int, max_chars: int) -> str:
        if not items:
            return "（なし）"
        return " / ".join(
            self._compact_prompt_text(item, max_chars)
            for item in items[:limit]
        )

    def _compact_multiline(self, text: str, limit: int, max_chars: int) -> str:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if not lines:
            return "（なし）"
        return "\n".join(
            self._compact_prompt_text(line, max_chars)
            for line in lines[:limit]
        )

    def _compact_tension_summary(
        self,
        tensions: list[dict[str, Any]],
        compact_for_gemma: bool,
    ) -> str:
        if not tensions:
            return "（なし）"
        limit = 3 if compact_for_gemma else len(tensions)
        max_chars = 56 if compact_for_gemma else 180
        return "\n".join(
            self._compact_prompt_text(
                f"- [#{t['id']}] {t.get('tension_type')}: {t.get('description', '')} ({t.get('status')})",
                max_chars,
            )
            for t in tensions[:limit]
        )

    def _compact_intervention_summary(
        self,
        interventions: list[dict[str, Any]],
        compact_for_gemma: bool,
    ) -> str:
        if not interventions:
            return "（なし）"
        limit = 2 if compact_for_gemma else len(interventions)
        max_chars = 56 if compact_for_gemma else 180
        return "\n".join(
            self._compact_prompt_text(
                f"- [#{iv['id']}] {iv.get('intervention_type')}: {iv.get('title', '')}",
                max_chars,
            )
            for iv in interventions[:limit]
        )

    def _compact_episode_summary(self, ctx: StoryDirectorContext) -> str:
        if not self._should_compact_for_gemma():
            return self._compact_prompt_text(
                f"{ctx.active_episode_type or '（なし）'} / {ctx.active_episode_goal or '（なし）'}",
                120,
            )
        summary = " / ".join(
            part
            for part in (
                ctx.active_episode_type,
                ctx.active_episode_goal,
                ctx.active_episode_stakes,
                ctx.active_episode_pattern_type,
            )
            if str(part or "").strip()
        )
        if not summary:
            return "（なし）"
        return self._compact_prompt_text(summary, 48)

    def _parse_intervention(
        self,
        text: str,
        turn_number: int,
    ) -> dict[str, Any] | None:
        """LLM 応答テキストから介入データをパースする。"""
        cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
        data: Any = None
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        if not isinstance(data, dict):
            return None

        return self._normalize_intervention_data(data, turn_number)

    def _normalize_intervention_data(
        self,
        data: dict[str, Any],
        turn_number: int,
    ) -> dict[str, Any] | None:
        """intervention payload を正規化して DB 挿入可能な dict へ変換する。"""
        i_type = data.get("intervention_type", "")
        if i_type not in VALID_INTERVENTION_TYPES:
            return None

        scope = str(data.get("scope", "all")).strip()
        if not self._is_valid_scope(scope):
            return None

        duration_rounds = self._normalize_duration_rounds(data.get("duration_rounds", 3))
        normalized = dict(data)
        normalized["status"] = "active"
        normalized["scope"] = scope
        normalized["active_until_turn"] = self._active_until_turn(turn_number, duration_rounds)
        if "tension_id" in normalized and normalized["tension_id"] is not None:
            try:
                normalized["tension_id"] = int(normalized["tension_id"])
            except (TypeError, ValueError):
                normalized["tension_id"] = None
        # compact_for_gemma スキーマでは一部フィールドが省略されることがあるため fallback を補完
        if not normalized.get("title"):
            normalized["title"] = str(i_type)
        if not normalized.get("description"):
            normalized["description"] = normalized.get("title", str(i_type))
        if not normalized.get("prompt_injection"):
            normalized["prompt_injection"] = self._fallback_prompt_injection(i_type, normalized)
        return normalized

    def _normalize_duration_rounds(self, raw_duration_rounds: Any) -> int:
        """介入 duration を floor/cap 付きで正規化する。"""
        requested = int(_safe_float(raw_duration_rounds, self._config.min_intervention_duration_rounds))
        return max(
            self._config.min_intervention_duration_rounds,
            min(5, requested),
        )

    @staticmethod
    def _active_until_turn(turn_number: int, duration_rounds: int) -> int:
        return turn_number + max(1, duration_rounds) - 1

    def _active_scope_counts(self, active_interventions: list[dict[str, Any]]) -> Counter[str]:
        """non-all scope ごとの active 件数を返す。"""
        counts: Counter[str] = Counter()
        for intervention in active_interventions:
            scope = str(intervention.get("scope") or "").strip()
            if scope and scope != "all":
                counts[scope] += 1
        return counts

    async def _list_recent_interventions(
        self,
        story_id: str,
        turn_number: int,
    ) -> list[dict[str, Any]]:
        """cooldown 判定に使う recent intervention を返す。"""
        cooldown_rounds = max(0, self._config.repeat_intervention_cooldown_rounds)
        if cooldown_rounds <= 0:
            return []
        cutoff_turn = turn_number - cooldown_rounds + 1
        conn = getattr(self._db, "_conn", None)
        if conn is None:
            return []
        cursor = await conn.execute(
            """
            SELECT *
            FROM director_interventions
            WHERE story_id = ?
              AND status IN ('active', 'acknowledged', 'expired', 'resolved')
              AND COALESCE(active_until_turn, active_from_turn, 0) >= ?
            ORDER BY id DESC
            """,
            (story_id, cutoff_turn),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    def _recent_tension_ids(self, recent_interventions: list[dict[str, Any]]) -> set[int]:
        """recent intervention が付いている tension id 集合を返す。"""
        result: set[int] = set()
        for intervention in recent_interventions:
            tension_id = intervention.get("tension_id")
            if tension_id is None:
                continue
            try:
                result.add(int(tension_id))
            except (TypeError, ValueError):
                continue
        return result

    def _can_insert_intervention(
        self,
        intervention: dict[str, Any],
        scope_counts: Counter[str],
        recent_interventions: list[dict[str, Any]],
    ) -> bool:
        """same-scope cap と recent duplicate cooldown を満たすか判定する。"""
        scope = str(intervention.get("scope") or "").strip()
        if (
            scope
            and scope != "all"
            and scope_counts[scope] >= self._config.max_same_scope_active_interventions
        ):
            return False
        return not self._is_intervention_on_cooldown(intervention, recent_interventions)

    def _is_intervention_on_cooldown(
        self,
        intervention: dict[str, Any],
        recent_interventions: list[dict[str, Any]],
    ) -> bool:
        """同一 tension/type/scope の直近介入が cooldown 中なら True。"""
        tension_id = intervention.get("tension_id")
        if tension_id is None:
            return False
        try:
            normalized_tension_id = int(tension_id)
        except (TypeError, ValueError):
            return False
        intervention_type = str(intervention.get("intervention_type") or "").strip()
        scope = str(intervention.get("scope") or "").strip()
        for row in recent_interventions:
            row_tension_id = row.get("tension_id")
            if row_tension_id is None:
                continue
            try:
                normalized_row_tension_id = int(row_tension_id)
            except (TypeError, ValueError):
                continue
            if normalized_row_tension_id != normalized_tension_id:
                continue
            if str(row.get("intervention_type") or "").strip() != intervention_type:
                continue
            if str(row.get("scope") or "").strip() != scope:
                continue
            return True
        return False

    def _register_inserted_intervention(
        self,
        intervention: dict[str, Any],
        scope_counts: Counter[str],
        recent_interventions: list[dict[str, Any]],
    ) -> None:
        """同一 pass 中の重複判定用に newly inserted intervention を反映する。"""
        scope = str(intervention.get("scope") or "").strip()
        if scope and scope != "all":
            scope_counts[scope] += 1
        recent_interventions.append(dict(intervention))

    def _is_valid_scope(self, scope: str) -> bool:
        """intervention scope の文字列を検証する。"""
        if scope == "all":
            return True
        if scope.startswith("place:"):
            return bool(scope.removeprefix("place:").strip())
        if not scope.startswith("char:"):
            return False
        char_ids = [
            char_id.strip()
            for char_id in scope.removeprefix("char:").split(",")
            if char_id.strip()
        ]
        return bool(char_ids)
