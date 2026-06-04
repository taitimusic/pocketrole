"""tools.monitor_engine_runtime のテスト。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db.db_manager import DatabaseManager
from tools.monitor_engine_runtime import (
    _build_monitor_warnings,
    _collect_story_quality_metrics,
    parse_args,
    render_markdown_entry,
    run_monitor,
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
        (_STORY_ID, "暗黒学園", "rule", "2025-04-01T09:30"),
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


def test_parse_args_defaults() -> None:
    """主要引数のデフォルト値が期待通り。"""
    ns = parse_args(["--story", "ankoku_gakuen", "--engine-pid", "1234"])
    assert ns.story == "ankoku_gakuen"
    assert ns.db == "db/pocketrole.db"
    assert ns.config == "config.yaml"
    assert ns.env == ".env"
    assert ns.interval_sec == 15.0
    assert ns.max_entries is None
    assert ns.engine_pid == 1234
    assert ns.window_turns == 20
    assert ns.stall_samples == 3
    assert ns.reply_ratio_warn_below == 0.20
    assert ns.all_chars_spoke_warn_above == 0.60
    assert ns.quality_retry_warn_above == 0.15


def test_render_markdown_entry_includes_probe_results() -> None:
    """Markdown には health と quality metrics / warnings が入る。"""
    entry = render_markdown_entry(
        {
            "captured_at": "2026-03-16T08:30:00+09:00",
            "story_id": "ankoku_gakuen",
            "story_intent_mode": "comic_ensemble",
            "max_turn_number": 42,
            "chat_log_count": 84,
            "last_char_id": "yokaze_yuuma",
            "last_message_len": 120,
            "last_sim_datetime": "2025-04-01T10:30",
            "engine_alive": True,
            "ollama_ok": True,
            "ollama_status": "HTTP 200",
            "gpu_summary": "gpu0: 4200/8192 MiB, util 87%",
            "speaking_chars_per_turn_avg": 2.33,
            "all_chars_spoke_ratio": 0.67,
            "reply_ratio": 0.18,
            "group_ratio": 0.45,
            "monologue_ratio": 0.37,
            "sessionless_monologues_recent": 6,
            "sessionless_monologue_ratio": 0.60,
            "active_scenes": 2,
            "closed_scenes_recent": 1,
            "solo_scenes_recent": 2,
            "open_hooks": 3,
            "hooks_created_recent": 2,
            "hooks_resolved_recent": 0,
            "active_tensions": 2,
            "max_character_turn_gap": 3,
            "stale_char_ids": ["char_c"],
            "resolved_tensions_recent": 1,
            "live_interventions": 0,
            "relationship_pairs_changed_recent": 4,
            "active_relationship_modes": 2,
            "relationship_mode_reinforcements_recent": 1,
            "relationship_mode_decay_updates_recent": 1,
            "multi_mode_pairs_active": 1,
            "complementary_mode_pairs_active": 1,
            "single_mode_pairs_active": 1,
            "relationship_mode_conflict_attention_pairs": 1,
            "dominant_relationship_modes": ["irritated_respect"],
            "active_canon_bits": 2,
            "canon_reinforcements_recent": 1,
            "canon_promotions_recent": 1,
            "canon_reignitions_recent": 1,
            "canon_triggered_hooks_open": 2,
            "dominant_canon_levels": ["recurring_bit"],
            "dominant_canon_motifs": ["irritated_respect"],
            "active_dramatic_pressures": 2,
            "pressure_reinforcements_recent": 1,
            "dominant_pressure_types": ["status_flashpoint"],
            "max_pressure_score": 0.82,
            "active_interaction_patterns": 2,
            "patterns_detected_recent": 2,
            "pattern_recurrences_recent": 1,
            "dominant_pattern_types": ["status_clash"],
            "active_episode_id": 7,
            "active_episode_type": "status_clash",
            "active_episode_age": 5,
            "episodes_closed_recent": 1,
            "growth_candidates_pending": 2,
            "growth_commits_recent": 1,
            "growth_empty_recent": 1,
            "growth_quality_rejections_recent": 2,
            "growth_parse_failures_recent": 1,
            "growth_rollbacks_recent": 0,
            "growth_superseded_recent": 2,
            "growth_expired_recent": 1,
            "quality_issues_recent": 5,
            "quality_retry_rate": 0.22,
            "quality_normalizations_recent": 3,
            "quality_fallbacks_recent": 1,
            "reply_quality_normalizations_recent": 2,
            "reply_quality_fallbacks_recent": 1,
            "reply_focus_misses_recent": 2,
            "generic_reply_tails_recent": 1,
            "voice_flat_replies_recent": 1,
            "reply_variety_press_recent": 2,
            "reply_variety_condition_recent": 1,
            "reply_variety_redirect_recent": 0,
            "reply_reused_opening_recent": 1,
            "reply_reused_ending_recent": 2,
            "signal_visibility_misses_recent": 1,
            "objective_visibility_misses_recent": 2,
            "signal_visibility_retry_recent": 1,
            "objective_visibility_retry_recent": 1,
            "scene_close_completion_rate": 1.0,
            "growth_candidate_rate": 0.5,
            "growth_commit_rate": 0.25,
            "episode_close_completion_rate": 1.0,
            "episode_arcs_recent": 1,
            "scene_arcs_recent": 1,
            "novel_outputs_recent": 1,
            "active_chapter_id": "chapter_1",
            "active_chapter_beat": 2,
            "active_chapter_opened_turn": 35,
            "active_chapter_age": 8,
            "active_director_persona_id": "sharp_cut",
            "director_satisfaction_overall": 0.52,
            "director_satisfaction_trend": "rising",
            "director_satisfaction_tension": 0.61,
            "director_satisfaction_pacing": 0.47,
            "director_satisfaction_surprise": 0.58,
            "warnings": [
                "reply_ratio_low",
                "tension_without_intervention",
            ],
            "recent_errors": ["web poster retry", "timeout recovered"],
        }
    )

    assert "### Health" in entry
    assert "turn=42" in entry
    assert "Engine process: alive" in entry
    assert "Ollama: OK (HTTP 200)" in entry
    assert "GPU: gpu0: 4200/8192 MiB, util 87%" in entry
    assert "### Story Quality" in entry
    assert "story_intent_mode=comic_ensemble" in entry
    assert "reply_ratio=0.18" in entry
    assert "sessionless_monologue_ratio=0.60" in entry
    assert "active_relationship_modes=2" in entry
    assert "relationship_mode_decay_updates_recent=1" in entry
    assert "multi_mode_pairs_active=1" in entry
    assert "complementary_mode_pairs_active=1" in entry
    assert "single_mode_pairs_active=1" in entry
    assert "dominant_relationship_modes=irritated_respect" in entry
    assert "active_canon_bits=2" in entry
    assert "canon_reignitions_recent=1" in entry
    assert "canon_triggered_hooks_open=2" in entry
    assert "dominant_canon_motifs=irritated_respect" in entry
    assert "active_dramatic_pressures=2" in entry
    assert "dominant_pressure_types=status_flashpoint" in entry
    assert "active_episode_id=7" in entry
    assert "active_episode_type=status_clash" in entry
    assert "episode_close_completion_rate=1.00" in entry
    assert "scene_close_completion_rate=1.00" in entry
    assert "active_chapter_id=chapter_1" in entry
    assert "active_chapter_beat=2" in entry
    assert "active_chapter_age=8" in entry
    assert "active_director_persona_id=sharp_cut" in entry
    assert "director_satisfaction_overall=0.520" in entry
    assert "quality_normalizations_recent=3" in entry
    assert "quality_fallbacks_recent=1" in entry
    assert "reply_quality_normalizations_recent=2" in entry
    assert "reply_quality_fallbacks_recent=1" in entry
    assert "reply_focus_misses_recent=2" in entry
    assert "generic_reply_tails_recent=1" in entry
    assert "voice_flat_replies_recent=1" in entry
    assert "reply_variety_press_recent=2" in entry
    assert "reply_reused_ending_recent=2" in entry
    assert "signal_visibility_misses_recent=1" in entry
    assert "objective_visibility_misses_recent=2" in entry
    assert "growth_empty_recent=1" in entry
    assert "growth_quality_rejections_recent=2" in entry
    assert "growth_parse_failures_recent=1" in entry
    assert "growth_rollbacks_recent=0" in entry
    assert "growth_superseded_recent=2" in entry
    assert "growth_expired_recent=1" in entry
    assert "open_hooks=3" in entry
    assert "max_character_turn_gap=3" in entry
    assert "stale_char_ids=char_c" in entry
    assert "### Warnings" in entry
    assert "reply_ratio_low" in entry
    assert "Recent warnings/errors: web poster retry | timeout recovered" in entry


def test_build_monitor_warnings_detects_stall_and_quality_issues() -> None:
    """warning 判定は stall と品質閾値を拾う。"""
    warnings = _build_monitor_warnings(
        {
            "engine_alive": True,
            "ollama_ok": True,
            "reply_ratio": 0.10,
            "all_chars_spoke_ratio": 0.75,
            "open_hooks": 2,
            "hooks_resolved_recent": 0,
            "active_tensions": 1,
            "intervention_eligible_now": 1,
            "live_interventions": 0,
            "max_character_turn_gap": 3,
            "quality_retry_rate": 0.30,
            "reply_quality_fallbacks_recent": 2,
            "reply_focus_misses_recent": 1,
            "generic_reply_tails_recent": 1,
            "voice_flat_replies_recent": 1,
            "closed_scenes_recent": 2,
            "scene_close_completion_rate": 0.0,
            "active_relationship_modes": 2,
            "multi_mode_pairs_active": 0,
            "active_chapter_id": "chapter_1",
            "active_chapter_age": 25,
            "director_satisfaction_overall": 0.32,
            "stall_samples_seen": 3,
        },
        stall_samples=3,
        reply_ratio_warn_below=0.20,
        all_chars_spoke_warn_above=0.60,
        quality_retry_warn_above=0.15,
    )

    assert warnings == [
        "turn_stalled",
        "reply_ratio_low",
        "all_chars_spoke_high",
        "turn_gap_high",
        "hook_resolution_stalled",
        "tension_without_intervention",
        "quality_retry_high",
        "reply_quality_fallback_high",
        "reply_quality_flat",
        "scene_close_weak",
        "relationship_mode_flat",
        "chapter_progress_stalled",
        "director_satisfaction_low",
    ]


def test_build_monitor_warnings_skips_tension_warning_before_intervention_window() -> None:
    """介入対象がまだ 0 件なら active_tensions があっても warning にしない。"""
    warnings = _build_monitor_warnings(
        {
            "engine_alive": True,
            "ollama_ok": True,
            "reply_ratio": 0.30,
            "all_chars_spoke_ratio": 0.40,
            "open_hooks": 1,
            "hooks_resolved_recent": 1,
            "active_tensions": 4,
            "intervention_eligible_now": 0,
            "live_interventions": 0,
            "max_character_turn_gap": 2,
            "quality_retry_rate": 0.0,
            "stall_samples_seen": 0,
        },
        stall_samples=3,
        reply_ratio_warn_below=0.20,
        all_chars_spoke_warn_above=0.60,
        quality_retry_warn_above=0.15,
    )

    assert warnings == []


def test_build_monitor_warnings_detects_high_turn_gap_only_above_threshold() -> None:
    """turn gap warning は 3 以上でのみ出る。"""
    assert _build_monitor_warnings(
        {
            "engine_alive": True,
            "ollama_ok": True,
            "reply_ratio": 0.30,
            "all_chars_spoke_ratio": 0.40,
            "open_hooks": 0,
            "hooks_resolved_recent": 0,
            "active_tensions": 0,
            "intervention_eligible_now": 0,
            "live_interventions": 0,
            "max_character_turn_gap": 2,
            "quality_retry_rate": 0.0,
            "stall_samples_seen": 0,
        },
        stall_samples=3,
        reply_ratio_warn_below=0.20,
        all_chars_spoke_warn_above=0.60,
        quality_retry_warn_above=0.15,
    ) == []
    assert _build_monitor_warnings(
        {
            "engine_alive": True,
            "ollama_ok": True,
            "reply_ratio": 0.30,
            "all_chars_spoke_ratio": 0.40,
            "open_hooks": 0,
            "hooks_resolved_recent": 0,
            "active_tensions": 0,
            "intervention_eligible_now": 0,
            "live_interventions": 0,
            "max_character_turn_gap": 3,
            "quality_retry_rate": 0.0,
            "stall_samples_seen": 0,
        },
        stall_samples=3,
        reply_ratio_warn_below=0.20,
        all_chars_spoke_warn_above=0.60,
        quality_retry_warn_above=0.15,
    ) == ["turn_gap_high"]


def test_build_monitor_warnings_detects_reply_quality_flat_without_reply_fallbacks() -> None:
    warnings = _build_monitor_warnings(
        {
            "engine_alive": True,
            "ollama_ok": True,
            "reply_ratio": 0.30,
            "all_chars_spoke_ratio": 0.40,
            "open_hooks": 0,
            "hooks_resolved_recent": 0,
            "active_tensions": 0,
            "intervention_eligible_now": 0,
            "live_interventions": 0,
            "max_character_turn_gap": 0,
            "quality_retry_rate": 0.0,
            "reply_quality_fallbacks_recent": 0,
            "reply_focus_misses_recent": 2,
            "generic_reply_tails_recent": 0,
            "voice_flat_replies_recent": 1,
            "closed_scenes_recent": 0,
            "scene_close_completion_rate": 0.0,
            "active_relationship_modes": 0,
            "multi_mode_pairs_active": 0,
            "active_chapter_id": None,
            "active_chapter_age": 0,
            "director_satisfaction_overall": None,
            "stall_samples_seen": 0,
        },
        stall_samples=3,
        reply_ratio_warn_below=0.20,
        all_chars_spoke_warn_above=0.60,
        quality_retry_warn_above=0.15,
    )

    assert warnings == ["reply_quality_flat"]


def test_render_markdown_entry_includes_complementary_mode_persistence_metrics() -> None:
    entry = render_markdown_entry(
        {
            "captured_at": "2026-03-16T08:30:00+09:00",
            "story_id": "ankoku_gakuen",
            "engine_alive": True,
            "ollama_ok": True,
            "ollama_status": "HTTP 200",
            "gpu_summary": "gpu0 ok",
            "max_turn_number": 42,
            "chat_log_count": 84,
            "last_char_id": "yokaze_yuuma",
            "last_message_len": 120,
            "last_sim_datetime": "2025-04-01T10:30",
            "speaking_chars_per_turn_avg": 2.33,
            "all_chars_spoke_ratio": 0.32,
            "reply_ratio": 0.41,
            "group_ratio": 0.30,
            "monologue_ratio": 0.29,
            "sessionless_monologue_ratio": 0.09,
            "active_scenes": 1,
            "closed_scenes_recent": 0,
            "solo_scenes_recent": 0,
            "closed_scenes_without_arc_recent": 0,
            "scene_arcs_without_novel_recent": 0,
            "scene_close_backlog_recent": 0,
            "scene_close_missing_arc_recent": 0,
            "scene_close_missing_novel_recent": 0,
            "scene_close_backlog_recovered_recent": 0,
            "open_hooks": 1,
            "hooks_created_recent": 1,
            "hooks_resolved_recent": 0,
            "active_tensions": 1,
            "intervention_eligible_now": 1,
            "max_character_turn_gap": 1,
            "stale_char_ids": [],
            "resolved_tensions_recent": 0,
            "live_interventions": 1,
            "relationship_pairs_changed_recent": 2,
            "active_relationship_modes": 3,
            "relationship_mode_reinforcements_recent": 2,
            "relationship_mode_decay_updates_recent": 1,
            "multi_mode_pairs_active": 1,
            "complementary_mode_pairs_active": 1,
            "single_mode_pairs_active": 1,
            "relationship_mode_conflict_attention_pairs": 1,
            "complementary_mode_reinforcements_recent": 1,
            "complementary_mode_decay_updates_recent": 2,
            "complementary_mode_deactivations_recent": 1,
            "dominant_relationship_modes": ["irritated_respect", "unsafe_confidant"],
            "active_canon_bits": 0,
            "canon_reinforcements_recent": 0,
            "canon_promotions_recent": 0,
            "canon_reignitions_recent": 0,
            "canon_triggered_hooks_open": 0,
            "dominant_canon_levels": [],
            "dominant_canon_motifs": [],
            "active_dramatic_pressures": 0,
            "pressure_reinforcements_recent": 0,
            "dominant_pressure_types": [],
            "max_pressure_score": 0.0,
            "active_interaction_patterns": 1,
            "patterns_detected_recent": 1,
            "pattern_recurrences_recent": 1,
            "dominant_pattern_types": ["status_clash"],
            "active_episode_id": 1,
            "active_episode_type": "status_clash",
            "active_episode_age": 2,
            "episodes_closed_recent": 0,
            "active_chapter_id": None,
            "active_chapter_beat": None,
            "active_chapter_age": 0,
            "active_director_persona_id": None,
            "growth_candidates_pending": 0,
            "growth_commits_recent": 0,
            "growth_empty_recent": 0,
            "growth_quality_rejections_recent": 0,
            "growth_parse_failures_recent": 0,
            "growth_rollbacks_recent": 0,
            "growth_superseded_recent": 0,
            "growth_expired_recent": 0,
            "sessionless_monologues_recent": 0,
            "quality_issues_recent": 0,
            "quality_normalizations_recent": 0,
            "quality_fallbacks_recent": 0,
            "reply_quality_normalizations_recent": 0,
            "reply_quality_fallbacks_recent": 0,
            "reply_fallback_focus_missing_recent": 0,
            "reply_fallback_direct_reaction_recent": 0,
            "reply_retry_kept_recent": 0,
            "reply_focus_misses_recent": 0,
            "generic_reply_tails_recent": 0,
            "voice_flat_replies_recent": 0,
            "reply_flat_generic_tail_recent": 0,
            "reply_flat_voice_recent": 0,
            "reply_flat_reused_tail_recent": 0,
            "reply_variety_press_recent": 0,
            "reply_variety_condition_recent": 0,
            "reply_variety_redirect_recent": 0,
            "reply_dramatic_move_missing_recent": 0,
            "reply_soft_landing_recent": 0,
            "reply_shape_dominance_recent": 0,
            "reply_story_quality_keep_blocked_recent": 0,
            "reply_reused_opening_recent": 0,
            "reply_reused_ending_recent": 0,
            "signal_visibility_misses_recent": 0,
            "objective_visibility_misses_recent": 0,
            "signal_visibility_retry_recent": 0,
            "objective_visibility_retry_recent": 0,
            "quality_retry_rate": 0.0,
            "scene_close_completion_rate": 0.0,
            "growth_candidate_rate": 0.0,
            "growth_commit_rate": 0.0,
            "episode_close_completion_rate": 0.0,
            "episode_arcs_recent": 0,
            "scene_arcs_recent": 0,
            "novel_outputs_recent": 0,
            "warnings": [],
            "recent_errors": [],
        }
    )

    assert "complementary_mode_reinforcements_recent=1" in entry
    assert "complementary_mode_decay_updates_recent=2" in entry
    assert "complementary_mode_deactivations_recent=1" in entry


def test_render_markdown_entry_includes_reply_fallback_root_cause_metrics() -> None:
    entry = render_markdown_entry(
        {
            "captured_at": "2026-03-16T08:30:00+09:00",
            "story_id": "ankoku_gakuen",
            "engine_alive": True,
            "ollama_ok": True,
            "ollama_status": "HTTP 200",
            "gpu_summary": "gpu0 ok",
            "max_turn_number": 42,
            "chat_log_count": 84,
            "last_char_id": "yokaze_yuuma",
            "last_message_len": 120,
            "last_sim_datetime": "2025-04-01T10:30",
            "speaking_chars_per_turn_avg": 2.33,
            "all_chars_spoke_ratio": 0.32,
            "reply_ratio": 0.41,
            "group_ratio": 0.30,
            "monologue_ratio": 0.29,
            "sessionless_monologue_ratio": 0.09,
            "active_scenes": 1,
            "closed_scenes_recent": 1,
            "solo_scenes_recent": 0,
            "open_hooks": 1,
            "hooks_created_recent": 1,
            "hooks_resolved_recent": 0,
            "active_tensions": 1,
            "intervention_eligible_now": 1,
            "max_character_turn_gap": 1,
            "stale_char_ids": [],
            "resolved_tensions_recent": 0,
            "live_interventions": 1,
            "relationship_pairs_changed_recent": 0,
            "active_relationship_modes": 0,
            "relationship_mode_reinforcements_recent": 0,
            "relationship_mode_decay_updates_recent": 0,
            "multi_mode_pairs_active": 0,
            "dominant_relationship_modes": [],
            "active_canon_bits": 0,
            "canon_reinforcements_recent": 0,
            "canon_promotions_recent": 0,
            "canon_reignitions_recent": 0,
            "canon_triggered_hooks_open": 0,
            "dominant_canon_levels": [],
            "dominant_canon_motifs": [],
            "active_dramatic_pressures": 0,
            "pressure_reinforcements_recent": 0,
            "dominant_pressure_types": [],
            "max_pressure_score": 0.0,
            "active_interaction_patterns": 0,
            "patterns_detected_recent": 0,
            "pattern_recurrences_recent": 0,
            "dominant_pattern_types": [],
            "active_episode_id": None,
            "active_episode_type": None,
            "active_episode_age": 0,
            "episodes_closed_recent": 0,
            "active_chapter_id": None,
            "active_chapter_beat": None,
            "active_chapter_age": 0,
            "active_director_persona_id": None,
            "director_satisfaction_overall": None,
            "director_satisfaction_trend": None,
            "director_satisfaction_tension": None,
            "director_satisfaction_pacing": None,
            "director_satisfaction_surprise": None,
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
            "reply_quality_fallbacks_recent": 2,
            "reply_focus_misses_recent": 1,
            "generic_reply_tails_recent": 0,
            "voice_flat_replies_recent": 0,
            "reply_fallback_focus_missing_recent": 2,
            "reply_fallback_direct_reaction_recent": 0,
            "reply_retry_kept_recent": 3,
            "scene_close_completion_rate": 1.0,
            "scene_close_missing_arc_recent": 0,
            "scene_close_missing_novel_recent": 0,
            "scene_close_backlog_recovered_recent": 1,
            "episode_close_completion_rate": 0.0,
            "episode_arcs_recent": 0,
            "scene_arcs_recent": 0,
            "novel_outputs_recent": 0,
            "warnings": [],
            "recent_errors": [],
        }
    )

    assert "reply_fallback_focus_missing_recent=2" in entry
    assert "reply_retry_kept_recent=3" in entry
    assert "scene_close_backlog_recovered_recent=1" in entry


@pytest.mark.asyncio
async def test_collect_story_quality_metrics_reads_runtime_tables() -> None:
    """scene / hook / tension / growth / novel 系の集計が snapshot 用に返る。"""
    async with _make_db() as db:
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "rooftop",
                "opened_turn": 10,
            },
        )
        await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "closed",
                "place_id": "classroom",
                "opened_turn": 8,
            },
        )
        log_1 = await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:10",
                "turn_number": 10,
                "char_id": "char_a",
                "msg_type": "reply",
                "target_char_id": "char_b",
                "place_id": "rooftop",
                "message": "返事する。",
                "scene_id": scene_id,
                "generation_attempt": 2,
            },
        )
        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:10",
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
                "sim_datetime": "2025-04-01T09:20",
                "turn_number": 11,
                "char_id": "char_c",
                "msg_type": "monologue",
                "place_id": "hallway",
                "message": "独白する。",
            },
        )
        await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "promise",
                "status": "open",
                "title": "放課後の約束",
                "description": "屋上で再会する。",
                "source_scene_id": scene_id,
            },
        )
        await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "まだ解けていない。",
                "detected_turn": 10,
                "status": "simmering",
            },
        )
        await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "pressure",
                "title": "背中を押す",
                "description": "前に進ませる。",
                "prompt_injection": "今こそ踏み込むべきだ。",
                "active_from_turn": 10,
                "active_until_turn": 12,
                "status": "acknowledged",
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
        await db.insert_character_growth_candidate(
            _STORY_ID,
            "char_a",
            {
                "field": "current_goal",
                "candidate_value": "踏み込む",
                "reason": "体験が重なった。",
                "detected_turn": 11,
            },
        )
        await db.insert_evolution(
            _STORY_ID,
            {
                "char_id": "char_a",
                "turn_number": 11,
                "field": "current_goal",
                "new_value": "踏み込む",
                "reason": "commit",
            },
        )
        await db._conn.execute(
            """
            INSERT INTO generation_quality_issues (
                story_id, log_id, scene_id, issue_type, severity, details, auto_action, created_turn
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_STORY_ID, log_1, scene_id, "too_long", "warning", "{}", "retry", 10),
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "growth_llm_empty",
                "severity": "warning",
                "details": {"char_id": "char_a"},
                "auto_action": "skip",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "growth_generic_rejected",
                "severity": "warning",
                "details": {"char_id": "char_a"},
                "auto_action": "skip",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "growth_field_mismatch_rejected",
                "severity": "warning",
                "details": {"char_id": "char_a"},
                "auto_action": "skip",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "growth_low_quality_rejected",
                "severity": "warning",
                "details": {"char_id": "char_a"},
                "auto_action": "skip",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "growth_parse_failed",
                "severity": "warning",
                "details": {"char_id": "char_a"},
                "auto_action": "skip",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "growth_write_rolled_back",
                "severity": "warning",
                "details": {"char_id": "char_a"},
                "auto_action": "rollback",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "reply_focus_missing",
                "severity": "warning",
                "details": {"msg_type": "reply"},
                "auto_action": "retry",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "generic_reply_tail",
                "severity": "warning",
                "details": {"msg_type": "reply"},
                "auto_action": "retry",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "voice_flat_reply",
                "severity": "warning",
                "details": {
                    "msg_type": "reply",
                    "reply_variety_shape": "answer_then_press",
                    "reply_variety_second_beat": "press",
                    "reply_dramatic_move_mode": "counter",
                    "reply_dramatic_move_seen": False,
                    "reply_soft_landing_used": True,
                    "reply_shape_reused_recently": True,
                    "reply_shape_mode": "answer_then_probe",
                    "reply_blandness_shape": "answer_then_probe",
                    "reply_blandness_shape_reused": True,
                    "reply_pressure_shift_seen": False,
                    "reply_story_pressure_seen": False,
                    "reply_story_flavor_seen": False,
                    "reply_second_beat_reused": True,
                    "reply_quality_contract_missed": True,
                    "reply_story_quality_contract_missed": True,
                    "story_flavor_weak": True,
                    "blandness_contract_missed": True,
                    "recent_opening_reused": False,
                    "recent_ending_reused": True,
                    "recent_second_beat_reused": True,
                },
                "auto_action": "retry",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "quality_output_normalized",
                "severity": "info",
                "details": {
                    "msg_type": "reply",
                    "reply_variety_shape": "answer_then_condition",
                    "reply_variety_second_beat": "condition",
                    "recent_opening_reused": True,
                    "recent_ending_reused": False,
                    "recent_second_beat_reused": False,
                },
                "auto_action": "normalize",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "signal_visibility_missing",
                "severity": "warning",
                "details": {
                    "reply_visibility_mode": "paired",
                    "signal_visible": False,
                    "objective_visible": True,
                },
                "auto_action": "retry",
                "created_turn": 11,
            },
        )
        await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "scene_objective_visibility_missing",
                "severity": "info",
                "details": {
                    "reply_visibility_mode": "paired",
                    "signal_visible": True,
                    "objective_visible": False,
                },
                "auto_action": "normalize",
                "created_turn": 11,
            },
        )
        await db.insert_relationship_mode(
            _STORY_ID,
            "char_a",
            "char_b",
            {
                "mode_type": "irritated_respect",
                "status": "active",
                "summary": "ぶつかるが認める。",
                "confidence": 0.70,
                "intensity": 0.60,
                "first_detected_turn": 10,
                "last_reinforced_turn": 11,
            },
        )
        await db.insert_relationship_mode(
            _STORY_ID,
            "char_a",
            "char_b",
            {
                "mode_type": "cannot_ignore",
                "status": "active",
                "summary": "放っておけない。",
                "confidence": 0.58,
                "intensity": 0.46,
                "first_detected_turn": 10,
                "last_reinforced_turn": 11,
            },
        )
        await db.insert_relationship_mode(
            _STORY_ID,
            "char_b",
            "char_c",
            {
                "mode_type": "chaos_partner",
                "status": "active",
                "summary": "絡むと騒ぎになる。",
                "confidence": 0.62,
                "intensity": 0.51,
                "first_detected_turn": 10,
                "last_reinforced_turn": 11,
            },
        )
        arc_id = await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "scene",
                "title": "閉じた場面",
                "summary": "まとまった。",
                "turn_from": 11,
                "source_scene_id": scene_id,
            },
        )
        await db.insert_novel_output(
            _STORY_ID,
            arc_id,
            {
                "content_type": "prose",
                "content": "散文。",
                "ordering": 1,
            },
        )
        episode_id = await db.insert_story_episode(
            _STORY_ID,
            {
                "episode_type": "status_clash",
                "goal": "A と B をぶつける",
                "focus_char_ids": ["char_a", "char_b"],
                "carry_over_hook_ids": [],
                "opened_turn": 10,
                "last_progress_turn": 11,
            },
        )
        await db.close_story_episode(
            episode_id,
            closed_turn=11,
            exit_condition="resolved",
            summary="張り合いが一段落した。",
        )
        await db.insert_story_episode(
            _STORY_ID,
            {
                "episode_type": "mystery",
                "goal": "残り火を追う",
                "focus_char_ids": ["char_c"],
                "carry_over_hook_ids": [],
                "opened_turn": 11,
                "last_progress_turn": 11,
            },
        )
        episode_arc_id = await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "episode",
                "title": "短期エピソード",
                "summary": "張り合いが一段落した。",
                "turn_from": 11,
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

        metrics = await _collect_story_quality_metrics(db, _STORY_ID, window_turns=5)

    assert metrics["active_scenes"] == 1
    assert metrics["closed_scenes_recent"] == 1
    assert metrics["open_hooks"] == 1
    assert metrics["active_tensions"] == 1
    assert metrics["live_interventions"] == 1
    assert metrics["relationship_pairs_changed_recent"] == 1
    assert metrics["active_relationship_modes"] == 3
    assert metrics["multi_mode_pairs_active"] == 1
    assert metrics["complementary_mode_pairs_active"] == 1
    assert metrics["single_mode_pairs_active"] == 1
    assert metrics["relationship_mode_conflict_attention_pairs"] == 1
    assert metrics["growth_candidates_pending"] == 1
    assert metrics["growth_commits_recent"] == 1
    assert metrics["growth_empty_recent"] == 1
    assert metrics["growth_quality_rejections_recent"] == 3
    assert metrics["growth_parse_failures_recent"] == 1
    assert metrics["growth_rollbacks_recent"] == 1
    assert metrics["active_episode_type"] == "mystery"
    assert metrics["episodes_closed_recent"] == 1
    assert metrics["quality_issues_recent"] == 13
    assert metrics["reply_focus_misses_recent"] == 1
    assert metrics["generic_reply_tails_recent"] == 1
    assert metrics["voice_flat_replies_recent"] == 1
    assert metrics["reply_variety_press_recent"] == 1
    assert metrics["reply_variety_condition_recent"] == 1
    assert metrics["reply_variety_redirect_recent"] == 0
    assert metrics["reply_dramatic_move_missing_recent"] == 1
    assert metrics["reply_soft_landing_recent"] == 1
    assert metrics["reply_shape_dominance_recent"] == 1
    assert metrics["reply_second_beat_reused_recent"] == 1
    assert metrics["reply_bland_shape_reused_recent"] == 1
    assert metrics["reply_pressure_shift_missing_recent"] == 1
    assert metrics["reply_story_flavor_weak_recent"] == 1
    assert metrics["reply_bland_keep_blocked_recent"] == 1
    assert metrics["reply_quality_keep_blocked_recent"] == 1
    assert metrics["reply_story_quality_keep_blocked_recent"] == 1
    assert metrics["reply_residual_keep_blocked_recent"] == 0
    assert metrics["reply_reused_opening_recent"] == 1
    assert metrics["reply_reused_ending_recent"] == 1
    assert metrics["reply_reused_second_beat_recent"] == 1
    assert metrics["signal_visibility_misses_recent"] == 1
    assert metrics["objective_visibility_misses_recent"] == 1
    assert metrics["signal_visibility_retry_recent"] == 1
    assert metrics["objective_visibility_retry_recent"] == 0
    assert metrics["episode_close_completion_rate"] == pytest.approx(1.0)
    assert metrics["episode_arcs_recent"] == 1
    assert metrics["scene_close_completion_rate"] == pytest.approx(1.0)
    assert metrics["scene_arcs_recent"] == 1
    assert metrics["novel_outputs_recent"] == 1


