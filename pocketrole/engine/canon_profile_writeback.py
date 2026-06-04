"""Deterministic canon -> profile overlay writeback."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager


_LEVEL_WEIGHT = {
    "proto_canon": 2,
    "canon": 3,
}
_BIT_TYPE_WEIGHT = {
    "pair_dynamic": 4,
    "character_tendency": 3,
    "group_routine": 2,
    "place_motif": 1,
}
_COOLDOWN_BY_LEVEL = {
    "proto_canon": 6,
    "canon": 4,
}
_STATUS_MOTIFS = {"status_clash", "irritated_respect", "role_reversal"}
_QUESTION_MOTIFS = {"misunderstanding", "near_reveal", "unsafe_confidant", "cannot_ignore"}
_SHOWOFF_MOTIFS = {"small_win_loss", "chaos_partner", "bluff_or_showoff"}


@dataclass(frozen=True)
class CanonOverlayPatch:
    char_id: str
    primary_canon_bit_id: int
    source_canon_bit_ids: list[int]
    overlay_json: dict[str, str]


class CanonProfileWritebackEngine:
    """Persist stable canon bits into a separate prompt-visible overlay layer."""

    def __init__(self, db: DatabaseManager | None = None, story_id: str = "") -> None:
        self._db = db
        self._story_id = story_id
        self._last_round_summary: dict[str, int] = {
            "upserted": 0,
            "cleared": 0,
            "evolution_rows": 0,
        }

    def get_last_round_summary(self) -> dict[str, int]:
        return dict(self._last_round_summary)

    async def process_round(self, turn_number: int) -> None:
        if self._db is None or not self._story_id:
            return

        characters = await self._db.get_characters(self._story_id)
        char_name_map = {
            str(char.get("id") or "").strip(): str(char.get("name_ja") or char.get("id") or "")
            for char in characters
        }
        eligible_bits = await self._db.get_writeback_eligible_canon_bits(self._story_id)
        bits_by_char = self._group_bits_by_char(eligible_bits)

        upserted = 0
        cleared = 0
        evolution_rows = 0
        for char in characters:
            char_id = str(char.get("id") or "").strip()
            if not char_id:
                continue
            desired_patch = self._build_overlay_patch(
                char,
                bits_by_char.get(char_id, []),
                char_name_map=char_name_map,
                turn_number=turn_number,
            )
            existing_overlay = await self._db.get_character_canon_overlay(self._story_id, char_id)
            growth_overlay_row = await self._db.get_character_profile_overlay(self._story_id, char_id)
            growth_overlay = dict(growth_overlay_row.get("overlay_json", {})) if growth_overlay_row else {}
            if desired_patch is None:
                if existing_overlay is None:
                    continue
                removed_rows = await self._record_overlay_field_changes(
                    turn_number=turn_number,
                    char=char,
                    growth_overlay=growth_overlay,
                    before_overlay=dict(existing_overlay.get("overlay_json", {})),
                    after_overlay={},
                    source_canon_bit_id=self._first_source_canon_bit_id(existing_overlay),
                )
                await self._db.delete_character_canon_overlay(self._story_id, char_id)
                cleared += 1
                evolution_rows += removed_rows
                continue

            overlay_changed = self._overlay_changed(existing_overlay, desired_patch)
            if not self._should_writeback(
                desired_patch.primary_canon_bit_id,
                eligible_bits,
                turn_number=turn_number,
                existing_overlay=existing_overlay,
                overlay_changed=overlay_changed,
            ):
                continue

            before_overlay = (
                dict(existing_overlay.get("overlay_json", {}))
                if existing_overlay is not None
                else {}
            )
            changed_rows = await self._record_overlay_field_changes(
                turn_number=turn_number,
                char=char,
                growth_overlay=growth_overlay,
                before_overlay=before_overlay,
                after_overlay=desired_patch.overlay_json,
                source_canon_bit_id=desired_patch.primary_canon_bit_id,
            )
            next_version = 1
            if existing_overlay is not None:
                next_version = int(existing_overlay.get("version", 1)) + 1
            await self._db.upsert_character_canon_overlay(
                self._story_id,
                char_id,
                {
                    "overlay_json": desired_patch.overlay_json,
                    "version": next_version,
                    "last_written_turn": turn_number,
                    "source_canon_bit_ids": desired_patch.source_canon_bit_ids,
                },
            )
            await self._db.update_story_canon_bit(
                desired_patch.primary_canon_bit_id,
                {
                    "last_writeback_turn": turn_number,
                    "writeback_count": self._next_writeback_count(
                        desired_patch.primary_canon_bit_id,
                        eligible_bits,
                    ),
                },
            )
            upserted += 1
            evolution_rows += changed_rows

        self._last_round_summary = {
            "upserted": upserted,
            "cleared": cleared,
            "evolution_rows": evolution_rows,
        }

    def _group_bits_by_char(self, bits: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for bit in bits:
            for char_id in [
                str(candidate).strip()
                for candidate in list(bit.get("focus_char_ids") or [])
                if str(candidate).strip()
            ]:
                grouped.setdefault(char_id, []).append(bit)
        return grouped

    def _build_overlay_patch(
        self,
        char: dict[str, Any],
        bits: list[dict[str, Any]],
        *,
        char_name_map: dict[str, str],
        turn_number: int,
    ) -> CanonOverlayPatch | None:
        if not bits:
            return None
        primary = max(bits, key=self._bit_priority)
        primary_id = self._as_int(primary.get("id"))
        if primary_id is None:
            return None
        overlay_json = self._build_overlay_json(char, primary, char_name_map=char_name_map)
        if not overlay_json:
            return None
        return CanonOverlayPatch(
            char_id=str(char["id"]),
            primary_canon_bit_id=primary_id,
            source_canon_bit_ids=[primary_id],
            overlay_json=overlay_json,
        )

    def _build_overlay_json(
        self,
        char: dict[str, Any],
        bit: dict[str, Any],
        *,
        char_name_map: dict[str, str],
    ) -> dict[str, str]:
        char_id = str(char.get("id") or "").strip()
        char_name = str(char.get("name_ja") or char_id)
        focus_char_ids = [
            str(candidate).strip()
            for candidate in list(bit.get("focus_char_ids") or [])
            if str(candidate).strip()
        ]
        target_id = next((candidate for candidate in focus_char_ids if candidate != char_id), None)
        target_name = char_name_map.get(target_id or "", target_id or None)
        motif_key = str(bit.get("motif_key") or "").strip()
        current_goal, current_worry, personality_note = self._template_for_motif(
            motif_key,
            char_name=char_name,
            target_name=target_name,
        )
        overlay_json = {
            "current_goal": current_goal,
            "current_worry": current_worry,
        }
        if personality_note:
            overlay_json["personality_core"] = self._merge_personality_core(
                str(char.get("personality_core") or ""),
                personality_note,
            )
        return overlay_json

    def _template_for_motif(
        self,
        motif_key: str,
        *,
        char_name: str,
        target_name: str | None,
    ) -> tuple[str, str, str | None]:
        if motif_key in _STATUS_MOTIFS:
            if target_name:
                return (
                    f"{target_name}との勝ち負けをはっきりさせたい",
                    f"{target_name}に押し切られるのは避けたい",
                    "相手を認めても引けなくなりやすい",
                )
            return (
                "勝ち負けをはっきりさせたい",
                "押し切られるのは避けたい",
                "相手を認めても引けなくなりやすい",
            )
        if motif_key in _QUESTION_MOTIFS:
            if target_name:
                return (
                    f"{target_name}との話をはっきりさせたい",
                    f"{target_name}との曖昧なままの話題を流したくない",
                    "曖昧さを放っておきにくい",
                )
            return (
                "曖昧な話をはっきりさせたい",
                "言い切らないまま流れるのは避けたい",
                "曖昧さを放っておきにくい",
            )
        if motif_key in _SHOWOFF_MOTIFS:
            if target_name:
                return (
                    f"{target_name}相手に見せ場を取りたい",
                    f"{target_name}に主導権を取られたくない",
                    "張り合いと悪ノリに引かれやすい",
                )
            return (
                "見せ場を取りたい",
                "主導権を取られたくない",
                "張り合いと悪ノリに引かれやすい",
            )
        return (
            f"{char_name}らしい流れを前に進めたい",
            "この流れを雑に手放したくない",
            None,
        )

    def _merge_personality_core(self, base_value: str, note: str) -> str:
        stripped_base = base_value.strip()
        if not stripped_base:
            return note
        if note in stripped_base:
            return stripped_base
        return f"{stripped_base}。{note}"

    async def _record_overlay_field_changes(
        self,
        *,
        turn_number: int,
        char: dict[str, Any],
        growth_overlay: dict[str, str],
        before_overlay: dict[str, str],
        after_overlay: dict[str, str],
        source_canon_bit_id: int | None,
    ) -> int:
        if self._db is None:
            return 0
        char_id = str(char["id"])
        changed = 0
        all_fields = set(before_overlay) | set(after_overlay)
        for field in sorted(all_fields):
            previous_value = (
                growth_overlay.get(field)
                or before_overlay.get(field)
                or str(char.get(field) or "")
            )
            new_value = (
                growth_overlay.get(field)
                or after_overlay.get(field)
                or str(char.get(field) or "")
            )
            if previous_value == new_value:
                continue
            await self._db.insert_evolution(
                self._story_id,
                {
                    "char_id": char_id,
                    "turn_number": turn_number,
                    "field": field,
                    "previous_value": previous_value,
                    "new_value": new_value,
                    "reason": f"canon_writeback: {field} updated from stable canon",
                    "source_memory_id": None,
                    "source_canon_bit_id": source_canon_bit_id,
                },
            )
            changed += 1
        return changed

    def _should_writeback(
        self,
        primary_canon_bit_id: int,
        eligible_bits: list[dict[str, Any]],
        *,
        turn_number: int,
        existing_overlay: dict[str, Any] | None,
        overlay_changed: bool,
    ) -> bool:
        if existing_overlay is None or overlay_changed:
            return True
        bit = next(
            (
                candidate
                for candidate in eligible_bits
                if self._as_int(candidate.get("id")) == primary_canon_bit_id
            ),
            None,
        )
        if bit is None:
            return False
        last_writeback_turn = self._as_int(bit.get("last_writeback_turn"))
        if last_writeback_turn is None:
            return True
        cooldown = _COOLDOWN_BY_LEVEL.get(str(bit.get("canon_level") or "proto_canon"), 6)
        return (turn_number - last_writeback_turn) >= cooldown

    def _overlay_changed(
        self,
        existing_overlay: dict[str, Any] | None,
        patch: CanonOverlayPatch,
    ) -> bool:
        if existing_overlay is None:
            return True
        return (
            dict(existing_overlay.get("overlay_json", {})) != patch.overlay_json
            or list(existing_overlay.get("source_canon_bit_ids", [])) != patch.source_canon_bit_ids
        )

    def _bit_priority(self, bit: dict[str, Any]) -> tuple[int, int, int, float, int]:
        bit_id = self._as_int(bit.get("id")) or 0
        return (
            _LEVEL_WEIGHT.get(str(bit.get("canon_level") or "proto_canon"), 0),
            _BIT_TYPE_WEIGHT.get(str(bit.get("bit_type") or ""), 0),
            int(bit.get("recurrence_count") or 0),
            float(bit.get("intent_alignment") or 0.0),
            bit_id,
        )

    def _next_writeback_count(self, bit_id: int, eligible_bits: list[dict[str, Any]]) -> int:
        for bit in eligible_bits:
            if self._as_int(bit.get("id")) == bit_id:
                return int(bit.get("writeback_count") or 0) + 1
        return 1

    @staticmethod
    def _first_source_canon_bit_id(row: dict[str, Any]) -> int | None:
        source_ids = [
            int(candidate)
            for candidate in list(row.get("source_canon_bit_ids") or [])
            if str(candidate).strip()
        ]
        return source_ids[0] if source_ids else None

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
