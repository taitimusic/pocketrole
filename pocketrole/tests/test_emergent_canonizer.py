"""Tests for engine/emergent_canonizer.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

from engine.emergent_canonizer import EmergentCanonizer
from tests._async_harness import async_to_sync


def _make_pattern(**overrides: object) -> dict[str, object]:
    row = {
        "id": 7,
        "pattern_type": "status_clash",
        "status": "active",
        "involved_chars": ["char_a", "char_b"],
        "recurrence_count": 2,
        "confidence": 0.75,
        "intensity": 0.55,
        "description": "char_a と char_b が張り合った。",
    }
    row.update(overrides)
    return row


def _make_relationship_mode(**overrides: object) -> dict[str, object]:
    row = {
        "id": 9,
        "mode_type": "irritated_respect",
        "status": "active",
        "char_id_from": "char_a",
        "char_id_to": "char_b",
        "summary": "char_a は char_b を認めつつ、ぶつかりやすい。",
        "confidence": 0.8,
        "intensity": 0.6,
    }
    row.update(overrides)
    return row


def _make_episode(**overrides: object) -> dict[str, object]:
    row = {
        "id": 5,
        "status": "closed",
        "episode_type": "status_clash",
        "focus_char_ids": ["char_a", "char_b"],
        "focus_place_id": "music_room",
        "summary": "音楽室で張り合いが続いた。",
    }
    row.update(overrides)
    return row


def _make_scene(**overrides: object) -> dict[str, object]:
    row = {
        "id": 11,
        "place_id": "music_room",
        "focus_char_ids": ["char_a", "char_b"],
        "outcome_type": "status_clash",
        "outcome_summary": "音楽室で勝ち負けの応酬になった。",
    }
    row.update(overrides)
    return row


def test_build_candidates_detects_pair_dynamic_from_relationship_mode() -> None:
    engine = EmergentCanonizer()

    candidates = engine.build_candidates(
        turn_number=20,
        patterns=[_make_pattern()],
        relationship_modes=[_make_relationship_mode()],
        episodes=[],
        scenes=[],
    )

    assert any(candidate.bit_type == "pair_dynamic" for candidate in candidates)
    pair_candidate = next(candidate for candidate in candidates if candidate.bit_type == "pair_dynamic")
    assert pair_candidate.motif_key == "irritated_respect"
    assert pair_candidate.dedupe_key == "pair:char_a:char_b:irritated_respect"


def test_build_candidates_detects_place_motif_from_repeated_episode_and_scene() -> None:
    engine = EmergentCanonizer()

    candidates = engine.build_candidates(
        turn_number=20,
        patterns=[],
        relationship_modes=[],
        episodes=[_make_episode(id=5), _make_episode(id=6)],
        scenes=[_make_scene(id=11), _make_scene(id=12)],
    )

    assert any(candidate.bit_type == "place_motif" for candidate in candidates)


@async_to_sync
async def test_process_round_reinforces_and_promotes_existing_bit() -> None:
    db = AsyncMock()
    db.get_recent_interaction_patterns = AsyncMock(return_value=[_make_pattern()])
    db.get_recent_relationship_modes = AsyncMock(return_value=[_make_relationship_mode()])
    db.get_recent_story_episodes = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(
        return_value=[
            {
                "id": 41,
                "status": "active",
                "dedupe_key": "pair:char_a:char_b:irritated_respect",
                "canon_level": "momentary_bit",
                "focus_char_ids": ["char_a", "char_b"],
                "evidence_sources": ["relationship_mode"],
                "first_detected_turn": 10,
                "last_reinforced_turn": 10,
                "recurrence_count": 1,
                "confidence": 0.8,
                "novelty": 0.65,
                "intent_alignment": 0.55,
            }
        ]
    )
    db.find_active_story_canon_bit_by_dedupe_key = AsyncMock(
        return_value={
            "id": 41,
            "status": "active",
            "dedupe_key": "pair:char_a:char_b:irritated_respect",
            "canon_level": "momentary_bit",
            "focus_char_ids": ["char_a", "char_b"],
            "evidence_sources": ["relationship_mode"],
            "first_detected_turn": 10,
            "last_reinforced_turn": 10,
            "recurrence_count": 1,
            "confidence": 0.8,
            "novelty": 0.65,
            "intent_alignment": 0.55,
        }
    )
    db.insert_story_canon_bit = AsyncMock()
    db.update_story_canon_bit = AsyncMock()
    db.archive_story_canon_bit = AsyncMock()

    engine = EmergentCanonizer(db, "test_story")

    await engine.process_round(20)

    assert db.update_story_canon_bit.await_count >= 1
    patches = [call.args[1] for call in db.update_story_canon_bit.await_args_list]
    assert any(patch["recurrence_count"] >= 2 for patch in patches)
    assert any(patch["canon_level"] in {"recurring_bit", "proto_canon", "canon"} for patch in patches)
