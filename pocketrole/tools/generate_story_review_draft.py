#!/usr/bin/env python3
"""tools/generate_story_review_draft.py — story quality review の下書きを生成する CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from db.db_manager import DatabaseManager
from engine.story_intent import detect_story_intent_gaps, get_story_intent_profile
from tools.monitor_engine_runtime import (
    _CHAPTER_PROGRESS_STALL_TURNS,
    _collect_story_progress,
    _collect_story_quality_metrics,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STORY_NOT_FOUND = 2

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_DEFAULT_DB = Path(__file__).parent.parent / "db" / "pocketrole.db"
_DEFAULT_MONITOR_DIR = Path(__file__).parent.parent / "docs" / "monitoring"
_DEFAULT_REVIEW_DIR = Path(__file__).parent.parent / "docs" / "system_review" / "generated"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="story quality review の下書きを Markdown へ生成する")
    parser.add_argument("--story", required=True, help="対象ストーリーID")
    parser.add_argument("--db", default=str(_DEFAULT_DB), help="SQLite DB ファイルパス")
    parser.add_argument("--output", type=Path, default=None, help="出力先 Markdown パス")
    parser.add_argument(
        "--window-turns",
        type=int,
        default=20,
        help="品質メトリクス集計に使う turn 窓（デフォルト: 20）",
    )
    parser.add_argument(
        "--monitor-file",
        type=Path,
        default=None,
        help="monitor Markdown を明示指定する",
    )
    parser.add_argument(
        "--monitor-dir",
        type=Path,
        default=_DEFAULT_MONITOR_DIR,
        help="monitor Markdown を探索するディレクトリ",
    )
    return parser.parse_args(argv)


def _default_output_path(story_id: str) -> Path:
    date_token = datetime.now(UTC).astimezone().date().isoformat()
    return _DEFAULT_REVIEW_DIR / f"{story_id}_review_draft_{date_token}.md"


def _resolve_monitor_file(
    story_id: str,
    *,
    monitor_file: Path | None,
    monitor_dir: Path,
) -> Path | None:
    if monitor_file is not None:
        return monitor_file if monitor_file.exists() else None

    candidates = sorted(
        monitor_dir.glob(f"{story_id}_runtime_monitor*.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _parse_latest_monitor_snapshot(monitor_path: Path | None) -> dict[str, Any]:
    if monitor_path is None or not monitor_path.exists():
        return {
            "monitor_source": "none",
            "monitor_captured_at": None,
            "monitor_warnings": [],
            "monitor_recent_errors": [],
        }

    text = monitor_path.read_text(encoding="utf-8")
    blocks = re.split(r"^## Snapshot ", text, flags=re.MULTILINE)
    if len(blocks) <= 1:
        return {
            "monitor_source": str(monitor_path),
            "monitor_captured_at": None,
            "monitor_warnings": [],
            "monitor_recent_errors": [],
        }

    latest_block = blocks[-1]
    first_line, _, remainder = latest_block.partition("\n")
    captured_at = first_line.strip()
    warnings_match = re.search(r"^- (.+)$", remainder, flags=re.MULTILINE)
    warning_summary = warnings_match.group(1).strip() if warnings_match else "none"
    errors_match = re.search(
        r"^- Recent warnings/errors: (.+)$",
        remainder,
        flags=re.MULTILINE,
    )
    errors_summary = errors_match.group(1).strip() if errors_match else "none"
    warnings = [] if warning_summary == "none" else [part.strip() for part in warning_summary.split("|")]
    recent_errors = [] if errors_summary == "none" else [part.strip() for part in errors_summary.split("|")]
    return {
        "monitor_source": str(monitor_path),
        "monitor_captured_at": captured_at or None,
        "monitor_warnings": warnings,
        "monitor_recent_errors": recent_errors,
    }


async def _fetch_scalar(
    db: DatabaseManager,
    sql: str,
    params: tuple[Any, ...],
) -> int:
    assert db._conn is not None
    cursor = await db._conn.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


async def _collect_review_snapshot(
    db: DatabaseManager,
    story_id: str,
    *,
    window_turns: int,
) -> dict[str, Any]:
    progress = await _collect_story_progress(db, story_id)
    quality = await _collect_story_quality_metrics(db, story_id, window_turns=window_turns)
    intent_profile = get_story_intent_profile(story_id)

    assert db._conn is not None
    turn_cursor = await db._conn.execute(
        """
        SELECT
            MIN(turn_number) AS min_turn,
            MAX(turn_number) AS max_turn
        FROM chat_logs
        WHERE story_id = ?
        """,
        (story_id,),
    )
    turn_row = await turn_cursor.fetchone()
    active_character_count = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM characters WHERE story_id = ? AND is_active = 1",
        (story_id,),
    )
    return {
        "story_id": story_id,
        **progress,
        **quality,
        "story_intent_mode": intent_profile.primary_mode,
        "min_turn_number": turn_row["min_turn"] if turn_row is not None else None,
        "max_turn_number": turn_row["max_turn"] if turn_row is not None else None,
        "active_character_count": active_character_count,
        "window_turns": window_turns,
    }


async def _collect_cause_inputs(
    db: DatabaseManager,
    story_id: str,
    *,
    window_turns: int,
) -> dict[str, float | int]:
    assert db._conn is not None
    latest_turn = await _fetch_scalar(
        db,
        "SELECT COALESCE(MAX(turn_number), 0) FROM chat_logs WHERE story_id = ?",
        (story_id,),
    )
    since_turn = max(0, latest_turn - max(window_turns, 1) + 1)

    recent_reply_logs = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM chat_logs
        WHERE story_id = ?
          AND turn_number >= ?
          AND msg_type = 'reply'
        """,
        (story_id, since_turn),
    )
    target_specific_reply_logs = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM chat_logs
        WHERE story_id = ?
          AND turn_number >= ?
          AND msg_type = 'reply'
          AND target_char_id IS NOT NULL
        """,
        (story_id, since_turn),
    )
    recent_scenes = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_scenes
        WHERE story_id = ?
          AND opened_turn >= ?
        """,
        (story_id, since_turn),
    )
    generic_objective_scenes = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_scenes
        WHERE story_id = ?
          AND opened_turn >= ?
          AND COALESCE(NULLIF(TRIM(objective), ''), '自然発生の会話') = '自然発生の会話'
        """,
        (story_id, since_turn),
    )
    canon_triggered_unresolved_hooks = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_hooks
        WHERE story_id = ?
          AND status = 'open'
          AND source_canon_bit_id IS NOT NULL
        """,
        (story_id,),
    )
    growth_pending = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM character_growth_candidates
        WHERE story_id = ?
          AND status = 'pending'
        """,
        (story_id,),
    )
    growth_commits = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM character_evolution
        WHERE story_id = ?
          AND turn_number >= ?
        """,
        (story_id, since_turn),
    )
    growth_rejections = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type IN (
              'growth_noop_rejected',
              'growth_generic_rejected',
              'growth_field_mismatch_rejected',
              'growth_low_quality_rejected'
          )
        """,
        (story_id, since_turn),
    )
    growth_empty = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'growth_llm_empty'
        """,
        (story_id, since_turn),
    )

    return {
        "recent_reply_logs": recent_reply_logs,
        "target_specific_reply_ratio": (
            target_specific_reply_logs / recent_reply_logs if recent_reply_logs > 0 else 0.0
        ),
        "generic_scene_objective_ratio": (
            generic_objective_scenes / recent_scenes if recent_scenes > 0 else 0.0
        ),
        "canon_triggered_unresolved_hooks": canon_triggered_unresolved_hooks,
        "growth_pending_commit_ratio": growth_pending / max(growth_commits, 1),
        "growth_rejection_empty_ratio": (growth_rejections + growth_empty) / max(window_turns, 1),
    }


