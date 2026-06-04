"""Deterministic persisted relationship-mode extraction and ranking helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager


RELATIONSHIP_MODE_TO_TENSION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "irritated_respect": ("conflict", "rivalry"),
    "unsafe_confidant": ("secret", "mystery"),
    "chaos_partner": ("rivalry", "conflict"),
    "cannot_ignore": ("mystery", "conflict", "secret"),
}

RELATIONSHIP_MODE_TO_INTERVENTION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "irritated_respect": ("relationship_catalyst", "opportunity"),
    "unsafe_confidant": ("revelation",),
    "chaos_partner": ("opportunity", "relationship_catalyst"),
    "cannot_ignore": ("relationship_catalyst", "revelation", "opportunity"),
}

_MODE_PRIORITY = {
    "irritated_respect": 4,
    "unsafe_confidant": 3,
    "chaos_partner": 2,
    "cannot_ignore": 1,
}
_MODE_CLUSTER = {
    "irritated_respect": "conflict",
    "chaos_partner": "conflict",
    "unsafe_confidant": "attention",
    "cannot_ignore": "attention",
}
_RELATIONSHIP_SUPPORT_PATTERN_TYPES = {
    "status_clash",
    "small_win_loss",
    "near_reveal",
    "misunderstanding",
}
_MODE_DECAY = {
    "irritated_respect": {"ttl": 3, "intensity": 0.06, "confidence": 0.04},
    "chaos_partner": {"ttl": 3, "intensity": 0.06, "confidence": 0.04},
    "unsafe_confidant": {"ttl": 4, "intensity": 0.05, "confidence": 0.03},
    "cannot_ignore": {"ttl": 4, "intensity": 0.04, "confidence": 0.03},
}


@dataclass(frozen=True)
class RelationshipModeCandidate:
    """Persistable relationship-mode candidate."""

    mode_type: str
    char_id_from: str
    char_id_to: str
    summary: str
    confidence: float
    intensity: float
    first_detected_turn: int
    last_reinforced_turn: int
    source_pattern_id: int | None = None
    source_episode_id: int | None = None
    last_trigger_event_id: int | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "mode_type": self.mode_type,
            "status": "active",
            "summary": self.summary,
            "confidence": self.confidence,
            "intensity": self.intensity,
            "source_pattern_id": self.source_pattern_id,
            "source_episode_id": self.source_episode_id,
            "first_detected_turn": self.first_detected_turn,
            "last_reinforced_turn": self.last_reinforced_turn,
            "last_trigger_event_id": self.last_trigger_event_id,
        }


class RelationshipModeEngine:
    """Build deterministic relationship modes from persisted runtime artifacts."""

    def __init__(
        self,
        db: DatabaseManager | None = None,
        story_id: str = "",
        *,
        inactive_after_rounds: int = 3,
    ) -> None:
        self._db = db
        self._story_id = story_id
        self._inactive_after_rounds = max(1, inactive_after_rounds)

    async def process_round(self, turn_number: int) -> None:
        """Extract / upsert / deactivate relationship modes for the round."""
        if self._db is None or not self._story_id:
            return

        relationships = await self._db.get_relationship_snapshots(self._story_id)
        patterns = await self._db.get_active_interaction_patterns(self._story_id)
        episodes = await self._db.get_recent_story_episodes(self._story_id, limit=6)
        relationship_events = await self._db.get_recent_relationship_events(
            self._story_id,
            since_turn=max(0, turn_number - self._inactive_after_rounds * 2),
            limit=30,
        )
        active_modes = await self._db.get_active_relationship_modes(self._story_id)
        active_mode_map = {
            (
                str(mode.get("char_id_from") or ""),
                str(mode.get("char_id_to") or ""),
                str(mode.get("mode_type") or ""),
            ): mode
            for mode in active_modes
        }
        candidates = self.build_candidates(
            turn_number=turn_number,
            relationships=relationships,
            patterns=patterns,
            episodes=episodes,
            relationship_events=relationship_events,
        )
        grouped_patterns = self._patterns_by_pair(patterns)
        grouped_events = self._events_by_pair(relationship_events)
        grouped_episodes = self._episodes_by_pair(episodes)
        active_pair_clusters: dict[tuple[str, str], set[str]] = {}
        for mode in active_modes:
            pair = (
                str(mode.get("char_id_from") or ""),
                str(mode.get("char_id_to") or ""),
            )
            cluster = self._cluster_for_mode(str(mode.get("mode_type") or ""))
            if pair[0] and pair[1] and cluster is not None:
                active_pair_clusters.setdefault(pair, set()).add(cluster)

        reinforced_mode_keys: set[tuple[str, str, str]] = set()
        selected_clusters: dict[tuple[str, str, str], str] = {}
        for candidate in candidates:
            mode_key = (candidate.char_id_from, candidate.char_id_to, candidate.mode_type)
            reinforced_mode_keys.add(mode_key)
            cluster = self._cluster_for_mode(candidate.mode_type)
            if cluster is not None:
                selected_clusters[(candidate.char_id_from, candidate.char_id_to, cluster)] = candidate.mode_type
            existing = await self._db.find_active_relationship_mode(
                self._story_id,
                candidate.char_id_from,
                candidate.char_id_to,
                candidate.mode_type,
            )
            if existing is None:
                existing = active_mode_map.get(mode_key)
            if existing is None:
                await self._db.insert_relationship_mode(
                    self._story_id,
                    candidate.char_id_from,
                    candidate.char_id_to,
                    candidate.to_record(),
                )
                continue
            await self._db.update_relationship_mode(
                int(existing["id"]),
                {
                    "summary": candidate.summary,
                    "confidence": max(float(existing.get("confidence") or 0.0), candidate.confidence),
                    "intensity": max(float(existing.get("intensity") or 0.0), candidate.intensity),
                    "source_pattern_id": candidate.source_pattern_id,
                    "source_episode_id": candidate.source_episode_id,
                    "last_reinforced_turn": turn_number,
                    "last_trigger_event_id": candidate.last_trigger_event_id,
                },
            )

        relationship_map = {
            (str(row.get("char_id_from") or ""), str(row.get("char_id_to") or "")): row
            for row in relationships
        }
        for mode in active_modes:
            pair = (
                str(mode.get("char_id_from") or ""),
                str(mode.get("char_id_to") or ""),
            )
            mode_key = (pair[0], pair[1], str(mode.get("mode_type") or ""))
            if mode_key in reinforced_mode_keys:
                continue
            cluster = self._cluster_for_mode(str(mode.get("mode_type") or ""))
            selected_mode = selected_clusters.get((pair[0], pair[1], cluster or ""))
            if selected_mode is not None and selected_mode != str(mode.get("mode_type") or ""):
                await self._db.deactivate_relationship_mode(
                    int(mode["id"]),
                    summary=f"superseded by {selected_mode}",
                )
                continue
            if self._should_refresh_complementary_mode(
                mode,
                pair=pair,
                turn_number=turn_number,
                relationship=relationship_map.get(pair),
                selected_clusters=selected_clusters,
                active_pair_clusters=active_pair_clusters,
                grouped_patterns=grouped_patterns,
                grouped_episodes=grouped_episodes,
                grouped_events=grouped_events,
            ):
                await self._db.update_relationship_mode(
                    int(mode["id"]),
                    self._build_complementary_refresh_patch(mode, turn_number=turn_number),
                )
                continue
            if self._should_deactivate(mode, turn_number=turn_number, relationship_map=relationship_map):
                if self._should_preserve_complementary_mode(
                    mode,
                    pair=pair,
                    relationship=relationship_map.get(pair),
                    turn_number=turn_number,
                    selected_clusters=selected_clusters,
                    active_pair_clusters=active_pair_clusters,
                    patterns=grouped_patterns.get(pair, []),
                    episodes=grouped_episodes.get(pair, []),
                    relationship_events=grouped_events.get(pair, []),
                ):
                    decay_patch = self._build_decay_patch(mode, turn_number=turn_number)
                    if decay_patch is not None:
                        await self._db.update_relationship_mode(int(mode["id"]), decay_patch)
                    continue
                await self._db.deactivate_relationship_mode(
                    int(mode["id"]),
                    summary="stale relationship mode",
                )
                continue
            decay_patch = self._build_decay_patch(mode, turn_number=turn_number)
            if decay_patch is not None:
                await self._db.update_relationship_mode(int(mode["id"]), decay_patch)

    def build_candidates(
        self,
        *,
        turn_number: int,
        relationships: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
        relationship_events: list[dict[str, Any]],
    ) -> list[RelationshipModeCandidate]:
        grouped_patterns = self._patterns_by_pair(patterns)
        grouped_events = self._events_by_pair(relationship_events)
        grouped_episodes = self._episodes_by_pair(episodes)

        winners: dict[tuple[str, str, str], RelationshipModeCandidate] = {}
        for relationship in relationships:
            char_id_from = str(relationship.get("char_id_from") or "").strip()
            char_id_to = str(relationship.get("char_id_to") or "").strip()
            if not char_id_from or not char_id_to:
                continue
            pair = (char_id_from, char_id_to)
            pair_candidates = self._build_candidates_for_pair(
                turn_number,
                relationship=relationship,
                patterns=grouped_patterns.get(pair, []),
                episodes=grouped_episodes.get(pair, []),
                relationship_events=grouped_events.get(pair, []),
            )
            for candidate in pair_candidates:
                cluster = self._cluster_for_mode(candidate.mode_type)
                if cluster is None:
                    continue
                key = (candidate.char_id_from, candidate.char_id_to, cluster)
                current = winners.get(key)
                if current is None or self._candidate_rank(candidate) > self._candidate_rank(current):
                    winners[key] = candidate
        return list(winners.values())

    def _build_candidates_for_pair(
        self,
        turn_number: int,
        *,
        relationship: dict[str, Any],
        patterns: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
        relationship_events: list[dict[str, Any]],
    ) -> list[RelationshipModeCandidate]:
        char_id_from = str(relationship.get("char_id_from") or "").strip()
        char_id_to = str(relationship.get("char_id_to") or "").strip()
        trust = float(relationship.get("trust") or 0.0)
        affinity = float(relationship.get("affinity") or 0.0)
        tension = float(relationship.get("tension") or 0.0)
        pattern_types = {str(pattern.get("pattern_type") or "") for pattern in patterns}
        top_pattern = patterns[0] if patterns else None
        top_event = relationship_events[0] if relationship_events else None
        active_episode = next((episode for episode in episodes if episode.get("status") == "active"), None)
        candidates: list[RelationshipModeCandidate] = []

        if (
            trust >= 0.40
            and tension >= 0.25
            and ("status_clash" in pattern_types or self._pattern_recurrence(patterns, "small_win_loss") >= 2)
        ):
            candidates.append(RelationshipModeCandidate(
                mode_type="irritated_respect",
                char_id_from=char_id_from,
                char_id_to=char_id_to,
                summary=f"{char_id_from} は {char_id_to} を認めつつ、ぶつかりやすい。",
                confidence=max(0.65, float((top_pattern or {}).get("confidence") or 0.0)),
                intensity=max(tension, float((top_pattern or {}).get("intensity") or 0.0), 0.45),
                source_pattern_id=int(top_pattern["id"]) if top_pattern and top_pattern.get("id") is not None else None,
                source_episode_id=int(active_episode["id"]) if active_episode and active_episode.get("id") is not None else None,
                first_detected_turn=turn_number,
                last_reinforced_turn=turn_number,
                last_trigger_event_id=int(top_event["id"]) if top_event and top_event.get("id") is not None else None,
            ))

        if trust >= 0.55 and {"near_reveal", "misunderstanding"} & pattern_types:
            candidates.append(RelationshipModeCandidate(
                mode_type="unsafe_confidant",
                char_id_from=char_id_from,
                char_id_to=char_id_to,
                summary=f"{char_id_from} は {char_id_to} に秘密をこぼしかねない。",
                confidence=max(0.68, float((top_pattern or {}).get("confidence") or 0.0)),
                intensity=max(trust, float((top_pattern or {}).get("intensity") or 0.0), 0.50),
                source_pattern_id=int(top_pattern["id"]) if top_pattern and top_pattern.get("id") is not None else None,
                source_episode_id=int(active_episode["id"]) if active_episode and active_episode.get("id") is not None else None,
                first_detected_turn=turn_number,
                last_reinforced_turn=turn_number,
                last_trigger_event_id=int(top_event["id"]) if top_event and top_event.get("id") is not None else None,
            ))

        if (
            self._pattern_recurrence(patterns, "status_clash") >= 2
            or self._pattern_recurrence(patterns, "small_win_loss") >= 2
        ) and len(relationship_events) >= 2 and (affinity >= 0.35 or tension >= 0.20):
            candidates.append(RelationshipModeCandidate(
                mode_type="chaos_partner",
                char_id_from=char_id_from,
                char_id_to=char_id_to,
                summary=f"{char_id_from} と {char_id_to} が絡むと騒ぎになりやすい。",
                confidence=max(0.62, float((top_pattern or {}).get("confidence") or 0.0)),
                intensity=max(
                    float((top_pattern or {}).get("intensity") or 0.0),
                    tension,
                    affinity,
                    0.45,
                ),
                source_pattern_id=int(top_pattern["id"]) if top_pattern and top_pattern.get("id") is not None else None,
                source_episode_id=int(active_episode["id"]) if active_episode and active_episode.get("id") is not None else None,
                first_detected_turn=turn_number,
                last_reinforced_turn=turn_number,
                last_trigger_event_id=int(top_event["id"]) if top_event and top_event.get("id") is not None else None,
            ))

        if active_episode is not None and relationship_events and (affinity >= 0.25 or tension >= 0.25):
            candidates.append(RelationshipModeCandidate(
                mode_type="cannot_ignore",
                char_id_from=char_id_from,
                char_id_to=char_id_to,
                summary=f"{char_id_from} は {char_id_to} を放っておけない。",
                confidence=0.58,
                intensity=max(affinity, tension, 0.40),
                source_pattern_id=int(top_pattern["id"]) if top_pattern and top_pattern.get("id") is not None else None,
                source_episode_id=int(active_episode["id"]) if active_episode and active_episode.get("id") is not None else None,
                first_detected_turn=turn_number,
                last_reinforced_turn=turn_number,
                last_trigger_event_id=int(top_event["id"]) if top_event and top_event.get("id") is not None else None,
            ))

        return candidates

    def _patterns_by_pair(self, patterns: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for pattern in patterns:
            chars = [
                str(char_id).strip()
                for char_id in list(pattern.get("involved_chars") or [])
                if str(char_id).strip()
            ]
            if len(chars) < 2:
                continue
            for char_id_from in chars:
                for char_id_to in chars:
                    if char_id_from == char_id_to:
                        continue
                    grouped.setdefault((char_id_from, char_id_to), []).append(pattern)
        for rows in grouped.values():
            rows.sort(
                key=lambda row: (
                    -int(row.get("recurrence_count") or 1),
                    -float(row.get("intensity") or 0.0),
                    -float(row.get("confidence") or 0.0),
                    -int(row.get("id") or 0),
                )
            )
        return grouped

    def _events_by_pair(
        self,
        relationship_events: list[dict[str, Any]],
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for event in relationship_events:
            pair = (
                str(event.get("char_id_from") or "").strip(),
                str(event.get("char_id_to") or "").strip(),
            )
            if not pair[0] or not pair[1]:
                continue
            grouped.setdefault(pair, []).append(event)
        for rows in grouped.values():
            rows.sort(key=lambda row: (-int(row.get("turn_number") or 0), -int(row.get("id") or 0)))
        return grouped

    def _episodes_by_pair(
        self,
        episodes: list[dict[str, Any]],
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for episode in episodes:
            focus_chars = [
                str(char_id).strip()
                for char_id in list(episode.get("focus_char_ids") or [])
                if str(char_id).strip()
            ]
            if len(focus_chars) < 2:
                continue
            for char_id_from in focus_chars:
                for char_id_to in focus_chars:
                    if char_id_from == char_id_to:
                        continue
                    grouped.setdefault((char_id_from, char_id_to), []).append(episode)
        for rows in grouped.values():
            rows.sort(
                key=lambda row: (
                    0 if str(row.get("status") or "") == "active" else 1,
                    -int(row.get("last_progress_turn") or 0),
                    -int(row.get("id") or 0),
                )
            )
        return grouped

    def _pattern_recurrence(self, patterns: list[dict[str, Any]], pattern_type: str) -> int:
        return max(
            (
                int(pattern.get("recurrence_count") or 1)
                for pattern in patterns
                if str(pattern.get("pattern_type") or "") == pattern_type
            ),
            default=0,
        )

    def _candidate_rank(self, candidate: RelationshipModeCandidate) -> tuple[float, float, int, str]:
        return (
            _MODE_PRIORITY.get(candidate.mode_type, 0),
            candidate.intensity,
            candidate.confidence,
            candidate.mode_type,
        )

    def _cluster_for_mode(self, mode_type: str) -> str | None:
        return _MODE_CLUSTER.get(mode_type)

    def _pair_has_active_support(
        self,
        *,
        patterns: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
        relationship_events: list[dict[str, Any]],
    ) -> bool:
        has_pattern_support = any(
            str(pattern.get("pattern_type") or "") in _RELATIONSHIP_SUPPORT_PATTERN_TYPES
            or int(pattern.get("recurrence_count") or 1) >= 2
            or float(pattern.get("intensity") or 0.0) >= 0.45
            or float(pattern.get("confidence") or 0.0) >= 0.65
            for pattern in patterns
        )
        has_episode_support = any(
            str(episode.get("status") or "") == "active"
            or int(episode.get("last_progress_turn") or 0) > 0
            for episode in episodes
        )
        has_event_support = len(relationship_events) > 0
        return (has_pattern_support and has_episode_support) or (
            has_event_support and (has_pattern_support or has_episode_support)
        )

    def _should_refresh_complementary_mode(
        self,
        mode: dict[str, Any],
        *,
        pair: tuple[str, str],
        turn_number: int,
        relationship: dict[str, Any] | None,
        selected_clusters: dict[tuple[str, str, str], str],
        active_pair_clusters: dict[tuple[str, str], set[str]],
        grouped_patterns: dict[tuple[str, str], list[dict[str, Any]]],
        grouped_episodes: dict[tuple[str, str], list[dict[str, Any]]],
        grouped_events: dict[tuple[str, str], list[dict[str, Any]]],
    ) -> bool:
        del turn_number
        mode_cluster = self._cluster_for_mode(str(mode.get("mode_type") or ""))
        if mode_cluster is None:
            return False
        complementary_selected = any(
            selected_clusters.get((pair[0], pair[1], cluster_name)) is not None
            for cluster_name in set(_MODE_CLUSTER.values())
            if cluster_name != mode_cluster
        )
        if not complementary_selected:
            return False
        pair_clusters = set(active_pair_clusters.get(pair, set()))
        pair_clusters.add(mode_cluster)
        if not self._pair_has_complementary_cluster(
            pair=pair,
            mode_cluster=mode_cluster,
            selected_clusters=selected_clusters,
            active_pair_clusters=active_pair_clusters,
        ):
            return False
        return self._cluster_support_active(
            cluster_name=mode_cluster,
            relationship=relationship,
            patterns=grouped_patterns.get(pair, []),
            episodes=grouped_episodes.get(pair, []),
            relationship_events=grouped_events.get(pair, []),
        ) and self._pair_heat_score(
            relationship=relationship,
            patterns=grouped_patterns.get(pair, []),
            episodes=grouped_episodes.get(pair, []),
            relationship_events=grouped_events.get(pair, []),
        ) >= 1.15

    def _pair_has_complementary_cluster(
        self,
        *,
        pair: tuple[str, str],
        mode_cluster: str,
        selected_clusters: dict[tuple[str, str, str], str],
        active_pair_clusters: dict[tuple[str, str], set[str]],
    ) -> bool:
        clusters = set(active_pair_clusters.get(pair, set()))
        clusters.update(
            cluster_name
            for cluster_name in set(_MODE_CLUSTER.values())
            if selected_clusters.get((pair[0], pair[1], cluster_name)) is not None
        )
        return any(cluster_name != mode_cluster for cluster_name in clusters)

    def _cluster_support_active(
        self,
        *,
        cluster_name: str,
        relationship: dict[str, Any] | None,
        patterns: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
        relationship_events: list[dict[str, Any]],
    ) -> bool:
        trust = float((relationship or {}).get("trust") or 0.0)
        affinity = float((relationship or {}).get("affinity") or 0.0)
        tension = float((relationship or {}).get("tension") or 0.0)
        if cluster_name == "conflict":
            return any(
                str(pattern.get("pattern_type") or "") in {"status_clash", "small_win_loss"}
                or int(pattern.get("recurrence_count") or 1) >= 2
                for pattern in patterns
            ) or tension >= 0.28 or len(relationship_events) >= 2
        if cluster_name == "attention":
            return any(
                str(pattern.get("pattern_type") or "") in {"near_reveal", "misunderstanding"}
                for pattern in patterns
            ) or (
                any(str(episode.get("status") or "") == "active" for episode in episodes)
                and len(relationship_events) >= 1
            ) or (
                trust >= 0.60 and len(relationship_events) >= 2
            ) or (
                affinity >= 0.45 and len(relationship_events) >= 1
            )
        return self._pair_has_active_support(
            patterns=patterns,
            episodes=episodes,
            relationship_events=relationship_events,
        )

    def _build_complementary_refresh_patch(
        self,
        mode: dict[str, Any],
        *,
        turn_number: int,
    ) -> dict[str, Any]:
        return {
            "last_reinforced_turn": turn_number,
            "intensity": min(0.85, float(mode.get("intensity") or 0.0) + 0.03),
            "confidence": min(0.85, float(mode.get("confidence") or 0.0) + 0.02),
        }

    def _pair_heat_score(
        self,
        *,
        relationship: dict[str, Any] | None,
        patterns: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
        relationship_events: list[dict[str, Any]],
    ) -> float:
        trust = float((relationship or {}).get("trust") or 0.0)
        affinity = float((relationship or {}).get("affinity") or 0.0)
        tension = float((relationship or {}).get("tension") or 0.0)
        pattern_bonus = 0.35 if patterns else 0.0
        recurrence_bonus = 0.20 if any(int(pattern.get("recurrence_count") or 1) >= 2 for pattern in patterns) else 0.0
        episode_bonus = 0.20 if any(str(episode.get("status") or "") == "active" for episode in episodes) else 0.0
        event_bonus = min(len(relationship_events), 2) * 0.12
        return trust + affinity + tension + pattern_bonus + recurrence_bonus + episode_bonus + event_bonus

    def _should_preserve_complementary_mode(
        self,
        mode: dict[str, Any],
        *,
        pair: tuple[str, str],
        relationship: dict[str, Any] | None,
        turn_number: int,
        selected_clusters: dict[tuple[str, str, str], str],
        active_pair_clusters: dict[tuple[str, str], set[str]],
        patterns: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
        relationship_events: list[dict[str, Any]],
    ) -> bool:
        mode_cluster = self._cluster_for_mode(str(mode.get("mode_type") or ""))
        if mode_cluster is None:
            return False
        if not self._pair_has_complementary_cluster(
            pair=pair,
            mode_cluster=mode_cluster,
            selected_clusters=selected_clusters,
            active_pair_clusters=active_pair_clusters,
        ):
            return False
        if not self._cluster_support_active(
            cluster_name=mode_cluster,
            relationship=relationship,
            patterns=patterns,
            episodes=episodes,
            relationship_events=relationship_events,
        ):
            return False
        if self._pair_heat_score(
            relationship=relationship,
            patterns=patterns,
            episodes=episodes,
            relationship_events=relationship_events,
        ) < 1.15:
            return False
        mode_type = str(mode.get("mode_type") or "")
        decay = _MODE_DECAY.get(mode_type, {})
        ttl = int(decay.get("ttl") or self._inactive_after_rounds)
        gap = turn_number - int(mode.get("last_reinforced_turn") or 0)
        relationship_floor_failed = self._relationship_floor_failed(mode_type, relationship)
        if gap < ttl and not relationship_floor_failed:
            return True
        intensity = float(mode.get("intensity") or 0.0)
        confidence = float(mode.get("confidence") or 0.0)
        if intensity < 0.20 and confidence < 0.25 and relationship_floor_failed:
            return False
        return True

    def _build_decay_patch(self, mode: dict[str, Any], *, turn_number: int) -> dict[str, Any] | None:
        mode_type = str(mode.get("mode_type") or "")
        decay = _MODE_DECAY.get(mode_type)
        if decay is None:
            return None
        gap = max(1, turn_number - int(mode.get("last_reinforced_turn") or turn_number))
        current_intensity = float(mode.get("intensity") or 0.0)
        current_confidence = float(mode.get("confidence") or 0.0)
        new_intensity = max(0.0, current_intensity - (float(decay["intensity"]) * gap))
        new_confidence = max(0.0, current_confidence - (float(decay["confidence"]) * gap))
        if new_intensity == current_intensity and new_confidence == current_confidence:
            return None
        return {
            "intensity": new_intensity,
            "confidence": new_confidence,
        }

    def _should_deactivate(
        self,
        mode: dict[str, Any],
        *,
        turn_number: int,
        relationship_map: dict[tuple[str, str], dict[str, Any]],
    ) -> bool:
        pair = (
            str(mode.get("char_id_from") or ""),
            str(mode.get("char_id_to") or ""),
        )
        relationship = relationship_map.get(pair)
        if relationship is None:
            return True
        mode_type = str(mode.get("mode_type") or "")
        decay = _MODE_DECAY.get(mode_type, {})
        ttl = int(decay.get("ttl") or self._inactive_after_rounds)
        if turn_number - int(mode.get("last_reinforced_turn") or 0) >= ttl:
            return True
        if float(mode.get("intensity") or 0.0) < 0.20:
            return True
        if float(mode.get("confidence") or 0.0) < 0.25:
            return True
        return self._relationship_floor_failed(mode_type, relationship)

    def _relationship_floor_failed(
        self,
        mode_type: str,
        relationship: dict[str, Any] | None,
    ) -> bool:
        if relationship is None:
            return True
        trust = float(relationship.get("trust") or 0.0)
        affinity = float(relationship.get("affinity") or 0.0)
        tension = float(relationship.get("tension") or 0.0)
        if mode_type == "irritated_respect":
            return trust < 0.35 or tension < 0.20
        if mode_type == "unsafe_confidant":
            return trust < 0.50
        if mode_type == "chaos_partner":
            return affinity < 0.30 and tension < 0.18
        if mode_type == "cannot_ignore":
            return affinity < 0.20 and tension < 0.20
        return False
