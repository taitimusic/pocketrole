"""Deterministic dramatic pressure extraction and ranking helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager
    from engine.story_intent import StoryIntentProfile


PRESSURE_TO_TENSION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "status_flashpoint": ("conflict", "rivalry"),
    "showoff_flashpoint": ("rivalry", "conflict"),
    "near_reveal": ("secret", "mystery"),
    "role_reversal_ready": ("mystery", "rivalry"),
    "payoff_ready": ("conflict", "rivalry", "secret", "mystery"),
    "stall_risk": ("conflict", "mystery"),
}

PRESSURE_TO_INTERVENTION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "status_flashpoint": ("relationship_catalyst", "opportunity"),
    "showoff_flashpoint": ("opportunity", "relationship_catalyst"),
    "near_reveal": ("revelation",),
    "role_reversal_ready": ("opportunity", "revelation"),
    "payoff_ready": ("revelation", "relationship_catalyst", "opportunity"),
    "stall_risk": ("opportunity", "relationship_catalyst"),
}


@dataclass(frozen=True)
class DramaticPressureCandidate:
    pressure_type: str
    title: str
    summary: str
    focus_char_ids: list[str]
    focus_place_id: str | None
    dedupe_key: str
    first_detected_turn: int
    last_detected_turn: int
    recurrence_count: int
    score: float
    urgency: float
    payoff_ready: float
    intent_alignment: float
    source_hook_id: int | None = None
    source_tension_id: int | None = None
    source_pattern_id: int | None = None
    source_episode_id: int | None = None
    source_relationship_mode_id: int | None = None
    source_canon_bit_id: int | None = None
    source_scene_id: int | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "pressure_type": self.pressure_type,
            "status": "active",
            "title": self.title,
            "summary": self.summary,
            "focus_char_ids": self.focus_char_ids,
            "focus_place_id": self.focus_place_id,
            "dedupe_key": self.dedupe_key,
            "source_hook_id": self.source_hook_id,
            "source_tension_id": self.source_tension_id,
            "source_pattern_id": self.source_pattern_id,
            "source_episode_id": self.source_episode_id,
            "source_relationship_mode_id": self.source_relationship_mode_id,
            "source_canon_bit_id": self.source_canon_bit_id,
            "source_scene_id": self.source_scene_id,
            "first_detected_turn": self.first_detected_turn,
            "last_detected_turn": self.last_detected_turn,
            "recurrence_count": self.recurrence_count,
            "score": self.score,
            "urgency": self.urgency,
            "payoff_ready": self.payoff_ready,
            "intent_alignment": self.intent_alignment,
        }


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, value))


class DramaticPressureEvaluator:
    """Persist and rank deterministic dramatic pressure signals."""

    def __init__(
        self,
        db: DatabaseManager | None = None,
        story_id: str = "",
        *,
        intent_profile: StoryIntentProfile | None = None,
        recent_window_turns: int = 8,
        min_pressure_score: float = 0.55,
        stale_rounds_before_resolve: int = 2,
        max_active_pressures: int = 12,
    ) -> None:
        self._db = db
        self._story_id = story_id
        self._intent_profile = intent_profile
        self._recent_window_turns = max(1, recent_window_turns)
        self._min_pressure_score = _clamp_unit(min_pressure_score)
        self._stale_rounds_before_resolve = max(1, stale_rounds_before_resolve)
        self._max_active_pressures = max(1, max_active_pressures)
        self._last_round_summary: dict[str, Any] = {
            "inserted": 0,
            "reinforced": 0,
            "resolved": 0,
        }

    def get_last_round_summary(self) -> dict[str, Any]:
        return dict(self._last_round_summary)

    async def process_round(self, turn_number: int) -> None:
        if self._db is None or not self._story_id:
            return

        hooks = await self._db.get_open_story_hooks(self._story_id)
        tensions = await self._db.get_active_tensions(self._story_id)
        patterns = await self._db.get_active_interaction_patterns(self._story_id)
        relationship_modes = await self._db.get_active_relationship_modes(self._story_id)
        canon_bits = await self._db.get_active_story_canon_bits(self._story_id)
        active_episode = await self._db.get_active_story_episode(self._story_id)
        recent_closed_scenes = await self._db.get_recent_closed_story_scenes(
            self._story_id,
            since_turn=max(0, turn_number - self._recent_window_turns),
            limit=10,
            scene_type="conversation",
        )
        live_interventions = await self._db.get_active_interventions(self._story_id, turn_number)
        active_pressures = await self._db.get_active_story_dramatic_pressures(self._story_id)
        hooks = hooks if isinstance(hooks, list) else []
        tensions = tensions if isinstance(tensions, list) else []
        patterns = patterns if isinstance(patterns, list) else []
        relationship_modes = relationship_modes if isinstance(relationship_modes, list) else []
        canon_bits = canon_bits if isinstance(canon_bits, list) else []
        recent_closed_scenes = recent_closed_scenes if isinstance(recent_closed_scenes, list) else []
        live_interventions = live_interventions if isinstance(live_interventions, list) else []
        active_pressures = active_pressures if isinstance(active_pressures, list) else []
        active_episode = active_episode if isinstance(active_episode, dict) else None
        candidates = self.build_candidates(
            turn_number=turn_number,
            hooks=hooks,
            tensions=tensions,
            patterns=patterns,
            relationship_modes=relationship_modes,
            canon_bits=canon_bits,
            active_episode=active_episode,
            recent_closed_scenes=recent_closed_scenes,
            live_interventions=live_interventions,
        )[: self._max_active_pressures]

        inserted = 0
        reinforced = 0
        seen_keys: set[str] = set()
        for candidate in candidates:
            seen_keys.add(candidate.dedupe_key)
            existing = await self._db.find_active_story_dramatic_pressure_by_dedupe_key(
                self._story_id,
                candidate.dedupe_key,
            )
            if existing is None:
                await self._db.insert_story_dramatic_pressure(self._story_id, candidate.to_record())
                inserted += 1
                continue
            await self._db.update_story_dramatic_pressure(
                int(existing["id"]),
                {
                    "title": candidate.title,
                    "summary": candidate.summary,
                    "focus_char_ids": candidate.focus_char_ids,
                    "focus_place_id": candidate.focus_place_id,
                    "last_detected_turn": turn_number,
                    "recurrence_count": int(existing.get("recurrence_count") or 1) + 1,
                    "score": max(float(existing.get("score") or 0.0), candidate.score),
                    "urgency": max(float(existing.get("urgency") or 0.0), candidate.urgency),
                    "payoff_ready": max(float(existing.get("payoff_ready") or 0.0), candidate.payoff_ready),
                    "intent_alignment": max(float(existing.get("intent_alignment") or 0.0), candidate.intent_alignment),
                    "source_hook_id": candidate.source_hook_id,
                    "source_tension_id": candidate.source_tension_id,
                    "source_pattern_id": candidate.source_pattern_id,
                    "source_episode_id": candidate.source_episode_id,
                    "source_relationship_mode_id": candidate.source_relationship_mode_id,
                    "source_canon_bit_id": candidate.source_canon_bit_id,
                    "source_scene_id": candidate.source_scene_id,
                },
            )
            reinforced += 1

        resolved = 0
        for pressure in active_pressures:
            if str(pressure.get("dedupe_key") or "") in seen_keys:
                continue
            if self._should_resolve(pressure, turn_number=turn_number):
                await self._db.resolve_story_dramatic_pressure(
                    int(pressure["id"]),
                    resolved_turn=turn_number,
                    resolution_note="stale dramatic pressure",
                )
                resolved += 1

        self._last_round_summary = {
            "inserted": inserted,
            "reinforced": reinforced,
            "resolved": resolved,
        }

    def build_candidates(
        self,
        *,
        turn_number: int,
        hooks: list[dict[str, Any]],
        tensions: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
        active_episode: dict[str, Any] | None,
        recent_closed_scenes: list[dict[str, Any]],
        live_interventions: list[dict[str, Any]],
    ) -> list[DramaticPressureCandidate]:
        results: list[DramaticPressureCandidate] = []
        results.extend(
            self._build_status_flashpoints(
                turn_number, tensions=tensions, patterns=patterns, relationship_modes=relationship_modes, canon_bits=canon_bits
            )
        )
        results.extend(
            self._build_showoff_flashpoints(
                turn_number, tensions=tensions, patterns=patterns, relationship_modes=relationship_modes, canon_bits=canon_bits
            )
        )
        results.extend(
            self._build_near_reveals(
                turn_number, hooks=hooks, tensions=tensions, patterns=patterns, relationship_modes=relationship_modes, canon_bits=canon_bits
            )
        )
        results.extend(
            self._build_role_reversal_ready(
                turn_number,
                tensions=tensions,
                patterns=patterns,
                relationship_modes=relationship_modes,
                canon_bits=canon_bits,
                active_episode=active_episode,
            )
        )
        results.extend(
            self._build_payoff_ready(
                turn_number,
                hooks=hooks,
                patterns=patterns,
                canon_bits=canon_bits,
                active_episode=active_episode,
            )
        )
        results.extend(
            self._build_stall_risk(
                turn_number, hooks=hooks, tensions=tensions, active_episode=active_episode, live_interventions=live_interventions
            )
        )
        filtered = [row for row in results if row.score >= self._min_pressure_score]
        return sorted(filtered, key=lambda row: (-row.score, -row.urgency, row.pressure_type, row.dedupe_key))

    def _build_status_flashpoints(
        self,
        turn_number: int,
        *,
        tensions: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> list[DramaticPressureCandidate]:
        candidates: list[DramaticPressureCandidate] = []
        for tension in tensions:
            tension_type = str(tension.get("tension_type") or "").strip()
            if tension_type not in {"conflict", "rivalry"}:
                continue
            chars = [str(char_id).strip() for char_id in list(tension.get("involved_chars") or []) if str(char_id).strip()]
            if not chars:
                continue
            matching_patterns = [
                row for row in patterns
                if str(row.get("pattern_type") or "").strip() == "status_clash"
                and self._overlap(chars, list(row.get("involved_chars") or []))
            ]
            matching_modes = [
                row for row in relationship_modes
                if str(row.get("mode_type") or "").strip() in {"irritated_respect", "chaos_partner"}
                and self._overlap(chars, [row.get("char_id_from"), row.get("char_id_to")])
            ]
            matching_canon = [
                row for row in canon_bits
                if str(row.get("motif_key") or "").strip() in {"status_clash", "irritated_respect", "chaos_partner"}
                and self._overlap(chars, list(row.get("focus_char_ids") or []))
            ]
            if not (matching_patterns or matching_modes or matching_canon):
                continue
            urgency = _clamp_unit(0.45 + float(tension.get("intensity") or 0.0) * 0.5)
            payoff_ready = _clamp_unit(0.35 + (0.15 if matching_canon else 0.0) + (0.10 if matching_patterns else 0.0))
            intent_alignment = self._intent_alignment("status_flashpoint")
            score = self._score(urgency, payoff_ready, intent_alignment)
            candidates.append(
                DramaticPressureCandidate(
                    pressure_type="status_flashpoint",
                    title="張り合いを押し出す局面",
                    summary=str(tension.get("description") or "").strip() or "張り合いの押し時が来ている。",
                    focus_char_ids=chars[:3],
                    focus_place_id=None,
                    dedupe_key=f"tension:{int(tension.get('id') or 0)}:status_flashpoint",
                    first_detected_turn=turn_number,
                    last_detected_turn=turn_number,
                    recurrence_count=max(1, int((matching_patterns[0] if matching_patterns else {}).get("recurrence_count") or 1)),
                    score=score,
                    urgency=urgency,
                    payoff_ready=payoff_ready,
                    intent_alignment=intent_alignment,
                    source_tension_id=self._as_int(tension.get("id")),
                    source_pattern_id=self._as_int((matching_patterns[0] if matching_patterns else {}).get("id")),
                    source_relationship_mode_id=self._as_int((matching_modes[0] if matching_modes else {}).get("id")),
                    source_canon_bit_id=self._as_int((matching_canon[0] if matching_canon else {}).get("id")),
                )
            )
        return candidates

    def _build_near_reveals(
        self,
        turn_number: int,
        *,
        hooks: list[dict[str, Any]],
        tensions: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> list[DramaticPressureCandidate]:
        candidates: list[DramaticPressureCandidate] = []
        open_question_hooks = [row for row in hooks if str(row.get("hook_type") or "").strip() == "question"]
        for tension in tensions:
            tension_type = str(tension.get("tension_type") or "").strip()
            if tension_type not in {"secret", "mystery"}:
                continue
            chars = [str(char_id).strip() for char_id in list(tension.get("involved_chars") or []) if str(char_id).strip()]
            matching_patterns = [
                row for row in patterns
                if str(row.get("pattern_type") or "").strip() == "near_reveal"
                and self._overlap(chars, list(row.get("involved_chars") or []))
            ]
            matching_modes = [
                row for row in relationship_modes
                if str(row.get("mode_type") or "").strip() == "unsafe_confidant"
                and self._overlap(chars, [row.get("char_id_from"), row.get("char_id_to")])
            ]
            matching_canon = [
                row for row in canon_bits
                if str(row.get("motif_key") or "").strip() in {"near_reveal", "unsafe_confidant"}
                and self._overlap(chars, list(row.get("focus_char_ids") or []))
            ]
            if not (matching_patterns or matching_modes or matching_canon):
                continue
            urgency = _clamp_unit(0.40 + float(tension.get("intensity") or 0.0) * 0.5)
            payoff_ready = _clamp_unit(
                0.35
                + (0.15 if matching_canon else 0.0)
                + (0.10 if matching_patterns else 0.0)
                + (0.10 if open_question_hooks else 0.0)
            )
            intent_alignment = self._intent_alignment("near_reveal")
            score = self._score(urgency, payoff_ready, intent_alignment)
            candidates.append(
                DramaticPressureCandidate(
                    pressure_type="near_reveal",
                    title="核心を一歩前に出す局面",
                    summary=str(tension.get("description") or "").strip() or "隠れていた輪郭を前に出す押し時がある。",
                    focus_char_ids=chars[:3],
                    focus_place_id=None,
                    dedupe_key=f"tension:{int(tension.get('id') or 0)}:near_reveal",
                    first_detected_turn=turn_number,
                    last_detected_turn=turn_number,
                    recurrence_count=max(1, int((matching_patterns[0] if matching_patterns else {}).get("recurrence_count") or 1)),
                    score=score,
                    urgency=urgency,
                    payoff_ready=payoff_ready,
                    intent_alignment=intent_alignment,
                    source_tension_id=self._as_int(tension.get("id")),
                    source_pattern_id=self._as_int((matching_patterns[0] if matching_patterns else {}).get("id")),
                    source_relationship_mode_id=self._as_int((matching_modes[0] if matching_modes else {}).get("id")),
                    source_canon_bit_id=self._as_int((matching_canon[0] if matching_canon else {}).get("id")),
                    source_hook_id=self._as_int(open_question_hooks[0].get("id")) if open_question_hooks else None,
                )
            )
        return candidates

    def _build_showoff_flashpoints(
        self,
        turn_number: int,
        *,
        tensions: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> list[DramaticPressureCandidate]:
        candidates: list[DramaticPressureCandidate] = []
        for tension in tensions:
            tension_type = str(tension.get("tension_type") or "").strip()
            if tension_type not in {"conflict", "rivalry"}:
                continue
            chars = [str(char_id).strip() for char_id in list(tension.get("involved_chars") or []) if str(char_id).strip()]
            matching_patterns = [
                row for row in patterns
                if str(row.get("pattern_type") or "").strip() == "bluff_or_showoff"
                and self._overlap(chars, list(row.get("involved_chars") or []))
            ]
            matching_modes = [
                row for row in relationship_modes
                if str(row.get("mode_type") or "").strip() == "chaos_partner"
                and self._overlap(chars, [row.get("char_id_from"), row.get("char_id_to")])
            ]
            matching_canon = [
                row for row in canon_bits
                if str(row.get("motif_key") or "").strip() in {"small_win_loss", "chaos_partner", "bluff_or_showoff"}
                and self._overlap(chars, list(row.get("focus_char_ids") or []))
            ]
            if not (matching_patterns or matching_modes or matching_canon):
                continue
            urgency = _clamp_unit(0.42 + float(tension.get("intensity") or 0.0) * 0.48)
            payoff_ready = _clamp_unit(
                0.38
                + (0.12 if matching_patterns else 0.0)
                + (0.12 if matching_modes else 0.0)
                + (0.10 if matching_canon else 0.0)
            )
            intent_alignment = self._intent_alignment("showoff_flashpoint", "bluff_or_showoff")
            score = self._score(urgency, payoff_ready, intent_alignment)
            candidates.append(
                DramaticPressureCandidate(
                    pressure_type="showoff_flashpoint",
                    title="見せ場を押し出す局面",
                    summary=str(tension.get("description") or "").strip() or "見せ場を張ると流れが動きやすい。",
                    focus_char_ids=chars[:3],
                    focus_place_id=None,
                    dedupe_key=f"tension:{int(tension.get('id') or 0)}:showoff_flashpoint",
                    first_detected_turn=turn_number,
                    last_detected_turn=turn_number,
                    recurrence_count=max(1, int((matching_patterns[0] if matching_patterns else {}).get("recurrence_count") or 1)),
                    score=score,
                    urgency=urgency,
                    payoff_ready=payoff_ready,
                    intent_alignment=intent_alignment,
                    source_tension_id=self._as_int(tension.get("id")),
                    source_pattern_id=self._as_int((matching_patterns[0] if matching_patterns else {}).get("id")),
                    source_relationship_mode_id=self._as_int((matching_modes[0] if matching_modes else {}).get("id")),
                    source_canon_bit_id=self._as_int((matching_canon[0] if matching_canon else {}).get("id")),
                )
            )
        return candidates

    def _build_role_reversal_ready(
        self,
        turn_number: int,
        *,
        tensions: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
        active_episode: dict[str, Any] | None,
    ) -> list[DramaticPressureCandidate]:
        candidates: list[DramaticPressureCandidate] = []
        for pattern in patterns:
            if str(pattern.get("pattern_type") or "").strip() != "role_reversal":
                continue
            chars = [str(char_id).strip() for char_id in list(pattern.get("involved_chars") or []) if str(char_id).strip()]
            matching_tension = next(
                (
                    row for row in tensions
                    if str(row.get("tension_type") or "").strip() in {"conflict", "rivalry", "mystery"}
                    and self._overlap(chars, list(row.get("involved_chars") or []))
                ),
                None,
            )
            matching_modes = [
                row for row in relationship_modes
                if str(row.get("mode_type") or "").strip() in {"irritated_respect", "cannot_ignore"}
                and self._overlap(chars, [row.get("char_id_from"), row.get("char_id_to")])
            ]
            matching_canon = [
                row for row in canon_bits
                if str(row.get("motif_key") or "").strip() in {"status_clash", "irritated_respect", "cannot_ignore", "role_reversal"}
                and self._overlap(chars, list(row.get("focus_char_ids") or []))
            ]
            episode_match = (
                isinstance(active_episode, dict)
                and self._overlap(chars, list(active_episode.get("focus_char_ids") or []))
            )
            if not (matching_tension or matching_modes or matching_canon or episode_match):
                continue
            urgency = _clamp_unit(
                0.40
                + (0.12 if matching_tension else 0.0)
                + (0.12 if episode_match else 0.0)
                + float(pattern.get("intensity") or 0.0) * 0.20
            )
            payoff_ready = _clamp_unit(
                0.38
                + (0.12 if matching_modes else 0.0)
                + (0.10 if matching_canon else 0.0)
                + (0.08 if int(pattern.get("recurrence_count") or 0) >= 2 else 0.0)
            )
            intent_alignment = self._intent_alignment("role_reversal_ready", "role_reversal")
            score = self._score(urgency, payoff_ready, intent_alignment)
            candidates.append(
                DramaticPressureCandidate(
                    pressure_type="role_reversal_ready",
                    title="主導権の入れ替わりを押し出す局面",
                    summary=str(pattern.get("description") or "").strip() or "いつもの力関係をひっくり返す押し時がある。",
                    focus_char_ids=chars[:3],
                    focus_place_id=str(active_episode.get("focus_place_id") or "").strip() or None if isinstance(active_episode, dict) else None,
                    dedupe_key=f"pattern:{int(pattern.get('id') or 0)}:role_reversal_ready",
                    first_detected_turn=turn_number,
                    last_detected_turn=turn_number,
                    recurrence_count=max(1, int(pattern.get("recurrence_count") or 1)),
                    score=score,
                    urgency=urgency,
                    payoff_ready=payoff_ready,
                    intent_alignment=intent_alignment,
                    source_pattern_id=self._as_int(pattern.get("id")),
                    source_tension_id=self._as_int(matching_tension.get("id")) if isinstance(matching_tension, dict) else None,
                    source_relationship_mode_id=self._as_int((matching_modes[0] if matching_modes else {}).get("id")),
                    source_canon_bit_id=self._as_int((matching_canon[0] if matching_canon else {}).get("id")),
                    source_episode_id=self._as_int(active_episode.get("id")) if isinstance(active_episode, dict) else None,
                )
            )
        return candidates

    def _build_payoff_ready(
        self,
        turn_number: int,
        *,
        hooks: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
        active_episode: dict[str, Any] | None,
    ) -> list[DramaticPressureCandidate]:
        if active_episode is None:
            return []
        opened_turn = int(active_episode.get("opened_turn") or turn_number)
        age_turns = max(0, turn_number - opened_turn + 1)
        episode_chars = [str(char_id).strip() for char_id in list(active_episode.get("focus_char_ids") or []) if str(char_id).strip()]
        episode_place = str(active_episode.get("focus_place_id") or "").strip() or None
        motif = str(active_episode.get("episode_type") or "").strip()
        matching_canon = [
            row for row in canon_bits
            if self._overlap(episode_chars, list(row.get("focus_char_ids") or []))
            or (episode_place is not None and str(row.get("focus_place_id") or "").strip() == episode_place)
        ]
        matching_patterns = [
            row for row in patterns
            if self._overlap(episode_chars, list(row.get("involved_chars") or []))
        ]
        canon_hooks = [
            row for row in hooks
            if row.get("source_canon_bit_id") is not None
            and (
                self._overlap(episode_chars, [row.get("owner_char_id"), row.get("target_char_id")])
                or self._as_int(row.get("source_canon_bit_id")) in {
                    self._as_int(bit.get("id")) for bit in matching_canon
                }
            )
        ]
        if age_turns < 4 and not matching_canon and not canon_hooks and not any(int(row.get("recurrence_count") or 0) >= 2 for row in matching_patterns):
            return []
        urgency = _clamp_unit(0.30 + min(age_turns, 8) * 0.07)
        payoff_ready = _clamp_unit(
            0.45
            + (0.15 if matching_canon else 0.0)
            + (0.12 if canon_hooks else 0.0)
            + (0.10 if any(int(row.get("recurrence_count") or 0) >= 2 for row in matching_patterns) else 0.0)
        )
        intent_alignment = self._intent_alignment("payoff_ready", motif)
        score = self._score(urgency, payoff_ready, intent_alignment)
        return [
            DramaticPressureCandidate(
                pressure_type="payoff_ready",
                title="回収を前に出せる局面",
                summary=str(active_episode.get("goal") or "").strip() or "継続してきた火種を回収する押し時がある。",
                focus_char_ids=episode_chars[:3],
                focus_place_id=episode_place,
                dedupe_key=f"episode:{int(active_episode.get('id') or 0)}:payoff_ready",
                first_detected_turn=turn_number,
                last_detected_turn=turn_number,
                recurrence_count=1 + sum(1 for _ in matching_canon[:1]) + sum(1 for _ in canon_hooks[:1]) + sum(1 for row in matching_patterns if int(row.get("recurrence_count") or 0) >= 2),
                score=score,
                urgency=urgency,
                payoff_ready=payoff_ready,
                intent_alignment=intent_alignment,
                source_episode_id=self._as_int(active_episode.get("id")),
                source_pattern_id=self._as_int((matching_patterns[0] if matching_patterns else {}).get("id")),
                source_canon_bit_id=self._as_int((matching_canon[0] if matching_canon else {}).get("id")),
                source_hook_id=self._as_int((canon_hooks[0] if canon_hooks else {}).get("id")),
            )
        ]

    def _build_stall_risk(
        self,
        turn_number: int,
        *,
        hooks: list[dict[str, Any]],
        tensions: list[dict[str, Any]],
        active_episode: dict[str, Any] | None,
        live_interventions: list[dict[str, Any]],
    ) -> list[DramaticPressureCandidate]:
        if active_episode is None:
            return []
        last_progress_turn = int(active_episode.get("last_progress_turn") or active_episode.get("opened_turn") or turn_number)
        stale_turns = max(0, turn_number - last_progress_turn)
        if stale_turns < self._stale_rounds_before_resolve:
            return []
        focus_chars = [str(char_id).strip() for char_id in list(active_episode.get("focus_char_ids") or []) if str(char_id).strip()]
        relevant_canon_hooks = [
            row for row in hooks
            if row.get("source_canon_bit_id") is not None
            and self._overlap(focus_chars, [row.get("owner_char_id"), row.get("target_char_id")])
        ]
        if not hooks and not tensions and live_interventions:
            return []
        urgency = _clamp_unit(0.50 + min(stale_turns, 4) * 0.10)
        payoff_ready = _clamp_unit(0.20 + (0.10 if hooks else 0.0) + (0.10 if tensions else 0.0) + (0.10 if relevant_canon_hooks else 0.0))
        intent_alignment = self._intent_alignment("stall_risk")
        score = self._score(urgency, payoff_ready, intent_alignment)
        return [
            DramaticPressureCandidate(
                pressure_type="stall_risk",
                title="火種が流れかけている局面",
                summary="episode の進行が停滞しており、次の一手が必要。",
                focus_char_ids=focus_chars[:3],
                focus_place_id=str(active_episode.get("focus_place_id") or "").strip() or None,
                dedupe_key=f"episode:{int(active_episode.get('id') or 0)}:stall_risk",
                first_detected_turn=turn_number,
                last_detected_turn=turn_number,
                recurrence_count=1,
                score=score,
                urgency=urgency,
                payoff_ready=payoff_ready,
                intent_alignment=intent_alignment,
                source_episode_id=self._as_int(active_episode.get("id")),
                source_hook_id=self._as_int((relevant_canon_hooks[0] if relevant_canon_hooks else {}).get("id")),
            )
        ]

    def _intent_alignment(self, pressure_type: str, motif_key: str | None = None) -> float:
        if self._intent_profile is None:
            return 0.55
        preferred_patterns = set(self._intent_profile.preferred_pattern_types)
        if pressure_type in {"status_flashpoint", "payoff_ready"} and (
            motif_key in {"status_clash", "small_win_loss", "role_reversal"} or "status_clash" in preferred_patterns
        ):
            return 0.80
        if pressure_type == "showoff_flashpoint" and (
            motif_key in {"bluff_or_showoff", "small_win_loss"} or "bluff_or_showoff" in preferred_patterns
        ):
            return 0.80
        if pressure_type == "near_reveal" and "misunderstanding" in preferred_patterns:
            return 0.80
        if pressure_type == "role_reversal_ready" and (
            motif_key == "role_reversal" or "role_reversal" in preferred_patterns
        ):
            return 0.80
        if pressure_type == "stall_risk":
            return 0.55
        return 0.55

    @staticmethod
    def _score(urgency: float, payoff_ready: float, intent_alignment: float) -> float:
        return _clamp_unit(0.45 * urgency + 0.35 * payoff_ready + 0.20 * intent_alignment)

    def _should_resolve(self, pressure: dict[str, Any], *, turn_number: int) -> bool:
        last_detected_turn = int(pressure.get("last_detected_turn") or 0)
        return turn_number - last_detected_turn >= self._stale_rounds_before_resolve

    @staticmethod
    def _overlap(left: list[Any], right: list[Any]) -> bool:
        left_set = {str(item).strip() for item in left if str(item).strip()}
        right_set = {str(item).strip() for item in right if str(item).strip()}
        return bool(left_set & right_set)

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