def _detect_strengths(snapshot: dict[str, Any], monitor: dict[str, Any]) -> list[str]:
    window_turns = int(snapshot["window_turns"])
    strengths: list[str] = []
    if float(snapshot["reply_ratio"]) >= 0.20:
        strengths.append(
            f"直近{window_turns} turn の `reply_ratio={snapshot['reply_ratio']:.2f}` で、対話の往復が最低ラインを超えている。"
        )
    if float(snapshot["all_chars_spoke_ratio"]) <= 0.60:
        strengths.append(
            f"`all_chars_spoke_ratio={snapshot['all_chars_spoke_ratio']:.2f}` で、毎ターン全員発話からは外れている。"
        )
    if float(snapshot.get("sessionless_monologue_ratio") or 0.0) <= 0.45:
        strengths.append(
            f"`sessionless_monologue_ratio={snapshot['sessionless_monologue_ratio']:.2f}` で、会話外の独演は抑えられている。"
        )
    if int(snapshot["hooks_resolved_recent"]) > 0:
        strengths.append(
            f"`hooks_resolved_recent={snapshot['hooks_resolved_recent']}` で、持ち越し hook の回収が動いている。"
        )
    if int(snapshot.get("active_interaction_patterns") or 0) > 0:
        dominant = ", ".join(snapshot.get("dominant_pattern_types") or [])
        strengths.append(
            f"`active_interaction_patterns={snapshot['active_interaction_patterns']}` で、pattern layer が起動している。dominant=`{dominant or 'none'}`。"
        )
    if snapshot.get("active_episode_id") is not None:
        strengths.append(
            f"`active_episode_type={snapshot.get('active_episode_type') or 'none'}` / `active_episode_age={snapshot.get('active_episode_age', 0)}` で、短期 continuity が維持されている。"
        )
    if int(snapshot.get("episodes_closed_recent") or 0) > 0 and int(snapshot.get("episode_arcs_recent") or 0) > 0:
        strengths.append(
            f"`episodes_closed_recent={snapshot.get('episodes_closed_recent', 0)}` / `episode_arcs_recent={snapshot.get('episode_arcs_recent', 0)}` で、episode close artifact が残っている。"
        )
    if int(snapshot["relationship_pairs_changed_recent"]) > 0:
        strengths.append(
            f"`relationship_pairs_changed_recent={snapshot['relationship_pairs_changed_recent']}` で、会話結果が関係変化として残っている。"
        )
    if int(snapshot.get("active_relationship_modes") or 0) > 0:
        strengths.append(
            f"`active_relationship_modes={snapshot.get('active_relationship_modes', 0)}` / `dominant_relationship_modes={', '.join(snapshot.get('dominant_relationship_modes', [])) or 'none'}` で、関係性の脚本モードが持続している。"
        )
    if int(snapshot.get("complementary_mode_pairs_active") or snapshot.get("multi_mode_pairs_active") or 0) > 0:
        strengths.append(
            f"`complementary_mode_pairs_active={snapshot.get('complementary_mode_pairs_active', 0)}` / "
            f"`relationship_mode_conflict_attention_pairs={snapshot.get('relationship_mode_conflict_attention_pairs', 0)}` で、同じ pair に conflict/attention の relationship mode が共存している。"
        )
    if int(snapshot.get("active_canon_bits") or 0) > 0:
        strengths.append(
            f"`active_canon_bits={snapshot.get('active_canon_bits', 0)}` / `dominant_canon_motifs={', '.join(snapshot.get('dominant_canon_motifs', [])) or 'none'}` で、偶発的な当たりが継続状態へ昇格している。"
        )
    if int(snapshot.get("canon_reignitions_recent") or 0) > 0:
        strengths.append(
            f"`canon_reignitions_recent={snapshot.get('canon_reignitions_recent', 0)}` / `canon_triggered_hooks_open={snapshot.get('canon_triggered_hooks_open', 0)}` で、canon bit が hook として再発火している。"
        )
    if int(snapshot.get("active_canon_profile_overlays") or 0) > 0:
        strengths.append(
            f"`active_canon_profile_overlays={snapshot.get('active_canon_profile_overlays', 0)}` / `canon_writebacks_recent={snapshot.get('canon_writebacks_recent', 0)}` で、stable canon が prompt-visible profile へ書き戻されている。"
        )
    if int(snapshot.get("active_dramatic_pressures") or 0) > 0:
        strengths.append(
            f"`active_dramatic_pressures={snapshot.get('active_dramatic_pressures', 0)}` / `dominant_pressure_types={', '.join(snapshot.get('dominant_pressure_types', [])) or 'none'}` で、押すべき火種が persisted state として観測できている。"
        )
    if int(snapshot["growth_commits_recent"]) > 0:
        strengths.append(
            f"`growth_commits_recent={snapshot['growth_commits_recent']}` で、キャラ変化が runtime overlay へ反映されている。"
        )
    if int(snapshot.get("growth_parse_failures_recent") or 0) == 0:
        strengths.append("直近観測窓では `growth_parse_failed` が出ておらず、growth parse は安定している。")
    if int(snapshot.get("quality_fallbacks_recent") or 0) == 0:
        strengths.append("直近観測窓では quality fallback が出ておらず、通常生成の保存が維持できている。")
    if int(snapshot.get("reply_quality_fallbacks_recent") or 0) == 0:
        strengths.append("reply 系の quality fallback が出ておらず、会話の往復は通常生成で残せている。")
    if int(snapshot["scene_arcs_recent"]) > 0 and int(snapshot["novel_outputs_recent"]) > 0:
        strengths.append(
            f"`scene_arcs_recent={snapshot['scene_arcs_recent']}` / `novel_outputs_recent={snapshot['novel_outputs_recent']}` で、scene close prose が生成されている。"
        )
    if monitor["monitor_source"] != "none" and not monitor["monitor_warnings"] and not monitor["monitor_recent_errors"]:
        strengths.append("最新 monitor snapshot では health warning が出ておらず、運転自体は安定している。")
    return strengths


