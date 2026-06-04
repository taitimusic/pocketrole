"""tools.generate_story_review_draft のテスト。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from db.db_manager import DatabaseManager
from tests._async_harness import async_to_sync
from tools.generate_story_review_draft import (
    EXIT_OK,
    EXIT_STORY_NOT_FOUND,
    _collect_cause_inputs,
    _detect_causes,
    _detect_gaps,
    _parse_latest_monitor_snapshot,
    _resolve_monitor_file,
    generate_story_review_draft,
    parse_args,
    render_review_markdown,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_STORY_ID = "ankoku_gakuen"


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title, world_rules, last_sim_time) VALUES (?, ?, ?, ?);",
        (_STORY_ID, "暗黒学園", "rule", "2025-04-01T10:00"),
    )
    await manager._conn.executemany(
        "INSERT INTO characters (id, story_id, name_ja) VALUES (?, ?, ?);",
        [
            ("char_a", _STORY_ID, "A"),
            ("char_b", _STORY_ID, "B"),
            ("char_c", _STORY_ID, "C"),
        ],
    )
    await manager._conn.commit()
    try:
        yield manager
    finally:
        await manager.close()


async def _seed_strength_case(db: DatabaseManager) -> None:
    scene_id = await db.insert_story_scene(
        _STORY_ID,
        {
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "rooftop",
            "opened_turn": 10,
            "closed_turn": 11,
        },
    )
    log_id = await db.insert_chat_log(
        _STORY_ID,
        {
            "sim_datetime": "2025-04-01T09:00",
            "turn_number": 10,
            "char_id": "char_a",
            "msg_type": "reply",
            "target_char_id": "char_b",
            "place_id": "rooftop",
            "message": "返事する。",
            "scene_id": scene_id,
        },
    )
    await db.insert_chat_log(
        _STORY_ID,
        {
            "sim_datetime": "2025-04-01T09:00",
            "turn_number": 10,
            "char_id": "char_b",
            "msg_type": "group",
            "place_id": "rooftop",
            "message": "みんなに言う。",
            "scene_id": scene_id,
        },
    )
    await db.insert_chat_log(
        _STORY_ID,
        {
            "sim_datetime": "2025-04-01T09:30",
            "turn_number": 11,
            "char_id": "char_c",
            "msg_type": "monologue",
            "place_id": "hallway",
            "message": "独白する。",
        },
    )
    hook_id = await db.insert_story_hook(
        _STORY_ID,
        {
            "hook_type": "question",
            "status": "open",
            "title": "答え待ち",
            "description": "返事を求める。",
            "source_scene_id": scene_id,
            "source_log_id": log_id,
        },
    )
    await db.resolve_story_hook(
        hook_id,
        resolution_log_id=log_id,
        resolved_turn=11,
        summary="返事が返ってきた。",
    )
    await db.insert_tension(
        _STORY_ID,
        {
            "tension_type": "conflict",
            "description": "すでに解決へ向かっている。",
            "detected_turn": 10,
            "status": "resolved",
            "resolved_turn": 11,
        },
    )
    await db.insert_relationship_event(
        _STORY_ID,
        {
            "char_id_from": "char_a",
            "char_id_to": "char_b",
            "event_type": "support",
            "delta_trust": 0.05,
            "summary": "距離が縮んだ。",
            "scene_id": scene_id,
            "turn_number": 10,
        },
    )
    await db.insert_evolution(
        _STORY_ID,
        {
            "char_id": "char_a",
            "turn_number": 11,
            "field": "current_goal",
            "previous_value": "様子を見る",
            "new_value": "踏み込む",
            "reason": "覚悟が固まった。",
        },
    )
    arc_id = await db.insert_arc(
        _STORY_ID,
        {
            "arc_type": "scene",
            "title": "屋上の対話",
            "summary": "屋上で距離が縮んだ。",
            "turn_from": 10,
            "turn_to": 11,
            "source_scene_id": scene_id,
        },
    )
    await db.insert_novel_output(
        _STORY_ID,
        arc_id,
        {
            "content_type": "scene_close",
            "content": "scene prose",
            "source_log_ids": [log_id],
            "ordering": 1,
        },
    )
    episode_id = await db.insert_story_episode(
        _STORY_ID,
        {
            "episode_type": "status_clash",
            "goal": "屋上の張り合いを前に出す",
            "focus_char_ids": ["char_a", "char_b"],
            "carry_over_hook_ids": [],
            "opened_turn": 10,
            "last_progress_turn": 11,
        },
    )
    episode_arc_id = await db.insert_arc(
        _STORY_ID,
        {
            "arc_type": "episode",
            "title": "短期エピソード",
            "summary": "屋上の張り合いが前に出た。",
            "turn_from": 10,
            "turn_to": 11,
        },
    )
    await db.insert_novel_output(
        _STORY_ID,
        episode_arc_id,
        {
            "content_type": "episode_close",
            "content": "episode prose",
            "ordering": 1,
        },
    )
    await db.close_story_episode(
        episode_id,
        closed_turn=11,
        exit_condition="resolved",
        summary="屋上の張り合いが一段落した。",
    )
    await db.insert_story_episode(
        _STORY_ID,
        {
            "episode_type": "mystery",
            "goal": "次の火種を追う",
            "focus_char_ids": ["char_c"],
            "carry_over_hook_ids": [],
            "opened_turn": 11,
            "last_progress_turn": 11,
        },
    )


async def _seed_gap_case(db: DatabaseManager) -> None:
    scene_id = await db.insert_story_scene(
        _STORY_ID,
        {
            "scene_type": "conversation",
            "status": "closed",
            "place_id": "classroom",
            "opened_turn": 20,
            "closed_turn": 20,
        },
    )
    for char_id in ("char_a", "char_b", "char_c"):
        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T10:00",
                "turn_number": 20,
                "char_id": char_id,
                "msg_type": "group",
                "place_id": "classroom",
                "message": f"{char_id} が長めに話す。",
                "scene_id": scene_id,
            },
        )
    await db.insert_story_hook(
        _STORY_ID,
        {
            "hook_type": "conflict",
            "status": "open",
            "title": "未解決の衝突",
            "description": "教室の空気が悪い。",
            "source_scene_id": scene_id,
        },
    )
    await db.insert_tension(
        _STORY_ID,
        {
            "tension_type": "conflict",
            "description": "まだ燻っている。",
            "detected_turn": 16,
            "status": "escalating",
        },
    )
    await db.insert_generation_quality_issue(
        _STORY_ID,
        {
            "issue_type": "abstract_loop",
            "severity": "warning",
            "details": {"token": "誰か"},
            "auto_action": "retry",
            "created_turn": 20,
        },
    )
    for idx in range(3):
        await db.insert_character_growth_candidate(
            _STORY_ID,
            "char_a",
            {
                "field": "current_goal",
                "candidate_value": f"変化{idx}",
                "reason": "保留中",
                "experience_score": 0.4,
                "identity_impact_score": 0.5,
                "confidence": 0.5,
                "detected_turn": 20,
                "status": "pending",
            },
        )


def test_parse_args_defaults() -> None:
    ns = parse_args(["--story", _STORY_ID])
    assert ns.story == _STORY_ID
    assert ns.window_turns == 20
    assert ns.monitor_file is None


def test_resolve_monitor_file_picks_latest_by_mtime(tmp_path: Path) -> None:
    older = tmp_path / f"{_STORY_ID}_runtime_monitor_older.md"
    newer = tmp_path / f"{_STORY_ID}_runtime_monitor_newer.md"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    older.touch()
    newer.touch()

    resolved = _resolve_monitor_file(_STORY_ID, monitor_file=None, monitor_dir=tmp_path)

    assert resolved == newer


def test_parse_latest_monitor_snapshot_extracts_warning_and_error_lines(tmp_path: Path) -> None:
    monitor = tmp_path / "monitor.md"
    monitor.write_text(
        "\n".join(
            [
                "# Runtime Monitor",
                "",
                "## Snapshot 2026-03-24T10:00:00+09:00",
                "### Warnings",
                "- turn_stalled | engine_stopped",
                "- Recent warnings/errors: sqlite timeout | ollama retry",
                "",
            ]
        ),
        encoding="utf-8",
    )

    parsed = _parse_latest_monitor_snapshot(monitor)

    assert parsed["monitor_captured_at"] == "2026-03-24T10:00:00+09:00"
    assert parsed["monitor_warnings"] == ["turn_stalled", "engine_stopped"]
    assert parsed["monitor_recent_errors"] == ["sqlite timeout", "ollama retry"]


def test_detect_gaps_includes_sessionless_monologue_high() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.22,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 0,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 0,
        "active_relationship_modes": 0,
        "relationship_mode_reinforcements_recent": 0,
        "relationship_mode_decay_updates_recent": 0,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": [],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 0,
        "live_interventions": 0,
        "quality_retry_rate": 0.0,
        "quality_issues_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "closed_scenes_recent": 0,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
        "sessionless_monologues_recent": 6,
        "sessionless_monologue_ratio": 0.60,
        "solo_scenes_recent": 2,
        "active_interaction_patterns": 1,
        "active_episode_id": None,
        "active_episode_type": None,
        "active_episode_age": 0,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "sessionless_monologue_high" in gap_codes
    assert "episode_missing" in gap_codes


def test_detect_gaps_flags_missing_relationship_mode_when_events_exist() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.30,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 0,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 2,
        "active_relationship_modes": 0,
        "relationship_mode_reinforcements_recent": 0,
        "relationship_mode_decay_updates_recent": 0,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": [],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 0,
        "live_interventions": 0,
        "quality_retry_rate": 0.0,
        "quality_issues_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "closed_scenes_recent": 0,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
        "sessionless_monologues_recent": 1,
        "sessionless_monologue_ratio": 0.10,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 1,
        "patterns_detected_recent": 1,
        "active_episode_id": 9,
        "active_episode_type": "status_clash",
        "active_episode_age": 3,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 0,
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "relationship_mode_missing" in gap_codes


def test_detect_gaps_flags_relationship_mode_flat_when_pair_is_single_layer() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.32,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 0,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 2,
        "active_relationship_modes": 2,
        "relationship_mode_reinforcements_recent": 1,
        "relationship_mode_decay_updates_recent": 0,
        "multi_mode_pairs_active": 0,
        "complementary_mode_pairs_active": 0,
        "single_mode_pairs_active": 2,
        "complementary_mode_reinforcements_recent": 0,
        "complementary_mode_decay_updates_recent": 2,
        "complementary_mode_deactivations_recent": 1,
        "relationship_mode_conflict_attention_pairs": 0,
        "dominant_relationship_modes": ["irritated_respect"],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 0,
        "live_interventions": 0,
        "quality_retry_rate": 0.0,
        "quality_issues_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "closed_scenes_recent": 0,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
        "sessionless_monologues_recent": 0,
        "sessionless_monologue_ratio": 0.0,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 1,
        "patterns_detected_recent": 1,
        "active_episode_id": 9,
        "active_episode_type": "status_clash",
        "active_episode_age": 3,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 0,
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "relationship_mode_flat" in gap_codes
    gap_map = dict(gaps)
    assert "complementary_mode_pairs_active=0" in gap_map["relationship_mode_flat"]
    assert "single_mode_pairs_active=2" in gap_map["relationship_mode_flat"]
    assert "complementary_mode_deactivations_recent=1" in gap_map["relationship_mode_flat"]


def test_detect_gaps_flags_growth_parse_and_rollback_issues() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.30,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 0,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 0,
        "active_relationship_modes": 0,
        "relationship_mode_reinforcements_recent": 0,
        "relationship_mode_decay_updates_recent": 0,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": [],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 0,
        "live_interventions": 0,
        "quality_retry_rate": 0.0,
        "quality_issues_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "growth_parse_failures_recent": 2,
        "growth_rollbacks_recent": 1,
        "closed_scenes_recent": 0,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
        "sessionless_monologues_recent": 0,
        "sessionless_monologue_ratio": 0.0,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 0,
        "patterns_detected_recent": 0,
        "active_episode_id": 1,
        "active_episode_type": "status_clash",
        "active_episode_age": 2,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 0,
        "active_canon_bits": 0,
        "canon_reinforcements_recent": 0,
        "canon_promotions_recent": 0,
        "dominant_canon_levels": [],
        "dominant_canon_motifs": [],
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "growth_parse_failed" in gap_codes
    assert "growth_write_rolled_back" in gap_codes


def test_detect_gaps_flags_payoff_push_weak_for_new_pressure_families() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.30,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 1,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 0,
        "active_relationship_modes": 1,
        "relationship_mode_reinforcements_recent": 1,
        "relationship_mode_decay_updates_recent": 0,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": ["irritated_respect"],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 1,
        "live_interventions": 0,
        "quality_retry_rate": 0.0,
        "quality_issues_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "closed_scenes_recent": 0,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
        "sessionless_monologues_recent": 0,
        "sessionless_monologue_ratio": 0.0,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 2,
        "patterns_detected_recent": 2,
        "active_episode_id": 9,
        "active_episode_type": "status_clash",
        "active_episode_age": 4,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 1,
        "active_canon_bits": 1,
        "canon_reinforcements_recent": 1,
        "canon_promotions_recent": 0,
        "dominant_canon_levels": ["recurring_bit"],
        "canon_reignitions_recent": 0,
        "canon_triggered_hooks_open": 0,
        "active_dramatic_pressures": 1,
        "pressure_reinforcements_recent": 1,
        "dominant_pressure_types": ["showoff_flashpoint"],
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "payoff_push_weak" in gap_codes


def test_detect_gaps_flags_quality_fallback_high() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.30,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 0,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 0,
        "active_relationship_modes": 0,
        "relationship_mode_reinforcements_recent": 0,
        "relationship_mode_decay_updates_recent": 0,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": [],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 0,
        "live_interventions": 0,
        "quality_retry_rate": 0.05,
        "quality_issues_recent": 1,
        "quality_normalizations_recent": 2,
        "quality_fallbacks_recent": 3,
        "reply_quality_normalizations_recent": 1,
        "reply_quality_fallbacks_recent": 2,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "growth_parse_failures_recent": 0,
        "growth_rollbacks_recent": 0,
        "closed_scenes_recent": 0,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
        "sessionless_monologues_recent": 0,
        "sessionless_monologue_ratio": 0.0,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 0,
        "patterns_detected_recent": 0,
        "active_episode_id": 1,
        "active_episode_type": "status_clash",
        "active_episode_age": 2,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 0,
        "active_canon_bits": 0,
        "canon_reinforcements_recent": 0,
        "canon_promotions_recent": 0,
        "dominant_canon_levels": [],
        "dominant_canon_motifs": [],
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "quality_fallback_high" in gap_codes
    assert "reply_quality_fallback_high" in gap_codes


def test_detect_gaps_flags_reply_quality_flat() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.30,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 0,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 0,
        "active_relationship_modes": 0,
        "relationship_mode_reinforcements_recent": 0,
        "relationship_mode_decay_updates_recent": 0,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": [],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 0,
        "live_interventions": 0,
        "quality_retry_rate": 0.05,
        "quality_issues_recent": 1,
        "quality_normalizations_recent": 1,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 1,
        "reply_quality_fallbacks_recent": 0,
        "reply_focus_misses_recent": 2,
        "generic_reply_tails_recent": 1,
        "voice_flat_replies_recent": 1,
        "reply_flat_generic_tail_recent": 1,
        "reply_flat_voice_recent": 1,
        "reply_flat_reused_tail_recent": 1,
        "reply_variety_press_recent": 2,
        "reply_variety_condition_recent": 0,
        "reply_variety_redirect_recent": 0,
        "reply_dramatic_move_missing_recent": 1,
        "reply_soft_landing_recent": 1,
        "reply_shape_dominance_recent": 1,
        "reply_second_beat_reused_recent": 2,
        "reply_bland_shape_reused_recent": 1,
        "reply_pressure_shift_missing_recent": 1,
        "reply_story_flavor_weak_recent": 0,
        "reply_bland_keep_blocked_recent": 0,
        "reply_quality_keep_blocked_recent": 0,
        "reply_story_quality_keep_blocked_recent": 1,
        "reply_residual_keep_blocked_recent": 1,
        "reply_reused_opening_recent": 0,
        "reply_reused_ending_recent": 1,
        "reply_reused_second_beat_recent": 2,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "growth_parse_failures_recent": 0,
        "growth_rollbacks_recent": 0,
        "closed_scenes_recent": 0,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
        "sessionless_monologues_recent": 0,
        "sessionless_monologue_ratio": 0.0,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 0,
        "patterns_detected_recent": 0,
        "active_episode_id": 1,
        "active_episode_type": "status_clash",
        "active_episode_age": 2,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 0,
        "active_canon_bits": 0,
        "canon_reinforcements_recent": 0,
        "canon_promotions_recent": 0,
        "dominant_canon_levels": [],
        "dominant_canon_motifs": [],
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "reply_quality_flat" in gap_codes
    gap_map = dict(gaps)
    assert "reply_flat_generic_tail_recent" in gap_map["reply_quality_flat"]
    assert "reply_flat_voice_recent" in gap_map["reply_quality_flat"]
    assert "reply_flat_reused_tail_recent" in gap_map["reply_quality_flat"]
    assert "reply_reused_second_beat_recent=2" in gap_map["reply_quality_flat"]
    assert "reply_second_beat_reused_recent=2" in gap_map["reply_quality_flat"]
    assert "reply_story_quality_keep_blocked_recent=1" in gap_map["reply_quality_flat"]
    assert "reply_residual_keep_blocked_recent=1" in gap_map["reply_quality_flat"]


def test_detect_gaps_flags_scene_close_weak_and_chapter_director_stall() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.31,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 1,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 1,
        "active_relationship_modes": 1,
        "relationship_mode_reinforcements_recent": 0,
        "relationship_mode_decay_updates_recent": 1,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": ["irritated_respect"],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 1,
        "live_interventions": 0,
        "quality_retry_rate": 0.0,
        "quality_issues_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "reply_focus_misses_recent": 0,
        "generic_reply_tails_recent": 0,
        "voice_flat_replies_recent": 0,
        "reply_variety_press_recent": 3,
        "reply_variety_condition_recent": 0,
        "reply_variety_redirect_recent": 0,
        "reply_dramatic_move_missing_recent": 2,
        "reply_soft_landing_recent": 2,
        "reply_shape_dominance_recent": 1,
        "reply_bland_shape_reused_recent": 2,
        "reply_pressure_shift_missing_recent": 1,
        "reply_story_flavor_weak_recent": 2,
        "reply_bland_keep_blocked_recent": 1,
        "reply_story_quality_keep_blocked_recent": 1,
        "reply_reused_opening_recent": 1,
        "reply_reused_ending_recent": 2,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "growth_empty_recent": 0,
        "growth_quality_rejections_recent": 0,
        "growth_parse_failures_recent": 0,
        "growth_rollbacks_recent": 0,
        "closed_scenes_recent": 4,
        "scene_close_completion_rate": 0.25,
        "scene_arcs_recent": 1,
        "novel_outputs_recent": 1,
        "closed_scenes_without_arc_recent": 2,
        "scene_arcs_without_novel_recent": 1,
        "scene_close_backlog_recent": 3,
        "scene_close_missing_arc_recent": 2,
        "scene_close_missing_novel_recent": 1,
        "scene_close_backlog_recovered_recent": 1,
        "sessionless_monologues_recent": 0,
        "sessionless_monologue_ratio": 0.0,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 1,
        "patterns_detected_recent": 1,
        "active_episode_id": 1,
        "active_episode_type": "status_clash",
        "active_episode_age": 3,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 1,
        "active_canon_bits": 0,
        "canon_reinforcements_recent": 0,
        "canon_promotions_recent": 0,
        "dominant_canon_levels": [],
        "dominant_canon_motifs": [],
        "active_dramatic_pressures": 1,
        "pressure_reinforcements_recent": 1,
        "dominant_pressure_types": ["payoff_ready"],
        "active_chapter_id": "festival_arc",
        "active_chapter_beat": 1,
        "active_chapter_opened_turn": 5,
        "active_chapter_age": 27,
        "director_satisfaction_overall": 0.35,
        "director_satisfaction_trend": "falling",
        "director_satisfaction_tension": 0.28,
        "director_satisfaction_pacing": 0.22,
        "director_satisfaction_surprise": 0.24,
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]

    assert "scene_close_weak" in gap_codes
    gap_map = dict(gaps)
    assert "scene_close_missing_arc_recent=2" in gap_map["scene_close_weak"]
    assert "scene_close_missing_novel_recent=1" in gap_map["scene_close_weak"]
    assert "scene_close_backlog_recovered_recent=1" in gap_map["scene_close_weak"]
    assert "reply_variety_press_recent=3" in gap_map["reply_quality_flat"]
    assert "reply_dramatic_move_missing_recent=2" in gap_map["reply_quality_flat"]
    assert "reply_soft_landing_recent=2" in gap_map["reply_quality_flat"]
    assert "reply_bland_shape_reused_recent=2" in gap_map["reply_quality_flat"]
    assert "reply_pressure_shift_missing_recent=1" in gap_map["reply_quality_flat"]
    assert "reply_story_flavor_weak_recent=2" in gap_map["reply_quality_flat"]
    assert "reply_story_quality_keep_blocked_recent=1" in gap_map["reply_quality_flat"]
    assert "reply_reused_opening_recent=1" in gap_map["reply_quality_flat"]
    assert "reply_reused_ending_recent=2" in gap_map["reply_quality_flat"]


def test_detect_causes_signal_visibility_mentions_variety_bias() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.24,
        "all_chars_spoke_ratio": 0.45,
        "sessionless_monologue_ratio": 0.20,
        "active_interaction_patterns": 4,
        "active_relationship_modes": 2,
        "active_canon_bits": 2,
        "active_dramatic_pressures": 1,
        "reply_focus_misses_recent": 1,
        "generic_reply_tails_recent": 1,
        "voice_flat_replies_recent": 1,
        "reply_quality_fallbacks_recent": 0,
        "quality_fallbacks_recent": 0,
        "quality_normalizations_recent": 1,
        "quality_retry_rate": 0.05,
        "reply_variety_press_recent": 4,
        "reply_variety_condition_recent": 0,
        "reply_variety_redirect_recent": 0,
        "reply_dramatic_move_missing_recent": 3,
        "reply_soft_landing_recent": 2,
        "reply_shape_dominance_recent": 2,
        "reply_reused_opening_recent": 2,
        "reply_reused_ending_recent": 3,
        "signal_visibility_misses_recent": 2,
        "objective_visibility_misses_recent": 1,
        "signal_visibility_retry_recent": 1,
        "objective_visibility_retry_recent": 0,
        "active_episode_id": 7,
        "active_episode_type": "status_clash",
        "episodes_closed_recent": 0,
        "hooks_resolved_recent": 0,
        "growth_empty_recent": 0,
        "growth_quality_rejections_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
    }
    cause_inputs = {
        "recent_reply_logs": 6,
        "target_specific_reply_ratio": 0.30,
        "generic_scene_objective_ratio": 0.25,
        "canon_triggered_unresolved_hooks": 0,
        "growth_pending_commit_ratio": 0.0,
        "growth_rejection_empty_ratio": 0.0,
    }

    causes = _detect_causes(snapshot, cause_inputs)

    signal_cause = next(cause for cause in causes if cause["code"] == "signal_visibility_weak")
    assert "reply_variety_press_recent=4" in signal_cause["evidence"]
    assert "reply_dramatic_move_missing_recent=3" in signal_cause["evidence"]
    assert "reply_reused_ending_recent=3" in signal_cause["evidence"]
    assert "signal_visibility_misses_recent=2" in signal_cause["evidence"]
    assert "objective_visibility_misses_recent=1" in signal_cause["evidence"]


def test_detect_gaps_flags_growth_quality_flat() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.32,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 2,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 2,
        "active_relationship_modes": 1,
        "relationship_mode_reinforcements_recent": 1,
        "relationship_mode_decay_updates_recent": 0,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": ["unsafe_confidant"],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 1,
        "live_interventions": 0,
        "quality_retry_rate": 0.0,
        "quality_issues_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "growth_empty_recent": 2,
        "growth_quality_rejections_recent": 3,
        "growth_parse_failures_recent": 0,
        "growth_rollbacks_recent": 0,
        "closed_scenes_recent": 1,
        "scene_arcs_recent": 1,
        "novel_outputs_recent": 1,
        "sessionless_monologues_recent": 0,
        "sessionless_monologue_ratio": 0.0,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 2,
        "patterns_detected_recent": 2,
        "active_episode_id": 1,
        "active_episode_type": "near_reveal",
        "active_episode_age": 5,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 1,
        "active_canon_bits": 1,
        "canon_reinforcements_recent": 1,
        "canon_promotions_recent": 0,
        "dominant_canon_levels": ["proto_canon"],
        "dominant_canon_motifs": ["near_reveal"],
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "growth_quality_flat" in gap_codes


def test_detect_gaps_does_not_flag_comic_pattern_mix_for_mystery_story() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "story_intent_mode": "mystery_drama",
        "reply_ratio": 0.35,
        "monologue_ratio": 0.20,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 0,
        "hooks_resolved_recent": 1,
        "relationship_pairs_changed_recent": 0,
        "active_relationship_modes": 0,
        "relationship_mode_reinforcements_recent": 0,
        "dominant_relationship_modes": [],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 0,
        "live_interventions": 0,
        "quality_retry_rate": 0.0,
        "quality_issues_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "closed_scenes_recent": 0,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
        "sessionless_monologues_recent": 1,
        "sessionless_monologue_ratio": 0.10,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 3,
        "patterns_detected_recent": 3,
        "dominant_pattern_types": ["status_clash", "small_win_loss"],
        "active_episode_id": 9,
        "active_episode_type": "status_clash",
        "active_episode_age": 3,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 0,
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "story_intent_pattern_mix_flat" not in gap_codes
    assert "pressure_missing" in gap_codes


def test_detect_gaps_flags_missing_pressure_when_layers_exist() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.30,
        "all_chars_spoke_ratio": 0.40,
        "open_hooks": 0,
        "hooks_resolved_recent": 0,
        "relationship_pairs_changed_recent": 1,
        "active_relationship_modes": 1,
        "relationship_mode_reinforcements_recent": 1,
        "dominant_relationship_modes": ["irritated_respect"],
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "intervention_eligible_now": 0,
        "live_interventions": 0,
        "quality_retry_rate": 0.0,
        "quality_issues_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "growth_parse_failures_recent": 0,
        "growth_rollbacks_recent": 0,
        "closed_scenes_recent": 0,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
        "sessionless_monologues_recent": 0,
        "sessionless_monologue_ratio": 0.0,
        "solo_scenes_recent": 0,
        "active_interaction_patterns": 1,
        "patterns_detected_recent": 1,
        "active_dramatic_pressures": 0,
        "pressure_reinforcements_recent": 0,
        "dominant_pressure_types": [],
        "max_pressure_score": 0.0,
        "active_episode_id": 1,
        "active_episode_type": "status_clash",
        "active_episode_age": 4,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "active_tensions": 1,
        "active_canon_bits": 1,
        "canon_reinforcements_recent": 1,
        "canon_promotions_recent": 0,
        "dominant_canon_levels": ["recurring_bit"],
        "dominant_canon_motifs": ["status_clash"],
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}

    gaps = _detect_gaps(snapshot, monitor)
    gap_codes = [code for code, _message in gaps]
    assert "pressure_missing" in gap_codes


def test_detect_causes_prioritizes_signal_visibility_and_quality_gate() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.24,
        "all_chars_spoke_ratio": 0.45,
        "sessionless_monologue_ratio": 0.20,
        "active_interaction_patterns": 4,
        "active_relationship_modes": 2,
        "active_canon_bits": 2,
        "active_dramatic_pressures": 1,
        "reply_focus_misses_recent": 3,
        "generic_reply_tails_recent": 2,
        "voice_flat_replies_recent": 1,
        "reply_quality_fallbacks_recent": 1,
        "quality_fallbacks_recent": 2,
        "quality_normalizations_recent": 3,
        "quality_retry_rate": 0.18,
        "active_episode_id": 7,
        "active_episode_type": "status_clash",
        "episodes_closed_recent": 0,
        "hooks_resolved_recent": 0,
        "growth_empty_recent": 0,
        "growth_quality_rejections_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
    }
    cause_inputs = {
        "recent_reply_logs": 6,
        "target_specific_reply_ratio": 0.16,
        "generic_scene_objective_ratio": 0.25,
        "canon_triggered_unresolved_hooks": 0,
        "growth_pending_commit_ratio": 0.0,
        "growth_rejection_empty_ratio": 0.0,
    }

    causes = _detect_causes(snapshot, cause_inputs)

    assert causes
    assert causes[0]["code"] == "signal_visibility_weak"
    assert any(cause["code"] == "quality_gate_suppression" for cause in causes[:3])
    assert all("why" in cause and "evidence" in cause and "experiment" in cause for cause in causes)


def test_detect_causes_flags_canon_and_growth_stall() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.31,
        "all_chars_spoke_ratio": 0.44,
        "sessionless_monologue_ratio": 0.10,
        "active_interaction_patterns": 2,
        "active_relationship_modes": 1,
        "active_canon_bits": 3,
        "canon_reignitions_recent": 0,
        "active_canon_profile_overlays": 0,
        "active_dramatic_pressures": 1,
        "quality_fallbacks_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_retry_rate": 0.0,
        "reply_focus_misses_recent": 0,
        "generic_reply_tails_recent": 0,
        "voice_flat_replies_recent": 0,
        "growth_empty_recent": 2,
        "growth_quality_rejections_recent": 3,
        "growth_candidates_pending": 4,
        "growth_commits_recent": 0,
        "active_episode_id": 9,
        "active_episode_type": "near_reveal",
        "episodes_closed_recent": 0,
        "hooks_resolved_recent": 0,
    }
    cause_inputs = {
        "recent_reply_logs": 4,
        "target_specific_reply_ratio": 0.75,
        "generic_scene_objective_ratio": 0.10,
        "canon_triggered_unresolved_hooks": 2,
        "growth_pending_commit_ratio": 4.0,
        "growth_rejection_empty_ratio": 5.0,
    }

    causes = _detect_causes(snapshot, cause_inputs)
    top_codes = [cause["code"] for cause in causes[:3]]

    assert "canon_carryover_weak" in top_codes
    assert "growth_not_sticking" in top_codes


def test_detect_causes_flags_chapter_and_director_alignment() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.30,
        "all_chars_spoke_ratio": 0.40,
        "sessionless_monologue_ratio": 0.10,
        "active_interaction_patterns": 1,
        "active_relationship_modes": 1,
        "active_canon_bits": 0,
        "active_dramatic_pressures": 1,
        "reply_focus_misses_recent": 0,
        "generic_reply_tails_recent": 0,
        "voice_flat_replies_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "quality_fallbacks_recent": 0,
        "quality_normalizations_recent": 0,
        "quality_retry_rate": 0.0,
        "active_episode_id": 7,
        "active_episode_type": "status_clash",
        "episodes_closed_recent": 0,
        "hooks_resolved_recent": 0,
        "growth_empty_recent": 0,
        "growth_quality_rejections_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "active_chapter_id": "festival_arc",
        "active_chapter_beat": 1,
        "active_chapter_age": 24,
        "open_hooks": 2,
        "active_tensions": 1,
        "live_interventions": 0,
        "scene_close_completion_rate": 0.25,
        "director_satisfaction_overall": 0.31,
        "director_satisfaction_tension": 0.28,
        "director_satisfaction_pacing": 0.26,
        "director_satisfaction_surprise": 0.22,
    }
    cause_inputs = {
        "recent_reply_logs": 4,
        "target_specific_reply_ratio": 0.70,
        "generic_scene_objective_ratio": 0.15,
        "canon_triggered_unresolved_hooks": 0,
        "growth_pending_commit_ratio": 0.0,
        "growth_rejection_empty_ratio": 0.0,
    }

    causes = _detect_causes(snapshot, cause_inputs)
    top_codes = [cause["code"] for cause in causes[:3]]

    assert "chapter_pressure_not_converting" in top_codes
    assert "director_alignment_low" in top_codes


def test_detect_causes_quality_gate_mentions_reply_fallback_root_causes() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "reply_ratio": 0.28,
        "all_chars_spoke_ratio": 0.44,
        "sessionless_monologue_ratio": 0.10,
        "active_interaction_patterns": 1,
        "active_relationship_modes": 1,
        "active_canon_bits": 0,
        "active_dramatic_pressures": 0,
        "reply_focus_misses_recent": 1,
        "generic_reply_tails_recent": 0,
        "voice_flat_replies_recent": 0,
        "reply_quality_fallbacks_recent": 3,
        "reply_fallback_focus_missing_recent": 2,
        "reply_fallback_direct_reaction_recent": 1,
        "reply_retry_kept_recent": 4,
        "quality_fallbacks_recent": 1,
        "quality_normalizations_recent": 1,
        "quality_retry_rate": 0.18,
        "active_episode_id": 7,
        "active_episode_type": "status_clash",
        "episodes_closed_recent": 0,
        "hooks_resolved_recent": 0,
        "growth_empty_recent": 0,
        "growth_quality_rejections_recent": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
    }
    cause_inputs = {
        "recent_reply_logs": 6,
        "target_specific_reply_ratio": 0.30,
        "generic_scene_objective_ratio": 0.10,
        "canon_triggered_unresolved_hooks": 0,
        "growth_pending_commit_ratio": 0.0,
        "growth_rejection_empty_ratio": 0.0,
    }

    causes = _detect_causes(snapshot, cause_inputs)
    quality_cause = next(cause for cause in causes if cause["code"] == "quality_gate_suppression")

    assert "reply_fallback_focus_missing_recent=2" in quality_cause["evidence"]
    assert "reply_fallback_direct_reaction_recent=1" in quality_cause["evidence"]
    assert "reply_retry_kept_recent=4" in quality_cause["evidence"]


@async_to_sync
async def test_collect_cause_inputs_does_not_double_count_growth_llm_empty(tmp_path: Path) -> None:
    async with _make_db() as db:
        await _seed_gap_case(db)
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "growth_llm_empty",
                "severity": "warning",
                "details": {"char_id": "char_a"},
                "auto_action": "skip",
                "created_turn": 20,
            },
        )

        cause_inputs = await _collect_cause_inputs(db, _STORY_ID, window_turns=5)

    assert cause_inputs["growth_rejection_empty_ratio"] == 0.2


def test_render_review_markdown_keeps_recommendations_and_experiments_separate() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "min_turn_number": 1,
        "max_turn_number": 20,
        "chat_log_count": 20,
        "active_character_count": 3,
        "last_char_id": "char_a",
        "last_message_len": 24,
        "last_sim_datetime": "2025-04-01T10:00",
        "story_intent_mode": "comic_ensemble",
        "active_interaction_patterns": 2,
        "patterns_detected_recent": 2,
        "pattern_recurrences_recent": 1,
        "dominant_pattern_types": ["status_clash"],
        "relationship_pairs_changed_recent": 1,
        "active_relationship_modes": 1,
        "relationship_mode_reinforcements_recent": 1,
        "relationship_mode_decay_updates_recent": 0,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": ["irritated_respect"],
        "active_canon_bits": 1,
        "canon_reinforcements_recent": 1,
        "canon_promotions_recent": 0,
        "canon_reignitions_recent": 0,
        "canon_triggered_hooks_open": 0,
        "active_canon_profile_overlays": 0,
        "canon_writebacks_recent": 0,
        "dominant_canon_levels": ["recurring_bit"],
        "dominant_canon_motifs": ["status_clash"],
        "active_dramatic_pressures": 1,
        "pressure_reinforcements_recent": 1,
        "dominant_pressure_types": ["status_flashpoint"],
        "max_pressure_score": 0.82,
        "active_episode_id": 7,
        "active_episode_type": "status_clash",
        "active_episode_goal": "張り合いを前に出す",
        "active_episode_age": 5,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "reply_ratio": 0.24,
        "group_ratio": 0.10,
        "monologue_ratio": 0.30,
        "speaking_chars_per_turn_avg": 2.0,
        "all_chars_spoke_ratio": 0.45,
        "sessionless_monologues_recent": 1,
        "sessionless_monologue_ratio": 0.20,
        "active_scenes": 1,
        "closed_scenes_recent": 0,
        "open_hooks": 1,
        "hooks_created_recent": 1,
        "hooks_resolved_recent": 0,
        "active_tensions": 1,
        "intervention_eligible_now": 1,
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "resolved_tensions_recent": 0,
        "live_interventions": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "growth_empty_recent": 0,
        "growth_quality_rejections_recent": 0,
        "growth_parse_failures_recent": 0,
        "growth_rollbacks_recent": 0,
        "quality_issues_recent": 4,
        "quality_retry_rate": 0.18,
        "quality_normalizations_recent": 4,
        "quality_fallbacks_recent": 1,
        "reply_quality_normalizations_recent": 2,
        "reply_quality_fallbacks_recent": 1,
        "reply_focus_misses_recent": 3,
        "generic_reply_tails_recent": 2,
        "voice_flat_replies_recent": 1,
        "scene_arcs_recent": 0,
        "novel_outputs_recent": 0,
    }
    monitor = {"monitor_source": "none", "monitor_warnings": [], "monitor_recent_errors": []}
    cause_inputs = {
        "recent_reply_logs": 6,
        "target_specific_reply_ratio": 0.16,
        "generic_scene_objective_ratio": 0.25,
        "canon_triggered_unresolved_hooks": 0,
        "growth_pending_commit_ratio": 0.0,
        "growth_rejection_empty_ratio": 0.0,
    }

    rendered = render_review_markdown(
        _STORY_ID,
        snapshot,
        monitor,
        db_path="db/pocketrole.db",
        cause_inputs=cause_inputs,
    )

    assert rendered.count("soft issue は normalize へ寄せ、hard retry は reply focus miss と generic tail に絞れているか再点検する。") == 1
    assert "### Recommended Experiments" in rendered


def test_render_review_markdown_includes_chapter_and_director_diagnosis() -> None:
    snapshot = {
        "story_id": _STORY_ID,
        "window_turns": 20,
        "min_turn_number": 1,
        "max_turn_number": 32,
        "chat_log_count": 40,
        "active_character_count": 3,
        "last_char_id": "char_a",
        "last_message_len": 28,
        "last_sim_datetime": "2025-04-01T10:00",
        "story_intent_mode": "comic_ensemble",
        "active_interaction_patterns": 1,
        "patterns_detected_recent": 1,
        "pattern_recurrences_recent": 0,
        "dominant_pattern_types": ["status_clash"],
        "relationship_pairs_changed_recent": 1,
        "active_relationship_modes": 1,
        "relationship_mode_reinforcements_recent": 0,
        "relationship_mode_decay_updates_recent": 1,
        "multi_mode_pairs_active": 0,
        "dominant_relationship_modes": ["irritated_respect"],
        "active_canon_bits": 0,
        "canon_reinforcements_recent": 0,
        "canon_promotions_recent": 0,
        "canon_reignitions_recent": 0,
        "canon_triggered_hooks_open": 0,
        "active_canon_profile_overlays": 0,
        "canon_writebacks_recent": 0,
        "dominant_canon_levels": [],
        "dominant_canon_motifs": [],
        "active_dramatic_pressures": 1,
        "pressure_reinforcements_recent": 1,
        "dominant_pressure_types": ["payoff_ready"],
        "max_pressure_score": 0.82,
        "active_episode_id": 7,
        "active_episode_type": "status_clash",
        "active_episode_goal": "張り合いを前に出す",
        "active_episode_age": 6,
        "episodes_closed_recent": 0,
        "episode_close_completion_rate": 0.0,
        "episode_arcs_recent": 0,
        "reply_ratio": 0.24,
        "group_ratio": 0.10,
        "monologue_ratio": 0.20,
        "speaking_chars_per_turn_avg": 2.0,
        "all_chars_spoke_ratio": 0.40,
        "sessionless_monologues_recent": 0,
        "sessionless_monologue_ratio": 0.0,
        "active_scenes": 1,
        "closed_scenes_recent": 4,
        "solo_scenes_recent": 0,
        "open_hooks": 2,
        "hooks_created_recent": 2,
        "hooks_resolved_recent": 0,
        "active_tensions": 1,
        "intervention_eligible_now": 1,
        "max_character_turn_gap": 0,
        "stale_char_ids": [],
        "resolved_tensions_recent": 0,
        "live_interventions": 0,
        "growth_candidates_pending": 0,
        "growth_commits_recent": 0,
        "growth_empty_recent": 0,
        "growth_quality_rejections_recent": 0,
        "growth_parse_failures_recent": 0,
        "growth_rollbacks_recent": 0,
        "growth_superseded_recent": 0,
        "growth_expired_recent": 0,
        "quality_issues_recent": 0,
        "quality_retry_rate": 0.0,
        "quality_normalizations_recent": 0,
        "quality_fallbacks_recent": 0,
        "reply_quality_normalizations_recent": 0,
        "reply_quality_fallbacks_recent": 0,
        "reply_focus_misses_recent": 0,
        "generic_reply_tails_recent": 0,
        "voice_flat_replies_recent": 0,
        "scene_close_completion_rate": 0.25,
        "scene_arcs_recent": 1,
        "novel_outputs_recent": 1,
        "active_chapter_id": "festival_arc",
        "active_chapter_beat": 1,
        "active_chapter_opened_turn": 5,
        "active_chapter_age": 28,
        "active_director_persona_id": "sharp_cut",
        "director_satisfaction_overall": 0.31,
        "director_satisfaction_trend": "falling",
        "director_satisfaction_tension": 0.28,
        "director_satisfaction_pacing": 0.22,
        "director_satisfaction_surprise": 0.24,
    }
    monitor = {
        "monitor_source": "none",
        "monitor_warnings": ["scene_close_weak", "chapter_progress_stalled", "director_satisfaction_low"],
        "monitor_recent_errors": [],
    }
    cause_inputs = {
        "recent_reply_logs": 4,
        "target_specific_reply_ratio": 0.70,
        "generic_scene_objective_ratio": 0.15,
        "canon_triggered_unresolved_hooks": 0,
        "growth_pending_commit_ratio": 0.0,
        "growth_rejection_empty_ratio": 0.0,
    }

    rendered = render_review_markdown(
        _STORY_ID,
        snapshot,
        monitor,
        db_path="db/pocketrole.db",
        cause_inputs=cause_inputs,
    )

    assert "active_chapter_id=`festival_arc`" in rendered
    assert "overall=`0.310`" in rendered
    assert "chapter_progress_stalled" in rendered
    assert "director_alignment_low" in rendered
    assert "scene_close_completion_rate >= 0.50" in rendered
    assert "director_satisfaction_overall >= 0.40" in rendered


@async_to_sync
async def test_generate_story_review_draft_reports_strengths_and_db_only_mode(tmp_path: Path) -> None:
    output = tmp_path / "draft.md"
    async with _make_db() as db:
        await _seed_strength_case(db)
        tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "A と B の張り合い。",
                "involved_chars": ["char_a", "char_b"],
                "detected_turn": 11,
                "status": "escalating",
            },
        )
        await db.insert_interaction_pattern(
            _STORY_ID,
            {
                "pattern_type": "status_clash",
                "status": "active",
                "title": "張り合い",
                "description": "A と B の張り合い。",
                "involved_chars": ["char_a", "char_b"],
                "dedupe_key": f"tension:{tension_id}:status_clash",
                "source_tension_id": tension_id,
                "first_detected_turn": 11,
                "last_detected_turn": 11,
                "recurrence_count": 2,
                "intensity": 0.7,
                "confidence": 0.7,
            },
        )
        await db.insert_interaction_pattern(
            _STORY_ID,
            {
                "pattern_type": "bluff_or_showoff",
                "status": "active",
                "title": "見せ場を作る",
                "description": "A がやってみせると前に出た。",
                "involved_chars": ["char_a", "char_b"],
                "dedupe_key": "scene:1:bluff_or_showoff",
                "source_scene_id": 1,
                "first_detected_turn": 11,
                "last_detected_turn": 11,
                "recurrence_count": 2,
                "intensity": 0.6,
                "confidence": 0.65,
            },
        )
        await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "relationship_catalyst",
                "title": "張り合いを前に出す",
                "description": "A と B の火種を前に出す。",
                "prompt_injection": "短く応酬させる。",
                "tension_id": tension_id,
                "active_from_turn": 11,
                "active_until_turn": 12,
                "status": "acknowledged",
            },
        )
        await db.insert_relationship_mode(
            _STORY_ID,
            "char_a",
            "char_b",
            {
                "mode_type": "irritated_respect",
                "status": "active",
                "summary": "char_a は char_b を認めつつ張り合っている。",
                "confidence": 0.72,
                "intensity": 0.61,
                "first_detected_turn": 11,
                "last_reinforced_turn": 11,
                "source_pattern_id": 1,
            },
        )
        await db.insert_relationship_mode(
            _STORY_ID,
            "char_a",
            "char_b",
            {
                "mode_type": "cannot_ignore",
                "status": "active",
                "summary": "char_a は char_b を放っておけない。",
                "confidence": 0.68,
                "intensity": 0.53,
                "first_detected_turn": 11,
                "last_reinforced_turn": 11,
                "source_episode_id": 1,
            },
        )
        await db.insert_story_canon_bit(
            _STORY_ID,
            {
                "bit_type": "pair_dynamic",
                "motif_key": "irritated_respect",
                "canon_level": "recurring_bit",
                "status": "active",
                "title": "張り合う二人",
                "summary": "char_a と char_b は張り合いへ戻りやすい。",
                "focus_char_ids": ["char_a", "char_b"],
                "focus_place_id": None,
                "dedupe_key": "pair:char_a:char_b:irritated_respect",
                "evidence_sources": ["relationship_mode", "pattern"],
                "anchor_pattern_id": 1,
                "anchor_episode_id": None,
                "anchor_relationship_mode_id": 1,
                "anchor_scene_id": None,
                "first_detected_turn": 11,
                "last_reinforced_turn": 11,
                "recurrence_count": 2,
                "confidence": 0.8,
                "novelty": 0.6,
                "intent_alignment": 0.8,
                "last_reignited_turn": 11,
                "reignition_count": 1,
                "last_writeback_turn": 11,
                "writeback_count": 1,
            },
        )
        await db.upsert_character_canon_overlay(
            _STORY_ID,
            "char_a",
            {
                "overlay_json": {
                    "current_goal": "char_bとの勝ち負けをはっきりさせたい",
                    "current_worry": "char_bに押し切られるのは避けたい",
                },
                "version": 1,
                "last_written_turn": 11,
                "source_canon_bit_ids": [1],
            },
        )
        await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "conflict",
                "status": "open",
                "owner_char_id": "char_a",
                "target_char_id": "char_b",
                "title": "前にも起きた張り合いが戻る",
                "description": "char_a と char_b の張り合いが戻りやすい。",
                "priority": 0.72,
                "source_canon_bit_id": 1,
            },
        )
        await db.insert_story_dramatic_pressure(
            _STORY_ID,
            {
                "pressure_type": "status_flashpoint",
                "status": "active",
                "title": "張り合いの火種",
                "summary": "A と B の張り合いが回収可能な状態にある。",
                "focus_char_ids": ["char_a", "char_b"],
                "focus_place_id": None,
                "dedupe_key": "tension:1:status_flashpoint",
                "source_tension_id": tension_id,
                "source_pattern_id": 1,
                "source_relationship_mode_id": 1,
                "source_canon_bit_id": 1,
                "first_detected_turn": 11,
                "last_detected_turn": 11,
                "recurrence_count": 1,
                "score": 0.82,
                "urgency": 0.8,
                "payoff_ready": 0.7,
                "intent_alignment": 0.8,
            },
        )
        code = await generate_story_review_draft(
            _STORY_ID,
            output=output,
            window_turns=5,
            db=db,
            monitor_dir=tmp_path / "missing",
        )

    assert code == EXIT_OK
    text = output.read_text(encoding="utf-8")
    assert "# Story Quality Review Draft" in text
    assert "### Scope" in text
    assert "### Intent Lens" in text
    assert "### Pattern Lens" in text
    assert "### Relationship Mode Lens" in text
    assert "### Canon Lens" in text
    assert "### Pressure Lens" in text
    assert "### Episode Lens" in text
    assert "### Cause Lens" in text
    assert "### Recommended Experiments" in text
    assert "story_intent_mode=`mystery_drama`" in text
    assert "status_clash" in text
    assert "irritated_respect" in text
    assert "`active_episode_type=mystery`" in text
    assert "### What Worked" in text
    assert "reply_ratio=0.33" in text
    assert "quality_normalizations_recent=0" in text
    assert "quality_fallbacks_recent=0" in text
    assert "reply_quality_normalizations_recent=0" in text
    assert "reply_quality_fallbacks_recent=0" in text
    assert "multi_mode_pairs_active=1" in text
    assert "canon_reignitions_recent=1" in text
    assert "canon_triggered_hooks_open=1" in text
    assert "monitor_source: `none`" in text
    assert "持ち越し hook の回収が動いている" in text
    assert "episode close artifact が残っている" in text
    assert "scene close prose が生成されている" in text
    assert "主要な構造ギャップは目立たない" in text


@async_to_sync
async def test_generate_story_review_draft_reports_gaps_from_metrics_and_monitor(tmp_path: Path) -> None:
    output = tmp_path / "draft.md"
    monitor = tmp_path / f"{_STORY_ID}_runtime_monitor.md"
    monitor.write_text(
        "\n".join(
            [
                "# Runtime Monitor",
                "",
                "## Snapshot 2026-03-24T11:00:00+09:00",
                "### Warnings",
                "- turn_stalled | ollama_failed",
                "- Recent warnings/errors: sqlite timeout",
                "",
            ]
        ),
        encoding="utf-8",
    )
    async with _make_db() as db:
        await _seed_gap_case(db)
        code = await generate_story_review_draft(
            _STORY_ID,
            output=output,
            window_turns=5,
            db=db,
            monitor_dir=tmp_path,
        )

    assert code == EXIT_OK
    text = output.read_text(encoding="utf-8")
    assert "reply_ratio=0.00" in text
    assert "all_chars_spoke_ratio=1.00" in text
    assert "story_intent_mode=`mystery_drama`" in text
    assert "未回収の火種が滞留している" in text
    assert "active episode が無く" in text
    assert "director 介入が追いついていない" in text
    assert "max_character_turn_gap=0" in text
    assert "turn_stalled" in text
    assert "ollama_failed" in text
    assert "scene-close novel trigger を見直し" in text
    assert "20 turn 以上の連続運転で stall warning が再発しないか" in text
    assert "### Cause Lens" in text
    assert "### Recommended Experiments" in text
    assert "Why:" in text
    assert "Evidence:" in text
    assert "Next experiment:" in text


@async_to_sync
async def test_generate_story_review_draft_reports_cause_lens_for_signal_visibility(tmp_path: Path) -> None:
    output = tmp_path / "cause_signal.md"
    async with _make_db() as db:
        await _seed_gap_case(db)
        await db.insert_interaction_pattern(
            _STORY_ID,
            {
                "pattern_type": "status_clash",
                "status": "active",
                "title": "教室の張り合い",
                "description": "char_a と char_b の張り合いが残っている。",
                "involved_chars": ["char_a", "char_b"],
                "dedupe_key": "scene:1:status_clash",
                "source_scene_id": 1,
                "first_detected_turn": 20,
                "last_detected_turn": 20,
                "recurrence_count": 1,
                "intensity": 0.6,
                "confidence": 0.6,
            },
        )
        for issue_type in ("reply_focus_missing", "generic_reply_tail", "voice_flat_reply"):
            await db.insert_generation_quality_issue(
                _STORY_ID,
                {
                    "issue_type": issue_type,
                    "severity": "warning",
                    "details": {"char_id": "char_a"},
                    "auto_action": "retry",
                    "created_turn": 20,
                },
            )
        code = await generate_story_review_draft(
            _STORY_ID,
            output=output,
            window_turns=5,
            db=db,
            monitor_dir=tmp_path / "missing",
        )

    assert code == EXIT_OK
    text = output.read_text(encoding="utf-8")
    assert "### Cause Lens" in text
    assert "signal_visibility_weak" in text
    assert "signal layer は立っているが" in text
    assert "### Recommended Experiments" in text



@async_to_sync
async def test_generate_story_review_draft_reports_growth_parse_failures(tmp_path: Path) -> None:
    output = tmp_path / "growth_fail.md"
    async with _make_db() as db:
        await _seed_gap_case(db)
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "growth_parse_failed",
                "severity": "warning",
                "details": {"char_id": "char_a", "done_reason": "length"},
                "auto_action": "skip",
                "created_turn": 20,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "growth_write_rolled_back",
                "severity": "warning",
                "details": {"char_id": "char_a", "error": "boom"},
                "auto_action": "rollback",
                "created_turn": 20,
            },
        )
        code = await generate_story_review_draft(
            _STORY_ID,
            output=output,
            window_turns=5,
            db=db,
            monitor_dir=tmp_path / "missing",
        )

    assert code == EXIT_OK
    text = output.read_text(encoding="utf-8")
    assert "growth_parse_failures_recent=1" in text
    assert "growth_rollbacks_recent=1" in text
    assert "GrowthEngine の structured parse failure が再発している" in text
    assert "GrowthEngine write batch の rollback が発生している" in text


@async_to_sync
async def test_generate_story_review_draft_rejects_missing_story(tmp_path: Path) -> None:
    async with _make_db() as db:
        code = await generate_story_review_draft("missing_story", output=tmp_path / "draft.md", db=db)
    assert code == EXIT_STORY_NOT_FOUND
