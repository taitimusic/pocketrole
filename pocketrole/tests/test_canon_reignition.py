"""Tests for engine/canon_reignition.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

from engine.canon_reignition import CanonReignitionEngine
from tests._async_harness import async_to_sync


def _make_canon_bit(**overrides: object) -> dict[str, object]:
    row = {
        "id": 41,
        "canon_level": "recurring_bit",
        "status": "active",
        "motif_key": "status_clash",
        "focus_char_ids": ["char_a", "char_b"],
        "focus_place_id": "music_room",
        "confidence": 0.82,
        "recurrence_count": 2,
        "reignition_count": 0,
        "last_reinforced_turn": 10,
        "last_reignited_turn": None,
    }
    row.update(overrides)
    return row


def _make_log(**overrides: object) -> dict[str, object]:
    row = {
        "id": 7,
        "char_id": "char_a",
        "place_id": "music_room",
        "turn_number": 12,
        "message": "まだ終わってない。",
    }
    row.update(overrides)
    return row


@async_to_sync
async def test_process_round_creates_hook_from_reignitable_bit() -> None:
    db = AsyncMock()
    db.get_reignitable_story_canon_bits = AsyncMock(return_value=[_make_canon_bit()])
    db.get_active_interaction_patterns = AsyncMock(return_value=[])
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_recent_chat_logs = AsyncMock(return_value=[_make_log()])
    db.get_open_hooks_for_canon_bit = AsyncMock(return_value=[])
    db.insert_story_hook = AsyncMock(return_value=91)
    db.update_story_canon_bit = AsyncMock()

    engine = CanonReignitionEngine(db, "test_story")

    await engine.process_round(12)

    db.insert_story_hook.assert_awaited_once()
    hook = db.insert_story_hook.await_args.args[1]
    assert hook["hook_type"] == "conflict"
    assert hook["source_canon_bit_id"] == 41
    assert hook["owner_char_id"] == "char_a"
    assert hook["target_char_id"] == "char_b"
    db.update_story_canon_bit.assert_awaited_once_with(
        41,
        {"last_reignited_turn": 12, "reignition_count": 1},
    )


@async_to_sync
async def test_process_round_skips_cooldown_and_existing_hook() -> None:
    db = AsyncMock()
    db.get_reignitable_story_canon_bits = AsyncMock(
        return_value=[
            _make_canon_bit(id=41, last_reignited_turn=10),
            _make_canon_bit(id=42, motif_key="near_reveal"),
        ]
    )
    db.get_active_interaction_patterns = AsyncMock(return_value=[])
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_recent_chat_logs = AsyncMock(return_value=[_make_log()])
    db.get_open_hooks_for_canon_bit = AsyncMock(return_value=[{"id": 11}])
    db.insert_story_hook = AsyncMock()
    db.update_story_canon_bit = AsyncMock()

    engine = CanonReignitionEngine(db, "test_story")

    await engine.process_round(12)

    db.insert_story_hook.assert_not_awaited()
    assert engine.get_last_round_summary()["skipped_cooldown"] == 1
    assert engine.get_last_round_summary()["skipped_existing_hook"] == 1


@async_to_sync
async def test_process_round_skips_when_matching_pattern_is_already_active() -> None:
    db = AsyncMock()
    db.get_reignitable_story_canon_bits = AsyncMock(return_value=[_make_canon_bit()])
    db.get_active_interaction_patterns = AsyncMock(
        return_value=[
            {
                "id": 13,
                "pattern_type": "status_clash",
                "involved_chars": ["char_a", "char_b"],
            }
        ]
    )
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_recent_chat_logs = AsyncMock(return_value=[_make_log()])
    db.get_open_hooks_for_canon_bit = AsyncMock(return_value=[])
    db.insert_story_hook = AsyncMock()
    db.update_story_canon_bit = AsyncMock()

    engine = CanonReignitionEngine(db, "test_story")

    await engine.process_round(12)

    db.insert_story_hook.assert_not_awaited()
    assert engine.get_last_round_summary()["skipped_saturated"] == 1
