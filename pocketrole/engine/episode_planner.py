"""Persisted episode planner built on top of patterns/hooks/tensions."""

from __future__ import annotations

import re
from typing import Any

from db.db_manager import DatabaseManager

_EPISODE_GOAL_BY_TYPE = {
    "misunderstanding": "誰かの認識のズレが明らかになる",
    "status_clash": "二人の立場や思いが正面からぶつかる",
    "near_reveal": "誰かが隠してきたことの端が見え始める",
    "small_win_loss": "小さな出来事が次の動きに続いていく",
    "conflict": "二人以上の思いが真正面からぶつかる",
    "mystery": "誰かの違和感や疑問が少しだけ形になる",
}

_EPISODE_STAKES_BY_TYPE = {
    "misunderstanding": "ズレが残ったまま進むと、後で大きな誤解につながりやすい。",
    "status_clash": "思いをぶつけ合わないと、二人の間に距離だけが残る。",
    "near_reveal": "隠したままにすると、誰かに先を越されるかもしれない。",
    "small_win_loss": "小さな出来事を見逃すと、重要な変化を見落とすことになる。",
    "conflict": "思いをぶつけないままにすると、二人の溝は深まるばかりだ。",
    "mystery": "違和感を放置すると、真実はさらに遠のいていく。",
}

_PRESSURE_GOAL_OVERRIDES = {
    "showoff_flashpoint": "ここで誰かが本気を見せる瞬間になる",
    "role_reversal_ready": "場の流れが予想外の方向へ向かう",
    "payoff_ready": "これまでの積み重ねが今ここで形になる",
    "stall_risk": "誰かが場を動かすために何か言うか行動する",
}

_PRESSURE_STAKES_OVERRIDES = {
    "showoff_flashpoint": "ここで何もしなければ、この機会は二度と来ないかもしれない。",
    "role_reversal_ready": "このまま何も変わらなければ、状況は固まってしまう。",
    "payoff_ready": "今動かなければ、せっかくの機会が消えていく。",
    "stall_risk": "このまま沈黙が続くと、本当に言うべきことが言えなくなる。",
}

_THEME_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "文化祭": ("status_clash", "small_win_loss", "showoff", "competition", "conflict"),
    "恋愛": ("romantic", "near_reveal", "misunderstanding"),
    "秘密": ("mystery", "near_reveal", "secret", "question"),
    "対立": ("conflict", "status_clash", "clash"),
    "勝負": ("status_clash", "small_win_loss", "competition"),
    "謎": ("mystery", "question", "near_reveal"),
}


