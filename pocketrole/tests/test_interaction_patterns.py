"""Tests for engine/interaction_patterns.py."""

from __future__ import annotations

from engine.interaction_patterns import InteractionPatternEngine


def test_detects_status_clash_from_conflict_tension() -> None:
    """conflict tension から status_clash pattern を抽出する。"""
    engine = InteractionPatternEngine()

    candidates = engine.build_candidates(
        turn_number=12,
        hooks=[],
        tensions=[
            {
                "id": 55,
                "tension_type": "conflict",
                "description": "char_a と char_b がぶつかっている。",
                "involved_chars": ["char_a", "char_b"],
                "status": "escalating",
            }
        ],
        closed_scenes=[],
        relationship_events=[],
    )

    assert len(candidates) == 1
    assert candidates[0].pattern_type == "status_clash"
    assert candidates[0].dedupe_key == "tension:55:status_clash"


def test_detects_role_reversal_from_scene_with_mode_evidence() -> None:
    engine = InteractionPatternEngine()

    candidates = engine.build_candidates(
        turn_number=18,
        hooks=[],
        tensions=[],
        closed_scenes=[
            {
                "id": 77,
                "outcome_summary": "普段と逆に char_b が主導権を握り、char_a が言わされる側へ回った。",
                "focus_char_ids": ["char_a", "char_b"],
            }
        ],
        relationship_events=[],
        relationship_modes=[
            {
                "mode_type": "irritated_respect",
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "status": "active",
            }
        ],
        canon_bits=[],
    )

    assert any(candidate.pattern_type == "role_reversal" for candidate in candidates)
    role_reversal = next(candidate for candidate in candidates if candidate.pattern_type == "role_reversal")
    assert role_reversal.dedupe_key == "scene:77:role_reversal"


def test_detects_bluff_or_showoff_from_rivalry_tension_with_chaos_partner_evidence() -> None:
    engine = InteractionPatternEngine()

    candidates = engine.build_candidates(
        turn_number=21,
        hooks=[],
        tensions=[
            {
                "id": 88,
                "tension_type": "rivalry",
                "description": "char_a がやってみせると大げさに見せつけている。",
                "involved_chars": ["char_a", "char_b"],
            }
        ],
        closed_scenes=[],
        relationship_events=[],
        relationship_modes=[
            {
                "mode_type": "chaos_partner",
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "status": "active",
            }
        ],
        canon_bits=[],
    )

    assert any(candidate.pattern_type == "bluff_or_showoff" for candidate in candidates)
    showoff = next(candidate for candidate in candidates if candidate.pattern_type == "bluff_or_showoff")
    assert showoff.dedupe_key == "tension:88:bluff_or_showoff"


def test_detects_role_reversal_from_scene_with_episode_focus_support() -> None:
    engine = InteractionPatternEngine()

    candidates = engine.build_candidates(
        turn_number=24,
        hooks=[],
        tensions=[],
        closed_scenes=[
            {
                "id": 91,
                "outcome_summary": "char_b が前に出て、char_a が押し返せないまま流れを渡した。",
                "focus_char_ids": ["char_a", "char_b"],
            }
        ],
        relationship_events=[],
        relationship_modes=[],
        canon_bits=[],
        active_episode={
            "id": 14,
            "episode_type": "status_clash",
            "focus_char_ids": ["char_a", "char_b"],
        },
    )

    assert any(candidate.pattern_type == "role_reversal" for candidate in candidates)