def _detect_gaps(snapshot: dict[str, Any], monitor: dict[str, Any]) -> list[tuple[str, str]]:
    gaps: list[tuple[str, str]] = []
    if float(snapshot["reply_ratio"]) < 0.20:
        gaps.append(
            (
                "reply_ratio_low",
                f"`reply_ratio={snapshot['reply_ratio']:.2f}` と低く、往復会話より独演寄りになっている。",
            )
        )
    if float(snapshot["all_chars_spoke_ratio"]) > 0.60:
        gaps.append(
            (
                "all_chars_spoke_high",
                f"`all_chars_spoke_ratio={snapshot['all_chars_spoke_ratio']:.2f}` と高く、まだラウンド制感が残っている。",
            )
        )
    if float(snapshot.get("sessionless_monologue_ratio") or 0.0) > 0.45:
        gaps.append(
            (
                "sessionless_monologue_high",
                f"`sessionless_monologue_ratio={snapshot['sessionless_monologue_ratio']:.2f}` / `sessionless_monologues_recent={snapshot['sessionless_monologues_recent']}` で、会話外の独演がまだ多い。",
            )
        )
    if int(snapshot["open_hooks"]) > 0 and int(snapshot["hooks_resolved_recent"]) == 0:
        gaps.append(
            (
                "hook_resolution_stalled",
                f"`open_hooks={snapshot['open_hooks']}` に対し `hooks_resolved_recent=0` で、未回収の火種が滞留している。",
            )
        )
    if (
        (int(snapshot.get("open_hooks") or 0) > 0 or int(snapshot.get("active_tensions") or 0) > 0)
        and int(snapshot.get("patterns_detected_recent") or 0) == 0
    ):
        gaps.append(
            (
                "pattern_detection_weak",
                f"`open_hooks={snapshot.get('open_hooks', 0)}` / `active_tensions={snapshot.get('active_tensions', 0)}` があるのに `patterns_detected_recent=0` で、friction pattern 化が弱い。",
            )
        )
    if int(snapshot["max_character_turn_gap"]) >= 3:
        stale_chars = ", ".join(snapshot["stale_char_ids"]) if snapshot["stale_char_ids"] else "none"
        gaps.append(
            (
                "turn_gap_high",
                f"`max_character_turn_gap={snapshot['max_character_turn_gap']}` / `stale_char_ids={stale_chars}` で、scene-aware の取り残しが残っている。",
            )
        )
    if int(snapshot["intervention_eligible_now"]) > 0 and int(snapshot["live_interventions"]) == 0:
        gaps.append(
            (
                "tension_without_intervention",
                f"`intervention_eligible_now={snapshot['intervention_eligible_now']}` に対し `live_interventions=0` で、director 介入が追いついていない。",
            )
        )
    if float(snapshot["quality_retry_rate"]) > 0.15 or int(snapshot["quality_issues_recent"]) > 0:
        gaps.append(
            (
                "quality_retry_high",
                f"`quality_retry_rate={snapshot['quality_retry_rate']:.2f}` / `quality_issues_recent={snapshot['quality_issues_recent']}` で、生成品質の手戻りが見える。",
            )
        )
    if int(snapshot.get("quality_fallbacks_recent") or 0) > 0:
        gaps.append(
            (
                "quality_fallback_high",
                f"`quality_fallbacks_recent={snapshot.get('quality_fallbacks_recent', 0)}` で、通常生成が fallback 保存へ落ちる比率がまだ高い。",
            )
        )
    if int(snapshot.get("reply_quality_fallbacks_recent") or 0) > 0:
        focus_root = int(snapshot.get("reply_fallback_focus_missing_recent") or 0)
        direct_root = int(snapshot.get("reply_fallback_direct_reaction_recent") or 0)
        kept_recent = int(snapshot.get("reply_retry_kept_recent") or 0)
        gaps.append(
            (
                "reply_quality_fallback_high",
                f"`reply_quality_fallbacks_recent={snapshot.get('reply_quality_fallbacks_recent', 0)}` / "
                f"`reply_fallback_focus_missing_recent={focus_root}` / "
                f"`reply_fallback_direct_reaction_recent={direct_root}` / "
                f"`reply_retry_kept_recent={kept_recent}` で、reply 系の通常生成がまだ gate に弾かれている。",
            )
        )
    if (
        int(snapshot.get("reply_focus_misses_recent") or 0) > 0
        or int(snapshot.get("generic_reply_tails_recent") or 0) > 0
        or int(snapshot.get("voice_flat_replies_recent") or 0) > 0
        or int(snapshot.get("reply_reused_opening_recent") or 0) > 0
        or int(snapshot.get("reply_reused_ending_recent") or 0) > 0
        or int(snapshot.get("reply_reused_second_beat_recent") or 0) > 0
    ):
        gaps.append(
            (
                "reply_quality_flat",
                f"`reply_focus_misses_recent={snapshot.get('reply_focus_misses_recent', 0)}` / "
                f"`generic_reply_tails_recent={snapshot.get('generic_reply_tails_recent', 0)}` / "
                f"`voice_flat_replies_recent={snapshot.get('voice_flat_replies_recent', 0)}` / "
                f"`reply_flat_generic_tail_recent={snapshot.get('reply_flat_generic_tail_recent', 0)}` / "
                f"`reply_flat_voice_recent={snapshot.get('reply_flat_voice_recent', 0)}` / "
                f"`reply_flat_reused_tail_recent={snapshot.get('reply_flat_reused_tail_recent', 0)}` / "
                f"`reply_variety_press_recent={snapshot.get('reply_variety_press_recent', 0)}` / "
                f"`reply_variety_condition_recent={snapshot.get('reply_variety_condition_recent', 0)}` / "
                f"`reply_variety_redirect_recent={snapshot.get('reply_variety_redirect_recent', 0)}` / "
                f"`reply_dramatic_move_missing_recent={snapshot.get('reply_dramatic_move_missing_recent', 0)}` / "
                f"`reply_soft_landing_recent={snapshot.get('reply_soft_landing_recent', 0)}` / "
                f"`reply_shape_dominance_recent={snapshot.get('reply_shape_dominance_recent', 0)}` / "
                f"`reply_second_beat_reused_recent={snapshot.get('reply_second_beat_reused_recent', 0)}` / "
                f"`reply_bland_shape_reused_recent={snapshot.get('reply_bland_shape_reused_recent', 0)}` / "
                f"`reply_pressure_shift_missing_recent={snapshot.get('reply_pressure_shift_missing_recent', 0)}` / "
                f"`reply_story_flavor_weak_recent={snapshot.get('reply_story_flavor_weak_recent', 0)}` / "
                f"`reply_quality_keep_blocked_recent={snapshot.get('reply_quality_keep_blocked_recent', 0)}` / "
                f"`reply_story_quality_keep_blocked_recent={snapshot.get('reply_story_quality_keep_blocked_recent', 0)}` / "
                f"`reply_residual_keep_blocked_recent={snapshot.get('reply_residual_keep_blocked_recent', 0)}` / "
                f"`reply_bland_keep_blocked_recent={snapshot.get('reply_bland_keep_blocked_recent', 0)}` / "
                f"`reply_reused_opening_recent={snapshot.get('reply_reused_opening_recent', 0)}` / "
                f"`reply_reused_ending_recent={snapshot.get('reply_reused_ending_recent', 0)}` / "
                f"`reply_reused_second_beat_recent={snapshot.get('reply_reused_second_beat_recent', 0)}` で、"
                "reply は保存されていても返し先やキャラ差がまだ平板である。",
            )
        )
    if int(snapshot["growth_candidates_pending"]) >= 3 and int(snapshot["growth_commits_recent"]) == 0:
        gaps.append(
            (
                "growth_stalled",
                f"`growth_candidates_pending={snapshot['growth_candidates_pending']}` に対し `growth_commits_recent=0` で、成長候補が人格変化まで届いていない。",
            )
        )
    if (
        int(snapshot.get("growth_commits_recent") or 0) == 0
        and (
            int(snapshot.get("growth_empty_recent") or 0) > 0
            or int(snapshot.get("growth_quality_rejections_recent") or 0) > 0
        )
    ):
        gaps.append(
            (
                "growth_quality_flat",
                f"`growth_empty_recent={snapshot.get('growth_empty_recent', 0)}` / `growth_quality_rejections_recent={snapshot.get('growth_quality_rejections_recent', 0)}` で、evidence はあるのに meaningful growth が commit へ届いていない。",
            )
        )
    if int(snapshot.get("growth_parse_failures_recent") or 0) > 0:
        gaps.append(
            (
                "growth_parse_failed",
                f"`growth_parse_failures_recent={snapshot.get('growth_parse_failures_recent', 0)}` で、GrowthEngine の structured parse failure が再発している。",
            )
        )
    if int(snapshot.get("growth_rollbacks_recent") or 0) > 0:
        gaps.append(
            (
                "growth_write_rolled_back",
                f"`growth_rollbacks_recent={snapshot.get('growth_rollbacks_recent', 0)}` で、GrowthEngine write batch の rollback が発生している。",
            )
        )
    if (
        int(snapshot["relationship_pairs_changed_recent"]) > 0
        and int(snapshot.get("active_relationship_modes") or 0) == 0
    ):
        gaps.append(
            (
                "relationship_mode_missing",
                f"`relationship_pairs_changed_recent={snapshot['relationship_pairs_changed_recent']}` に対し `active_relationship_modes=0` で、関係変化が脚本モードへ昇格していない。",
            )
        )
    if (
        int(snapshot.get("active_relationship_modes") or 0) > 0
        and int(snapshot.get("relationship_mode_reinforcements_recent") or 0) == 0
    ):
        gaps.append(
            (
                "relationship_mode_stale",
                f"`active_relationship_modes={snapshot.get('active_relationship_modes', 0)}` に対し `relationship_mode_reinforcements_recent=0` で、関係モードが更新されていない。",
            )
        )
    if (
        int(snapshot.get("active_relationship_modes") or 0) > 0
        and int(snapshot.get("relationship_pairs_changed_recent") or 0) > 0
        and int(
            snapshot.get(
                "complementary_mode_pairs_active",
                snapshot.get("multi_mode_pairs_active") or 0,
            )
            or 0
        ) == 0
        and int(snapshot.get("single_mode_pairs_active") or 0) > 0
    ):
        gaps.append(
            (
                "relationship_mode_flat",
                f"`active_relationship_modes={snapshot.get('active_relationship_modes', 0)}` / "
                f"`complementary_mode_pairs_active={snapshot.get('complementary_mode_pairs_active', 0)}` / "
                f"`single_mode_pairs_active={snapshot.get('single_mode_pairs_active', 0)}` / "
                f"`complementary_mode_reinforcements_recent={snapshot.get('complementary_mode_reinforcements_recent', 0)}` / "
                f"`complementary_mode_deactivations_recent={snapshot.get('complementary_mode_deactivations_recent', 0)}` で、pairwise mode がまだ single-layer に留まっている。",
            )
        )
    if (
        (
            int(snapshot.get("active_interaction_patterns") or 0) > 0
            or int(snapshot.get("active_relationship_modes") or 0) > 0
            or snapshot.get("active_episode_id") is not None
        )
        and int(snapshot.get("active_canon_bits") or 0) == 0
    ):
        gaps.append(
            (
                "canon_missing",
                "pattern / relationship mode / episode はあるのに active canon bit が無く、当たりが継続状態へ昇格していない。",
            )
        )
    if (
        int(snapshot.get("active_canon_bits") or 0) > 0
        and int(snapshot.get("canon_reinforcements_recent") or 0) == 0
    ):
        gaps.append(
            (
                "canon_stale",
                f"`active_canon_bits={snapshot.get('active_canon_bits', 0)}` に対し `canon_reinforcements_recent=0` で、canon bit が再強化されていない。",
            )
        )
    if (
        int(snapshot.get("canon_reinforcements_recent") or 0) > 0
        and int(snapshot.get("canon_promotions_recent") or 0) == 0
        and int(snapshot.get("active_canon_bits") or 0) > 0
        and all(level == "momentary_bit" for level in list(snapshot.get("dominant_canon_levels") or []))
    ):
        gaps.append(
            (
                "canon_promotion_stalled",
                f"`canon_reinforcements_recent={snapshot.get('canon_reinforcements_recent', 0)}` があるのに `canon_promotions_recent=0` で、canon の昇格が止まっている。",
            )
        )
    if (
        int(snapshot.get("active_canon_bits") or 0) > 0
        and int(snapshot.get("canon_reignitions_recent") or 0) == 0
    ):
        gaps.append(
            (
                "canon_reignition_missing",
                f"`active_canon_bits={snapshot.get('active_canon_bits', 0)}` に対し `canon_reignitions_recent=0` で、canon が hook として再発火していない。",
            )
        )
    if (
        int(snapshot.get("active_canon_bits") or 0) > 0
        and int(snapshot.get("active_canon_profile_overlays") or 0) == 0
    ):
        gaps.append(
            (
                "canon_writeback_missing",
                f"`active_canon_bits={snapshot.get('active_canon_bits', 0)}` に対し `active_canon_profile_overlays=0` で、stable canon が profile へ戻っていない。",
            )
        )
    if (
        int(snapshot.get("active_canon_profile_overlays") or 0) > 0
        and int(snapshot.get("canon_writebacks_recent") or 0) == 0
    ):
        gaps.append(
            (
                "canon_writeback_stale",
                f"`active_canon_profile_overlays={snapshot.get('active_canon_profile_overlays', 0)}` に対し `canon_writebacks_recent=0` で、canon writeback が更新されていない。",
            )
        )
    if (
        int(snapshot.get("canon_triggered_hooks_open") or 0) > 0
        and int(snapshot.get("hooks_resolved_recent") or 0) == 0
    ):
        gaps.append(
            (
                "canon_reignition_stuck",
                f"`canon_triggered_hooks_open={snapshot.get('canon_triggered_hooks_open', 0)}` に対し `hooks_resolved_recent=0` で、canon 起点の hook が停滞している。",
            )
        )
    if (
        (
            int(snapshot.get("active_interaction_patterns") or 0) > 0
            or int(snapshot.get("active_relationship_modes") or 0) > 0
            or int(snapshot.get("active_canon_bits") or 0) > 0
            or snapshot.get("active_episode_id") is not None
        )
        and int(snapshot.get("active_dramatic_pressures") or 0) == 0
    ):
        gaps.append(
            (
                "pressure_missing",
                "pattern / relationship mode / canon / episode はあるのに active dramatic pressure が無く、押すべき火種の解釈レイヤーが立っていない。",
            )
        )
    if (
        int(snapshot.get("active_dramatic_pressures") or 0) > 0
        and int(snapshot.get("pressure_reinforcements_recent") or 0) == 0
    ):
        gaps.append(
            (
                "pressure_stale",
                f"`active_dramatic_pressures={snapshot.get('active_dramatic_pressures', 0)}` に対し `pressure_reinforcements_recent=0` で、dramatic pressure が更新されていない。",
            )
        )
    if (
        "payoff_ready" in list(snapshot.get("dominant_pressure_types") or [])
        and int(snapshot.get("episodes_closed_recent") or 0) == 0
        and int(snapshot.get("hooks_resolved_recent") or 0) == 0
        and int(snapshot.get("live_interventions") or 0) == 0
    ):
        gaps.append(
            (
                "payoff_ready_ignored",
                "`payoff_ready` pressure が見えているのに episode close / hook resolve / intervention が起きておらず、回収の押し込みが弱い。",
            )
        )
    if (
        {"payoff_ready", "role_reversal_ready", "showoff_flashpoint"}
        & set(str(item) for item in snapshot.get("dominant_pressure_types") or [])
        and int(snapshot.get("episodes_closed_recent") or 0) == 0
        and int(snapshot.get("hooks_resolved_recent") or 0) == 0
        and int(snapshot.get("live_interventions") or 0) == 0
    ):
        gaps.append(
            (
                "payoff_push_weak",
                "payoff / reversal / showoff 系 pressure は見えているのに、scene close・hook resolve・intervention への押し込みが弱い。",
            )
        )
    if (
        int(snapshot.get("open_hooks") or 0) > 0
        or int(snapshot.get("active_tensions") or 0) > 0
        or int(snapshot.get("active_interaction_patterns") or 0) > 0
    ) and snapshot.get("active_episode_id") is None:
        gaps.append(
            (
                "episode_missing",
                "hook / tension / pattern はあるのに active episode が無く、短期 continuity の軸が立っていない。",
            )
        )
    if snapshot.get("active_episode_id") is not None and int(snapshot.get("active_episode_age") or 0) >= 8:
        gaps.append(
            (
                "episode_stalled",
                f"`active_episode_type={snapshot.get('active_episode_type') or 'none'}` / `active_episode_age={snapshot.get('active_episode_age', 0)}` で、episode が長引いている。",
            )
        )
    if int(snapshot.get("episodes_closed_recent") or 0) >= 3 and float(snapshot.get("episode_close_completion_rate") or 0.0) < 0.5:
        gaps.append(
            (
                "episode_churn_high",
                f"`episodes_closed_recent={snapshot.get('episodes_closed_recent', 0)}` に対し `episode_close_completion_rate={snapshot.get('episode_close_completion_rate', 0.0):.2f}` で、episode artifact が追いついていない。",
            )
        )
    if (
        int(snapshot.get("closed_scenes_recent") or 0) > 0
        and float(snapshot.get("scene_close_completion_rate") or 0.0) < 0.5
    ):
        missing_arc = int(
            snapshot.get(
                "scene_close_missing_arc_recent",
                snapshot.get("closed_scenes_without_arc_recent") or 0,
            )
            or 0
        )
        missing_novel = int(
            snapshot.get(
                "scene_close_missing_novel_recent",
                snapshot.get("scene_arcs_without_novel_recent") or 0,
            )
            or 0
        )
        backlog = int(snapshot.get("scene_close_backlog_recent") or 0)
        recovered = int(snapshot.get("scene_close_backlog_recovered_recent") or 0)
        if missing_arc >= missing_novel and missing_arc > 0:
            dominant = "missing_arc dominant"
        elif missing_novel > 0:
            dominant = "missing_novel_output dominant"
        else:
            dominant = "backlog dominant"
        gaps.append(
            (
                "scene_close_weak",
                f"`closed_scenes_recent={snapshot.get('closed_scenes_recent', 0)}` / "
                f"`scene_close_completion_rate={snapshot.get('scene_close_completion_rate', 0.0):.2f}` / "
                f"`scene_close_missing_arc_recent={missing_arc}` / "
                f"`scene_close_missing_novel_recent={missing_novel}` / "
                f"`scene_close_backlog_recent={backlog}` / "
                f"`scene_close_backlog_recovered_recent={recovered}` で、scene close artifact の追随がまだ弱い。"
                f" {dominant}。",
            )
        )
    if int(snapshot["closed_scenes_recent"]) > 0 and int(snapshot["scene_arcs_recent"]) == 0:
        missing_arc = int(snapshot.get("closed_scenes_without_arc_recent") or 0)
        backlog = int(snapshot.get("scene_close_backlog_recent") or 0)
        gaps.append(
            (
                "scene_prose_missing",
                f"`closed_scenes_recent={snapshot['closed_scenes_recent']}` / "
                f"`closed_scenes_without_arc_recent={missing_arc}` / "
                f"`scene_close_backlog_recent={backlog}` に対し `scene_arcs_recent=0` で、閉じた scene が prose 化されていない。",
            )
        )
    if (
        snapshot.get("active_chapter_id") is not None
        and int(snapshot.get("active_chapter_age") or 0) >= _CHAPTER_PROGRESS_STALL_TURNS
    ):
        gaps.append(
            (
                "chapter_progress_stalled",
                f"`active_chapter_id={snapshot.get('active_chapter_id')}` / `active_chapter_beat={snapshot.get('active_chapter_beat') or 'none'}` / `active_chapter_age={snapshot.get('active_chapter_age', 0)}` で、chapter の beat progress が停滞している。",
            )
        )

    warning_messages = {
        "turn_stalled": "runtime monitor で `turn_stalled` を検知しており、長時間運転の継続性に不安がある。",
        "engine_stopped": "runtime monitor で `engine_stopped` を検知しており、review 前に運転停止要因の切り分けが必要である。",
        "ollama_failed": "runtime monitor で `ollama_failed` を検知しており、モデル到達性が不安定である。",
    }
    for code in ("turn_stalled", "engine_stopped", "ollama_failed"):
        if code in monitor["monitor_warnings"]:
            gaps.append((code, warning_messages[code]))
    gaps.extend(
        detect_story_intent_gaps(
            get_story_intent_profile(str(snapshot.get("story_id", ""))),
            snapshot,
        )
    )
    # v2 upgrade: Director Persona 満足度の低い軸に gap を追加
    overall_sat = snapshot.get("director_satisfaction_overall")
    if overall_sat is not None and float(overall_sat) < 0.4:
        gaps.append((
            "director_satisfaction_low",
            f"director overall satisfaction=`{float(overall_sat):.3f}` が低い。"
            f"trend=`{snapshot.get('director_satisfaction_trend', 'flat')}`。"
            "tension/pacing/surprise の各軸を確認すること。",
        ))
    tension_sat = snapshot.get("director_satisfaction_tension")
    if tension_sat is not None and float(tension_sat) < 0.3:
        gaps.append((
            "director_tension_sat_low",
            f"tension_sat=`{float(tension_sat):.3f}` が低い。active tensions が少ないか intensity が弱い可能性。",
        ))
    pacing_sat = snapshot.get("director_satisfaction_pacing")
    if pacing_sat is not None and float(pacing_sat) < 0.3:
        gaps.append((
            "director_pacing_sat_low",
            f"pacing_sat=`{float(pacing_sat):.3f}` が低い。episode progress や scene close の前進が弱い可能性。",
        ))
    surprise_sat = snapshot.get("director_satisfaction_surprise")
    if surprise_sat is not None and float(surprise_sat) < 0.3:
        gaps.append((
            "director_surprise_sat_low",
            f"surprise_sat=`{float(surprise_sat):.3f}` が低い。pattern variation や intervention の変化幅が弱い可能性。",
        ))
    return gaps


