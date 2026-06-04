"""Deterministic persisted canon extraction and reinforcement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager
    from engine.story_intent import StoryIntentProfile


CANON_TO_TENSION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "status_clash": ("conflict", "rivalry"),
    "irritated_respect": ("conflict", "rivalry"),
    "misunderstanding": ("mystery", "conflict"),
    "near_reveal": ("secret", "mystery"),
    "unsafe_confidant": ("secret", "mystery"),
    "small_win_loss": ("rivalry", "conflict"),
    "chaos_partner": ("rivalry", "conflict"),
    "cannot_ignore": ("mystery", "conflict", "secret"),
}

CANON_TO_INTERVENTION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "status_clash": ("relationship_catalyst",),
    "irritated_respect": ("relationship_catalyst",),
    "misunderstanding": ("revelation",),
    "near_reveal": ("revelation",),
    "unsafe_confidant": ("revelation",),
    "small_win_loss": ("opportunity",),
    "chaos_partner": ("opportunity", "relationship_catalyst"),
    "cannot_ignore": ("relationship_catalyst", "revelation", "opportunity"),
}

_PROMOTION_ORDER = {
    "momentary_bit": 0,
    "recurring_bit": 1,
    "proto_canon": 2,
    "canon": 3,
}

_STALE_TTL_BY_LEVEL = {
    "momentary_bit": 4,
    "recurring_bit": 6,
    "proto_canon": 8,
    "canon": 12,
}

_CONFIDENCE_BY_SOURCE = {
    "relationship_mode": 0.80,
    "pattern": 0.75,
    "episode": 0.70,
    "scene": 0.65,
}


@dataclass(frozen=True)
class CanonBitCandidate:
    bit_type: str
    motif_key: str
    title: str
    summary: str
    focus_char_ids: list[str]
    focus_place_id: str | None
    dedupe_key: str
    evidence_sources: list[str]
    first_detected_turn: int
    last_reinforced_turn: int
    recurrence_count: int
    confidence: float
    novelty: float
    intent_alignment: float
    anchor_pattern_id: int | None = None
    anchor_episode_id: int | None = None
    anchor_relationship_mode_id: int | None = None
    anchor_scene_id: int | None = None

    def initial_level(self) -> str:
        if self.recurrence_count >= 4 and len(self.evidence_sources) >= 2:
            return "proto_canon"
        if self.recurrence_count >= 3 and len(self.evidence_sources) >= 2:
            return "proto_canon"
        if self.recurrence_count >= 2:
            return "recurring_bit"
        return "momentary_bit"

    def to_record(self) -> dict[str, Any]:
        return {
            "bit_type": self.bit_type,
            "motif_key": self.motif_key,
            "canon_level": self.initial_level(),
            "status": "active",
            "title": self.title,
            "summary": self.summary,
            "focus_char_ids": self.focus_char_ids,
            "focus_place_id": self.focus_place_id,
            "dedupe_key": self.dedupe_key,
            "evidence_sources": self.evidence_sources,
            "anchor_pattern_id": self.anchor_pattern_id,
            "anchor_episode_id": self.anchor_episode_id,
            "anchor_relationship_mode_id": self.anchor_relationship_mode_id,
            "anchor_scene_id": self.anchor_scene_id,
            "first_detected_turn": self.first_detected_turn,
            "last_reinforced_turn": self.last_reinforced_turn,
            "recurrence_count": self.recurrence_count,
            "confidence": self.confidence,
            "novelty": self.novelty,
            "intent_alignment": self.intent_alignment,
        }


class EmergentCanonizer:
    """Promote repeated runtime motifs into persisted canon bits."""

    def __init__(
        self,
        db: DatabaseManager | None = None,
        story_id: str = "",
        *,
        intent_profile: StoryIntentProfile | None = None,
    ) -> None:
        self._db = db
        self._story_id = story_id
        self._intent_profile = intent_profile
        self._last_round_summary: dict[str, Any] = {
            "inserted": 0,
            "reinforced": 0,
            "promoted": 0,
            "archived": 0,
        }

    def get_last_round_summary(self) -> dict[str, Any]:
        return dict(self._last_round_summary)

    async def process_round(self, turn_number: int) -> None:
        if self._db is None or not self._story_id:
            return

        patterns = await self._db.get_recent_interaction_patterns(
            self._story_id,
            since_turn=max(0, turn_number - 8),
            limit=12,
        )
        relationship_modes = await self._db.get_recent_relationship_modes(self._story_id, limit=12)
        episodes = await self._db.get_recent_story_episodes(self._story_id, limit=12)
        scenes = await self._db.get_recent_closed_story_scenes(
            self._story_id,
            since_turn=max(0, turn_number - 8),
            limit=12,
            scene_type="conversation",
        )
        active_bits = await self._db.get_active_story_canon_bits(self._story_id)
        candidates = self.build_candidates(
            turn_number=turn_number,
            patterns=patterns,
            relationship_modes=relationship_modes,
            episodes=episodes,
            scenes=scenes,
        )

        inserted = 0
        reinforced = 0
        promoted = 0
        seen_dedupe_keys: set[str] = set()
        for candidate in candidates:
            seen_dedupe_keys.add(candidate.dedupe_key)
            existing = await self._db.find_active_story_canon_bit_by_dedupe_key(
                self._story_id, candidate.dedupe_key
            )
            if existing is None:
                await self._db.insert_story_canon_bit(self._story_id, candidate.to_record())
                inserted += 1
                continue
            patch = self._build_reinforcement_patch(existing, candidate, turn_number)
            before_level = str(existing.get("canon_level") or "momentary_bit")
            after_level = str(patch.get("canon_level") or before_level)
            await self._db.update_story_canon_bit(int(existing["id"]), patch)
            reinforced += 1
            if _PROMOTION_ORDER.get(after_level, 0) > _PROMOTION_ORDER.get(before_level, 0):
                promoted += 1

        archived = 0
        for bit in active_bits:
            if str(bit.get("dedupe_key") or "") in seen_dedupe_keys:
                continue
            if self._should_archive(bit, turn_number=turn_number):
                await self._db.archive_story_canon_bit(int(bit["id"]))
                archived += 1

        self._last_round_summary = {
            "inserted": inserted,
            "reinforced": reinforced,
            "promoted": promoted,
            "archived": archived,
        }

    def build_candidates(
        self,
        *,
        turn_number: int,
        patterns: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
        scenes: list[dict[str, Any]],
    ) -> list[CanonBitCandidate]:
        candidates: list[CanonBitCandidate] = []
        candidates.extend(self._build_pair_dynamic_candidates(turn_number, relationship_modes, patterns))
        candidates.extend(self._build_character_tendency_candidates(turn_number, patterns, episodes))
        candidates.extend(self._build_group_routine_candidates(turn_number, episodes, scenes))
        candidates.extend(self._build_place_motif_candidates(turn_number, episodes, scenes))
        return candidates

    def _build_pair_dynamic_candidates(
        self,
        turn_number: int,
        relationship_modes: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
    ) -> list[CanonBitCandidate]:
        grouped_patterns = self._patterns_by_pair(patterns)
        results: list[CanonBitCandidate] = []
        for mode in relationship_modes:
            if str(mode.get("status") or "") != "active":
                continue
            pair = sorted(
                [
                    str(mode.get("char_id_from") or "").strip(),
                    str(mode.get("char_id_to") or "").strip(),
                ]
            )
            if len(pair) != 2 or not pair[0] or not pair[1]:
                continue
            motif_key = str(mode.get("mode_type") or "").strip()
            if not motif_key:
                continue
            pair_patterns = grouped_patterns.get(tuple(pair), [])
            recurrence_count = 1 + max(
                (
                    int(pattern.get("recurrence_count") or 1)
                    for pattern in pair_patterns
                    if str(pattern.get("pattern_type") or "").strip() == motif_key
                    or motif_key in {"irritated_respect", "chaos_partner", "unsafe_confidant", "cannot_ignore"}
                ),
                default=0,
            )
            results.append(
                CanonBitCandidate(
                    bit_type="pair_dynamic",
                    motif_key=motif_key,
                    title=f"{pair[0]} と {pair[1]} の持ち味",
                    summary=str(mode.get("summary") or "").strip()
                    or f"{pair[0]} と {pair[1]} の {motif_key} が続いている。",
                    focus_char_ids=pair,
                    focus_place_id=None,
                    dedupe_key=f"pair:{':'.join(pair)}:{motif_key}",
                    evidence_sources=["relationship_mode"] + (["pattern"] if pair_patterns else []),
                    first_detected_turn=turn_number,
                    last_reinforced_turn=turn_number,
                    recurrence_count=max(1, recurrence_count),
                    confidence=max(
                        _CONFIDENCE_BY_SOURCE["relationship_mode"],
                        float(mode.get("confidence") or 0.0),
                    ),
                    novelty=0.65,
                    intent_alignment=self._intent_alignment(motif_key),
                    anchor_relationship_mode_id=self._as_int(mode.get("id")),
                    anchor_pattern_id=self._as_int(pair_patterns[0].get("id")) if pair_patterns else None,
                )
            )
        return results

    def _build_character_tendency_candidates(
        self,
        turn_number: int,
        patterns: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
    ) -> list[CanonBitCandidate]:
        char_motifs: dict[tuple[str, str], dict[str, Any]] = {}
        for pattern in patterns:
            motif_key = str(pattern.get("pattern_type") or "").strip()
            involved = [str(char_id).strip() for char_id in list(pattern.get("involved_chars") or []) if str(char_id).strip()]
            for char_id in involved[:3]:
                key = (char_id, motif_key)
                row = char_motifs.setdefault(
                    key,
                    {"count": 0, "pattern_id": None, "evidence": set(), "summary": "", "place_id": None},
                )
                row["count"] += int(pattern.get("recurrence_count") or 1)
                row["pattern_id"] = row["pattern_id"] or self._as_int(pattern.get("id"))
                row["evidence"].add("pattern")
                row["summary"] = row["summary"] or str(pattern.get("description") or "").strip()
        for episode in episodes:
            motif_key = str(episode.get("episode_type") or "").strip()
            focus_char_ids = [
                str(char_id).strip() for char_id in list(episode.get("focus_char_ids") or []) if str(char_id).strip()
            ]
            for char_id in focus_char_ids[:3]:
                key = (char_id, motif_key)
                row = char_motifs.setdefault(
                    key,
                    {"count": 0, "pattern_id": None, "episode_id": None, "evidence": set(), "summary": "", "place_id": None},
                )
                row["count"] += 1
                row["episode_id"] = row.get("episode_id") or self._as_int(episode.get("id"))
                row["evidence"].add("episode")
                row["summary"] = row["summary"] or str(episode.get("summary") or "").strip()
                row["place_id"] = row["place_id"] or str(episode.get("focus_place_id") or "").strip() or None
        results: list[CanonBitCandidate] = []
        for (char_id, motif_key), row in char_motifs.items():
            if int(row.get("count") or 0) < 2:
                continue
            evidence_sources = sorted(str(source) for source in row.get("evidence", set()))
            results.append(
                CanonBitCandidate(
                    bit_type="character_tendency",
                    motif_key=motif_key,
                    title=f"{char_id} の出やすい流れ",
                    summary=str(row.get("summary") or "").strip() or f"{char_id} は {motif_key} を起こしやすい。",
                    focus_char_ids=[char_id],
                    focus_place_id=row.get("place_id"),
                    dedupe_key=f"char:{char_id}:{motif_key}",
                    evidence_sources=evidence_sources,
                    first_detected_turn=turn_number,
                    last_reinforced_turn=turn_number,
                    recurrence_count=int(row.get("count") or 1),
                    confidence=max(_CONFIDENCE_BY_SOURCE.get(evidence_sources[0], 0.65), 0.70),
                    novelty=0.70,
                    intent_alignment=self._intent_alignment(motif_key),
                    anchor_pattern_id=row.get("pattern_id"),
                    anchor_episode_id=row.get("episode_id"),
                )
            )
        return results

    def _build_group_routine_candidates(
        self,
        turn_number: int,
        episodes: list[dict[str, Any]],
        scenes: list[dict[str, Any]],
    ) -> list[CanonBitCandidate]:
        grouped: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
        for episode in episodes:
            motif_key = str(episode.get("episode_type") or "").strip()
            chars = tuple(sorted(str(char_id).strip() for char_id in list(episode.get("focus_char_ids") or []) if str(char_id).strip())[:3])
            if len(chars) < 2:
                continue
            key = (chars, motif_key)
            row = grouped.setdefault(key, {"count": 0, "evidence": set(), "episode_id": None, "scene_id": None, "summary": "", "place_id": None})
            row["count"] += 1
            row["evidence"].add("episode")
            row["episode_id"] = row["episode_id"] or self._as_int(episode.get("id"))
            row["summary"] = row["summary"] or str(episode.get("summary") or "").strip()
            row["place_id"] = row["place_id"] or str(episode.get("focus_place_id") or "").strip() or None
        for scene in scenes:
            chars = tuple(sorted(str(char_id).strip() for char_id in list(scene.get("focus_char_ids") or []) if str(char_id).strip())[:3])
            if len(chars) < 2:
                continue
            motif_key = self._scene_motif_key(scene)
            key = (chars, motif_key)
            row = grouped.setdefault(key, {"count": 0, "evidence": set(), "episode_id": None, "scene_id": None, "summary": "", "place_id": None})
            row["count"] += 1
            row["evidence"].add("scene")
            row["scene_id"] = row["scene_id"] or self._as_int(scene.get("id"))
            row["summary"] = row["summary"] or str(scene.get("outcome_summary") or "").strip()
            row["place_id"] = row["place_id"] or str(scene.get("place_id") or "").strip() or None
        results: list[CanonBitCandidate] = []
        for (chars, motif_key), row in grouped.items():
            if int(row.get("count") or 0) < 2:
                continue
            evidence_sources = sorted(str(source) for source in row.get("evidence", set()))
            results.append(
                CanonBitCandidate(
                    bit_type="group_routine",
                    motif_key=motif_key,
                    title=f"{'・'.join(chars)} のお決まり",
                    summary=str(row.get("summary") or "").strip() or f"{'・'.join(chars)} は {motif_key} に戻りやすい。",
                    focus_char_ids=list(chars),
                    focus_place_id=row.get("place_id"),
                    dedupe_key=f"group:{':'.join(chars)}:{motif_key}",
                    evidence_sources=evidence_sources,
                    first_detected_turn=turn_number,
                    last_reinforced_turn=turn_number,
                    recurrence_count=int(row.get("count") or 1),
                    confidence=max(_CONFIDENCE_BY_SOURCE.get(evidence_sources[0], 0.65), 0.68),
                    novelty=0.60,
                    intent_alignment=self._intent_alignment(motif_key),
                    anchor_episode_id=row.get("episode_id"),
                    anchor_scene_id=row.get("scene_id"),
                )
            )
        return results

    def _build_place_motif_candidates(
        self,
        turn_number: int,
        episodes: list[dict[str, Any]],
        scenes: list[dict[str, Any]],
    ) -> list[CanonBitCandidate]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for episode in episodes:
            place_id = str(episode.get("focus_place_id") or "").strip()
            motif_key = str(episode.get("episode_type") or "").strip()
            if not place_id or not motif_key:
                continue
            key = (place_id, motif_key)
            row = grouped.setdefault(key, {"count": 0, "evidence": set(), "episode_id": None, "scene_id": None, "chars": []})
            row["count"] += 1
            row["evidence"].add("episode")
            row["episode_id"] = row["episode_id"] or self._as_int(episode.get("id"))
            row["chars"] = row["chars"] or list(episode.get("focus_char_ids") or [])[:3]
        for scene in scenes:
            place_id = str(scene.get("place_id") or "").strip()
            motif_key = self._scene_motif_key(scene)
            if not place_id or not motif_key:
                continue
            key = (place_id, motif_key)
            row = grouped.setdefault(key, {"count": 0, "evidence": set(), "episode_id": None, "scene_id": None, "chars": []})
            row["count"] += 1
            row["evidence"].add("scene")
            row["scene_id"] = row["scene_id"] or self._as_int(scene.get("id"))
            row["chars"] = row["chars"] or list(scene.get("focus_char_ids") or [])[:3]
        results: list[CanonBitCandidate] = []
        for (place_id, motif_key), row in grouped.items():
            if int(row.get("count") or 0) < 2:
                continue
            evidence_sources = sorted(str(source) for source in row.get("evidence", set()))
            focus_char_ids = [str(char_id).strip() for char_id in list(row.get("chars") or []) if str(char_id).strip()][:3]
            results.append(
                CanonBitCandidate(
                    bit_type="place_motif",
                    motif_key=motif_key,
                    title=f"{place_id} で起きやすい流れ",
                    summary=f"{place_id} では {motif_key} が繰り返されている。",
                    focus_char_ids=focus_char_ids,
                    focus_place_id=place_id,
                    dedupe_key=f"place:{place_id}:{motif_key}",
                    evidence_sources=evidence_sources,
                    first_detected_turn=turn_number,
                    last_reinforced_turn=turn_number,
                    recurrence_count=int(row.get("count") or 1),
                    confidence=max(_CONFIDENCE_BY_SOURCE.get(evidence_sources[0], 0.65), 0.66),
                    novelty=0.55,
                    intent_alignment=self._intent_alignment(motif_key),
                    anchor_episode_id=row.get("episode_id"),
                    anchor_scene_id=row.get("scene_id"),
                )
            )
        return results

    def _build_reinforcement_patch(
        self,
        existing: dict[str, Any],
        candidate: CanonBitCandidate,
        turn_number: int,
    ) -> dict[str, Any]:
        recurrence_count = max(
            int(existing.get("recurrence_count") or 1) + 1,
            candidate.recurrence_count,
        )
        evidence_sources = sorted(
            {
                *[str(source) for source in list(existing.get("evidence_sources") or [])],
                *candidate.evidence_sources,
            }
        )
        current_level = str(existing.get("canon_level") or "momentary_bit")
        promoted_level = self._promote_level(
            current_level,
            recurrence_count=recurrence_count,
            evidence_source_count=len(evidence_sources),
            first_detected_turn=int(existing.get("first_detected_turn") or turn_number),
            last_reinforced_turn=turn_number,
        )
        return {
            "title": candidate.title,
            "summary": candidate.summary,
            "focus_char_ids": candidate.focus_char_ids,
            "focus_place_id": candidate.focus_place_id,
            "evidence_sources": evidence_sources,
            "anchor_pattern_id": candidate.anchor_pattern_id or existing.get("anchor_pattern_id"),
            "anchor_episode_id": candidate.anchor_episode_id or existing.get("anchor_episode_id"),
            "anchor_relationship_mode_id": candidate.anchor_relationship_mode_id or existing.get("anchor_relationship_mode_id"),
            "anchor_scene_id": candidate.anchor_scene_id or existing.get("anchor_scene_id"),
            "last_reinforced_turn": turn_number,
            "recurrence_count": recurrence_count,
            "confidence": max(float(existing.get("confidence") or 0.0), candidate.confidence),
            "novelty": float(existing.get("novelty") or candidate.novelty),
            "intent_alignment": max(float(existing.get("intent_alignment") or 0.0), candidate.intent_alignment),
            "canon_level": promoted_level,
        }

    def _promote_level(
        self,
        current_level: str,
        *,
        recurrence_count: int,
        evidence_source_count: int,
        first_detected_turn: int,
        last_reinforced_turn: int,
    ) -> str:
        level = current_level
        if recurrence_count >= 2:
            level = max((level, "recurring_bit"), key=lambda item: _PROMOTION_ORDER.get(item, 0))
        if recurrence_count >= 3 and evidence_source_count >= 2:
            level = max((level, "proto_canon"), key=lambda item: _PROMOTION_ORDER.get(item, 0))
        if (
            recurrence_count >= 4
            and evidence_source_count >= 2
            and (last_reinforced_turn - first_detected_turn) >= 8
        ):
            level = "canon"
        return level

    def _should_archive(self, bit: dict[str, Any], *, turn_number: int) -> bool:
        if str(bit.get("status") or "") != "active":
            return False
        level = str(bit.get("canon_level") or "momentary_bit")
        ttl = _STALE_TTL_BY_LEVEL.get(level, 6)
        last_reinforced_turn = int(bit.get("last_reinforced_turn") or 0)
        return (turn_number - last_reinforced_turn) >= ttl

    def _intent_alignment(self, motif_key: str) -> float:
        profile = self._intent_profile
        if profile is None:
            return 0.55
        if motif_key in set(profile.preferred_pattern_types):
            return 0.80
        if motif_key in set(profile.deprioritized_pattern_types):
            return 0.30
        return 0.55

    @staticmethod
    def _patterns_by_pair(patterns: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for pattern in patterns:
            involved = sorted(
                str(char_id).strip()
                for char_id in list(pattern.get("involved_chars") or [])
                if str(char_id).strip()
            )
            if len(involved) != 2:
                continue
            grouped.setdefault((involved[0], involved[1]), []).append(pattern)
        return grouped

    @staticmethod
    def _scene_motif_key(scene: dict[str, Any]) -> str:
        outcome = str(scene.get("outcome_type") or "").strip()
        summary = str(scene.get("outcome_summary") or "").strip()
        text = f"{outcome} {summary}"
        if any(cue in text for cue in ("秘密", "隠", "言えない", "別の声")):
            return "near_reveal"
        if any(cue in text for cue in ("勝", "負", "上回", "負け", "先手")):
            return "small_win_loss"
        if any(cue in text for cue in ("食い違", "誤解", "勘違", "すれ違")):
            return "misunderstanding"
        return "status_clash"

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