@pytest.mark.asyncio
async def test_collect_story_quality_metrics_counts_intervention_on_turn_boundary() -> None:
    async with _make_db() as db:
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "rooftop",
                "opened_turn": 12,
            },
        )
        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:20",
                "turn_number": 12,
                "char_id": "char_a",
                "msg_type": "reply",
                "target_char_id": "char_b",
                "place_id": "rooftop",
                "message": "境界ターンの確認。",
                "scene_id": scene_id,
            },
        )
        await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "opportunity",
                "title": "返答を迫る",
                "description": "ここで返答を迫る。",
                "prompt_injection": "いま返答を先延ばしにしない。",
                "scope": "char:char_a,char_b",
                "active_from_turn": 10,
                "active_until_turn": 12,
                "status": "active",
            },
        )

        metrics = await _collect_story_quality_metrics(db, _STORY_ID, window_turns=5)

    assert metrics["live_interventions"] == 1


@pytest.mark.asyncio
async def test_run_monitor_writes_initial_and_turn_advanced_entries(
    tmp_path: Path,
) -> None:
    """初回と turn 更新時だけ監視エントリを追記する。"""
    output_path = tmp_path / "monitor.md"

    config = MagicMock()
    config.llm = MagicMock()
    config.llm.providers = {"ollama": MagicMock(base_url="http://localhost:11434")}

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)

    snapshots = [
        {
            "captured_at": "2026-03-16T08:30:00+09:00",
            "story_id": "ankoku_gakuen",
            "max_turn_number": 10,
            "chat_log_count": 20,
            "last_char_id": "char_a",
            "last_message_len": 90,
            "last_sim_datetime": "2025-04-01T09:00",
            "engine_alive": True,
            "ollama_ok": True,
            "ollama_status": "HTTP 200",
            "gpu_summary": "gpu0: 1024/8192 MiB, util 10%",
            "recent_errors": [],
        },
        {
            "captured_at": "2026-03-16T08:30:05+09:00",
            "story_id": "ankoku_gakuen",
            "max_turn_number": 10,
            "chat_log_count": 21,
            "last_char_id": "char_b",
            "last_message_len": 91,
            "last_sim_datetime": "2025-04-01T09:00",
            "engine_alive": True,
            "ollama_ok": True,
            "ollama_status": "HTTP 200",
            "gpu_summary": "gpu0: 1200/8192 MiB, util 12%",
            "recent_errors": [],
        },
        {
            "captured_at": "2026-03-16T08:30:10+09:00",
            "story_id": "ankoku_gakuen",
            "max_turn_number": 11,
            "chat_log_count": 22,
            "last_char_id": "char_c",
            "last_message_len": 92,
            "last_sim_datetime": "2025-04-01T09:30",
            "engine_alive": True,
            "ollama_ok": True,
            "ollama_status": "HTTP 200",
            "gpu_summary": None,
            "recent_errors": ["gpu unavailable"],
        },
    ]

    with (
        patch("tools.monitor_engine_runtime.load_config", return_value=config),
        patch("tools.monitor_engine_runtime.DatabaseManager", return_value=fake_db),
        patch(
            "tools.monitor_engine_runtime._collect_runtime_snapshot",
            AsyncMock(side_effect=snapshots),
        ),
        patch("tools.monitor_engine_runtime.asyncio.sleep", AsyncMock()),
    ):
        result = await run_monitor(
            story_id="ankoku_gakuen",
            db_path="db/pocketrole.db",
            output_path=output_path,
            interval_sec=0.01,
            max_entries=2,
            engine_pid=4321,
        )

    text = output_path.read_text(encoding="utf-8")
    assert "# Runtime Monitor" in text
    assert text.count("## Snapshot") == 2
    assert "turn=10" in text
    assert "turn=11" in text
    assert "GPU: unavailable" in text
    assert result["entries_written"] == 2
    assert result["last_turn_number"] == 11