def _recommendations_for_gap_codes(gap_codes: list[str]) -> list[str]:
    messages = {
        "reply_ratio_low": "participation planner と reply bias を見直し、`reply_ratio >= 0.20` を維持できるか確認する。",
        "all_chars_spoke_high": "scene / participation planner を締め、毎ターン全員発話に戻らないかを再観測する。",
        "sessionless_monologue_high": "scene / move pressure を見直し、単独ターンが会話 scene へ収束するか確認する。",
        "hook_resolution_stalled": "scene-close hook consolidation と hook resolve 条件を見直し、open hook の回収経路を増やす。",
        "pattern_detection_weak": "interaction pattern の抽出条件を見直し、hook / tension から recurring friction を拾えるか確認する。",
        "turn_gap_high": "scene-aware planner の starvation を潰し、特定キャラの turn gap が 2 以下へ戻るか確認する。",
        "tension_without_intervention": "director threshold と intervention scope を調整し、active tension に live intervention が付くか確認する。",
        "quality_retry_high": "quality guard と prompt 制約を見直し、retry と quality issue の発生率を下げる。",
        "quality_fallback_high": "soft issue は normalize へ寄せ、hard retry は reply focus miss と generic tail に絞れているか再点検する。",
        "reply_quality_fallback_high": "reply_without_direct_reaction の判定と reply prompt を見直し、会話返答を通常保存へ戻す。",
        "reply_quality_flat": "reply focus / signal visibility / dramatic move を見直し、shape dominance と second beat reuse と story flavor weak を減らす。",
        "growth_stalled": "growth evidence 抽出と commit threshold を見直し、pending candidate が commit まで到達するか確認する。",
        "growth_parse_failed": "GrowthEngine の structured output 制約と repair path を見直し、parse failure の再発率を下げる。",
        "growth_write_rolled_back": "GrowthEngine write batch の入力整合と DB write path を見直し、rollback を再発させない。",
        "relationship_mode_missing": "relationship event / pattern から relationship mode への昇格条件を見直し、pairwise script mode が立つか確認する。",
        "relationship_mode_stale": "relationship mode の強化条件と decay 条件を見直し、active mode が再強化されるか確認する。",
        "relationship_mode_flat": "single-mode pair を multi-mode pair へ広げ、complementary mode が同居できるか確認する。",
        "canon_missing": "pattern / relationship mode / episode から canon bit への昇格条件を見直し、偶発的な当たりが継続状態へ残るか確認する。",
        "canon_stale": "canon reinforcement 条件と stale archive 条件を見直し、active canon bit が再強化されるか確認する。",
        "canon_promotion_stalled": "canon promotion ladder を見直し、momentary bit が recurring/proto へ進めるか確認する。",
        "canon_reignition_missing": "canon bit から hook を再発火させ、既存の pattern / episode 経路へ再投入できるか確認する。",
        "canon_reignition_stuck": "canon 起点 hook の cooldown と resolve 条件を見直し、再発火が停滞 hook で詰まらないか確認する。",
        "pressure_missing": "pattern / relationship mode / canon / episode から dramatic pressure への解釈条件を見直し、押すべき火種が persisted state として立つか確認する。",
        "pressure_stale": "dramatic pressure の再強化条件と resolve 条件を見直し、active pressure が stale のまま残らないか確認する。",
        "payoff_ready_ignored": "`payoff_ready` pressure がある時に episode close / hook resolve / intervention が起きるよう、director と planner の bonus を見直す。",
        "payoff_push_weak": "payoff / reversal / showoff 系 pressure が scene objective・episode close・intervention へ押し込まれるよう、director と planner の bonus を見直す。",
        "episode_missing": "episode planner の起動条件を見直し、hook / tension / pattern から短期 continuity を立ち上げる。",
        "episode_stalled": "episode close 条件と progress 判定を見直し、同じ episode が長引きすぎないか確認する。",
        "episode_churn_high": "episode close artifact と close 条件を見直し、閉じた episode が `story_arc` へ落ちるか確認する。",
        "scene_close_weak": "scene close 後の artifact handoff を見直し、closed scene に対して `story_arc` / `novel_output` が半数以上追随するか確認する。",
        "scene_prose_missing": "scene-close novel trigger を見直し、closed scene が `story_arc` / `novel_output` に落ちるか確認する。",
        "director_satisfaction_low": "director satisfaction の低い軸を確認し、tension / pacing / surprise のどこで intent と runtime state がズレているか切り分ける。",
        "director_tension_sat_low": "active tension の量と intensity を見直し、director が tension 不足と感じる窓を減らす。",
        "director_pacing_sat_low": "episode progress と scene close の cadence を見直し、director の pacing dissatisfaction を減らす。",
        "director_surprise_sat_low": "pattern variation と intervention flavor を見直し、director の surprise dissatisfaction を減らす。",
        "chapter_progress_stalled": "chapter beat progress の条件と handoff を見直し、1 chapter が 20 turn 以上停滞しないか確認する。",
        "turn_stalled": "monitor に出た stall 原因を先に潰し、安定した長時間運転の母数を確保する。",
        "engine_stopped": "engine stop の根本原因を先に解消し、review 以前に継続運転を回復する。",
        "ollama_failed": "Ollama 到達性と model 常駐状態を先に安定化させる。",
        "story_intent_pattern_mix_flat": "Pattern 2B を進め、role_reversal / bluff_or_showoff が recurring friction として観測できるか確認する。",
    }
    recommendations: list[str] = []
    for code in gap_codes:
        message = messages.get(code)
        if message and message not in recommendations:
            recommendations.append(message)
    return recommendations


