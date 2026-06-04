"""Tests for engine/canon_profile_writeback.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

from engine.canon_profile_writeback import CanonProfileWritebackEngine
from tests._async_harness import async_to_sync


def _make_char(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "char_a",
        "name_ja": "星風ルナ",
        "personality_core": "負けず嫌いで真っ直ぐ。",
        "current_goal": "穏やかに過ごしたい",
        "current_worry": "面倒は避けたい",
    }
    row.update(overrides)
    return row


def _make_bit(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 41,
        "bit_type": "pair_dynamic",
        "motif_key": "status_clash",
        "canon_level": "proto_canon",
        "status": "active",
        "focus_char_ids": ["char_a", "char_b"],
        "recurrence_count": 3,
        "intent_alignment": 0.8,
        "last_writeback_turn": None,
        "writeback_count": 0,
    }
    row.update(overrides)
    return row


@async_to_sync
async def test_process_round_upserts_canon_overlay_and_evolution_rows() -> None:
    db = AsyncMock()
    db.get_characters = AsyncMock(return_value=[_make_char()])
    db.get_writeback_eligible_canon_bits = AsyncMock(return_value=[_make_bit()])
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.insert_evolution = AsyncMock(return_value=1)
    db.upsert_character_canon_overlay = AsyncMock()
    db.update_story_canon_bit = AsyncMock()
    db.delete_character_canon_overlay = AsyncMock()

    engine = CanonProfileWritebackEngine(db, "test_story")

    await engine.process_round(18)

    db.upsert_character_canon_overlay.assert_awaited_once()
    overlay_payload = db.upsert_character_canon_overlay.await_args.args[2]
    assert "char_bとの勝ち負けをはっきりさせたい" == overlay_payload["overlay_json"]["current_goal"]
    assert overlay_payload["source_canon_bit_ids"] == [41]
    assert db.insert_evolution.await_count >= 2
    assert any(
        call.kwargs == {}
        and call.args[1].get("source_canon_bit_id") == 41
        for call in db.insert_evolution.await_args_list
    )


@async_to_sync
async def test_process_round_skips_evolution_when_growth_overlay_already_wins() -> None:
    db = AsyncMock()
    db.get_characters = AsyncMock(return_value=[_make_char()])
    db.get_writeback_eligible_canon_bits = AsyncMock(return_value=[_make_bit()])
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.get_character_profile_overlay = AsyncMock(
        return_value={"overlay_json": {"current_goal": "いまは別の約束を守りたい"}}
    )
    db.insert_evolution = AsyncMock(return_value=1)
    db.upsert_character_canon_overlay = AsyncMock()
    db.update_story_canon_bit = AsyncMock()
    db.delete_character_canon_overlay = AsyncMock()

    engine = CanonProfileWritebackEngine(db, "test_story")

    await engine.process_round(18)

    assert any(
        call.args[1]["field"] == "current_worry"
        for call in db.insert_evolution.await_args_list
    )
    assert not any(
        call.args[1]["field"] == "current_goal"
        for call in db.insert_evolution.await_args_list
    )


@async_to_sync
async def test_process_round_clears_existing_overlay_when_no_eligible_bits_remain() -> None:
    db = AsyncMock()
    db.get_characters = AsyncMock(return_value=[_make_char()])
    db.get_writeback_eligible_canon_bits = AsyncMock(return_value=[])
    db.get_character_canon_overlay = AsyncMock(
        return_value={
            "overlay_json": {
                "current_goal": "char_bとの勝ち負けをはっきりさせたい",
                "current_worry": "char_bに押し切られるのは避けたい",
            },
            "source_canon_bit_ids": [41],
            "version": 2,
        }
    )
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.insert_evolution = AsyncMock(return_value=1)
    db.upsert_character_canon_overlay = AsyncMock()
    db.update_story_canon_bit = AsyncMock()
    db.delete_character_canon_overlay = AsyncMock()

    engine = CanonProfileWritebackEngine(db, "test_story")

    await engine.process_round(24)

    db.delete_character_canon_overlay.assert_awaited_once_with("test_story", "char_a")
    assert any(
        call.args[1]["field"] == "current_goal"
        for call in db.insert_evolution.await_args_list
    )