@pytest.mark.asyncio
async def test_run_monitor_writes_when_warning_changes_without_turn_advance(
    tmp_path: Path,
) -> None:
    """turn が進まなくても warning 集合が変われば追記する。"""
    output_path = tmp_path / "monitor.md"

    config = MagicMock()
    config.llm = MagicMock()
    config.llm.providers = {"ollama": MagicMock(base_url="http://localhost:11434")}

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)

    snapshots = [
        {
            "captured_at": "2026-03-16T08:30:00+09:00",
            "story_id": "ankoku_gakuen",
            "max_turn_number": 10,
            "chat_log_count": 20,
            "last_char_id": "char_a",
            "last_message_len": 90,
            "last_sim_datetime": "2025-04-01T09:00",
            "engine_alive": True,
            "ollama_ok": True,
            "ollama_status": "HTTP 200",
            "gpu_summary": None,
            "reply_ratio": 0.50,
            "all_chars_spoke_ratio": 0.30,
            "open_hooks": 0,
            "hooks_resolved_recent": 0,
            "active_tensions": 0,
            "live_interventions": 0,
            "quality_retry_rate": 0.0,
            "recent_errors": [],
        },
        {
            "captured_at": "2026-03-16T08:30:05+09:00",
            "story_id": "ankoku_gakuen",
            "max_turn_number": 10,
            "chat_log_count": 20,
            "last_char_id": "char_a",
            "last_message_len": 90,
            "last_sim_datetime": "2025-04-01T09:00",
            "engine_alive": True,
            "ollama_ok": True,
            "ollama_status": "HTTP 200",
            "gpu_summary": None,
            "reply_ratio": 0.10,
            "all_chars_spoke_ratio": 0.30,
            "open_hooks": 0,
            "hooks_resolved_recent": 0,
            "active_tensions": 0,
            "live_interventions": 0,
            "quality_retry_rate": 0.0,
            "recent_errors": [],
        },
    ]

    with (
        patch("tools.monitor_engine_runtime.load_config", return_value=config),
        patch("tools.monitor_engine_runtime.DatabaseManager", return_value=fake_db),
        patch(
            "tools.monitor_engine_runtime._collect_runtime_snapshot",
            AsyncMock(side_effect=snapshots),
        ),
        patch("tools.monitor_engine_runtime.asyncio.sleep", AsyncMock()),
    ):
        result = await run_monitor(
            story_id="ankoku_gakuen",
            db_path="db/pocketrole.db",
            output_path=output_path,
            interval_sec=0.01,
            max_entries=2,
            engine_pid=4321,
        )

    text = output_path.read_text(encoding="utf-8")
    assert text.count("## Snapshot") == 2
    assert "reply_ratio_low" in text
    assert result["entries_written"] == 2