def _cause(
    code: str,
    score: float,
    why: str,
    evidence: str,
    experiment: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "score": score,
        "why": why,
        "evidence": evidence,
        "experiment": experiment,
    }


def _detect_causes(
    snapshot: dict[str, Any],
    cause_inputs: dict[str, float | int],
) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []

    active_signal_layers = (
        int(snapshot.get("active_interaction_patterns") or 0)
        + int(snapshot.get("active_relationship_modes") or 0)
        + int(snapshot.get("active_canon_bits") or 0)
        + int(snapshot.get("active_dramatic_pressures") or 0)
    )
    signal_surface_misses = (
        int(snapshot.get("reply_focus_misses_recent") or 0)
        + int(snapshot.get("generic_reply_tails_recent") or 0)
        + int(snapshot.get("voice_flat_replies_recent") or 0)
        + int(snapshot.get("reply_quality_fallbacks_recent") or 0)
        + int(snapshot.get("signal_visibility_misses_recent") or 0)
        + int(snapshot.get("objective_visibility_misses_recent") or 0)
    )
    target_specific_ratio = float(cause_inputs.get("target_specific_reply_ratio") or 0.0)
    if active_signal_layers > 0 and (signal_surface_misses > 0 or target_specific_ratio < 0.35):
        causes.append(
            _cause(
                "signal_visibility_weak",
                1.4 + signal_surface_misses * 0.2 + max(0.0, 0.35 - target_specific_ratio),
                "signal layer は立っているが、reply が返し先やキャラ差として十分に表面化していない。",
                (
                    f"active layers={active_signal_layers}, "
                    f"reply_focus_misses_recent={snapshot.get('reply_focus_misses_recent', 0)}, "
                    f"generic_reply_tails_recent={snapshot.get('generic_reply_tails_recent', 0)}, "
                    f"voice_flat_replies_recent={snapshot.get('voice_flat_replies_recent', 0)}, "
                    f"reply_variety_press_recent={snapshot.get('reply_variety_press_recent', 0)}, "
                    f"reply_variety_condition_recent={snapshot.get('reply_variety_condition_recent', 0)}, "
                    f"reply_variety_redirect_recent={snapshot.get('reply_variety_redirect_recent', 0)}, "
                    f"reply_dramatic_move_missing_recent={snapshot.get('reply_dramatic_move_missing_recent', 0)}, "
                    f"reply_soft_landing_recent={snapshot.get('reply_soft_landing_recent', 0)}, "
                    f"reply_shape_dominance_recent={snapshot.get('reply_shape_dominance_recent', 0)}, "
                    f"reply_reused_opening_recent={snapshot.get('reply_reused_opening_recent', 0)}, "
                    f"reply_reused_ending_recent={snapshot.get('reply_reused_ending_recent', 0)}, "
                    f"reply_reused_second_beat_recent={snapshot.get('reply_reused_second_beat_recent', 0)}, "
                    f"signal_visibility_misses_recent={snapshot.get('signal_visibility_misses_recent', 0)}, "
                    f"objective_visibility_misses_recent={snapshot.get('objective_visibility_misses_recent', 0)}, "
                    f"signal_visibility_retry_recent={snapshot.get('signal_visibility_retry_recent', 0)}, "
                    f"objective_visibility_retry_recent={snapshot.get('objective_visibility_retry_recent', 0)}, "
                    f"target_specific_reply_ratio={target_specific_ratio:.2f}"
                ),
                "reply prompt の target excerpt / reply focus を優先し、generic tail を減らす small tuning を試す。",
            )
        )

    generic_objective_ratio = float(cause_inputs.get("generic_scene_objective_ratio") or 0.0)
    dominant_pressures = {str(item) for item in snapshot.get("dominant_pressure_types") or []}
    if (
        snapshot.get("active_episode_id") is not None
        or int(snapshot.get("active_dramatic_pressures") or 0) > 0
        or {"payoff_ready", "role_reversal_ready", "showoff_flashpoint"} & dominant_pressures
    ) and (
        generic_objective_ratio >= 0.5
        or int(snapshot.get("episodes_closed_recent") or 0) == 0
        and int(snapshot.get("hooks_resolved_recent") or 0) == 0
    ):
        causes.append(
            _cause(
                "scene_purpose_not_visible",
                1.2 + generic_objective_ratio + (0.3 if dominant_pressures else 0.0),
                "scene の争点や payoff push が persisted state にある割に、scene objective と close へ十分見えていない。",
                (
                    f"generic_scene_objective_ratio={generic_objective_ratio:.2f}, "
                    f"active_episode_id={snapshot.get('active_episode_id')}, "
                    f"dominant_pressure_types={', '.join(snapshot.get('dominant_pressure_types', [])) or 'none'}, "
                    f"episodes_closed_recent={snapshot.get('episodes_closed_recent', 0)}, "
                    f"hooks_resolved_recent={snapshot.get('hooks_resolved_recent', 0)}, "
                    f"scene_close_backlog_recent={snapshot.get('scene_close_backlog_recent', 0)}, "
                    f"closed_scenes_without_arc_recent={snapshot.get('closed_scenes_without_arc_recent', 0)}, "
                    f"scene_arcs_without_novel_recent={snapshot.get('scene_arcs_without_novel_recent', 0)}"
                ),
                "scene objective の pressure-first 選定と payoff push bonus を再観測し、generic objective を減らす。",
            )
        )

    quality_retry_rate = float(snapshot.get("quality_retry_rate") or 0.0)
    quality_fallbacks = int(snapshot.get("quality_fallbacks_recent") or 0)
    quality_normalizations = int(snapshot.get("quality_normalizations_recent") or 0)
    if quality_fallbacks > 0 or quality_retry_rate > 0.15 or quality_normalizations >= 4:
        reply_focus_fallbacks = int(snapshot.get("reply_fallback_focus_missing_recent") or 0)
        reply_direct_reaction_fallbacks = int(
            snapshot.get("reply_fallback_direct_reaction_recent") or 0
        )
        reply_retry_kept_recent = int(snapshot.get("reply_retry_kept_recent") or 0)
        causes.append(
            _cause(
                "quality_gate_suppression",
                1.0
                + quality_fallbacks * 0.35
                + quality_retry_rate
                + int(snapshot.get("reply_quality_fallbacks_recent") or 0) * 0.15
                + reply_focus_fallbacks * 0.1
                + reply_direct_reaction_fallbacks * 0.05
                + (
                    int(snapshot.get("reply_focus_misses_recent") or 0)
                    + int(snapshot.get("voice_flat_replies_recent") or 0)
                )
                * 0.05,
                "quality gate の normalize / retry / fallback が高く、通常生成が保存まで届きにくい。",
                (
                    f"quality_retry_rate={quality_retry_rate:.2f}, "
                    f"quality_normalizations_recent={quality_normalizations}, "
                    f"quality_fallbacks_recent={quality_fallbacks}, "
                    f"reply_quality_fallbacks_recent={snapshot.get('reply_quality_fallbacks_recent', 0)}, "
                    f"reply_fallback_focus_missing_recent={reply_focus_fallbacks}, "
                    f"reply_fallback_direct_reaction_recent={reply_direct_reaction_fallbacks}, "
                    f"reply_retry_kept_recent={reply_retry_kept_recent}, "
                    f"reply_focus_misses_recent={snapshot.get('reply_focus_misses_recent', 0)}, "
                    f"voice_flat_replies_recent={snapshot.get('voice_flat_replies_recent', 0)}"
                ),
                "quality gate の retry/fallback 根拠を再点検し、reply focus miss と generic tail 以外で hard stop しすぎていないか確認する。",
            )
        )

    if (
        snapshot.get("active_chapter_id") is not None
        and int(snapshot.get("active_chapter_age") or 0) >= _CHAPTER_PROGRESS_STALL_TURNS
        and (
            int(snapshot.get("open_hooks") or 0) > 0
            or int(snapshot.get("active_tensions") or 0) > 0
            or int(snapshot.get("active_dramatic_pressures") or 0) > 0
        )
    ):
        causes.append(
            _cause(
                "chapter_pressure_not_converting",
                1.15
                + min(int(snapshot.get("active_chapter_age") or 0) / 20.0, 1.0) * 0.3
                + (0.2 if float(snapshot.get("scene_close_completion_rate") or 0.0) < 0.5 else 0.0),
                "chapter は active だが、beat progress が scene close・hook resolve・intervention へ十分に変換されていない。",
                (
                    f"active_chapter_id={snapshot.get('active_chapter_id')}, "
                    f"active_chapter_beat={snapshot.get('active_chapter_beat') or 'none'}, "
                    f"active_chapter_age={snapshot.get('active_chapter_age', 0)}, "
                    f"open_hooks={snapshot.get('open_hooks', 0)}, "
                    f"active_tensions={snapshot.get('active_tensions', 0)}, "
                    f"live_interventions={snapshot.get('live_interventions', 0)}, "
                    f"scene_close_completion_rate={float(snapshot.get('scene_close_completion_rate') or 0.0):.2f}"
                ),
                "chapter beat progress 条件と scene/hook/intervention handoff を見直し、chapter pressure を次の artifact へ押し出す。",
            )
        )

    overall_sat = snapshot.get("director_satisfaction_overall")
    pacing_sat = snapshot.get("director_satisfaction_pacing")
    surprise_sat = snapshot.get("director_satisfaction_surprise")
    tension_sat = snapshot.get("director_satisfaction_tension")
    if (
        (overall_sat is not None and float(overall_sat) < 0.4)
        or (pacing_sat is not None and float(pacing_sat) < 0.3)
        or (surprise_sat is not None and float(surprise_sat) < 0.3)
        or (tension_sat is not None and float(tension_sat) < 0.3)
    ):
        deficits = [
            max(0.0, 0.4 - float(overall_sat)) if overall_sat is not None else 0.0,
            max(0.0, 0.3 - float(tension_sat)) if tension_sat is not None else 0.0,
            max(0.0, 0.3 - float(pacing_sat)) if pacing_sat is not None else 0.0,
            max(0.0, 0.3 - float(surprise_sat)) if surprise_sat is not None else 0.0,
        ]
        causes.append(
            _cause(
                "director_alignment_low",
                1.05 + sum(deficits),
                "director persona の満足度が低く、story intent と runtime の出力配分がまだ噛み合っていない。",
                (
                    f"director_satisfaction_overall={overall_sat if overall_sat is not None else 'none'}, "
                    f"director_satisfaction_tension={tension_sat if tension_sat is not None else 'none'}, "
                    f"director_satisfaction_pacing={pacing_sat if pacing_sat is not None else 'none'}, "
                    f"director_satisfaction_surprise={surprise_sat if surprise_sat is not None else 'none'}, "
                    f"trend={snapshot.get('director_satisfaction_trend', 'flat')}"
                ),
                "低い satisfaction axis を中心に、tension / pacing / surprise のどこが intent とズレているかを monitor と review で再切り分けする。",
            )
        )

    canon_unresolved = int(cause_inputs.get("canon_triggered_unresolved_hooks") or 0)
    if int(snapshot.get("active_canon_bits") or 0) > 0 and (
        int(snapshot.get("canon_reignitions_recent") or 0) == 0
        or int(snapshot.get("active_canon_profile_overlays") or 0) == 0
        or canon_unresolved > 0
    ):
        causes.append(
            _cause(
                "canon_carryover_weak",
                0.9 + int(snapshot.get("active_canon_bits") or 0) * 0.1 + canon_unresolved * 0.25,
                "canon は立っているが、hook への再発火や profile への持ち越しが弱く、次の run へ効き切っていない。",
                (
                    f"active_canon_bits={snapshot.get('active_canon_bits', 0)}, "
                    f"canon_reignitions_recent={snapshot.get('canon_reignitions_recent', 0)}, "
                    f"active_canon_profile_overlays={snapshot.get('active_canon_profile_overlays', 0)}, "
                    f"canon_triggered_unresolved_hooks={canon_unresolved}"
                ),
                "canon reignition の cooldown と writeback cadence を見直し、canon-triggered hook が unresolved のまま詰まらないか確認する。",
            )
        )

    growth_pending_commit_ratio = float(cause_inputs.get("growth_pending_commit_ratio") or 0.0)
    growth_rejection_empty_ratio = float(cause_inputs.get("growth_rejection_empty_ratio") or 0.0)
    if (
        int(snapshot.get("growth_commits_recent") or 0) == 0
        and (
            int(snapshot.get("growth_candidates_pending") or 0) > 0
            or int(snapshot.get("growth_empty_recent") or 0) > 0
            or int(snapshot.get("growth_quality_rejections_recent") or 0) > 0
        )
    ):
        causes.append(
            _cause(
                "growth_not_sticking",
                0.8
                + min(growth_pending_commit_ratio, 3.0) * 0.25
                + min(growth_rejection_empty_ratio, 3.0) * 0.25,
                "growth evidence はあるが、empty/rejection/pending accumulation が先に立って commit まで届いていない。",
                (
                    f"growth_candidates_pending={snapshot.get('growth_candidates_pending', 0)}, "
                    f"growth_commits_recent={snapshot.get('growth_commits_recent', 0)}, "
                    f"growth_empty_recent={snapshot.get('growth_empty_recent', 0)}, "
                    f"growth_quality_rejections_recent={snapshot.get('growth_quality_rejections_recent', 0)}, "
                    f"growth_pending_commit_ratio={growth_pending_commit_ratio:.2f}"
                ),
                "goal/worry の commit threshold と durable evidence pack を優先し、personality_core は引き続き厳しく保つ。",
            )
        )

    reply_ratio = float(snapshot.get("reply_ratio") or 0.0)
    sessionless_monologue_ratio = float(snapshot.get("sessionless_monologue_ratio") or 0.0)
    all_chars_spoke_ratio = float(snapshot.get("all_chars_spoke_ratio") or 0.0)
    if reply_ratio < 0.20 or sessionless_monologue_ratio > 0.45 or all_chars_spoke_ratio > 0.60:
        causes.append(
            _cause(
                "turnflow_weak",
                0.7
                + max(0.0, 0.20 - reply_ratio) * 2.0
                + max(0.0, sessionless_monologue_ratio - 0.45)
                + max(0.0, all_chars_spoke_ratio - 0.60),
                "会話の往復と scene participation の流れ自体がまだ弱く、物語レイヤー以前に turnflow が flat 寄りである。",
                (
                    f"reply_ratio={reply_ratio:.2f}, "
                    f"sessionless_monologue_ratio={sessionless_monologue_ratio:.2f}, "
                    f"all_chars_spoke_ratio={all_chars_spoke_ratio:.2f}"
                ),
                "participation planner と solo suppression を再観測し、reply_ratio を守りつつ独演を減らす。",
            )
        )

    causes.sort(key=lambda item: float(item["score"]), reverse=True)
    return causes[:3]


