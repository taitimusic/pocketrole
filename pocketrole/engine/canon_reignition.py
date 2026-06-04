"""Deterministic canon -> hook reignition."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager


_REIGNITABLE_LEVELS = {"recurring_bit", "proto_canon", "canon"}
_COOLDOWN_BY_LEVEL = {
    "recurring_bit": 6,
    "proto_canon": 5,
    "canon": 4,
}
_LEVEL_WEIGHT = {
    "momentary_bit": 0,
    "recurring_bit": 1,
    "proto_canon": 2,
    "canon": 3,
}
_QUESTION_MOTIFS = {"misunderstanding", "near_reveal", "unsafe_confidant", "cannot_ignore"}
_CONFLICT_MOTIFS = {
    "status_clash",
    "irritated_respect",
    "small_win_loss",
    "chaos_partner",
    "bluff_or_showoff",
    "role_reversal",
}


class CanonReignitionEngine:
    """Reignite durable canon bits back into open hooks."""

    def __init__(self, db: DatabaseManager | None = None, story_id: str = "") -> None:
        self._db = db
        self._story_id = story_id
        self._last_round_summary: dict[str, int] = {
            "created": 0,
            "skipped_cooldown": 0,
            "skipped_existing_hook": 0,
            "skipped_saturated": 0,
        }

    def get_last_round_summary(self) -> dict[str, int]:
        return dict(self._last_round_summary)

    async def process_round(self, turn_number: int) -> None:
        if self._db is None or not self._story_id:
            return

        canon_bits = await self._db.get_reignitable_story_canon_bits(self._story_id)
        active_patterns = await self._db.get_active_interaction_patterns(self._story_id)
        active_episode = await self._db.get_active_story_episode(self._story_id)
        recent_logs = await self._db.get_recent_chat_logs(self._story_id, limit=24)

        created = 0
        skipped_cooldown = 0
        skipped_existing_hook = 0
        skipped_saturated = 0

        for bit in canon_bits:
            if created >= 2:
                break
            if not self._is_recently_active(bit, recent_logs):
                continue
            if self._is_on_cooldown(bit, turn_number):
                skipped_cooldown += 1
                continue
            bit_id = self._as_int(bit.get("id"))
            if bit_id is None:
                continue
            open_hooks = await self._db.get_open_hooks_for_canon_bit(self._story_id, bit_id)
            if open_hooks:
                skipped_existing_hook += 1
                continue
            if self._is_saturated(bit, active_patterns, active_episode):
                skipped_saturated += 1
                continue
            await self._db.insert_story_hook(self._story_id, self._build_hook_payload(bit, turn_number))
            await self._db.update_story_canon_bit(
                bit_id,
                {
                    "last_reignited_turn": turn_number,
                    "reignition_count": int(bit.get("reignition_count") or 0) + 1,
                },
            )
            created += 1

        self._last_round_summary = {
            "created": created,
            "skipped_cooldown": skipped_cooldown,
            "skipped_existing_hook": skipped_existing_hook,
            "skipped_saturated": skipped_saturated,
        }

    def _is_on_cooldown(self, bit: dict[str, Any], turn_number: int) -> bool:
        level = str(bit.get("canon_level") or "recurring_bit")
        cooldown = _COOLDOWN_BY_LEVEL.get(level, 6)
        last_reignited_turn = self._as_int(bit.get("last_reignited_turn"))
        if last_reignited_turn is None:
            return False
        return (turn_number - last_reignited_turn) < cooldown

    def _is_recently_active(self, bit: dict[str, Any], recent_logs: list[dict[str, Any]]) -> bool:
        focus_chars = {
            str(char_id).strip()
            for char_id in list(bit.get("focus_char_ids") or [])
            if str(char_id).strip()
        }
        focus_place_id = str(bit.get("focus_place_id") or "").strip()
        if not focus_chars and not focus_place_id:
            return False
        for log in recent_logs:
            log_char_id = str(log.get("char_id") or "").strip()
            log_place_id = str(log.get("place_id") or "").strip()
            if focus_chars and log_char_id in focus_chars:
                return True
            if focus_place_id and log_place_id == focus_place_id:
                return True
        return False

    def _is_saturated(
        self,
        bit: dict[str, Any],
        active_patterns: list[dict[str, Any]],
        active_episode: dict[str, Any] | None,
    ) -> bool:
        motif_key = str(bit.get("motif_key") or "").strip()
        focus_chars = {
            str(char_id).strip()
            for char_id in list(bit.get("focus_char_ids") or [])
            if str(char_id).strip()
        }
        for pattern in active_patterns:
            pattern_type = str(pattern.get("pattern_type") or "").strip()
            involved_chars = {
                str(char_id).strip()
                for char_id in list(pattern.get("involved_chars") or [])
                if str(char_id).strip()
            }
            if pattern_type == motif_key and (not focus_chars or involved_chars & focus_chars):
                return True
        if active_episode is not None:
            episode_type = str(active_episode.get("episode_type") or "").strip()
            episode_focus_chars = {
                str(char_id).strip()
                for char_id in list(active_episode.get("focus_char_ids") or [])
                if str(char_id).strip()
            }
            if episode_type == motif_key and (not focus_chars or episode_focus_chars & focus_chars):
                return True
        return False

    def _build_hook_payload(self, bit: dict[str, Any], turn_number: int) -> dict[str, Any]:
        motif_key = str(bit.get("motif_key") or "").strip()
        focus_char_ids = [
            str(char_id).strip()
            for char_id in list(bit.get("focus_char_ids") or [])
            if str(char_id).strip()
        ]
        owner_char_id = focus_char_ids[0] if focus_char_ids else None
        target_char_id = focus_char_ids[1] if len(focus_char_ids) >= 2 else None
        bit_id = self._as_int(bit.get("id"))
        hook_type = self._hook_type_for_motif(motif_key)
        return {
            "hook_type": hook_type,
            "status": "open",
            "owner_char_id": owner_char_id,
            "target_char_id": target_char_id,
            "title": self._hook_title_for_motif(motif_key),
            "description": self._hook_description(bit, motif_key),
            "priority": self._hook_priority(bit),
            "due_turn": turn_number + 2,
            "source_canon_bit_id": bit_id,
        }

    def _hook_type_for_motif(self, motif_key: str) -> str:
        if motif_key in _QUESTION_MOTIFS:
            return "question"
        if motif_key in _CONFLICT_MOTIFS:
            return "conflict"
        return "question"

    def _hook_title_for_motif(self, motif_key: str) -> str:
        if motif_key in _QUESTION_MOTIFS:
            return "まだ噛み合っていない話題が戻る"
        return "前にも起きた張り合いが戻る"

    def _hook_description(self, bit: dict[str, Any], motif_key: str) -> str:
        focus_char_ids = [
            str(char_id).strip()
            for char_id in list(bit.get("focus_char_ids") or [])
            if str(char_id).strip()
        ]
        focus_place_id = str(bit.get("focus_place_id") or "").strip()
        if motif_key in _QUESTION_MOTIFS:
            base = "まだ言い切られていない話題が再び前に出やすい。"
        else:
            base = "以前の勝ち負けや張り合いが再び表に出やすい。"
        if len(focus_char_ids) >= 2:
            return f"{focus_char_ids[0]} と {focus_char_ids[1]} の間で、{base}"
        if focus_char_ids:
            return f"{focus_char_ids[0]} を中心に、{base}"
        if focus_place_id:
            return f"{focus_place_id} では、{base}"
        return base

    def _hook_priority(self, bit: dict[str, Any]) -> float:
        level = str(bit.get("canon_level") or "recurring_bit")
        level_bonus = {
            "recurring_bit": 0.58,
            "proto_canon": 0.68,
            "canon": 0.78,
        }.get(level, 0.55)
        confidence = float(bit.get("confidence") or 0.0)
        return min(0.95, max(level_bonus, confidence))

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