class EpisodePlanner:
    """Manage one persisted active episode per story."""

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        *,
        min_turns_per_episode: int = 4,
        max_turns_per_episode: int = 12,
        carry_hook_limit: int = 3,
        stale_rounds_before_close: int = 2,
        episode_tempo: str = "moderate",
        dramatic_pressure_evaluator: object | None = None,
    ) -> None:
        self._story_id = story_id
        self._db = db
        self._carry_hook_limit = max(1, carry_hook_limit)
        self._stale_rounds_before_close = max(1, stale_rounds_before_close)
        self._last_active_episode: dict[str, Any] | None = None
        self._dramatic_pressure_evaluator = dramatic_pressure_evaluator

        if episode_tempo == "fast":
            self._min_turns = max(2, min_turns_per_episode)
            self._max_turns = min(max_turns_per_episode, 10)
        elif episode_tempo == "slow":
            self._min_turns = max(6, min_turns_per_episode)
            self._max_turns = max(max_turns_per_episode, 14)
        else:
            self._min_turns = min_turns_per_episode
            self._max_turns = max_turns_per_episode

    async def get_active_episode(self) -> dict[str, Any] | None:
        episode = await self._db.get_active_story_episode(self._story_id)
        self._last_active_episode = episode if isinstance(episode, dict) else None
        return self._last_active_episode

    async def process_round(
        self,
        turn_number: int,
        *,
        active_chapter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_patterns = await self._db.get_active_interaction_patterns(self._story_id)
        patterns = raw_patterns if isinstance(raw_patterns, list) else []
        raw_hooks = await self._db.get_open_story_hooks(self._story_id)
        hooks = raw_hooks if isinstance(raw_hooks, list) else []
        raw_tensions = await self._db.get_active_tensions(self._story_id)
        tensions = raw_tensions if isinstance(raw_tensions, list) else []
        raw_recent_events = await self._db.get_recent_relationship_events(
            self._story_id,
            since_turn=max(0, turn_number - self._stale_rounds_before_close),
            limit=10,
        )
        recent_events = raw_recent_events if isinstance(raw_recent_events, list) else []
        raw_recent_closed_scenes = await self._db.get_recent_closed_story_scenes(
            self._story_id,
            since_turn=max(0, turn_number - self._stale_rounds_before_close),
            limit=5,
            scene_type="conversation",
        )
        recent_closed_scenes = (
            raw_recent_closed_scenes if isinstance(raw_recent_closed_scenes, list) else []
        )
        raw_relationship_modes = await self._db.get_active_relationship_modes(self._story_id)
        relationship_modes = (
            raw_relationship_modes if isinstance(raw_relationship_modes, list) else []
        )
        raw_canon_bits = await self._db.get_active_story_canon_bits(self._story_id)
        canon_bits = raw_canon_bits if isinstance(raw_canon_bits, list) else []
        raw_dramatic_pressures = await self._db.get_active_story_dramatic_pressures(self._story_id)
        dramatic_pressures = raw_dramatic_pressures if isinstance(raw_dramatic_pressures, list) else []
        raw_active_episode = await self._db.get_active_story_episode(self._story_id)
        active_episode = raw_active_episode if isinstance(raw_active_episode, dict) else None
        closed_episode: dict[str, Any] | None = None

        if active_episode is not None:
            close_payload = self._close_payload_if_needed(
                active_episode,
                turn_number=turn_number,
                patterns=patterns,
                hooks=hooks,
            )
            if close_payload is not None:
                await self._db.close_story_episode(
                    int(active_episode["id"]),
                    closed_turn=turn_number,
                    exit_condition=close_payload["exit_condition"],
                    summary=close_payload["summary"],
                )
                closed_episode = {
                    **active_episode,
                    "status": "closed",
                    "closed_turn": turn_number,
                    **close_payload,
                }
                active_episode = None
            else:
                progress_turn = self._progress_turn(
                    active_episode,
                    turn_number=turn_number,
                    recent_events=recent_events,
                    recent_closed_scenes=recent_closed_scenes,
                    patterns=patterns,
                    hooks=hooks,
                )
                if progress_turn is not None:
                    await self._db.update_story_episode(
                        int(active_episode["id"]),
                        {"last_progress_turn": progress_turn},
                    )
                    active_episode["last_progress_turn"] = progress_turn

        if active_episode is None:
            opening = self._build_opening_episode(
                turn_number,
                active_chapter=active_chapter,
                patterns=patterns,
                hooks=hooks,
                tensions=tensions,
                relationship_modes=relationship_modes,
                canon_bits=canon_bits,
                dramatic_pressures=dramatic_pressures,
            )
            if opening is not None:
                episode_id = await self._db.insert_story_episode(self._story_id, opening)
                active_episode = {"id": episode_id, **opening}

        self._last_active_episode = active_episode
        return {
            "active_episode": active_episode,
            "closed_episode": closed_episode,
        }

    def _build_opening_episode(
        self,
        turn_number: int,
        *,
        active_chapter: dict[str, Any] | None,
        patterns: list[dict[str, Any]],
        hooks: list[dict[str, Any]],
        tensions: list[dict[str, Any]],
        relationship_modes: list[dict[str, Any]],
        canon_bits: list[dict[str, Any]],
        dramatic_pressures: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        pressure_override = self._best_pressure_override(dramatic_pressures)
        if patterns:
            pattern = max(
                patterns,
                key=lambda row: (
                    self._theme_priority_score(
                        active_chapter,
                        text_parts=[
                            row.get("pattern_type"),
                            row.get("title"),
                            row.get("description"),
                        ],
                    ),
                    self._dramatic_pressure_bonus(
                        [str(char_id) for char_id in list(row.get("involved_chars") or []) if str(char_id).strip()],
                        None,
                        dramatic_pressures,
                    ),
                    self._canon_bonus(
                        [str(char_id) for char_id in list(row.get("involved_chars") or []) if str(char_id).strip()],
                        str(row.get("pattern_type") or "").strip(),
                        None,
                        canon_bits,
                    ),
                    self._relationship_mode_bonus(
                        [str(char_id) for char_id in list(row.get("involved_chars") or []) if str(char_id).strip()],
                        relationship_modes,
                    ),
                    int(row.get("recurrence_count") or 1),
                    float(row.get("intensity") or 0.0),
                    -int(row.get("id") or 0),
                ),
            )
            focus_char_ids = [str(char_id) for char_id in list(pattern.get("involved_chars") or [])[:3]]
            carry_hook_ids = self._collect_carry_hook_ids(
                hooks,
                focus_char_ids=focus_char_ids,
                source_hook_id=pattern.get("source_hook_id"),
            )
            episode_type = str(pattern.get("pattern_type") or "conflict")
            return {
                "status": "active",
                "episode_type": episode_type,
                "goal": pressure_override.get("goal") or _EPISODE_GOAL_BY_TYPE.get(episode_type, "次の争点を前に出す"),
                "stakes": pressure_override.get("stakes") or _EPISODE_STAKES_BY_TYPE.get(episode_type, "進展が弱いと火種が流れやすい。"),
                "active_pattern_id": pattern.get("id"),
                "active_pattern_type": episode_type,
                "focus_char_ids": focus_char_ids,
                "carry_over_hook_ids": carry_hook_ids,
                "focus_place_id": None,
                "opened_turn": turn_number,
                "last_progress_turn": turn_number,
                "summary": pressure_override.get("summary") or str(pattern.get("description") or "").strip() or None,
            }
        if hooks:
            hook = max(
                hooks,
                key=lambda row: (
                    self._theme_priority_score(
                        active_chapter,
                        text_parts=[
                            row.get("hook_type"),
                            row.get("title"),
                            row.get("description"),
                        ],
                    ),
                    self._dramatic_pressure_bonus(
                        [
                            str(char_id).strip()
                            for char_id in [row.get("owner_char_id"), row.get("target_char_id")]
                            if str(char_id or "").strip()
                        ],
                        str(row.get("source_place_id") or "").strip() or None,
                        dramatic_pressures,
                    ),
                    self._canon_bonus(
                        [
                            str(char_id).strip()
                            for char_id in [row.get("owner_char_id"), row.get("target_char_id")]
                            if str(char_id or "").strip()
                        ],
                        str(row.get("hook_type") or "").strip(),
                        str(row.get("source_place_id") or "").strip() or None,
                        canon_bits,
                    ),
                    self._relationship_mode_bonus(
                        [
                            str(char_id).strip()
                            for char_id in [row.get("owner_char_id"), row.get("target_char_id")]
                            if str(char_id or "").strip()
                        ],
                        relationship_modes,
                    ),
                    float(row.get("priority") or 0.0),
                    -int(row.get("id") or 0),
                ),
            )
            focus_char_ids = [
                str(char_id)
                for char_id in [hook.get("owner_char_id"), hook.get("target_char_id")]
                if str(char_id or "").strip()
            ][:3]
            return {
                "status": "active",
                "episode_type": "mystery" if str(hook.get("hook_type") or "") == "question" else "conflict",
                "goal": pressure_override.get("goal") or "未解決のフックを次の場面へつなぐ",
                "stakes": pressure_override.get("stakes") or "フックが回らないと短期エピソードが立ちにくい。",
                "active_pattern_id": None,
                "active_pattern_type": None,
                "focus_char_ids": focus_char_ids,
                "carry_over_hook_ids": [int(hook["id"])],
                "focus_place_id": hook.get("source_place_id"),
                "opened_turn": turn_number,
                "last_progress_turn": turn_number,
                "summary": pressure_override.get("summary") or str(hook.get("description") or "").strip() or None,
            }
        if tensions:
            tension = max(
                tensions,
                key=lambda row: (
                    self._theme_priority_score(
                        active_chapter,
                        text_parts=[
                            row.get("tension_type"),
                            row.get("description"),
                        ],
                    ),
                    self._dramatic_pressure_bonus(
                        [str(char_id) for char_id in list(row.get("involved_chars") or []) if str(char_id).strip()],
                        None,
                        dramatic_pressures,
                    ),
                    self._canon_bonus(
                        [str(char_id) for char_id in list(row.get("involved_chars") or []) if str(char_id).strip()],
                        str(row.get("tension_type") or "").strip(),
                        None,
                        canon_bits,
                    ),
                    self._relationship_mode_bonus(
                        [str(char_id) for char_id in list(row.get("involved_chars") or []) if str(char_id).strip()],
                        relationship_modes,
                    ),
                    float(row.get("intensity") or 0.0),
                    -int(row.get("id") or 0),
                ),
            )
            episode_type = str(tension.get("tension_type") or "conflict")
            return {
                "status": "active",
                "episode_type": episode_type,
                "goal": pressure_override.get("goal") or _EPISODE_GOAL_BY_TYPE.get(episode_type, "緊張の争点を前に出す"),
                "stakes": pressure_override.get("stakes") or _EPISODE_STAKES_BY_TYPE.get(episode_type, "緊張が停滞すると流れやすい。"),
                "active_pattern_id": None,
                "active_pattern_type": None,
                "focus_char_ids": [str(char_id) for char_id in list(tension.get("involved_chars") or [])[:3]],
                "carry_over_hook_ids": [],
                "focus_place_id": None,
                "opened_turn": turn_number,
                "last_progress_turn": turn_number,
                "summary": pressure_override.get("summary") or str(tension.get("description") or "").strip() or None,
            }
        return None

    @staticmethod
    def _theme_priority_score(
        active_chapter: dict[str, Any] | None,
        *,
        text_parts: list[Any],
    ) -> float:
        theme = str((active_chapter or {}).get("theme") or "").strip()
        if not theme:
            return 0.0
        haystack = " ".join(str(part or "").strip() for part in text_parts).lower()
        if not haystack:
            return 0.0
        if theme.lower() in haystack:
            return 1.0

        keywords = [
            token.lower()
            for token in re.split(r"[\s、。！？!?,:：;；/／（）()「」『』]+", theme)
            if len(token.strip()) >= 2
        ]
        if any(keyword in haystack for keyword in keywords):
            return 1.0

        for alias_theme, aliases in _THEME_TYPE_ALIASES.items():
            if alias_theme not in theme:
                continue
            if any(alias.lower() in haystack for alias in aliases):
                return 0.6
        return 0.0

    @staticmethod
    def _best_pressure_override(dramatic_pressures: list[dict[str, Any]]) -> dict[str, str]:
        preferred = [pressure for pressure in dramatic_pressures if str(pressure.get("pressure_type") or "") in _PRESSURE_GOAL_OVERRIDES]
        if not preferred:
            return {}
        best = max(preferred, key=lambda pressure: float(pressure.get("score") or 0.0))
        pressure_type = str(best.get("pressure_type") or "")
        return {
            "goal": _PRESSURE_GOAL_OVERRIDES.get(pressure_type, ""),
            "stakes": _PRESSURE_STAKES_OVERRIDES.get(pressure_type, ""),
            "summary": str(best.get("summary") or "").strip(),
        }

    @staticmethod
    def _canon_bonus(
        focus_char_ids: list[str],
        motif_key: str,
        focus_place_id: str | None,
        canon_bits: list[dict[str, Any]],
    ) -> float:
        focus_chars = {str(char_id).strip() for char_id in focus_char_ids if str(char_id).strip()}
        best = 0.0
        for bit in canon_bits:
            bit_chars = {
                str(char_id).strip()
                for char_id in list(bit.get("focus_char_ids") or [])
                if str(char_id).strip()
            }
            if motif_key and motif_key == str(bit.get("motif_key") or "").strip():
                best = max(best, 1.0)
            if focus_chars and bit_chars and focus_chars & bit_chars:
                best = max(best, 0.75)
            if focus_place_id and focus_place_id == str(bit.get("focus_place_id") or "").strip():
                best = max(best, 0.80)
        return best

    @staticmethod
    def _relationship_mode_bonus(
        focus_char_ids: list[str],
        relationship_modes: list[dict[str, Any]],
    ) -> float:
        focus_chars = {str(char_id).strip() for char_id in focus_char_ids if str(char_id).strip()}
        if len(focus_chars) < 2:
            return 0.0
        cluster_best: dict[str, float] = {}
        for mode in relationship_modes:
            mode_chars = {
                str(mode.get("char_id_from") or "").strip(),
                str(mode.get("char_id_to") or "").strip(),
            }
            if "" in mode_chars:
                continue
            if mode_chars <= focus_chars:
                cluster = "conflict" if str(mode.get("mode_type") or "") in {"irritated_respect", "chaos_partner"} else "attention"
                cluster_best[cluster] = max(cluster_best.get(cluster, 0.0), float(mode.get("intensity") or 0.0))
        return min(1.2, sum(cluster_best.values()))

    @staticmethod
    def _dramatic_pressure_bonus(
        focus_char_ids: list[str],
        focus_place_id: str | None,
        dramatic_pressures: list[dict[str, Any]],
    ) -> float:
        focus_chars = {str(char_id).strip() for char_id in focus_char_ids if str(char_id).strip()}
        best = 0.0
        for pressure in dramatic_pressures:
            pressure_chars = {
                str(char_id).strip()
                for char_id in list(pressure.get("focus_char_ids") or [])
                if str(char_id).strip()
            }
            if focus_chars and pressure_chars and focus_chars & pressure_chars:
                best = max(best, float(pressure.get("score") or 0.0))
            if focus_place_id and focus_place_id == str(pressure.get("focus_place_id") or "").strip():
                best = max(best, float(pressure.get("score") or 0.0))
        return best

    def _collect_carry_hook_ids(
        self,
        hooks: list[dict[str, Any]],
        *,
        focus_char_ids: list[str],
        source_hook_id: Any,
    ) -> list[int]:
        carry_ids: list[int] = []
        if source_hook_id is not None:
            try:
                carry_ids.append(int(source_hook_id))
            except (TypeError, ValueError):
                pass
        for hook in hooks:
            try:
                hook_id = int(hook["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if hook_id in carry_ids:
                continue
            owner = str(hook.get("owner_char_id") or "")
            target = str(hook.get("target_char_id") or "")
            if focus_char_ids and (owner in focus_char_ids or target in focus_char_ids):
                carry_ids.append(hook_id)
            if len(carry_ids) >= self._carry_hook_limit:
                break
        return carry_ids[: self._carry_hook_limit]

    def _close_payload_if_needed(
        self,
        episode: dict[str, Any],
        *,
        turn_number: int,
        patterns: list[dict[str, Any]],
        hooks: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        opened_turn = int(episode.get("opened_turn") or turn_number)
        age = turn_number - opened_turn + 1
        if age >= self._max_turns:
            return {
                "exit_condition": "timed_out",
                "summary": str(episode.get("summary") or episode.get("goal") or "").strip(),
            }

        active_pattern_id = episode.get("active_pattern_id")
        carry_hook_ids = {int(hook_id) for hook_id in list(episode.get("carry_over_hook_ids") or [])}
        active_pattern_ids = {
            int(pattern["id"])
            for pattern in patterns
            if pattern.get("id") is not None
        }
        open_hook_ids = {
            int(hook["id"])
            for hook in hooks
            if hook.get("id") is not None
        }
        anchor_resolved = (
            (active_pattern_id is None or int(active_pattern_id) not in active_pattern_ids)
            and not (carry_hook_ids & open_hook_ids)
        )
        if anchor_resolved and age >= self._min_turns:
            return {
                "exit_condition": "resolved",
                "summary": str(episode.get("summary") or episode.get("goal") or "").strip(),
            }

        last_progress_turn = int(episode.get("last_progress_turn") or opened_turn)
        if age >= self._min_turns and turn_number - last_progress_turn >= self._stale_rounds_before_close:
            return {
                "exit_condition": "stalled",
                "summary": str(episode.get("summary") or episode.get("goal") or "").strip(),
            }
        return None

    def _progress_turn(
        self,
        episode: dict[str, Any],
        *,
        turn_number: int,
        recent_events: list[dict[str, Any]],
        recent_closed_scenes: list[dict[str, Any]],
        patterns: list[dict[str, Any]],
        hooks: list[dict[str, Any]],
    ) -> int | None:
        focus_char_ids = {str(char_id) for char_id in list(episode.get("focus_char_ids") or [])}
        carry_hook_ids = {int(hook_id) for hook_id in list(episode.get("carry_over_hook_ids") or [])}
        if any(int(pattern.get("id") or 0) == int(episode.get("active_pattern_id") or 0) for pattern in patterns):
            return turn_number
        if any(int(hook.get("id") or 0) in carry_hook_ids for hook in hooks):
            return turn_number
        for scene in recent_closed_scenes:
            scene_chars = {str(char_id) for char_id in list(scene.get("focus_char_ids") or [])}
            if focus_char_ids & scene_chars:
                return turn_number
        for event in recent_events:
            if str(event.get("char_id_from") or "") in focus_char_ids or str(event.get("char_id_to") or "") in focus_char_ids:
                return turn_number
        return None