def _next_checkpoints_for_gap_codes(gap_codes: list[str]) -> list[str]:
    checkpoints = {
        "reply_ratio_low": "`reply_ratio` を 0.20 以上で維持できるか。",
        "all_chars_spoke_high": "`all_chars_spoke_ratio` を 0.60 未満で維持できるか。",
        "sessionless_monologue_high": "`sessionless_monologue_ratio` を 0.45 以下へ下げられるか。",
        "hook_resolution_stalled": "`open_hooks` が増えるだけでなく `hooks_resolved_recent` が発生するか。",
        "pattern_detection_weak": "`patterns_detected_recent` が 0 のまま停滞しないか。",
        "turn_gap_high": "`max_character_turn_gap` を 2 以下で維持できるか。",
        "tension_without_intervention": "`active_tensions` に対して `live_interventions` が追随するか。",
        "quality_retry_high": "`quality_retry_rate` を 0.15 以下に抑えられるか。",
        "quality_fallback_high": "`quality_fallbacks_recent` を 0 へ近づけ、通常生成の保存率を戻せるか。",
        "reply_quality_fallback_high": "`reply_quality_fallbacks_recent` を 0 へ近づけ、reply の通常保存率を戻せるか。",
        "reply_quality_flat": "`reply_focus_misses_recent` / `generic_reply_tails_recent` / `voice_flat_replies_recent` に加え、`reply_dramatic_move_missing_recent` / `reply_soft_landing_recent` / `reply_reused_second_beat_recent` / `reply_story_flavor_weak_recent` を 0 に寄せられるか。",
        "growth_stalled": "`growth_candidates_pending` が溜まるだけでなく `growth_commits_recent` が発生するか。",
        "growth_quality_flat": "`growth_empty_recent` と `growth_quality_rejections_recent` を下げ、meaningful growth commit を発生させられるか。",
        "growth_parse_failed": "`growth_parse_failures_recent` を 0 に戻せるか。",
        "growth_write_rolled_back": "`growth_rollbacks_recent` が再発しないか。",
        "relationship_mode_missing": "`active_relationship_modes` が 0 のまま停滞しないか。",
        "relationship_mode_stale": "`relationship_mode_reinforcements_recent` が発生するか。",
        "relationship_mode_flat": "`multi_mode_pairs_active` が 0 のまま停滞せず、同 pair の複合 mode が立つか。",
        "canon_missing": "`active_canon_bits` が 0 のまま停滞しないか。",
        "canon_stale": "`canon_reinforcements_recent` が発生するか。",
        "canon_promotion_stalled": "`canon_promotions_recent` が発生し、canon level が上がるか。",
        "canon_reignition_missing": "`canon_reignitions_recent` が 0 のまま停滞せず、canon bit が hook として戻るか。",
        "canon_reignition_stuck": "`canon_triggered_hooks_open` が unresolved のまま溜まらないか。",
        "pressure_missing": "`active_dramatic_pressures` が 0 のまま停滞しないか。",
        "pressure_stale": "`pressure_reinforcements_recent` が発生するか。",
        "payoff_ready_ignored": "`payoff_ready` pressure が見えた窓で episode close / hook resolve / intervention が追随するか。",
        "payoff_push_weak": "`payoff_ready` / `role_reversal_ready` / `showoff_flashpoint` が見えた窓で close・resolve・intervention が追随するか。",
        "episode_missing": "`active_episode_id` が空のまま停滞しないか。",
        "episode_stalled": "`active_episode_age` が伸び続けず、episode が適切に close するか。",
        "episode_churn_high": "`episodes_closed_recent` に対して `episode_arcs_recent` が追随するか。",
        "scene_close_weak": "`scene_close_completion_rate >= 0.50` を維持できるか。",
        "scene_prose_missing": "`closed_scenes_recent` に対して `scene_arcs_recent` と `novel_outputs_recent` が追随するか。",
        "director_satisfaction_low": "`director_satisfaction_overall >= 0.40` を回復できるか。",
        "director_tension_sat_low": "`director_satisfaction_tension >= 0.30` を回復できるか。",
        "director_pacing_sat_low": "`director_satisfaction_pacing >= 0.30` を回復できるか。",
        "director_surprise_sat_low": "`director_satisfaction_surprise >= 0.30` を回復できるか。",
        "chapter_progress_stalled": "active chapter の beat が 20 turn 以内に進むか。",
        "turn_stalled": "20 turn 以上の連続運転で stall warning が再発しないか。",
        "engine_stopped": "監視中に engine 停止が再発しないか。",
        "ollama_failed": "長時間運転中に model endpoint が安定して応答するか。",
        "story_intent_pattern_mix_flat": "`dominant_pattern_types` に role_reversal / bluff_or_showoff が混ざり、pattern 構成が平坦なまま停滞しないか。",
    }
    ordered = [checkpoints[code] for code in gap_codes if code in checkpoints]
    if not ordered:
        return ["現状維持で 20 turn 以上を再観測し、主要指標が崩れないか確認する。"]
    return ordered


