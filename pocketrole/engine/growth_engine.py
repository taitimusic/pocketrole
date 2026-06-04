"""
engine/growth_engine.py — runtime growth engine

Hybrid signals を使って growth candidate を生成し、
条件を満たしたものを character_evolution / profile overlay へ commit する。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from db.db_manager import DatabaseManager
from engine.config import GrowthEngineConfig
from engine.llm.router import LLMRouter
from engine.structured_generation import StructuredJSONPolicy, generate_structured_json
from engine.structured_values import normalize_unit_score

logger = logging.getLogger(__name__)

SUPPORTED_GROWTH_FIELDS = frozenset({"current_goal", "current_worry", "personality_core"})
_GENERIC_GROWTH_CUES = (
    "前向き",
    "成長",
    "頑張",
    "強くな",
    "良くな",
    "変わりたい",
)
# 常に transient とみなす感情状態語（candidate_value 内に含まれれば reject）
_TRANSIENT_PERSONALITY_CUES_ALWAYS = ("安心", "嬉し", "落ち着")
# 冒頭（8文字未満）または短い値（15文字未満）でのみ transient とみなす修飾語
_TRANSIENT_PERSONALITY_CUES_POSITIONAL = ("少し", "ちょっと", "前向き")

_GROWTH_SYSTEM_PROMPT = """\
あなたはキャラクターの成長候補を抽出する観察者です。
提示された最近の経験・関係変化・未解決フックから、持続しうる内面変化だけを提案してください。
「前向きになる」「成長する」のような抽象的で generic な変化は禁止です。
良い変化だけでなく、閉じる・疑う・特定相手にだけ崩れる変化も許容します。
対象フィールドは current_goal, current_worry, personality_core だけです。
変化がなければ空の JSON 配列を返してください。
"""


class GrowthEngine:
    """Runtime growth manager."""

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        llm_router: LLMRouter,
        config: GrowthEngineConfig,
        *,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self.story_id = story_id
        self._db = db
        self._llm_router = llm_router
        self._config = config
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._last_round_results: dict[str, dict[str, Any]] = {}

    def _should_compact_for_gemma(self) -> bool:
        return self._llm_provider == "ollama" and self._llm_model.lower().startswith("gemma4")

    def get_last_round_results(self) -> dict[str, dict[str, Any]]:
        """直近 round の growth 観測結果を返す。"""
        return {
            char_id: dict(result)
            for char_id, result in self._last_round_results.items()
        }

    async def get_profile_overlay(self, char_id: str) -> dict[str, str]:
        """Prompt 反映用の overlay を返す。"""
        growth_overlay = await self._db.get_character_profile_overlay(self.story_id, char_id)
        if isinstance(growth_overlay, dict):
            merged = dict(growth_overlay.get("overlay_json", {}))
        else:
            merged = await self._db.get_latest_evolution_overlay(self.story_id, char_id)
            if not isinstance(merged, dict):
                merged = {}
        canon_overlay = await self._db.get_character_canon_overlay(self.story_id, char_id)
        if not isinstance(canon_overlay, dict):
            return dict(merged)
        effective = dict(canon_overlay.get("overlay_json", {}))
        effective.update(merged)
        return effective

    async def process_round(self, turn_number: int, characters: list[dict[str, Any]]) -> None:
        """各キャラの growth candidate 抽出と commit 判定を行う。"""
        self._last_round_results = {}
        if not self._config.enabled:
            return
        for char in characters:
            await self._process_character(turn_number, char)

    async def _process_character(self, turn_number: int, char: dict[str, Any]) -> None:
        char_id = str(char["id"])
        window_start = max(0, turn_number - self._config.rolling_window_turns)

        memories = await self._db.get_relevant_story_memories(self.story_id, char_id, limit=5)
        hooks = await self._db.get_relevant_story_hooks(self.story_id, char_id, "", limit=3)
        relationship_events = await self._db.get_recent_relationship_events_for_character(
            self.story_id,
            char_id,
            since_turn=window_start,
            limit=5,
        )
        raw_active_tensions = await self._db.get_active_tensions(self.story_id)
        active_tensions = [
            tension
            for tension in (raw_active_tensions if isinstance(raw_active_tensions, list) else [])
            if char_id in list(tension.get("involved_chars") or [])
        ][:3]
        raw_recent_closed_scenes = await self._db.get_recent_closed_story_scenes(
            self.story_id,
            since_turn=window_start,
            limit=3,
            scene_type="conversation",
        )
        recent_closed_scenes = (
            raw_recent_closed_scenes if isinstance(raw_recent_closed_scenes, list) else []
        )
        active_episode_row = await self._db.get_active_story_episode(self.story_id)
        active_episode = active_episode_row if isinstance(active_episode_row, dict) else None
        if active_episode is not None and char_id not in list(active_episode.get("focus_char_ids") or []):
            active_episode = None
        raw_relationship_modes = await self._db.get_active_relationship_modes(self.story_id)
        active_relationship_modes = [
            mode
            for mode in (raw_relationship_modes if isinstance(raw_relationship_modes, list) else [])
            if str(mode.get("char_id_from")) == char_id or str(mode.get("char_id_to")) == char_id
        ][:2]
        raw_canon_bits = await self._db.get_active_story_canon_bits(self.story_id)
        active_canon_bits = [
            bit
            for bit in (raw_canon_bits if isinstance(raw_canon_bits, list) else [])
            if char_id in list(bit.get("focus_char_ids") or [])
        ][:2]
        raw_pressures = await self._db.get_active_story_dramatic_pressures(self.story_id)
        active_pressures = [
            pressure
            for pressure in (raw_pressures if isinstance(raw_pressures, list) else [])
            if char_id in list(pressure.get("focus_char_ids") or [])
        ][:2]
        result_summary: dict[str, Any] = {
            "memory_count": len(memories),
            "hook_count": len(hooks),
            "relationship_event_count": len(relationship_events),
            "active_tension_count": len(active_tensions),
            "recent_closed_scene_count": len(recent_closed_scenes),
            "active_episode_count": 1 if active_episode is not None else 0,
            "active_relationship_mode_count": len(active_relationship_modes),
            "active_canon_bit_count": len(active_canon_bits),
            "active_pressure_count": len(active_pressures),
        }
        result_summary["evidence_count"] = sum(
            int(result_summary[key])
            for key in (
                "memory_count",
                "hook_count",
                "relationship_event_count",
                "active_tension_count",
                "recent_closed_scene_count",
                "active_episode_count",
                "active_relationship_mode_count",
                "active_canon_bit_count",
                "active_pressure_count",
            )
        )
        self._last_round_results[char_id] = result_summary
        if (
            not memories
            and not hooks
            and not relationship_events
            and not active_tensions
            and not recent_closed_scenes
            and active_episode is None
            and not active_relationship_modes
            and not active_canon_bits
            and not active_pressures
        ):
            result_summary["skip_reason"] = "no_evidence"
            logger.info(
                "GrowthEngine skipped character",
                extra={
                    "story_id": self.story_id,
                    "char_id": char_id,
                    "skip_reason": "no_evidence",
                },
            )
            return

        overlay = await self.get_profile_overlay(char_id)
        compact_for_gemma = self._should_compact_for_gemma()
        user_prompt = _build_growth_prompt(
            char,
            overlay,
            memories,
            hooks,
            relationship_events,
            active_tensions=active_tensions,
            recent_closed_scenes=recent_closed_scenes,
            active_episode=active_episode,
            active_relationship_modes=active_relationship_modes,
            active_canon_bits=active_canon_bits,
            active_pressures=active_pressures,
            compact_for_gemma=compact_for_gemma,
        )
        response_max_tokens = self._config.response_max_tokens
        repair_max_tokens = self._config.repair_max_tokens
        if compact_for_gemma:
            response_max_tokens = min(response_max_tokens, 220)
            repair_max_tokens = min(repair_max_tokens, 300)

        valid_memory_ids = {int(item["id"]) for item in memories if item.get("id") is not None}
        valid_hook_ids = {int(item["id"]) for item in hooks if item.get("id") is not None}
        valid_log_ids = {
            int(item["source_log_id"])
            for item in relationship_events
            if item.get("source_log_id") is not None
        }
        valid_scene_ids = {
            int(item["scene_id"])
            for item in relationship_events
            if item.get("scene_id") is not None
        }
        result = await generate_structured_json(
            router=self._llm_router,
            provider=self._llm_provider,
            system_prompt=_GROWTH_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=self._llm_model,
            parser=lambda text: _try_parse_growth_candidates(
                text,
                valid_memory_ids=valid_memory_ids,
                valid_hook_ids=valid_hook_ids,
                valid_log_ids=valid_log_ids,
                valid_scene_ids=valid_scene_ids,
            ),
            repair_schema_prompt=(
                '[{"field":"current_goal","candidate_value":"...",'
                '"reason":"...","experience_score":0.7,'
                '"identity_impact_score":0.7,"confidence":0.7}]'
            ),
            policy=StructuredJSONPolicy(
                name="growth_engine",
                response_max_tokens=response_max_tokens,
                repair_max_tokens=repair_max_tokens,
            ),
        )
        candidates = result.parsed
        if candidates is None:
            result_summary["skip_reason"] = "parse_failed"
            result_summary["done_reason"] = result.done_reason
            result_summary["used_repair"] = result.used_repair
            if result.parser_error is not None:
                result_summary["parser_error"] = result.parser_error
            logger.warning(
                "GrowthEngine failed to parse candidate JSON",
                extra={
                    "story_id": self.story_id,
                    "char_id": char_id,
                    "skip_reason": "parse_failed",
                    "done_reason": result.done_reason,
                    "used_repair": result.used_repair,
                    "parser_error": result.parser_error,
                },
            )
            await self._record_quality_issue(
                turn_number,
                char_id,
                issue_type="growth_parse_failed",
                details={
                    "done_reason": result.done_reason,
                    "used_repair": result.used_repair,
                    "parser_error": result.parser_error,
                },
                auto_action="skip",
            )
            return
        if not candidates:
            result_summary["result"] = "llm_empty"
            result_summary["done_reason"] = result.done_reason
            result_summary["used_repair"] = result.used_repair
            if _has_rich_growth_evidence(
                active_episode=active_episode,
                active_relationship_modes=active_relationship_modes,
                active_canon_bits=active_canon_bits,
                active_pressures=active_pressures,
                result_summary=result_summary,
                rich_evidence_min_items=self._config.rich_evidence_min_items,
            ):
                await self._record_quality_issue(
                    turn_number,
                    char_id,
                    issue_type="growth_llm_empty",
                    details={
                        "done_reason": result.done_reason,
                        "used_repair": result.used_repair,
                    },
                    auto_action="skip",
                )
            logger.info(
                "GrowthEngine returned no candidates",
                extra={
                    "story_id": self.story_id,
                    "char_id": char_id,
                    "result": "llm_empty",
                    "done_reason": result.done_reason,
                    "used_repair": result.used_repair,
                },
            )
            return
        filtered_candidates: list[dict[str, Any]] = []
        durable_support_count = int(active_episode is not None) + int(bool(active_relationship_modes)) + int(bool(active_canon_bits))
        for candidate in candidates:
            rejection = _validate_growth_candidate(
                char=char,
                overlay=overlay,
                candidate=candidate,
                durable_support_count=durable_support_count,
                personality_core_min_durable_support=(
                    self._config.personality_core_min_durable_support
                ),
            )
            if rejection is None:
                filtered_candidates.append(candidate)
                continue
            await self._record_quality_issue(
                turn_number,
                char_id,
                issue_type=rejection,
                details={
                    "field": candidate.get("field"),
                    "candidate_value": candidate.get("candidate_value"),
                },
                auto_action="skip",
            )
        if not filtered_candidates:
            result_summary["skip_reason"] = "quality_rejected"
            return

        existing_overlay_row = await self._db.get_character_profile_overlay(self.story_id, char_id)
        next_version = 1
        if existing_overlay_row is not None:
            next_version = int(existing_overlay_row.get("version", 1)) + 1

        inserted_count = 0
        committed_count = 0
        batch = await self._build_growth_write_batch(
            turn_number,
            char=char,
            overlay=overlay,
            next_version=next_version,
            candidates=filtered_candidates,
            window_start=window_start,
        )
        inserted_count = len(batch["candidate_rows"])
        committed_count = len(batch["commit_actions"])
        if self._should_use_growth_batch_helper():
            try:
                await self._db.apply_growth_batch(  # type: ignore[attr-defined]
                    self.story_id,
                    char_id,
                    batch,
                )
            except Exception as exc:
                result_summary["skip_reason"] = "write_rolled_back"
                result_summary["write_error"] = str(exc)
                logger.warning(
                    "GrowthEngine rolled back growth batch",
                    extra={
                        "story_id": self.story_id,
                        "char_id": char_id,
                        "skip_reason": "write_rolled_back",
                        "error": str(exc),
                    },
                )
                await self._record_quality_issue(
                    turn_number,
                    char_id,
                    issue_type="growth_write_rolled_back",
                    details={"error": str(exc)},
                    auto_action="rollback",
                )
                return
        else:
            await self._apply_growth_write_batch_non_transactional(char_id, batch)

        for commit_action in batch["commit_actions"]:
            logger.info(
                "GrowthEngine committed overlay",
                extra={
                    "story_id": self.story_id,
                    "char_id": char_id,
                    "field": commit_action["field"],
                    "turn_number": turn_number,
                },
            )
        result_summary["candidate_count"] = inserted_count
        result_summary["commit_count"] = committed_count
        if committed_count > 0:
            result_summary["result"] = "committed"
        elif inserted_count > 0:
            result_summary["result"] = "pending_candidate_inserted"
            logger.info(
                "GrowthEngine inserted pending candidates",
                extra={
                    "story_id": self.story_id,
                    "char_id": char_id,
                    "result": "pending_candidate_inserted",
                    "candidate_count": inserted_count,
                },
            )
        else:
            result_summary["skip_reason"] = "below_commit_threshold"

    def _should_use_growth_batch_helper(self) -> bool:
        helper = getattr(self._db, "apply_growth_batch", None)
        helper_dict = getattr(self._db, "__dict__", {})
        class_dict = getattr(type(self._db), "__dict__", {})
        return callable(helper) and (
            "apply_growth_batch" in helper_dict or "apply_growth_batch" in class_dict
        )

    async def _build_growth_write_batch(
        self,
        turn_number: int,
        *,
        char: dict[str, Any],
        overlay: dict[str, str],
        next_version: int,
        candidates: list[dict[str, Any]],
        window_start: int,
    ) -> dict[str, Any]:
        batch_overlay = dict(overlay)
        candidate_rows: list[dict[str, Any]] = []
        commit_actions: list[dict[str, Any]] = []
        status_updates: list[dict[str, Any]] = []
        for candidate in candidates:
            candidate_index = len(candidate_rows)
            candidate_row = {
                **candidate,
                "detected_turn": turn_number,
                "status": "pending",
            }
            candidate_rows.append(candidate_row)
            immediate_commit = (
                candidate["identity_impact_score"] >= self._config.immediate_commit_identity_threshold
                and candidate["confidence"] >= 0.75
            )
            pending_statuses = await self._classify_pending_candidates(
                str(char["id"]),
                candidate["field"],
                candidate["candidate_value"],
                turn_number=turn_number,
                window_start=window_start,
            )
            matching_pending = pending_statuses["matching"]
            _merge_status_update(
                status_updates,
                status="expired",
                candidate_ids=pending_statuses["expired_ids"],
            )
            _merge_status_update(
                status_updates,
                status="superseded",
                candidate_ids=pending_statuses["superseded_ids"],
            )
            aggregate_score = (
                float(candidate.get("experience_score", 0.0)) * float(candidate.get("confidence", 0.0))
                + sum(
                    float(item.get("experience_score", 0.0)) * float(item.get("confidence", 0.0))
                    for item in matching_pending
                )
            )
            if not immediate_commit and aggregate_score < self._aggregate_commit_threshold(
                candidate["field"]
            ):
                continue
            previous_value = batch_overlay.get(candidate["field"]) or char.get(candidate["field"])
            batch_overlay[candidate["field"]] = candidate["candidate_value"]
            commit_actions.append(
                {
                    "candidate_index": candidate_index,
                    "field": candidate["field"],
                    "candidate_value": candidate["candidate_value"],
                    "reason": candidate["reason"],
                    "previous_value": previous_value,
                    "source_memory_id": candidate.get("source_memory_id"),
                    "existing_candidate_ids": [int(item["id"]) for item in matching_pending],
                }
            )
        overlay_payload = None
        if commit_actions:
            overlay_payload = {
                "overlay_json": batch_overlay,
                "version": next_version,
                "last_committed_turn": turn_number,
            }
        return {
            "candidate_rows": candidate_rows,
            "commit_actions": commit_actions,
            "status_updates": status_updates,
            "overlay_payload": overlay_payload,
            "turn_number": turn_number,
        }

    async def _apply_growth_write_batch_non_transactional(
        self,
        char_id: str,
        batch: dict[str, Any],
    ) -> None:
        inserted_candidate_ids: list[int] = []
        for candidate_row in batch["candidate_rows"]:
            candidate_id = await self._db.insert_character_growth_candidate(
                self.story_id,
                char_id,
                candidate_row,
            )
            inserted_candidate_ids.append(candidate_id)
        for status_update in list(batch.get("status_updates") or []):
            for candidate_id in list(status_update.get("candidate_ids") or []):
                await self._db.update_character_growth_candidate_status(
                    int(candidate_id),
                    str(status_update["status"]),
                )
        last_evolution_id: int | None = None
        overlay_payload = batch.get("overlay_payload")
        for commit_action in batch["commit_actions"]:
            evolution_id = await self._db.insert_evolution(
                self.story_id,
                {
                    "char_id": char_id,
                    "turn_number": batch["turn_number"],
                    "field": commit_action["field"],
                    "previous_value": commit_action["previous_value"],
                    "new_value": commit_action["candidate_value"],
                    "reason": commit_action["reason"],
                    "source_memory_id": commit_action.get("source_memory_id"),
                },
            )
            last_evolution_id = evolution_id
            await self._db.update_character_growth_candidate_status(
                inserted_candidate_ids[commit_action["candidate_index"]],
                "committed",
            )
            for existing_candidate_id in commit_action["existing_candidate_ids"]:
                await self._db.update_character_growth_candidate_status(
                    int(existing_candidate_id),
                    "committed",
                )
        if overlay_payload is not None:
            await self._db.upsert_character_profile_overlay(
                self.story_id,
                char_id,
                {
                    **overlay_payload,
                    "source_evolution_id": last_evolution_id,
                },
            )

    async def _record_quality_issue(
        self,
        turn_number: int,
        char_id: str,
        *,
        issue_type: str,
        details: dict[str, Any],
        auto_action: str,
    ) -> None:
        try:
            await self._db.insert_generation_quality_issue(
                self.story_id,
                {
                    "log_id": None,
                    "scene_id": None,
                    "issue_type": issue_type,
                    "severity": "warning",
                    "details": {
                        "char_id": char_id,
                        **details,
                    },
                    "auto_action": auto_action,
                    "created_turn": turn_number,
                },
            )
        except Exception:
            logger.exception("GrowthEngine failed to record quality issue")

    async def _matching_pending_candidates(
        self,
        char_id: str,
        field: str,
        candidate_value: str,
        *,
        window_start: int,
    ) -> list[dict[str, Any]]:
        pending = await self._db.get_pending_growth_candidates(self.story_id, char_id)
        matches = [
            item
            for item in pending
            if item.get("field") == field
            and item.get("candidate_value") == candidate_value
            and int(item.get("detected_turn", 0)) >= window_start
        ]
        return sorted(matches, key=lambda item: int(item["id"]))

    def _aggregate_commit_threshold(self, field: str) -> float:
        if field == "personality_core":
            return self._config.personality_core_aggregate_commit_threshold
        if field in {"current_goal", "current_worry"}:
            return self._config.goal_worry_aggregate_commit_threshold
        return max(
            self._config.goal_worry_aggregate_commit_threshold,
            self._config.personality_core_aggregate_commit_threshold,
        )

    async def _classify_pending_candidates(
        self,
        char_id: str,
        field: str,
        candidate_value: str,
        *,
        turn_number: int,
        window_start: int,
    ) -> dict[str, Any]:
        pending = await self._db.get_pending_growth_candidates(self.story_id, char_id)
        matching: list[dict[str, Any]] = []
        expired_ids: list[int] = []
        superseded_ids: list[int] = []

        for item in pending:
            candidate_id = int(item.get("id") or 0)
            detected_turn = int(item.get("detected_turn") or 0)
            if self._is_pending_candidate_stale(turn_number, detected_turn):
                expired_ids.append(candidate_id)
                continue
            if item.get("field") != field:
                continue
            if item.get("candidate_value") == candidate_value and detected_turn >= window_start:
                matching.append(item)
                continue
            superseded_ids.append(candidate_id)

        return {
            "matching": sorted(matching, key=lambda item: int(item["id"])),
            "expired_ids": sorted(set(expired_ids)),
            "superseded_ids": sorted(set(superseded_ids)),
        }

    def _is_pending_candidate_stale(self, current_turn: int, detected_turn: int) -> bool:
        ttl_turns = max(int(self._config.pending_candidate_ttl_turns), 1)
        return current_turn - detected_turn > ttl_turns


def _build_growth_prompt(
    char: dict[str, Any],
    overlay: dict[str, str],
    memories: list[dict[str, Any]],
    hooks: list[dict[str, Any]],
    relationship_events: list[dict[str, Any]],
    *,
    active_tensions: list[dict[str, Any]],
    recent_closed_scenes: list[dict[str, Any]],
    active_episode: dict[str, Any] | None,
    active_relationship_modes: list[dict[str, Any]],
    active_canon_bits: list[dict[str, Any]],
    active_pressures: list[dict[str, Any]],
    compact_for_gemma: bool = False,
) -> str:
    current_goal = overlay.get("current_goal") or char.get("current_goal") or "（不明）"
    current_worry = overlay.get("current_worry") or char.get("current_worry") or "（不明）"
    personality_core = overlay.get("personality_core") or char.get("personality_core") or "（不明）"

    evidence_lines = _build_growth_evidence_lines(
        memories=memories,
        hooks=hooks,
        relationship_events=relationship_events,
        active_tensions=active_tensions,
        recent_closed_scenes=recent_closed_scenes,
        active_episode=active_episode,
        active_relationship_modes=active_relationship_modes,
        active_canon_bits=active_canon_bits,
        active_pressures=active_pressures,
        compact_for_gemma=compact_for_gemma,
    )
    if compact_for_gemma:
        compact_evidence = " / ".join(
            _compact_growth_text(evidence, 46)
            for evidence in evidence_lines[:5]
        ) or "（なし）"
        lines = [
            f"キャラ:{_compact_growth_text(char.get('name_ja', char['id']), 16)}",
            f"目標:{_compact_growth_text(current_goal, 34)}",
            f"悩み:{_compact_growth_text(current_worry, 34)}",
            f"核:{_compact_growth_text(personality_core, 34)}",
            f"証拠:{compact_evidence}",
            "JSON配列のみ。最大1件。generic禁止。",
            "keys=field,candidate_value,reason,experience_score,identity_impact_score,confidence",
            "必要なら source_memory_id,source_hook_id,source_log_id,source_scene_id",
        ]
        return "\n".join(lines)

    lines = [
        f"【キャラクター: {char.get('name_ja', char['id'])}】",
        f"現在の目標: {current_goal}",
        f"現在の悩み: {current_worry}",
        f"性格の核: {personality_core}",
        "",
        "【変化につながりうる証拠】",
    ]
    for evidence in evidence_lines:
        lines.append(f"- {evidence}")

    lines.extend(
        [
            "",
            "持続しうる変化だけを JSON 配列で返してください。",
            "返答は `[]` または `[{...}]` のみで、前置きや説明文は書かないでください。",
            "返す候補は最大1件までにしてください。",
            "generic な自己啓発風の変化は禁止です。",
            "各要素には field, candidate_value, reason, experience_score, "
            "identity_impact_score, confidence を含めてください。",
            "必要なら source_memory_id, source_hook_id, source_log_id, source_scene_id も含めてください。",
        ]
    )
    return "\n".join(lines)


def _build_growth_evidence_lines(
    *,
    memories: list[dict[str, Any]],
    hooks: list[dict[str, Any]],
    relationship_events: list[dict[str, Any]],
    active_tensions: list[dict[str, Any]],
    recent_closed_scenes: list[dict[str, Any]],
    active_episode: dict[str, Any] | None,
    active_relationship_modes: list[dict[str, Any]],
    active_canon_bits: list[dict[str, Any]],
    active_pressures: list[dict[str, Any]],
    compact_for_gemma: bool = False,
) -> list[str]:
    lines: list[str] = []
    if active_episode is not None and active_episode.get("goal"):
        lines.append(f"episode#{active_episode.get('id')}: {active_episode.get('goal')}")
    for mode in active_relationship_modes[:2]:
        if mode.get("summary"):
            lines.append(f"relationship_mode: {mode.get('summary')}")
    for bit in active_canon_bits[:2]:
        if bit.get("summary"):
            lines.append(f"canon#{bit.get('id')}: {bit.get('summary')}")
    for pressure in active_pressures[:2]:
        if pressure.get("summary"):
            lines.append(f"pressure#{pressure.get('id')}: {pressure.get('summary')}")
    for hook in hooks[:2]:
        if hook.get("description"):
            lines.append(f"hook#{hook.get('id')}: {hook.get('description')}")
    for scene in recent_closed_scenes[:1]:
        if scene.get("outcome_summary"):
            lines.append(f"scene#{scene.get('id')}: {scene.get('outcome_summary')}")
    for event in relationship_events[:1]:
        if event.get("summary"):
            lines.append(f"relationship_event: {event.get('summary')}")
    for tension in active_tensions[:1]:
        if tension.get("description"):
            lines.append(f"tension#{tension.get('id')}: {tension.get('description')}")
    for memory in memories[:1]:
        if memory.get("summary"):
            lines.append(f"memory#{memory.get('id')}: {memory.get('summary')}")
    if compact_for_gemma:
        return [_compact_growth_text(line, 52) for line in lines[:5]]
    return lines[:6]


def _compact_growth_text(text: Any, max_chars: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _has_rich_growth_evidence(
    *,
    active_episode: dict[str, Any] | None,
    active_relationship_modes: list[dict[str, Any]],
    active_canon_bits: list[dict[str, Any]],
    active_pressures: list[dict[str, Any]],
    result_summary: dict[str, Any],
    rich_evidence_min_items: int,
) -> bool:
    if active_episode is not None or active_relationship_modes or active_canon_bits or active_pressures:
        return True
    return int(result_summary.get("evidence_count") or 0) >= max(rich_evidence_min_items, 1)


def _normalize_growth_text(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _is_generic_cue_dominated(candidate_value: str, cues: tuple[str, ...]) -> bool:
    """candidate_value が汎用的なキーワードに支配されているか判定する。

    短い値（15文字未満）でキューを含む場合、または冒頭8文字以内にキューが現れる場合のみ
    True を返す。長い具体的な表現の中での cue 出現は false positive として除外する。
    """
    for cue in cues:
        if cue not in candidate_value:
            continue
        if len(candidate_value) < 15:
            return True
        if candidate_value.find(cue) < 8:
            return True
    return False


def _validate_growth_candidate(
    *,
    char: dict[str, Any],
    overlay: dict[str, str],
    candidate: dict[str, Any],
    durable_support_count: int,
    personality_core_min_durable_support: int,
) -> str | None:
    field = str(candidate.get("field") or "")
    candidate_value = str(candidate.get("candidate_value") or "")
    current_value = overlay.get(field) or str(char.get(field) or "")
    normalized_value = _normalize_growth_text(candidate_value)
    if normalized_value and normalized_value == _normalize_growth_text(current_value):
        return "growth_noop_rejected"
    if field in {"current_goal", "current_worry"} and _is_generic_cue_dominated(
        candidate_value, _GENERIC_GROWTH_CUES
    ):
        return "growth_generic_rejected"
    if field == "personality_core":
        if durable_support_count < personality_core_min_durable_support:
            return "growth_low_quality_rejected"
        if any(cue in candidate_value for cue in _TRANSIENT_PERSONALITY_CUES_ALWAYS):
            return "growth_field_mismatch_rejected"
        if _is_generic_cue_dominated(candidate_value, _TRANSIENT_PERSONALITY_CUES_POSITIONAL):
            return "growth_field_mismatch_rejected"
    return None


def _merge_status_update(
    status_updates: list[dict[str, Any]],
    *,
    status: str,
    candidate_ids: list[int],
) -> None:
    if not candidate_ids:
        return
    normalized_ids = sorted({int(candidate_id) for candidate_id in candidate_ids})
    for item in status_updates:
        if item.get("status") != status:
            continue
        item["candidate_ids"] = sorted(
            {int(candidate_id) for candidate_id in list(item.get("candidate_ids") or [])}
            | set(normalized_ids)
        )
        return
    status_updates.append({"status": status, "candidate_ids": normalized_ids})


def _validated_optional_id(raw: Any, valid_ids: set[int]) -> int | None:
    if raw is None:
        return None
    try:
        candidate = int(raw)
    except (TypeError, ValueError):
        return None
    if candidate not in valid_ids:
        return None
    return candidate


def _parse_growth_candidates(
    text: str,
    *,
    valid_memory_ids: set[int],
    valid_hook_ids: set[int],
    valid_log_ids: set[int],
    valid_scene_ids: set[int],
) -> list[dict[str, Any]]:
    parsed = _try_parse_growth_candidates(
        text,
        valid_memory_ids=valid_memory_ids,
        valid_hook_ids=valid_hook_ids,
        valid_log_ids=valid_log_ids,
        valid_scene_ids=valid_scene_ids,
    )
    if parsed is None:
        logger.warning("GrowthEngine failed to parse candidate JSON")
        return []
    return parsed


def _try_parse_growth_candidates(
    text: str,
    *,
    valid_memory_ids: set[int],
    valid_hook_ids: set[int],
    valid_log_ids: set[int],
    valid_scene_ids: set[int],
) -> list[dict[str, Any]] | None:
    cleaned = re.sub(r"```json\s*", "", text)
    cleaned = re.sub(r"```\s*", "", cleaned).strip()

    parsed: list[Any] | None = None
    for fragment in _candidate_growth_payload_fragments(cleaned):
        try:
            parsed = _normalize_growth_payload(json.loads(fragment))
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if parsed is not None:
            break

    if parsed is None:
        return None

    candidates: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        candidate_value = item.get("candidate_value")
        if field not in SUPPORTED_GROWTH_FIELDS or not isinstance(candidate_value, str):
            continue
        candidates.append(
            {
                "field": field,
                "candidate_value": candidate_value,
                "reason": str(item.get("reason") or "最近の経験による変化"),
                "experience_score": normalize_unit_score(item.get("experience_score"), default=0.5),
                "identity_impact_score": normalize_unit_score(
                    item.get("identity_impact_score"),
                    default=0.5,
                ),
                "confidence": normalize_unit_score(item.get("confidence"), default=0.5),
                "source_memory_id": _validated_optional_id(item.get("source_memory_id"), valid_memory_ids),
                "source_hook_id": _validated_optional_id(item.get("source_hook_id"), valid_hook_ids),
                "source_log_id": _validated_optional_id(item.get("source_log_id"), valid_log_ids),
                "source_scene_id": _validated_optional_id(item.get("source_scene_id"), valid_scene_ids),
            }
        )
    return candidates[:1]


def _normalize_growth_payload(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return None


def _repair_single_object_payload(text: str) -> str | None:
    return _repair_growth_payload_fragment(text)


def _candidate_growth_payload_fragments(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []

    fragments: list[str] = []

    def _append(fragment: str | None) -> None:
        if fragment is None:
            return
        normalized = fragment.strip()
        if normalized and normalized not in fragments:
            fragments.append(normalized)

    _append(stripped)

    array_match = re.search(r"\[.*\]", stripped, re.DOTALL)
    if array_match is not None:
        _append(array_match.group())

    object_match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if object_match is not None:
        _append(object_match.group())
    for object_fragment in re.findall(r"\{.*?\}", stripped, re.DOTALL):
        _append(object_fragment)

    first_array = stripped.find("[")
    first_object = stripped.find("{")
    first_indices = [index for index in (first_array, first_object) if index >= 0]
    if first_indices:
        _append(stripped[min(first_indices):])

    # truncation 対策: 配列先頭から最初の完全な {...} オブジェクトだけを抽出
    # done_reason="length" で配列が途中切れの場合に最初の完全 object を回収する
    if stripped.find("[") >= 0 and stripped.find("{") >= 0:
        first_obj_match = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", stripped)
        if first_obj_match is not None:
            _append("[" + first_obj_match.group() + "]")

    repaired_fragments = [_repair_growth_payload_fragment(fragment) for fragment in list(fragments)]
    for fragment in repaired_fragments:
        _append(fragment)
    return fragments


def _repair_growth_payload_fragment(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None

    first_array = stripped.find("[")
    first_object = stripped.find("{")
    first_indices = [index for index in (first_array, first_object) if index >= 0]
    if not first_indices:
        return None
    stripped = stripped[min(first_indices):]

    last_closing_array = stripped.rfind("]")
    last_closing_object = stripped.rfind("}")
    last_indices = [index for index in (last_closing_array, last_closing_object) if index >= 0]
    if last_indices:
        stripped = stripped[:max(last_indices) + 1]

    stripped = re.sub(r",\s*([}\]])", r"\1", stripped)

    open_braces = stripped.count("{")
    close_braces = stripped.count("}")
    open_brackets = stripped.count("[")
    close_brackets = stripped.count("]")
    if open_braces > close_braces:
        stripped += "}" * (open_braces - close_braces)
    if open_brackets > close_brackets:
        stripped += "]" * (open_brackets - close_brackets)
    return stripped
