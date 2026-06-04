"""Tests for engine/dramatic_pressure.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

from engine.dramatic_pressure import DramaticPressureEvaluator
from engine.story_intent import get_story_intent_profile
from tests._async_harness import async_to_sync


def _make_tension(**overrides: object) -> dict[str, object]:
    row = {
        "id": 11,
        "tension_type": "conflict",
        "status": "escalating",
        "description": "char_a と char_b の張り合いが強まる。",
        "involved_chars": ["char_a", "char_b"],
        "intensity": 0.8,
        "detected_turn": 8,
    }
    row.update(overrides)
    return row


def _make_pattern(**overrides: object) -> dict[str, object]:
    row = {
        "id": 21,
        "pattern_type": "status_clash",
        "status": "active",
        "description": "主導権争いが続いている。",
        "involved_chars": ["char_a", "char_b"],
        "recurrence_count": 2,
        "intensity": 0.7,
        "confidence": 0.75,
    }
    row.update(overrides)
    return row


def _make_episode(**overrides: object) -> dict[str, object]:
    row = {
        "id": 31,
        "status": "active",
        "episode_type": "status_clash",
        "goal": "張り合いの勝負どころを作る",
        "focus_char_ids": ["char_a", "char_b"],
        "focus_place_id": "music_room",
        "opened_turn": 10,
        "last_progress_turn": 12,
    }
    row.update(overrides)
    return row


def _make_mode(**overrides: object) -> dict[str, object]:
    row = {
        "id": 41,
        "mode_type": "irritated_respect",
        "status": "active",
        "char_id_from": "char_a",
        "char_id_to": "char_b",
        "intensity": 0.6,
        "confidence": 0.7,
    }
    row.update(overrides)
    return row


def _make_canon(**overrides: object) -> dict[str, object]:
    row = {
        "id": 51,
        "motif_key": "status_clash",
        "canon_level": "recurring_bit",
        "status": "active",
        "focus_char_ids": ["char_a", "char_b"],
        "focus_place_id": "music_room",
    }
    row.update(overrides)
    return row


def test_build_candidates_detects_status_flashpoint() -> None:
    evaluator = DramaticPressureEvaluator(
        story_id="ankoku_gakuen",
        intent_profile=get_story_intent_profile("ankoku_gakuen"),
    )

    candidates = evaluator.build_candidates(
        turn_number=14,
        hooks=[],
        tensions=[_make_tension()],
        patterns=[_make_pattern()],
        relationship_modes=[_make_mode()],
        canon_bits=[_make_canon()],
        active_episode=None,
        recent_closed_scenes=[],
        live_interventions=[],
    )

    status_flashpoints = [row for row in candidates if row.pressure_type == "status_flashpoint"]
    assert len(status_flashpoints) == 1
    assert status_flashpoints[0].focus_char_ids == ["char_a", "char_b"]
    assert status_flashpoints[0].score >= 0.55


def test_build_candidates_detects_payoff_ready_from_aged_episode() -> None:
    evaluator = DramaticPressureEvaluator(
        story_id="ankoku_gakuen",
        intent_profile=get_story_intent_profile("ankoku_gakuen"),
        min_pressure_score=0.0,
    )

    candidates = evaluator.build_candidates(
        turn_number=14,
        hooks=[],
        tensions=[],
        patterns=[_make_pattern()],
        relationship_modes=[],
        canon_bits=[_make_canon()],
        active_episode=_make_episode(opened_turn=10, last_progress_turn=13),
        recent_closed_scenes=[],
        live_interventions=[],
    )

    payoff_ready = [row for row in candidates if row.pressure_type == "payoff_ready"]
    assert len(payoff_ready) == 1
    assert payoff_ready[0].focus_place_id == "music_room"
    assert payoff_ready[0].score >= 0.0


def test_build_candidates_detects_showoff_flashpoint() -> None:
    evaluator = DramaticPressureEvaluator(
        story_id="ankoku_gakuen",
        intent_profile=get_story_intent_profile("ankoku_gakuen"),
        min_pressure_score=0.0,
    )

    candidates = evaluator.build_candidates(
        turn_number=16,
        hooks=[],
        tensions=[_make_tension(tension_type="rivalry", description="char_a と char_b が勝ち負けを競っている。")],
        patterns=[_make_pattern(pattern_type="bluff_or_showoff", description="char_a が大きく見せようとしている。")],
        relationship_modes=[_make_mode(mode_type="chaos_partner")],
        canon_bits=[_make_canon(motif_key="small_win_loss")],
        active_episode=None,
        recent_closed_scenes=[],
        live_interventions=[],
    )

    showoff = [row for row in candidates if row.pressure_type == "showoff_flashpoint"]
    assert len(showoff) == 1
    assert showoff[0].focus_char_ids == ["char_a", "char_b"]


def test_build_candidates_detects_role_reversal_ready() -> None:
    evaluator = DramaticPressureEvaluator(
        story_id="ankoku_gakuen",
        intent_profile=get_story_intent_profile("ankoku_gakuen"),
        min_pressure_score=0.0,
    )

    candidates = evaluator.build_candidates(
        turn_number=17,
        hooks=[],
        tensions=[_make_tension(tension_type="conflict", description="char_a と char_b の主導権が揺れている。")],
        patterns=[_make_pattern(pattern_type="role_reversal", description="いつもの力関係が崩れた。")],
        relationship_modes=[_make_mode(mode_type="irritated_respect")],
        canon_bits=[_make_canon(motif_key="irritated_respect")],
        active_episode=_make_episode(episode_type="status_clash"),
        recent_closed_scenes=[],
        live_interventions=[],
    )

    reversal = [row for row in candidates if row.pressure_type == "role_reversal_ready"]
    assert len(reversal) == 1
    assert reversal[0].focus_char_ids == ["char_a", "char_b"]


def test_build_candidates_detects_payoff_ready_from_canon_reignition_hook() -> None:
    evaluator = DramaticPressureEvaluator(
        story_id="ankoku_gakuen",
        intent_profile=get_story_intent_profile("ankoku_gakuen"),
        min_pressure_score=0.0,
    )

    candidates = evaluator.build_candidates(
        turn_number=13,
        hooks=[
            {
                "id": 61,
                "hook_type": "question",
                "status": "open",
                "owner_char_id": "char_a",
                "target_char_id": "char_b",
                "source_canon_bit_id": 51,
                "description": "前に戻った食い違いがまた残っている。",
            }
        ],
        tensions=[],
        patterns=[_make_pattern(recurrence_count=3)],
        relationship_modes=[],
        canon_bits=[_make_canon(canon_level="proto_canon")],
        active_episode=_make_episode(opened_turn=12, last_progress_turn=12),
        recent_closed_scenes=[],
        live_interventions=[],
    )

    payoff_ready = [row for row in candidates if row.pressure_type == "payoff_ready"]
    assert len(payoff_ready) == 1
    assert payoff_ready[0].source_canon_bit_id == 51


@async_to_sync
async def test_process_round_upserts_and_resolves_pressure() -> None:
    db = AsyncMock()
    db.get_open_story_hooks = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[_make_tension()])
    db.get_active_interaction_patterns = AsyncMock(return_value=[_make_pattern()])
    db.get_active_relationship_modes = AsyncMock(return_value=[_make_mode()])
    db.get_active_story_canon_bits = AsyncMock(return_value=[_make_canon()])
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_active_interventions = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "id": 91,
                    "status": "active",
                    "pressure_type": "status_flashpoint",
                    "dedupe_key": "tension:11:status_flashpoint",
                    "last_detected_turn": 14,
                    "score": 0.8,
                }
            ],
        ]
    )
    db.find_active_story_dramatic_pressure_by_dedupe_key = AsyncMock(
        side_effect=[
            None,
            {
                "id": 91,
                "status": "active",
                "pressure_type": "status_flashpoint",
                "dedupe_key": "tension:11:status_flashpoint",
                "last_detected_turn": 14,
                "score": 0.8,
            },
        ]
    )
    db.insert_story_dramatic_pressure = AsyncMock(return_value=91)
    db.update_story_dramatic_pressure = AsyncMock()
    db.resolve_story_dramatic_pressure = AsyncMock()

    evaluator = DramaticPressureEvaluator(
        db=db,
        story_id="ankoku_gakuen",
        intent_profile=get_story_intent_profile("ankoku_gakuen"),
    )

    await evaluator.process_round(14)
    await evaluator.process_round(18)

    db.insert_story_dramatic_pressure.assert_awaited_once()
    db.update_story_dramatic_pressure.assert_awaited()
    db.resolve_story_dramatic_pressure.assert_not_awaited()