def _recommended_experiments_for_causes(causes: list[dict[str, Any]]) -> list[str]:
    experiments: list[str] = []
    for cause in causes[:2]:
        experiment = str(cause.get("experiment") or "").strip()
        if experiment and experiment not in experiments:
            experiments.append(experiment)
    return experiments


def render_review_markdown(
    story_id: str,
    snapshot: dict[str, Any],
    monitor: dict[str, Any],
    *,
    db_path: str | Path,
    cause_inputs: dict[str, float | int] | None = None,
) -> str:
    strengths = _detect_strengths(snapshot, monitor)
    gaps = _detect_gaps(snapshot, monitor)
    causes = _detect_causes(snapshot, cause_inputs or {})
    gap_codes = [code for code, _ in gaps]
    recommendations = _recommendations_for_gap_codes(gap_codes)
    checkpoints = _next_checkpoints_for_gap_codes(gap_codes)
    date_token = datetime.now(UTC).astimezone().date().isoformat()
    turn_range = (
        f"{snapshot['min_turn_number']}-{snapshot['max_turn_number']}"
        if snapshot["max_turn_number"] is not None
        else "none"
    )
    monitor_source = monitor["monitor_source"]
    monitor_warnings = "none" if not monitor["monitor_warnings"] else " | ".join(monitor["monitor_warnings"])
    monitor_errors = "none" if not monitor["monitor_recent_errors"] else " | ".join(monitor["monitor_recent_errors"])

    lines = [
        "# Story Quality Review Draft",
        "",
        f"- story_id: `{story_id}`",
        f"- generated_at: `{datetime.now(UTC).astimezone().isoformat(timespec='seconds')}`",
        f"- db_path: `{db_path}`",
        f"- window_turns: `{snapshot['window_turns']}`",
        f"- monitor_source: `{monitor_source}`",
        "- note: `sidecar draft only`",
        "",
        f"## Review Entry: {date_token}",
        "",
        "### Scope",
        "",
        f"- 対象 story: `{story_id}`",
        f"- 対象 DB: `{db_path}`",
        f"- 直近観測窓: `{snapshot['window_turns']}` turn",
        f"- monitor 参照: `{monitor_source}`",
        "",
        "### Intent Lens",
        "",
        f"- story_intent_mode=`{snapshot.get('story_intent_mode', 'default')}`",
        "",
        "### Pattern Lens",
        "",
        f"- `active_interaction_patterns={snapshot.get('active_interaction_patterns', 0)}`",
        f"- `patterns_detected_recent={snapshot.get('patterns_detected_recent', 0)}` / `pattern_recurrences_recent={snapshot.get('pattern_recurrences_recent', 0)}`",
        f"- dominant_pattern_types=`{', '.join(snapshot.get('dominant_pattern_types', [])) or 'none'}`",
        "",
        "### Relationship Mode Lens",
        "",
        f"- `active_relationship_modes={snapshot.get('active_relationship_modes', 0)}`",
        f"- `relationship_mode_reinforcements_recent={snapshot.get('relationship_mode_reinforcements_recent', 0)}` / `relationship_mode_decay_updates_recent={snapshot.get('relationship_mode_decay_updates_recent', 0)}`",
        f"- `multi_mode_pairs_active={snapshot.get('multi_mode_pairs_active', 0)}` / `complementary_mode_pairs_active={snapshot.get('complementary_mode_pairs_active', 0)}` / `single_mode_pairs_active={snapshot.get('single_mode_pairs_active', 0)}`",
        f"- `complementary_mode_reinforcements_recent={snapshot.get('complementary_mode_reinforcements_recent', 0)}` / `complementary_mode_decay_updates_recent={snapshot.get('complementary_mode_decay_updates_recent', 0)}` / `complementary_mode_deactivations_recent={snapshot.get('complementary_mode_deactivations_recent', 0)}`",
        f"- dominant_relationship_modes=`{', '.join(snapshot.get('dominant_relationship_modes', [])) or 'none'}`",
        "",
        "### Canon Lens",
        "",
        f"- `active_canon_bits={snapshot.get('active_canon_bits', 0)}`",
        f"- `canon_reinforcements_recent={snapshot.get('canon_reinforcements_recent', 0)}` / `canon_promotions_recent={snapshot.get('canon_promotions_recent', 0)}` / `canon_reignitions_recent={snapshot.get('canon_reignitions_recent', 0)}`",
        f"- `canon_triggered_hooks_open={snapshot.get('canon_triggered_hooks_open', 0)}`",
        f"- dominant_canon_levels=`{', '.join(snapshot.get('dominant_canon_levels', [])) or 'none'}`",
        f"- dominant_canon_motifs=`{', '.join(snapshot.get('dominant_canon_motifs', [])) or 'none'}`",
        "",
        "### Pressure Lens",
        "",
        f"- `active_dramatic_pressures={snapshot.get('active_dramatic_pressures', 0)}`",
        f"- `pressure_reinforcements_recent={snapshot.get('pressure_reinforcements_recent', 0)}` / `max_pressure_score={snapshot.get('max_pressure_score', 0.0):.2f}`",
        f"- dominant_pressure_types=`{', '.join(snapshot.get('dominant_pressure_types', [])) or 'none'}`",
        "",
        "### Episode Lens",
        "",
        f"- `active_episode_id={snapshot.get('active_episode_id')}`",
        f"- `active_episode_type={snapshot.get('active_episode_type') or 'none'}` / `active_episode_age={snapshot.get('active_episode_age', 0)}`",
        f"- `active_episode_goal={snapshot.get('active_episode_goal') or 'none'}`",
        f"- `episodes_closed_recent={snapshot.get('episodes_closed_recent', 0)}` / `episode_close_completion_rate={snapshot.get('episode_close_completion_rate', 0.0):.2f}` / `episode_arcs_recent={snapshot.get('episode_arcs_recent', 0)}`",
        "",
        "### Chapter Lens",
        "",
        f"- active_chapter_id=`{snapshot.get('active_chapter_id') or 'none'}`"
        f"  beat=`{snapshot.get('active_chapter_beat') or 'none'}`"
        f"  opened_turn=`{snapshot.get('active_chapter_opened_turn')}`"
        f"  age=`{snapshot.get('active_chapter_age', 0)}`",
        "",
        "### Director Persona Lens",
        "",
    ]
    if snapshot.get("active_director_persona_id"):
        overall = snapshot.get("director_satisfaction_overall")
        trend = snapshot.get("director_satisfaction_trend", "flat")
        overall_str = f"{overall:.3f}" if overall is not None else "none"
        tension_str = f"{snapshot.get('director_satisfaction_tension'):.3f}" if snapshot.get("director_satisfaction_tension") is not None else "none"
        pacing_str = f"{snapshot.get('director_satisfaction_pacing'):.3f}" if snapshot.get("director_satisfaction_pacing") is not None else "none"
        surprise_str = f"{snapshot.get('director_satisfaction_surprise'):.3f}" if snapshot.get("director_satisfaction_surprise") is not None else "none"
        lines.append(
            f"- active_persona=`{snapshot['active_director_persona_id']}`"
            f"  overall=`{overall_str}`  trend=`{trend}`"
        )
        lines.append(
            f"- axis: tension=`{tension_str}`"
            f"  pacing=`{pacing_str}`"
            f"  surprise=`{surprise_str}`"
        )
    else:
        lines.append("- director_persona: `disabled` or `no active persona`")
    lines.extend([
        "",
        "### Data Snapshot",
        "",
        f"- `chat_logs`: {snapshot['chat_log_count']} 件",
        f"- turn 範囲: {turn_range}",
        f"- 発話キャラ数: {snapshot['active_character_count']}",
        f"- 最新ログ: turn={snapshot['max_turn_number']}, char={snapshot['last_char_id']}, len={snapshot['last_message_len']}",
        f"- story clock: {snapshot['last_sim_datetime']}",
        f"- `reply_ratio={snapshot['reply_ratio']:.2f}`, `group_ratio={snapshot['group_ratio']:.2f}`, `monologue_ratio={snapshot['monologue_ratio']:.2f}`",
        f"- `speaking_chars_per_turn_avg={snapshot['speaking_chars_per_turn_avg']:.2f}`, `all_chars_spoke_ratio={snapshot['all_chars_spoke_ratio']:.2f}`",
        f"- `active_scenes={snapshot['active_scenes']}`, `closed_scenes_recent={snapshot['closed_scenes_recent']}`",
        f"- `open_hooks={snapshot['open_hooks']}`, `hooks_created_recent={snapshot['hooks_created_recent']}`, `hooks_resolved_recent={snapshot['hooks_resolved_recent']}`",
        f"- `active_tensions={snapshot['active_tensions']}`, `intervention_eligible_now={snapshot['intervention_eligible_now']}`, `max_character_turn_gap={snapshot['max_character_turn_gap']}`, `stale_char_ids={', '.join(snapshot['stale_char_ids']) if snapshot['stale_char_ids'] else 'none'}`, `resolved_tensions_recent={snapshot['resolved_tensions_recent']}`, `live_interventions={snapshot['live_interventions']}`",
        f"- `relationship_pairs_changed_recent={snapshot['relationship_pairs_changed_recent']}`",
        f"- `active_relationship_modes={snapshot.get('active_relationship_modes', 0)}`, `relationship_mode_reinforcements_recent={snapshot.get('relationship_mode_reinforcements_recent', 0)}`, `relationship_mode_decay_updates_recent={snapshot.get('relationship_mode_decay_updates_recent', 0)}`, `multi_mode_pairs_active={snapshot.get('multi_mode_pairs_active', 0)}`, `complementary_mode_pairs_active={snapshot.get('complementary_mode_pairs_active', 0)}`, `complementary_mode_reinforcements_recent={snapshot.get('complementary_mode_reinforcements_recent', 0)}`, `complementary_mode_deactivations_recent={snapshot.get('complementary_mode_deactivations_recent', 0)}`, `dominant_relationship_modes={', '.join(snapshot.get('dominant_relationship_modes', [])) or 'none'}`",
        f"- `active_canon_bits={snapshot.get('active_canon_bits', 0)}`, `canon_reinforcements_recent={snapshot.get('canon_reinforcements_recent', 0)}`, `canon_promotions_recent={snapshot.get('canon_promotions_recent', 0)}`, `canon_reignitions_recent={snapshot.get('canon_reignitions_recent', 0)}`, `canon_triggered_hooks_open={snapshot.get('canon_triggered_hooks_open', 0)}`, `dominant_canon_motifs={', '.join(snapshot.get('dominant_canon_motifs', [])) or 'none'}`",
        f"- `active_dramatic_pressures={snapshot.get('active_dramatic_pressures', 0)}`, `pressure_reinforcements_recent={snapshot.get('pressure_reinforcements_recent', 0)}`, `dominant_pressure_types={', '.join(snapshot.get('dominant_pressure_types', [])) or 'none'}`, `max_pressure_score={snapshot.get('max_pressure_score', 0.0):.2f}`",
        f"- `active_episode_id={snapshot.get('active_episode_id')}`, `active_episode_type={snapshot.get('active_episode_type') or 'none'}`, `active_episode_age={snapshot.get('active_episode_age', 0)}`, `episodes_closed_recent={snapshot.get('episodes_closed_recent', 0)}`",
        f"- `growth_candidates_pending={snapshot['growth_candidates_pending']}`, `growth_commits_recent={snapshot['growth_commits_recent']}`, `growth_empty_recent={snapshot.get('growth_empty_recent', 0)}`, `growth_quality_rejections_recent={snapshot.get('growth_quality_rejections_recent', 0)}`, `growth_parse_failures_recent={snapshot.get('growth_parse_failures_recent', 0)}`, `growth_rollbacks_recent={snapshot.get('growth_rollbacks_recent', 0)}`, `growth_superseded_recent={snapshot.get('growth_superseded_recent', 0)}`, `growth_expired_recent={snapshot.get('growth_expired_recent', 0)}`",
        f"- `quality_issues_recent={snapshot['quality_issues_recent']}`, `quality_retry_rate={snapshot['quality_retry_rate']:.2f}`, `quality_normalizations_recent={snapshot.get('quality_normalizations_recent', 0)}`, `quality_fallbacks_recent={snapshot.get('quality_fallbacks_recent', 0)}`, `reply_quality_normalizations_recent={snapshot.get('reply_quality_normalizations_recent', 0)}`, `reply_quality_fallbacks_recent={snapshot.get('reply_quality_fallbacks_recent', 0)}`",
        f"- `reply_fallback_focus_missing_recent={snapshot.get('reply_fallback_focus_missing_recent', 0)}`, `reply_fallback_direct_reaction_recent={snapshot.get('reply_fallback_direct_reaction_recent', 0)}`, `reply_retry_kept_recent={snapshot.get('reply_retry_kept_recent', 0)}`",
        f"- `episode_close_completion_rate={snapshot.get('episode_close_completion_rate', 0.0):.2f}`, `episode_arcs_recent={snapshot.get('episode_arcs_recent', 0)}`",
        f"- `scene_arcs_recent={snapshot['scene_arcs_recent']}`, `novel_outputs_recent={snapshot['novel_outputs_recent']}`",
        f"- monitor warnings: {monitor_warnings}",
        f"- monitor recent warnings/errors: {monitor_errors}",
        "",
        "### What Worked",
        "",
    ])
    if strengths:
        lines.extend(f"- {item}" for item in strengths)
    else:
        lines.append("- 直近観測窓では、強い改善指標をまだ十分に確認できない。")

    lines.extend(["", "### Gaps", ""])
    if gaps:
        lines.extend(f"- {message}" for _, message in gaps)
    else:
        lines.append("- 直近観測窓では、主要な構造ギャップは目立たない。")

    lines.extend(["", "### Cause Lens", ""])
    if causes:
        for cause in causes:
            lines.append(f"- `{cause['code']}`")
            lines.append(f"  Why: {cause['why']}")
            lines.append(f"  Evidence: {cause['evidence']}")
            lines.append(f"  Next experiment: {cause['experiment']}")
    else:
        lines.append("- 直近観測窓では、主要な flatness cause はまだ強く特定できない。")

    lines.extend(["", "### Recommendations", ""])
    if recommendations:
        lines.extend(f"- {item}" for item in recommendations)
    else:
        lines.append("- 現状の設定を維持しつつ、同条件で 20 turn 以上の再観測を行う。")

    lines.extend(["", "### Recommended Experiments", ""])
    cause_experiments = _recommended_experiments_for_causes(causes)
    if cause_experiments:
        lines.extend(f"- {item}" for item in cause_experiments)
    else:
        lines.append("- Cause Lens が薄い場合は、現状維持で 20 turn 以上を再観測する。")

    lines.extend(["", "### Next Checkpoints", ""])
    lines.extend(f"- {item}" for item in checkpoints)
    lines.append("")
    return "\n".join(lines)