@pytest.mark.asyncio
async def test_collect_story_quality_metrics_includes_sessionless_monologue_metrics() -> None:
    """quality metrics は sessionless monologue 指標を含む。"""
    async with _make_db() as db:
        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:00",
                "turn_number": 20,
                "char_id": "char_a",
                "msg_type": "monologue",
                "place_id": "classroom",
                "message": "ひとりで話す。",
            },
        )
        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:00",
                "turn_number": 20,
                "char_id": "char_b",
                "msg_type": "monologue",
                "place_id": "classroom",
                "message": "続けてひとりで話す。",
            },
        )
        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:00",
                "turn_number": 20,
                "char_id": "char_c",
                "msg_type": "reply",
                "target_char_id": "char_a",
                "place_id": "classroom",
                "message": "返事する。",
                "conversation_session_id": 11,
            },
        )
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "solo",
                "status": "active",
                "place_id": "classroom",
                "opened_turn": 20,
            },
        )
        await db.close_story_scene(
            scene_id,
            outcome_type="pause",
            outcome_summary="ひとりの時間が終わった。",
            closed_turn=20,
        )

        metrics = await _collect_story_quality_metrics(db, _STORY_ID, window_turns=20)

    assert metrics["sessionless_monologues_recent"] == 2
    assert metrics["solo_scenes_recent"] == 1
    assert metrics["sessionless_monologue_ratio"] == 0.67


