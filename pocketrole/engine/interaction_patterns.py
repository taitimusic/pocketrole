"""Deterministic interaction-pattern extraction and ranking helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager


@dataclass(frozen=True)
class InteractionPatternCandidate:
    """Persistable interaction-pattern candidate."""

    pattern_type: str
    title: str
    description: str
    involved_chars: list[str]
    dedupe_key: str
    first_detected_turn: int
    last_detected_turn: int
    recurrence_count: int = 1
    intensity: float = 0.5
    confidence: float = 0.5
    source_hook_id: int | None = None
    source_tension_id: int | None = None
    source_scene_id: int | None = None
    source_event_id: int | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "pattern_type": self.pattern_type,
            "status": "active",
            "title": self.title,
            "description": self.description,
            "involved_chars": self.involved_chars,
            "dedupe_key": self.dedupe_key,
            "source_hook_id": self.source_hook_id,
            "source_tension_id": self.source_tension_id,
            "source_scene_id": self.source_scene_id,
            "source_event_id": self.source_event_id,
            "first_detected_turn": self.first_detected_turn,
            "last_detected_turn": self.last_detected_turn,
            "recurrence_count": self.recurrence_count,
            "intensity": self.intensity,
            "confidence": self.confidence,
        }


PATTERN_TO_TENSION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "misunderstanding": ("mystery", "conflict"),
    "status_clash": ("conflict", "rivalry"),
    "bluff_or_showoff": ("rivalry", "conflict"),
    "near_reveal": ("secret", "mystery"),
    "role_reversal": ("mystery", "rivalry"),
    "small_win_loss": ("rivalry", "conflict"),
}

PATTERN_TO_INTERVENTION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "misunderstanding": ("revelation", "opportunity"),
    "status_clash": ("relationship_catalyst", "opportunity"),
    "bluff_or_showoff": ("opportunity", "relationship_catalyst"),
    "near_reveal": ("revelation",),
    "role_reversal": ("opportunity", "revelation"),
    "small_win_loss": ("opportunity", "relationship_catalyst"),
}

_SECRET_OUTCOME_CUES = ("秘密", "隠", "言えない", "別の声", "demo")
_SHOWOFF_CUES = (
    "見せつけ",
    "自慢",
    "誇示",
    "ドヤ",
    "格好つけ",
    "やってみせ",
    "show off",
    "brag",
)
_REVERSAL_CUES = (
    "逆",
    "入れ替",
    "立場",
    "主導権が移る",
    "普段と逆",
    "やらされ",
    "言わされ",
    "role reversal",
    "turned the tables",
)


class InteractionPatternEngine:
    """Build deterministic interaction-pattern candidates from runtime artifacts."""

    def __init__(
        self,
        db: DatabaseManager | None = None,
        story_id: str = "",
        *,
        analysis_interval_rounds: int = 3,
    ) -> None:
        self._db = db
        self._story_id = story_id
        self._analysis_interval_rounds = max(1, analysis_interval_rounds)

    async def process_round(self, turn_number: int) -> None:
        """Extract / upsert / resolve interaction patterns for the round."""
        if self._db is None or not self._story_id:
            return
        hooks = await self._db.get_open_story_hooks(self._story_id)
        tensions = await self._db.get_active_tensions(self._story_id)
        since_turn = max(0, turn_number - self._analysis_interval_rounds)
        closed_scenes = await self._db.get_recent_closed_story_scenes(
            self._story_id,
            since_turn=since_turn,
            limit=10,
            scene_type="conversation",
        )
        relationship_events = await self._db.get_recent_relationship_events(
            self._story_id,
            since_turn=since_turn,
            limit=20,
        )
        active_episode = await self._db.get_active_story_episode(self._story_id)
        relationship_modes = await self._db.get_active_relationship_modes(self._story_id)
        canon_bits = await self._db.get_active_story_canon_bits(self._story_id)
        candidates = self.build_candidates(
            turn_number=turn_number,
            hooks=hooks,
            tensions=tensions,
            closed_scenes=closed_scenes,
            relationship_events=relationship_events,
            active_episode=active_episode if isinstance(active_episode, dict) else None,
            relationship_modes=relationship_modes,
            canon_bits=canon_bits,
        )
        for candidate in candidates:
            existing = await self._db.find_active_interaction_pattern_by_dedupe_key(
                self._story_id,
                candidate.dedupe_key,
            )
            if existing is None:
                await self._db.insert_interaction_pattern(self._story_id, candidate.to_record())
                continue
            last_detected_turn = int(existing.get("last_detected_turn") or 0)
            recurrence_count = int(existing.get("recurrence_count") or 1)
            await self._db.update_interaction_pattern(
                int(existing["id"]),
                {
                    "last_detected_turn": turn_number,
                    "recurrence_count": recurrence_count if last_detected_turn == turn_number else recurrence_count + 1,
                    "intensity": max(float(existing.get("intensity") or 0.0), candidate.intensity),
                    "confidence": max(float(existing.get("confidence") or 0.0), candidate.confidence),
                },
            )
        await self._resolve_stale_patterns(turn_number, hooks=hooks, tensions=tensions)

    def build_candidates(
        self,
        *,
        turn_number: int,
        hooks: list[dict[str, Any]],
        tensions: list[dict[str, Any]],
        closed_scenes: list[dict[str, Any]],
        relationship_events: list[dict[str, Any]],
        active_episode: dict[str, Any] | None = None,
        relationship_modes: list[dict[str, Any]] | None = None,
        canon_bits: list[dict[str, Any]] | None = None,
    ) -> list[InteractionPatternCandidate]:
        relationship_modes = relationship_modes or []
        canon_bits = canon_bits or []
        candidates: list[InteractionPatternCandidate] = []
        candidates.extend(self._from_tensions(turn_number, tensions, relationship_modes, canon_bits))
        candidates.extend(self._from_hooks(turn_number, hooks, tensions, closed_scenes, relationship_modes, canon_bits))
        candidates.extend(
            self._from_scenes(
                turn_number,
                closed_scenes,
                active_episode,
                relationship_modes,
                canon_bits,
            )
        )
        candidates.extend(self._from_relationship_events(turn_number, relationship_events, relationship_modes, canon_bits))
        return candidates

    def _from_tensions(
        self,
        turn_number: int,
        tensions: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> list[InteractionPatternCandidate]:
        candidates: list[InteractionPatternCandidate] = []
        for tension in tensions:
            tension_id = int(tension.get("id", 0))
            tension_type = str(tension.get("tension_type") or "").strip()
            chars = [str(char_id).strip() for char_id in tension.get("involved_chars", []) if str(char_id).strip()]
            if tension_type in {"conflict", "rivalry"}:
                candidates.append(
                    InteractionPatternCandidate(
                        pattern_type="status_clash",
                        title="張り合いが前景化する",
                        description=str(tension.get("description") or "").strip(),
                        involved_chars=chars,
                        dedupe_key=f"tension:{tension_id}:status_clash",
                        source_tension_id=tension_id,
                        first_detected_turn=turn_number,
                        last_detected_turn=turn_number,
                        intensity=float(tension.get("intensity") or 0.5),
                        confidence=0.75,
                    )
                )
                description = str(tension.get("description") or "").strip()
                if self._contains_any(description, _SHOWOFF_CUES) or self._supports_bluff_pattern(
                    chars,
                    relationship_modes=relationship_modes,
                    canon_bits=canon_bits,
                ):
                    candidates.append(
                        InteractionPatternCandidate(
                            pattern_type="bluff_or_showoff",
                            title="見せ場の誇示が火種になる",
                            description=description,
                            involved_chars=chars,
                            dedupe_key=f"tension:{tension_id}:bluff_or_showoff",
                            source_tension_id=tension_id,
                            first_detected_turn=turn_number,
                            last_detected_turn=turn_number,
                            intensity=float(tension.get("intensity") or 0.5),
                            confidence=0.68,
                        )
                    )
            if tension_type in {"secret", "mystery"}:
                candidates.append(
                    InteractionPatternCandidate(
                        pattern_type="near_reveal",
                        title="隠れていた輪郭が近づく",
                        description=str(tension.get("description") or "").strip(),
                        involved_chars=chars,
                        dedupe_key=f"tension:{tension_id}:near_reveal",
                        source_tension_id=tension_id,
                        first_detected_turn=turn_number,
                        last_detected_turn=turn_number,
                        intensity=float(tension.get("intensity") or 0.45),
                        confidence=0.7,
                    )
                )
        return candidates

    def _from_hooks(
        self,
        turn_number: int,
        hooks: list[dict[str, Any]],
        tensions: list[dict[str, Any]],
        closed_scenes: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> list[InteractionPatternCandidate]:
        candidates: list[InteractionPatternCandidate] = []
        active_tension_types = {str(t.get("tension_type") or "").strip() for t in tensions}
        recent_closed_scene_ids = {int(scene["id"]) for scene in closed_scenes if scene.get("id") is not None}
        for hook in hooks:
            hook_id = int(hook.get("id", 0))
            hook_type = str(hook.get("hook_type") or "").strip()
            involved_chars = [
                str(char_id).strip()
                for char_id in [hook.get("owner_char_id"), hook.get("target_char_id")]
                if str(char_id or "").strip()
            ]
            if hook_type == "conflict":
                candidates.append(
                    InteractionPatternCandidate(
                        pattern_type="status_clash",
                        title="張り合いが始まる",
                        description=str(hook.get("description") or "").strip(),
                        involved_chars=involved_chars,
                        dedupe_key=f"hook:{hook_id}:status_clash",
                        source_hook_id=hook_id,
                        source_scene_id=hook.get("source_scene_id"),
                        first_detected_turn=turn_number,
                        last_detected_turn=turn_number,
                        intensity=float(hook.get("priority") or 0.5),
                        confidence=0.7,
                    )
                )
                description = str(hook.get("description") or "").strip()
                if self._contains_any(description, _SHOWOFF_CUES) or self._supports_bluff_pattern(
                    involved_chars,
                    relationship_modes=relationship_modes,
                    canon_bits=canon_bits,
                ):
                    candidates.append(
                        InteractionPatternCandidate(
                            pattern_type="bluff_or_showoff",
                            title="張り合いが見せ場づくりへ傾く",
                            description=description,
                            involved_chars=involved_chars,
                            dedupe_key=f"hook:{hook_id}:bluff_or_showoff",
                            source_hook_id=hook_id,
                            source_scene_id=hook.get("source_scene_id"),
                            first_detected_turn=turn_number,
                            last_detected_turn=turn_number,
                            intensity=float(hook.get("priority") or 0.5),
                            confidence=0.62,
                        )
                    )
            if hook_type == "question" and (
                {"mystery", "conflict"} & active_tension_types
                or int(hook.get("source_scene_id") or 0) in recent_closed_scene_ids
            ):
                candidates.append(
                    InteractionPatternCandidate(
                        pattern_type="misunderstanding",
                        title="食い違いが残る",
                        description=str(hook.get("description") or "").strip(),
                        involved_chars=involved_chars,
                        dedupe_key=f"hook:{hook_id}:misunderstanding",
                        source_hook_id=hook_id,
                        source_scene_id=hook.get("source_scene_id"),
                        first_detected_turn=turn_number,
                        last_detected_turn=turn_number,
                        intensity=float(hook.get("priority") or 0.5),
                        confidence=0.65,
                    )
                )
        return candidates

    def _from_scenes(
        self,
        turn_number: int,
        closed_scenes: list[dict[str, Any]],
        active_episode: dict[str, Any] | None,
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> list[InteractionPatternCandidate]:
        candidates: list[InteractionPatternCandidate] = []
        for scene in closed_scenes:
            scene_id = int(scene.get("id", 0))
            summary = str(scene.get("outcome_summary") or "").strip()
            if not summary:
                continue
            lowered = summary.lower()
            involved_chars = [
                str(char_id).strip()
                for char_id in scene.get("focus_char_ids", [])
                if str(char_id).strip()
            ]
            if any(cue in summary or cue in lowered for cue in _SECRET_OUTCOME_CUES):
                candidates.append(
                    InteractionPatternCandidate(
                        pattern_type="near_reveal",
                        title="言い切られない何かが残る",
                        description=summary,
                        involved_chars=involved_chars,
                        dedupe_key=f"scene:{scene_id}:near_reveal",
                        source_scene_id=scene_id,
                        first_detected_turn=turn_number,
                        last_detected_turn=turn_number,
                        intensity=0.55,
                        confidence=0.6,
                    )
                )
            if self._contains_any(summary, _SHOWOFF_CUES):
                candidates.append(
                    InteractionPatternCandidate(
                        pattern_type="bluff_or_showoff",
                        title="見せ場づくりが空気を動かす",
                        description=summary,
                        involved_chars=involved_chars,
                        dedupe_key=f"scene:{scene_id}:bluff_or_showoff",
                        source_scene_id=scene_id,
                        first_detected_turn=turn_number,
                        last_detected_turn=turn_number,
                        intensity=0.52,
                        confidence=0.6,
                    )
                )
            if len(involved_chars) >= 2 and self._supports_role_reversal(
                involved_chars,
                summary=summary,
                active_episode=active_episode,
                relationship_modes=relationship_modes,
                canon_bits=canon_bits,
            ):
                candidates.append(
                    InteractionPatternCandidate(
                        pattern_type="role_reversal",
                        title="いつもの力関係がひっくり返る",
                        description=summary,
                        involved_chars=involved_chars,
                        dedupe_key=f"scene:{scene_id}:role_reversal",
                        source_scene_id=scene_id,
                        first_detected_turn=turn_number,
                        last_detected_turn=turn_number,
                        intensity=0.58 + self._relationship_canon_boost(
                            involved_chars,
                            relationship_modes=relationship_modes,
                            canon_bits=canon_bits,
                        ),
                        confidence=0.66 + self._episode_overlap_boost(
                            involved_chars,
                            active_episode=active_episode,
                        ),
                    )
                )
        return candidates

    def _from_relationship_events(
        self,
        turn_number: int,
        relationship_events: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> list[InteractionPatternCandidate]:
        candidates: list[InteractionPatternCandidate] = []
        for event in relationship_events:
            event_id = int(event.get("id", 0))
            delta_tension = float(event.get("delta_tension") or 0.0)
            total_delta = abs(delta_tension) + abs(float(event.get("delta_trust") or 0.0)) + abs(
                float(event.get("delta_affinity") or 0.0)
            )
            involved_chars = [
                str(event.get("char_id_from") or "").strip(),
                str(event.get("char_id_to") or "").strip(),
            ]
            if delta_tension > 0:
                candidates.append(
                    InteractionPatternCandidate(
                        pattern_type="status_clash",
                        title="関係の張りが増す",
                        description=str(event.get("summary") or "").strip(),
                        involved_chars=involved_chars,
                        dedupe_key=f"event:{event_id}:status_clash",
                        source_event_id=event_id,
                        source_scene_id=event.get("scene_id"),
                        first_detected_turn=turn_number,
                        last_detected_turn=turn_number,
                        intensity=max(0.4, total_delta),
                        confidence=0.6,
                    )
                )
                summary = str(event.get("summary") or "").strip()
                if self._contains_any(summary, _SHOWOFF_CUES) or self._supports_bluff_pattern(
                    involved_chars,
                    relationship_modes=relationship_modes,
                    canon_bits=canon_bits,
                ):
                    candidates.append(
                        InteractionPatternCandidate(
                            pattern_type="bluff_or_showoff",
                            title="張り合いが見栄へ傾く",
                            description=summary,
                            involved_chars=involved_chars,
                            dedupe_key=f"event:{event_id}:bluff_or_showoff",
                            source_event_id=event_id,
                            source_scene_id=event.get("scene_id"),
                            first_detected_turn=turn_number,
                            last_detected_turn=turn_number,
                            intensity=max(0.35, total_delta),
                            confidence=0.55,
                        )
                    )
            if total_delta >= 0.05:
                candidates.append(
                    InteractionPatternCandidate(
                        pattern_type="small_win_loss",
                        title="小さな勝ち負けが残る",
                        description=str(event.get("summary") or "").strip(),
                        involved_chars=involved_chars,
                        dedupe_key=f"event:{event_id}:small_win_loss",
                        source_event_id=event_id,
                        source_scene_id=event.get("scene_id"),
                        first_detected_turn=turn_number,
                        last_detected_turn=turn_number,
                        intensity=max(0.35, total_delta),
                        confidence=0.55,
                    )
                )
        return candidates

    def _supports_bluff_pattern(
        self,
        involved_chars: list[str],
        *,
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> bool:
        chars = self._normalize_chars(involved_chars)
        if not chars:
            return False
        for mode in relationship_modes:
            if str(mode.get("mode_type") or "").strip() == "chaos_partner" and self._overlap(chars, self._mode_chars(mode)):
                return True
        for canon_bit in canon_bits:
            motif_key = str(canon_bit.get("motif_key") or "").strip()
            if motif_key in {"small_win_loss", "chaos_partner"} and self._overlap(chars, self._normalize_chars(canon_bit.get("focus_char_ids", []))):
                return True
        return False

    def _supports_role_reversal(
        self,
        involved_chars: list[str],
        *,
        summary: str = "",
        active_episode: dict[str, Any] | None = None,
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> bool:
        chars = self._normalize_chars(involved_chars)
        if len(chars) < 2:
            return False
        episode_chars = self._normalize_chars(
            list(active_episode.get("focus_char_ids") or []) if isinstance(active_episode, dict) else []
        )
        if episode_chars and self._overlap(chars, episode_chars):
            if self._contains_any(summary, ("前に出", "押し返せない", "流れを渡", "押し切", "主導")):
                return True
        for mode in relationship_modes:
            mode_type = str(mode.get("mode_type") or "").strip()
            if mode_type in {"irritated_respect", "cannot_ignore"} and self._overlap(chars, self._mode_chars(mode)):
                return True
        for canon_bit in canon_bits:
            motif_key = str(canon_bit.get("motif_key") or "").strip()
            if motif_key in {"status_clash", "irritated_respect", "cannot_ignore"} and self._overlap(chars, self._normalize_chars(canon_bit.get("focus_char_ids", []))):
                return True
        return False

    def _relationship_canon_boost(
        self,
        involved_chars: list[str],
        *,
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
    ) -> float:
        chars = self._normalize_chars(involved_chars)
        boost = 0.0
        if any(self._overlap(chars, self._mode_chars(mode)) for mode in relationship_modes):
            boost += 0.04
        if any(
            self._overlap(chars, self._normalize_chars(canon_bit.get("focus_char_ids", [])))
            for canon_bit in canon_bits
        ):
            boost += 0.04
        return boost

    def _episode_overlap_boost(
        self,
        involved_chars: list[str],
        *,
        active_episode: dict[str, Any] | None,
    ) -> float:
        if not isinstance(active_episode, dict):
            return 0.0
        episode_chars = self._normalize_chars(list(active_episode.get("focus_char_ids") or []))
        return 0.06 if episode_chars and self._overlap(self._normalize_chars(involved_chars), episode_chars) else 0.0

    def _mode_chars(self, mode: dict[str, Any]) -> list[str]:
        return self._normalize_chars([mode.get("char_id_from"), mode.get("char_id_to")])

    def _normalize_chars(self, raw_chars: Any) -> list[str]:
        if not isinstance(raw_chars, list):
            raw_chars = list(raw_chars or [])
        return [str(char_id).strip() for char_id in raw_chars if str(char_id or "").strip()]

    def _contains_any(self, text: str, cues: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(cue in text or cue in lowered for cue in cues)

    def _overlap(self, left: list[str], right: list[str]) -> bool:
        return bool(set(left) & set(right))

    async def _resolve_stale_patterns(
        self,
        turn_number: int,
        *,
        hooks: list[dict[str, Any]],
        tensions: list[dict[str, Any]],
    ) -> None:
        if self._db is None or not self._story_id:
            return
        active_patterns = await self._db.get_active_interaction_patterns(self._story_id)
        open_hook_ids = {int(hook["id"]) for hook in hooks if hook.get("id") is not None}
        active_tension_ids = {int(tension["id"]) for tension in tensions if tension.get("id") is not None}
        ttl = self._analysis_interval_rounds
        for pattern in active_patterns:
            should_resolve = False
            source_hook_id = pattern.get("source_hook_id")
            source_tension_id = pattern.get("source_tension_id")
            last_detected_turn = int(pattern.get("last_detected_turn") or 0)
            if source_hook_id is not None and int(source_hook_id) not in open_hook_ids:
                should_resolve = True
            if source_tension_id is not None and int(source_tension_id) not in active_tension_ids:
                should_resolve = True
            if source_hook_id is None and source_tension_id is None and turn_number - last_detected_turn >= ttl:
                should_resolve = True
            if should_resolve:
                await self._db.resolve_interaction_pattern(
                    int(pattern["id"]),
                    resolved_turn=turn_number,
                    resolution_note="pattern resolved or cooled",
                )