async def generate_story_review_draft(
    story_id: str,
    *,
    output: Path | None = None,
    db_path: str | Path = _DEFAULT_DB,
    window_turns: int = 20,
    monitor_file: Path | None = None,
    monitor_dir: Path = _DEFAULT_MONITOR_DIR,
    db: DatabaseManager | None = None,
) -> int:
    async def _run(manager: DatabaseManager) -> int:
        story = await manager.get_story(story_id)
        if story is None:
            logger.error("ストーリーが見つかりません: story_id=%s", story_id)
            return EXIT_STORY_NOT_FOUND

        snapshot = await _collect_review_snapshot(manager, story_id, window_turns=window_turns)
        cause_inputs = await _collect_cause_inputs(manager, story_id, window_turns=window_turns)
        resolved_monitor = _resolve_monitor_file(
            story_id,
            monitor_file=monitor_file,
            monitor_dir=monitor_dir,
        )
        monitor = _parse_latest_monitor_snapshot(resolved_monitor)
        rendered = render_review_markdown(
            story_id,
            snapshot,
            monitor,
            db_path=db_path,
            cause_inputs=cause_inputs,
        )
        destination = output or _default_output_path(story_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
        return EXIT_OK

    try:
        if db is not None:
            return await _run(db)
        async with DatabaseManager(db_path, _MIGRATIONS_DIR) as manager:
            return await _run(manager)
    except Exception:
        logger.exception("review draft generation failed")
        return EXIT_ERROR


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(
        generate_story_review_draft(
            story_id=args.story,
            output=args.output,
            db_path=args.db,
            window_turns=args.window_turns,
            monitor_file=args.monitor_file,
            monitor_dir=args.monitor_dir,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