@pytest.mark.asyncio
async def test_collect_story_quality_metrics_includes_interaction_pattern_metrics() -> None:
    """quality metrics は interaction pattern 指標を含む。"""
    async with _make_db() as db:
        tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "A と B が張り合っている。",
                "involved_chars": ["char_a", "char_b"],
                "detected_turn": 18,
                "status": "escalating",
            },
        )
        await db.insert_interaction_pattern(
            _STORY_ID,
            {
                "pattern_type": "status_clash",
                "status": "active",
                "title": "張り合い",
                "description": "A と B が張り合っている。",
                "involved_chars": ["char_a", "char_b"],
                "dedupe_key": f"tension:{tension_id}:status_clash",
                "source_tension_id": tension_id,
                "first_detected_turn": 20,
                "last_detected_turn": 20,
                "recurrence_count": 2,
                "intensity": 0.8,
                "confidence": 0.8,
            },
        )

        metrics = await _collect_story_quality_metrics(db, _STORY_ID, window_turns=5)

    assert metrics["active_interaction_patterns"] == 1
    assert metrics["patterns_detected_recent"] == 1
    assert metrics["pattern_recurrences_recent"] == 1
    assert metrics["dominant_pattern_types"] == ["status_clash"]


@pytest.mark.asyncio
async def test_collect_story_quality_metrics_includes_scene_close_backlog_metrics() -> None:
    async with _make_db() as db:
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "closed",
                "place_id": "rooftop",
                "opened_turn": 18,
                "closed_turn": 20,
            },
        )
        arc_id = await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "scene",
                "title": "既存 arc",
                "summary": "summary",
                "turn_from": 20,
                "source_scene_id": 7,
            },
        )
        await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "closed",
                "place_id": "classroom",
                "opened_turn": 19,
                "closed_turn": 20,
            },
        )
        await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "scene",
                "title": "missing prose",
                "summary": "summary",
                "turn_from": 20,
                "source_scene_id": scene_id,
            },
        )
        await db.insert_novel_output(
            _STORY_ID,
            arc_id,
            {
                "content_type": "prose",
                "content": "scene prose",
                "source_log_ids": [],
                "ordering": 1,
            },
        )
        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:00",
                "turn_number": 20,
                "char_id": "char_a",
                "msg_type": "reply",
                "target_char_id": "char_b",
                "place_id": "rooftop",
                "message": "返事する。",
            },
        )

        metrics = await _collect_story_quality_metrics(db, _STORY_ID, window_turns=20)

    assert metrics["closed_scenes_without_arc_recent"] == 1
    assert metrics["scene_arcs_without_novel_recent"] == 1
    assert metrics["scene_close_backlog_recent"] == 2
    assert metrics["scene_close_missing_arc_recent"] == 1
    assert metrics["scene_close_missing_novel_recent"] == 1
    assert metrics["scene_close_backlog_recovered_recent"] == 0


