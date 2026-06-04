"""Tests for engine/relationship_modes.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

from engine.relationship_modes import RelationshipModeEngine
from tests._async_harness import async_to_sync


def _make_relationship(**overrides: object) -> dict[str, object]:
    row = {
        "char_id_from": "char_a",
        "char_id_to": "char_b",
        "trust": 0.62,
        "affinity": 0.48,
        "tension": 0.32,
        "familiarity": 0.50,
    }
    row.update(overrides)
    return row


def _make_pattern(**overrides: object) -> dict[str, object]:
    row = {
        "id": 7,
        "pattern_type": "status_clash",
        "status": "active",
        "involved_chars": ["char_a", "char_b"],
        "recurrence_count": 1,
        "confidence": 0.75,
        "intensity": 0.55,
    }
    row.update(overrides)
    return row


def _make_event(**overrides: object) -> dict[str, object]:
    row = {
        "id": 9,
        "char_id_from": "char_a",
        "char_id_to": "char_b",
        "delta_trust": 0.02,
        "delta_affinity": 0.01,
        "delta_tension": 0.04,
        "turn_number": 12,
        "summary": "char_a は char_b に張り合った。",
    }
    row.update(overrides)
    return row


def _make_episode(**overrides: object) -> dict[str, object]:
    row = {
        "id": 5,
        "status": "active",
        "episode_type": "status_clash",
        "focus_char_ids": ["char_a", "char_b"],
        "opened_turn": 10,
        "last_progress_turn": 12,
    }
    row.update(overrides)
    return row


def test_build_candidates_detects_irritated_respect() -> None:
    engine = RelationshipModeEngine()

    candidates = engine.build_candidates(
        turn_number=12,
        relationships=[_make_relationship()],
        patterns=[_make_pattern(pattern_type="status_clash")],
        episodes=[],
        relationship_events=[_make_event()],
    )

    assert len(candidates) == 1
    assert candidates[0].mode_type == "irritated_respect"
    assert candidates[0].char_id_from == "char_a"
    assert candidates[0].char_id_to == "char_b"


def test_build_candidates_detects_unsafe_confidant() -> None:
    engine = RelationshipModeEngine()

    candidates = engine.build_candidates(
        turn_number=12,
        relationships=[_make_relationship(trust=0.71, tension=0.18)],
        patterns=[_make_pattern(pattern_type="near_reveal", confidence=0.70)],
        episodes=[],
        relationship_events=[_make_event(delta_tension=0.01)],
    )

    assert len(candidates) == 1
    assert candidates[0].mode_type == "unsafe_confidant"


def test_build_candidates_detects_chaos_partner_from_recurrence() -> None:
    engine = RelationshipModeEngine()

    candidates = engine.build_candidates(
        turn_number=12,
        relationships=[_make_relationship(trust=0.22, affinity=0.44, tension=0.28)],
        patterns=[
            _make_pattern(
                pattern_type="small_win_loss",
                recurrence_count=3,
                intensity=0.60,
            )
        ],
        episodes=[],
        relationship_events=[_make_event(id=9), _make_event(id=10, turn_number=11)],
    )

    assert len(candidates) == 1
    assert candidates[0].mode_type == "chaos_partner"


def test_build_candidates_detects_cannot_ignore_from_episode_focus() -> None:
    engine = RelationshipModeEngine()

    candidates = engine.build_candidates(
        turn_number=12,
        relationships=[_make_relationship(trust=0.35, tension=0.29)],
        patterns=[],
        episodes=[_make_episode()],
        relationship_events=[_make_event(id=11, turn_number=12)],
    )

    assert len(candidates) == 1
    assert candidates[0].mode_type == "cannot_ignore"


def test_build_candidates_allows_two_modes_from_different_clusters() -> None:
    engine = RelationshipModeEngine()

    candidates = engine.build_candidates(
        turn_number=12,
        relationships=[_make_relationship(trust=0.68, affinity=0.42, tension=0.31)],
        patterns=[
            _make_pattern(pattern_type="status_clash", id=7, confidence=0.78, intensity=0.62),
            _make_pattern(pattern_type="near_reveal", id=8, confidence=0.76, intensity=0.57),
        ],
        episodes=[_make_episode(id=5)],
        relationship_events=[_make_event(id=9), _make_event(id=10, turn_number=11)],
    )

    mode_types = {candidate.mode_type for candidate in candidates}
    assert mode_types == {"irritated_respect", "unsafe_confidant"}


def test_build_candidates_keeps_one_winner_per_cluster() -> None:
    engine = RelationshipModeEngine()

    candidates = engine.build_candidates(
        turn_number=12,
        relationships=[_make_relationship(trust=0.58, affinity=0.44, tension=0.33)],
        patterns=[
            _make_pattern(pattern_type="status_clash", id=7, confidence=0.82, intensity=0.66),
            _make_pattern(pattern_type="small_win_loss", id=8, recurrence_count=3, confidence=0.72, intensity=0.52),
        ],
        episodes=[_make_episode(id=5)],
        relationship_events=[_make_event(id=9), _make_event(id=10, turn_number=11)],
    )

    mode_types = [candidate.mode_type for candidate in candidates]
    assert "irritated_respect" in mode_types
    assert "chaos_partner" not in mode_types


@async_to_sync
async def test_process_round_deactivates_stale_mode() -> None:
    db = AsyncMock()
    db.get_relationship_snapshots = AsyncMock(return_value=[])
    db.get_active_interaction_patterns = AsyncMock(return_value=[])
    db.get_recent_story_episodes = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[])
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "id": 41,
                "mode_type": "cannot_ignore",
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "status": "active",
                "last_reinforced_turn": 6,
            }
        ]
    )
    db.deactivate_relationship_mode = AsyncMock()
    db.find_active_relationship_mode_pair = AsyncMock(return_value=None)
    db.insert_relationship_mode = AsyncMock()
    db.update_relationship_mode = AsyncMock()

    engine = RelationshipModeEngine(db, "test_story")

    await engine.process_round(12)

    db.deactivate_relationship_mode.assert_awaited_once()
    assert db.deactivate_relationship_mode.await_args.args[0] == 41


@async_to_sync
async def test_process_round_decays_unreinforced_mode_before_ttl() -> None:
    db = AsyncMock()
    db.get_relationship_snapshots = AsyncMock(return_value=[_make_relationship()])
    db.get_active_interaction_patterns = AsyncMock(return_value=[])
    db.get_recent_story_episodes = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[])
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "id": 41,
                "mode_type": "cannot_ignore",
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "status": "active",
                "intensity": 0.60,
                "confidence": 0.70,
                "last_reinforced_turn": 10,
            }
        ]
    )
    db.deactivate_relationship_mode = AsyncMock()
    db.find_active_relationship_mode = AsyncMock(return_value=None)
    db.insert_relationship_mode = AsyncMock()
    db.update_relationship_mode = AsyncMock()

    engine = RelationshipModeEngine(db, "test_story")

    await engine.process_round(12)

    db.deactivate_relationship_mode.assert_not_awaited()
    db.update_relationship_mode.assert_awaited_once()
    patch = db.update_relationship_mode.await_args.args[1]
    assert patch["intensity"] < 0.60
    assert patch["confidence"] < 0.70
    assert "last_reinforced_turn" not in patch


@async_to_sync
async def test_process_round_keeps_cross_cluster_mode_active_when_new_mode_is_reinforced() -> None:
    db = AsyncMock()
    db.get_relationship_snapshots = AsyncMock(return_value=[_make_relationship(trust=0.62, tension=0.33)])
    db.get_active_interaction_patterns = AsyncMock(return_value=[_make_pattern(pattern_type="status_clash")])
    db.get_recent_story_episodes = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[_make_event()])
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "id": 41,
                "mode_type": "unsafe_confidant",
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "status": "active",
                "intensity": 0.61,
                "confidence": 0.72,
                "last_reinforced_turn": 11,
            }
        ]
    )
    db.find_active_relationship_mode = AsyncMock(return_value=None)
    db.insert_relationship_mode = AsyncMock()
    db.update_relationship_mode = AsyncMock()
    db.deactivate_relationship_mode = AsyncMock()

    engine = RelationshipModeEngine(db, "test_story")

    await engine.process_round(12)

    db.insert_relationship_mode.assert_awaited_once()
    db.deactivate_relationship_mode.assert_not_awaited()
    db.update_relationship_mode.assert_awaited_once()
    assert db.update_relationship_mode.await_args.args[0] == 41


@async_to_sync
async def test_process_round_refreshes_complementary_mode_when_pair_stays_hot() -> None:
    db = AsyncMock()
    db.get_relationship_snapshots = AsyncMock(return_value=[_make_relationship(trust=0.62, affinity=0.48, tension=0.33)])
    db.get_active_interaction_patterns = AsyncMock(return_value=[_make_pattern(pattern_type="status_clash")])
    db.get_recent_story_episodes = AsyncMock(return_value=[_make_episode(id=5)])
    db.get_recent_relationship_events = AsyncMock(return_value=[_make_event(id=9), _make_event(id=10, turn_number=11)])
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "id": 41,
                "mode_type": "cannot_ignore",
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "status": "active",
                "intensity": 0.46,
                "confidence": 0.52,
                "last_reinforced_turn": 8,
            }
        ]
    )
    db.find_active_relationship_mode = AsyncMock(return_value=None)
    db.insert_relationship_mode = AsyncMock()
    db.update_relationship_mode = AsyncMock()
    db.deactivate_relationship_mode = AsyncMock()

    engine = RelationshipModeEngine(db, "test_story")

    await engine.process_round(12)

    db.deactivate_relationship_mode.assert_not_awaited()
    patches = [call.args[1] for call in db.update_relationship_mode.await_args_list]
    assert any(patch.get("last_reinforced_turn") == 12 for patch in patches)
    complementary_patch = next(patch for patch in patches if patch.get("last_reinforced_turn") == 12)
    assert complementary_patch["intensity"] > 0.46
    assert complementary_patch["confidence"] > 0.52


@async_to_sync
async def test_process_round_still_supersedes_same_cluster_mode() -> None:
    db = AsyncMock()
    db.get_relationship_snapshots = AsyncMock(return_value=[_make_relationship(trust=0.62, tension=0.33)])
    db.get_active_interaction_patterns = AsyncMock(return_value=[_make_pattern(pattern_type="status_clash")])
    db.get_recent_story_episodes = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[_make_event()])
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "id": 42,
                "mode_type": "chaos_partner",
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "status": "active",
                "intensity": 0.50,
                "confidence": 0.58,
                "last_reinforced_turn": 11,
            }
        ]
    )
    db.find_active_relationship_mode = AsyncMock(return_value=None)
    db.insert_relationship_mode = AsyncMock()
    db.update_relationship_mode = AsyncMock()
    db.deactivate_relationship_mode = AsyncMock()

    engine = RelationshipModeEngine(db, "test_story")

    await engine.process_round(12)

    db.deactivate_relationship_mode.assert_awaited_once()
    assert db.deactivate_relationship_mode.await_args.args[0] == 42
    assert "superseded by irritated_respect" in db.deactivate_relationship_mode.await_args.kwargs["summary"]


@async_to_sync
async def test_process_round_keeps_complementary_mode_past_ttl_when_pair_heat_remains() -> None:
    db = AsyncMock()
    db.get_relationship_snapshots = AsyncMock(
        return_value=[_make_relationship(trust=0.66, affinity=0.52, tension=0.34)]
    )
    db.get_active_interaction_patterns = AsyncMock(
        return_value=[
            _make_pattern(pattern_type="status_clash", id=7, recurrence_count=2, intensity=0.62),
            _make_pattern(pattern_type="near_reveal", id=8, confidence=0.74, intensity=0.57),
        ]
    )
    db.get_recent_story_episodes = AsyncMock(return_value=[_make_episode(id=5)])
    db.get_recent_relationship_events = AsyncMock(
        return_value=[_make_event(id=9), _make_event(id=10, turn_number=11)]
    )
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "id": 41,
                "mode_type": "unsafe_confidant",
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "status": "active",
                "intensity": 0.44,
                "confidence": 0.49,
                "last_reinforced_turn": 8,
            }
        ]
    )
    db.find_active_relationship_mode = AsyncMock(return_value=None)
    db.insert_relationship_mode = AsyncMock()
    db.update_relationship_mode = AsyncMock()
    db.deactivate_relationship_mode = AsyncMock()

    engine = RelationshipModeEngine(db, "test_story")

    await engine.process_round(12)

    db.deactivate_relationship_mode.assert_not_awaited()
    patches = [call.args[1] for call in db.update_relationship_mode.await_args_list]
    assert any(patch.get("last_reinforced_turn") == 12 for patch in patches)


@async_to_sync
async def test_process_round_deactivates_complementary_mode_when_cross_cluster_support_is_missing() -> None:
    db = AsyncMock()
    db.get_relationship_snapshots = AsyncMock(
        return_value=[_make_relationship(trust=0.58, affinity=0.42, tension=0.31)]
    )
    db.get_active_interaction_patterns = AsyncMock(
        return_value=[_make_pattern(pattern_type="status_clash", id=7, recurrence_count=2, intensity=0.60)]
    )
    db.get_recent_story_episodes = AsyncMock(return_value=[])
    db.get_recent_relationship_events = AsyncMock(return_value=[_make_event(id=9)])
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "id": 41,
                "mode_type": "unsafe_confidant",
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "status": "active",
                "intensity": 0.19,
                "confidence": 0.24,
                "last_reinforced_turn": 9,
            }
        ]
    )
    db.find_active_relationship_mode = AsyncMock(return_value=None)
    db.insert_relationship_mode = AsyncMock()
    db.update_relationship_mode = AsyncMock()
    db.deactivate_relationship_mode = AsyncMock()

    engine = RelationshipModeEngine(db, "test_story")

    await engine.process_round(12)

    db.deactivate_relationship_mode.assert_awaited_once()
    assert db.deactivate_relationship_mode.await_args.args[0] == 41