def test_render_markdown_entry_includes_reply_flat_breakdown_metrics() -> None:
    entry = render_markdown_entry(
        {
            "captured_at": "2026-03-16T08:30:00+09:00",
            "story_id": "ankoku_gakuen",
            "engine_alive": True,
            "ollama_ok": True,
            "ollama_status": "HTTP 200",
            "gpu_summary": "gpu0 ok",
            "max_turn_number": 42,
            "chat_log_count": 84,
            "last_char_id": "yokaze_yuuma",
            "last_message_len": 120,
            "last_sim_datetime": "2025-04-01T10:30",
            "speaking_chars_per_turn_avg": 2.33,
            "all_chars_spoke_ratio": 0.32,
            "reply_ratio": 0.41,
            "group_ratio": 0.30,
            "monologue_ratio": 0.29,
            "sessionless_monologue_ratio": 0.09,
            "active_scenes": 1,
            "closed_scenes_recent": 1,
            "solo_scenes_recent": 0,
            "open_hooks": 0,
            "hooks_created_recent": 0,
            "hooks_resolved_recent": 0,
            "active_tensions": 0,
            "intervention_eligible_now": 0,
            "max_character_turn_gap": 0,
            "stale_char_ids": [],
            "resolved_tensions_recent": 0,
            "live_interventions": 0,
            "relationship_pairs_changed_recent": 0,
            "active_relationship_modes": 0,
            "relationship_mode_reinforcements_recent": 0,
            "relationship_mode_decay_updates_recent": 0,
            "multi_mode_pairs_active": 0,
            "dominant_relationship_modes": [],
            "active_canon_bits": 0,
            "canon_reinforcements_recent": 0,
            "canon_promotions_recent": 0,
            "canon_reignitions_recent": 0,
            "canon_triggered_hooks_open": 0,
            "dominant_canon_levels": [],
            "dominant_canon_motifs": [],
            "active_dramatic_pressures": 0,
            "pressure_reinforcements_recent": 0,
            "dominant_pressure_types": [],
            "max_pressure_score": 0.0,
            "active_interaction_patterns": 0,
            "patterns_detected_recent": 0,
            "pattern_recurrences_recent": 0,
            "dominant_pattern_types": [],
            "active_episode_id": None,
            "active_episode_type": None,
            "active_episode_age": 0,
            "episodes_closed_recent": 0,
            "active_chapter_id": None,
            "active_chapter_beat": None,
            "active_chapter_age": 0,
            "active_director_persona_id": None,
            "director_satisfaction_overall": None,
            "director_satisfaction_trend": None,
            "director_satisfaction_tension": None,
            "director_satisfaction_pacing": None,
            "director_satisfaction_surprise": None,
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
            "reply_quality_normalizations_recent": 2,
            "reply_quality_fallbacks_recent": 0,
            "reply_focus_misses_recent": 0,
            "generic_reply_tails_recent": 2,
            "voice_flat_replies_recent": 1,
            "reply_flat_generic_tail_recent": 2,
            "reply_flat_voice_recent": 1,
            "reply_flat_reused_tail_recent": 1,
            "reply_bland_shape_reused_recent": 1,
            "reply_pressure_shift_missing_recent": 1,
            "reply_story_flavor_weak_recent": 1,
            "reply_bland_keep_blocked_recent": 1,
            "scene_close_completion_rate": 1.0,
            "episode_close_completion_rate": 0.0,
            "episode_arcs_recent": 0,
            "scene_arcs_recent": 0,
            "novel_outputs_recent": 0,
            "warnings": ["reply_quality_flat"],
            "recent_errors": [],
        }
    )

    assert "reply_flat_generic_tail_recent=2" in entry
    assert "reply_flat_voice_recent=1" in entry
    assert "reply_flat_reused_tail_recent=1" in entry
    assert "reply_bland_shape_reused_recent=1" in entry
    assert "reply_pressure_shift_missing_recent=1" in entry
    assert "reply_story_flavor_weak_recent=1" in entry
    assert "reply_bland_keep_blocked_recent=1" in entry
