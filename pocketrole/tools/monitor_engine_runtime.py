#!/usr/bin/env python3
"""実 DB 運用中の engine を外から監視し、Markdown へ追記する CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from db.db_manager import DatabaseManager
from engine.config import Config, load_config
from engine.story_intent import get_story_intent_profile

EXIT_OK = 0
EXIT_ERROR = 1
_TURN_GAP_WARN_THRESHOLD = 3
_CHAPTER_PROGRESS_STALL_TURNS = 20
_RELATIONSHIP_MODE_CLUSTERS = {
    "irritated_respect": "conflict",
    "chaos_partner": "conflict",
    "unsafe_confidant": "attention",
    "cannot_ignore": "attention",
}
_RELATIONSHIP_COMPLEMENTARY_CLUSTERS = frozenset({"conflict", "attention"})

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """コマンドライン引数を解析する。"""
    parser = argparse.ArgumentParser(
        description="engine.main の実行を外部監視し Markdown に記録する"
    )
    parser.add_argument("--story", required=True, help="対象ストーリーID")
    parser.add_argument(
        "--db",
        default="db/pocketrole.db",
        help="SQLite DB ファイルパス（デフォルト: db/pocketrole.db）",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="設定ファイルパス（デフォルト: config.yaml）",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help=".env パス（デフォルト: .env）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="監視 Markdown 出力先（省略時: docs/monitoring/<story>_runtime_monitor.md）",
    )
    parser.add_argument(
        "--interval-sec",
        type=float,
        default=15.0,
        help="監視間隔秒（デフォルト: 15.0）",
    )
    parser.add_argument(
        "--max-entries",
        type=int,
        default=None,
        help="書き込むスナップショット数上限（省略時は継続監視）",
    )
    parser.add_argument(
        "--engine-pid",
        type=int,
        default=None,
        help="監視対象 engine.main の PID",
    )
    parser.add_argument(
        "--log-path",
        default="logs/engine.log",
        help="engine JSON ログパス（デフォルト: logs/engine.log）",
    )
    parser.add_argument(
        "--window-turns",
        type=int,
        default=20,
        help="品質メトリクス集計に使う turn 窓（デフォルト: 20）",
    )
    parser.add_argument(
        "--stall-samples",
        type=int,
        default=3,
        help="turn 不変を stall とみなす連続 sample 数（デフォルト: 3）",
    )
    parser.add_argument(
        "--reply-ratio-warn-below",
        type=float,
        default=0.20,
        help="reply_ratio warning 下限（デフォルト: 0.20）",
    )
    parser.add_argument(
        "--all-chars-spoke-warn-above",
        type=float,
        default=0.60,
        help="all_chars_spoke_ratio warning 上限（デフォルト: 0.60）",
    )
    parser.add_argument(
        "--quality-retry-warn-above",
        type=float,
        default=0.15,
        help="quality_retry_rate warning 上限（デフォルト: 0.15）",
    )
    return parser.parse_args(argv)


def _default_output_path(story_id: str) -> Path:
    return _REPO_ROOT / "docs" / "monitoring" / f"{story_id}_runtime_monitor.md"


def _is_process_alive(pid: int | None) -> bool | None:
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _probe_ollama(base_url: str) -> tuple[bool, str]:
    url = f"{base_url.rstrip('/')}/api/tags"
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return True, "HTTP 200"
                return False, f"HTTP {response.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def _probe_gpu() -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None

    stdout, _stderr = await proc.communicate()
    if proc.returncode != 0:
        return None

    rows = stdout.decode("utf-8", errors="replace").splitlines()
    summaries: list[str] = []
    for idx, row in enumerate(rows):
        parts = [part.strip() for part in row.split(",")]
        if len(parts) != 4:
            continue
        used, total, util, temp = parts
        summaries.append(
            f"gpu{idx}: {used}/{total} MiB, util {util}%, temp {temp}C"
        )
    return "; ".join(summaries) if summaries else None


def _read_recent_errors(log_path: Path, limit: int = 3) -> list[str]:
    if not log_path.exists():
        return []

    matches: deque[str] = deque(maxlen=limit)
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            level = str(payload.get("level", ""))
            msg = str(payload.get("msg", ""))
            if level not in {"WARNING", "ERROR", "CRITICAL"} and "FAILED" not in msg:
                continue
            matches.append(f"{level} {msg}")
    return list(matches)


async def _collect_story_progress(
    db: DatabaseManager,
    story_id: str,
) -> dict[str, Any]:
    assert db._conn is not None

    count_cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM chat_logs WHERE story_id = ?",
        (story_id,),
    )
    count_row = await count_cursor.fetchone()
    chat_log_count = int(count_row[0]) if count_row is not None else 0

    latest_cursor = await db._conn.execute(
        """
        SELECT turn_number, char_id, LENGTH(COALESCE(message, ''))
        FROM chat_logs
        WHERE story_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (story_id,),
    )
    latest_row = await latest_cursor.fetchone()

    story = await db.get_story(story_id)
    return {
        "chat_log_count": chat_log_count,
        "max_turn_number": latest_row[0] if latest_row is not None else None,
        "last_char_id": latest_row[1] if latest_row is not None else None,
        "last_message_len": latest_row[2] if latest_row is not None else None,
        "last_sim_datetime": story["last_sim_time"] if story is not None else None,
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


async def _collect_story_quality_metrics(
    db: DatabaseManager,
    story_id: str,
    *,
    window_turns: int,
) -> dict[str, Any]:
    """既存 runtime tables から物語品質メトリクスを集計する。"""
    assert db._conn is not None
    intent_profile = get_story_intent_profile(story_id)

    latest_turn = await _fetch_scalar(
        db,
        "SELECT COALESCE(MAX(turn_number), 0) FROM chat_logs WHERE story_id = ?",
        (story_id,),
    )
    since_turn = max(0, latest_turn - max(window_turns, 1) + 1)
    active_characters = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM characters WHERE story_id = ? AND is_active = 1",
        (story_id,),
    )
    total_recent_logs = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM chat_logs WHERE story_id = ? AND turn_number >= ?",
        (story_id, since_turn),
    )

    cursor = await db._conn.execute(
        """
        SELECT msg_type, COUNT(*) AS count
        FROM chat_logs
        WHERE story_id = ? AND turn_number >= ?
        GROUP BY msg_type
        """,
        (story_id, since_turn),
    )
    msg_type_counts = {
        str(row["msg_type"]): int(row["count"])
        for row in await cursor.fetchall()
    }

    cursor = await db._conn.execute(
        """
        SELECT turn_number, COUNT(DISTINCT char_id) AS speakers
        FROM chat_logs
        WHERE story_id = ? AND turn_number >= ? AND char_id != '_narrator'
        GROUP BY turn_number
        ORDER BY turn_number ASC
        """,
        (story_id, since_turn),
    )
    speaker_rows = await cursor.fetchall()
    turn_count = len(speaker_rows)
    total_speakers = sum(int(row["speakers"]) for row in speaker_rows)
    all_chars_turns = sum(
        1
        for row in speaker_rows
        if active_characters > 0 and int(row["speakers"]) >= active_characters
    )

    active_scenes = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM story_scenes WHERE story_id = ? AND status = 'active'",
        (story_id,),
    )
    closed_scenes_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_scenes
        WHERE story_id = ?
          AND status = 'closed'
          AND COALESCE(closed_turn, opened_turn, 0) >= ?
        """,
        (story_id, since_turn),
    )
    open_hooks = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM story_hooks WHERE story_id = ? AND status = 'open'",
        (story_id,),
    )
    hooks_created_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_hooks h
        LEFT JOIN chat_logs l ON l.id = h.source_log_id
        LEFT JOIN story_scenes s ON s.id = h.source_scene_id
        WHERE h.story_id = ?
          AND COALESCE(l.turn_number, s.opened_turn, 0) >= ?
        """,
        (story_id, since_turn),
    )
    hooks_resolved_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_hooks
        WHERE story_id = ?
          AND status = 'resolved'
          AND resolved_turn IS NOT NULL
          AND resolved_turn >= ?
        """,
        (story_id, since_turn),
    )
    active_tensions = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM narrative_tensions WHERE story_id = ? AND status != 'resolved'",
        (story_id,),
    )
    intervention_eligible_now = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM narrative_tensions
        WHERE story_id = ?
          AND status != 'resolved'
          AND status IN ('escalating', 'climax')
        """,
        (story_id,),
    )
    resolved_tensions_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM narrative_tensions
        WHERE story_id = ?
          AND status = 'resolved'
          AND resolved_turn IS NOT NULL
          AND resolved_turn >= ?
        """,
        (story_id, since_turn),
    )
    live_interventions = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM director_interventions
        WHERE story_id = ?
          AND status IN ('active', 'acknowledged')
          AND active_from_turn <= ?
          AND (active_until_turn IS NULL OR active_until_turn >= ?)
        """,
        (story_id, latest_turn, latest_turn),
    )
    relationship_pairs_changed_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM (
            SELECT DISTINCT char_id_from, char_id_to
            FROM relationship_events
            WHERE story_id = ? AND turn_number >= ?
        )
        """,
        (story_id, since_turn),
    )
    active_relationship_modes = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM relationship_modes
        WHERE story_id = ? AND status = 'active'
        """,
        (story_id,),
    )
    relationship_mode_reinforcements_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM relationship_modes
        WHERE story_id = ? AND status = 'active' AND last_reinforced_turn >= ?
        """,
        (story_id, since_turn),
    )
    relationship_mode_decay_updates_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM relationship_modes
        WHERE story_id = ?
          AND status = 'active'
          AND last_reinforced_turn < ?
          AND updated_at >= datetime('now', '-1 day')
        """,
        (story_id, since_turn),
    )
    cursor = await db._conn.execute(
        """
        SELECT char_id_from, char_id_to, mode_type, last_reinforced_turn, updated_at
        FROM relationship_modes
        WHERE story_id = ? AND status = 'active'
        """,
        (story_id,),
    )
    active_mode_rows = await cursor.fetchall()
    pair_modes: dict[tuple[str, str], set[str]] = {}
    pair_clusters: dict[tuple[str, str], set[str]] = {}
    for row in active_mode_rows:
        pair = (str(row["char_id_from"]), str(row["char_id_to"]))
        mode_type = str(row["mode_type"])
        pair_modes.setdefault(pair, set()).add(mode_type)
        cluster = _RELATIONSHIP_MODE_CLUSTERS.get(mode_type)
        if cluster is not None:
            pair_clusters.setdefault(pair, set()).add(cluster)
    multi_mode_pairs_active = sum(1 for modes in pair_modes.values() if len(modes) >= 2)
    single_mode_pairs_active = sum(1 for modes in pair_modes.values() if len(modes) == 1)
    complementary_mode_pairs_active = sum(
        1 for clusters in pair_clusters.values() if _RELATIONSHIP_COMPLEMENTARY_CLUSTERS.issubset(clusters)
    )
    relationship_mode_conflict_attention_pairs = complementary_mode_pairs_active
    complementary_pairs = {
        pair for pair, clusters in pair_clusters.items()
        if _RELATIONSHIP_COMPLEMENTARY_CLUSTERS.issubset(clusters)
    }
    complementary_mode_reinforcements_recent = sum(
        1
        for row in active_mode_rows
        if (
            (str(row["char_id_from"]), str(row["char_id_to"])) in complementary_pairs
            and int(row["last_reinforced_turn"] or 0) >= since_turn
        )
    )
    complementary_mode_decay_updates_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM relationship_modes
        WHERE story_id = ?
          AND status = 'active'
          AND last_reinforced_turn < ?
          AND updated_at >= datetime('now', '-1 day')
          AND EXISTS (
            SELECT 1
            FROM relationship_modes rm2
            WHERE rm2.story_id = relationship_modes.story_id
              AND rm2.status = 'active'
              AND rm2.char_id_from = relationship_modes.char_id_from
              AND rm2.char_id_to = relationship_modes.char_id_to
              AND rm2.mode_type != relationship_modes.mode_type
          )
        """,
        (story_id, since_turn),
    )
    complementary_mode_deactivations_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM relationship_modes
        WHERE story_id = ?
          AND status = 'inactive'
          AND updated_at >= datetime('now', '-1 day')
        """,
        (story_id,),
    )
    cursor = await db._conn.execute(
        """
        SELECT mode_type
        FROM relationship_modes
        WHERE story_id = ? AND status = 'active'
        GROUP BY mode_type
        ORDER BY COUNT(*) DESC, mode_type ASC
        LIMIT 3
        """,
        (story_id,),
    )
    dominant_relationship_modes = [str(row["mode_type"]) for row in await cursor.fetchall()]
    active_canon_bits = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_canon_bits
        WHERE story_id = ? AND status = 'active'
        """,
        (story_id,),
    )
    canon_reinforcements_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_canon_bits
        WHERE story_id = ? AND status = 'active' AND last_reinforced_turn >= ?
        """,
        (story_id, since_turn),
    )
    canon_promotions_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_canon_bits
        WHERE story_id = ? AND status = 'active' AND canon_level IN ('proto_canon', 'canon') AND last_reinforced_turn >= ?
        """,
        (story_id, since_turn),
    )
    canon_reignitions_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_canon_bits
        WHERE story_id = ?
          AND status = 'active'
          AND last_reignited_turn IS NOT NULL
          AND last_reignited_turn >= ?
        """,
        (story_id, since_turn),
    )
    canon_triggered_hooks_open = await _fetch_scalar(
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
    active_canon_profile_overlays = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM character_canon_overlays
        WHERE story_id = ?
        """,
        (story_id,),
    )
    canon_writebacks_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_canon_bits
        WHERE story_id = ?
          AND status = 'active'
          AND last_writeback_turn IS NOT NULL
          AND last_writeback_turn >= ?
        """,
        (story_id, since_turn),
    )
    canon_overlay_chars_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM character_canon_overlays
        WHERE story_id = ?
          AND last_written_turn IS NOT NULL
          AND last_written_turn >= ?
        """,
        (story_id, since_turn),
    )
    cursor = await db._conn.execute(
        """
        SELECT canon_level
        FROM story_canon_bits
        WHERE story_id = ? AND status = 'active'
        GROUP BY canon_level
        ORDER BY COUNT(*) DESC, canon_level ASC
        LIMIT 3
        """,
        (story_id,),
    )
    dominant_canon_levels = [str(row["canon_level"]) for row in await cursor.fetchall()]
    cursor = await db._conn.execute(
        """
        SELECT motif_key
        FROM story_canon_bits
        WHERE story_id = ? AND status = 'active'
        GROUP BY motif_key
        ORDER BY COUNT(*) DESC, motif_key ASC
        LIMIT 3
        """,
        (story_id,),
    )
    dominant_canon_motifs = [str(row["motif_key"]) for row in await cursor.fetchall()]
    active_dramatic_pressures = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_dramatic_pressures
        WHERE story_id = ? AND status = 'active'
        """,
        (story_id,),
    )
    pressure_reinforcements_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_dramatic_pressures
        WHERE story_id = ? AND status = 'active' AND last_detected_turn >= ?
        """,
        (story_id, since_turn),
    )
    cursor = await db._conn.execute(
        """
        SELECT pressure_type
        FROM story_dramatic_pressures
        WHERE story_id = ? AND status = 'active'
        GROUP BY pressure_type
        ORDER BY COUNT(*) DESC, pressure_type ASC
        LIMIT 3
        """,
        (story_id,),
    )
    dominant_pressure_types = [str(row["pressure_type"]) for row in await cursor.fetchall()]
    max_pressure_score_cursor = await db._conn.execute(
        """
        SELECT MAX(score)
        FROM story_dramatic_pressures
        WHERE story_id = ? AND status = 'active'
        """,
        (story_id,),
    )
    max_pressure_score_row = await max_pressure_score_cursor.fetchone()
    max_pressure_score = (
        round(float(max_pressure_score_row[0]), 2)
        if max_pressure_score_row is not None and max_pressure_score_row[0] is not None
        else 0.0
    )
    growth_candidates_pending = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM character_growth_candidates
        WHERE story_id = ? AND status = 'pending'
        """,
        (story_id,),
    )
    growth_commits_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM character_evolution
        WHERE story_id = ? AND turn_number >= ?
        """,
        (story_id, since_turn),
    )
    growth_empty_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ? AND created_turn >= ? AND issue_type = 'growth_llm_empty'
        """,
        (story_id, since_turn),
    )
    growth_quality_rejections_recent = await _fetch_scalar(
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
    growth_parse_failures_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ? AND created_turn >= ? AND issue_type = 'growth_parse_failed'
        """,
        (story_id, since_turn),
    )
    growth_rollbacks_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ? AND created_turn >= ? AND issue_type = 'growth_write_rolled_back'
        """,
        (story_id, since_turn),
    )
    growth_superseded_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM character_growth_candidates
        WHERE story_id = ? AND status = 'superseded' AND detected_turn >= ?
        """,
        (story_id, since_turn),
    )
    growth_expired_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM character_growth_candidates
        WHERE story_id = ? AND status = 'expired' AND detected_turn >= ?
        """,
        (story_id, since_turn),
    )
    active_interaction_patterns = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_interaction_patterns
        WHERE story_id = ? AND status = 'active'
        """,
        (story_id,),
    )
    patterns_detected_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_interaction_patterns
        WHERE story_id = ? AND last_detected_turn >= ?
        """,
        (story_id, since_turn),
    )
    pattern_recurrences_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_interaction_patterns
        WHERE story_id = ? AND last_detected_turn >= ? AND recurrence_count > 1
        """,
        (story_id, since_turn),
    )
    cursor = await db._conn.execute(
        """
        SELECT pattern_type
        FROM story_interaction_patterns
        WHERE story_id = ? AND status = 'active'
        GROUP BY pattern_type
        ORDER BY COUNT(*) DESC, pattern_type ASC
        LIMIT 3
        """,
        (story_id,),
    )
    dominant_pattern_types = [str(row["pattern_type"]) for row in await cursor.fetchall()]
    active_episode = await db.get_active_story_episode(story_id)
    recent_episodes = await db.get_recent_story_episodes(story_id, limit=10)
    episodes_closed_recent = sum(
        1
        for episode in recent_episodes
        if episode.get("status") == "closed"
        and int(episode.get("closed_turn") or 0) >= since_turn
    )
    episode_arcs_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_arc
        WHERE story_id = ?
          AND arc_type = 'episode'
          AND turn_from >= ?
        """,
        (story_id, since_turn),
    )
    sessionless_monologues_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM chat_logs
        WHERE story_id = ?
          AND turn_number >= ?
          AND msg_type = 'monologue'
          AND conversation_session_id IS NULL
        """,
        (story_id, since_turn),
    )
    solo_scenes_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_scenes
        WHERE story_id = ?
          AND scene_type = 'solo'
          AND closed_turn IS NOT NULL
          AND closed_turn >= ?
        """,
        (story_id, since_turn),
    )
    quality_issues_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ? AND created_turn >= ?
        """,
        (story_id, since_turn),
    )
    quality_normalizations_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ? AND created_turn >= ? AND issue_type = 'quality_output_normalized'
        """,
        (story_id, since_turn),
    )
    quality_fallbacks_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ? AND created_turn >= ? AND issue_type = 'quality_output_fallback'
        """,
        (story_id, since_turn),
    )
    reply_quality_normalizations_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'quality_output_normalized'
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
        """,
        (story_id, since_turn),
    )
    reply_quality_fallbacks_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'quality_output_fallback'
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
        """,
        (story_id, since_turn),
    )
    reply_fallback_focus_missing_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'quality_output_fallback'
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.fallback_root_issue'), '') = 'reply_focus_missing'
        """,
        (story_id, since_turn),
    )
    reply_fallback_direct_reaction_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'quality_output_fallback'
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.fallback_root_issue'), '') = 'reply_without_direct_reaction'
        """,
        (story_id, since_turn),
    )
    reply_retry_kept_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'quality_output_normalized'
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.retry_kept_without_fallback'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_focus_misses_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'reply_focus_missing'
        """,
        (story_id, since_turn),
    )
    generic_reply_tails_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'generic_reply_tail'
        """,
        (story_id, since_turn),
    )
    voice_flat_replies_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'voice_flat_reply'
        """,
        (story_id, since_turn),
    )
    reply_flat_generic_tail_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'generic_reply_tail'
        """,
        (story_id, since_turn),
    )
    reply_flat_voice_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'voice_flat_reply'
          AND COALESCE(json_extract(details, '$.flat_issue_family'), '') != 'reused_tail'
        """,
        (story_id, since_turn),
    )
    reply_flat_reused_tail_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.recent_self_tail_reused'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_variety_press_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_variety_second_beat'), '') = 'press'
        """,
        (story_id, since_turn),
    )
    reply_variety_condition_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_variety_second_beat'), '') = 'condition'
        """,
        (story_id, since_turn),
    )
    reply_variety_redirect_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_variety_second_beat'), '') = 'redirect'
        """,
        (story_id, since_turn),
    )
    reply_dramatic_move_missing_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_dramatic_move_seen'), 1) = 0
        """,
        (story_id, since_turn),
    )
    reply_soft_landing_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_soft_landing_used'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_shape_dominance_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_shape_reused_recently'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_bland_shape_reused_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_blandness_shape_reused'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_pressure_shift_missing_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_pressure_shift_seen'), 1) = 0
        """,
        (story_id, since_turn),
    )
    reply_story_flavor_weak_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.story_flavor_weak'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_quality_keep_blocked_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_quality_contract_missed'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_story_quality_keep_blocked_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_story_quality_contract_missed'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_residual_keep_blocked_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_residual_contract_missed'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_bland_keep_blocked_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.blandness_contract_missed'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_second_beat_reused_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.reply_second_beat_reused'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_reused_opening_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.recent_opening_reused'), 0) = 1
        """,
        (story_id, since_turn),
    )
    reply_reused_ending_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND (
            COALESCE(json_extract(details, '$.recent_ending_reused'), 0) = 1
            OR COALESCE(json_extract(details, '$.recent_self_tail_reused'), 0) = 1
          )
        """,
        (story_id, since_turn),
    )
    reply_reused_second_beat_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND COALESCE(json_extract(details, '$.msg_type'), '') = 'reply'
          AND COALESCE(json_extract(details, '$.recent_second_beat_reused'), 0) = 1
        """,
        (story_id, since_turn),
    )
    signal_visibility_misses_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'signal_visibility_missing'
        """,
        (story_id, since_turn),
    )
    objective_visibility_misses_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'scene_objective_visibility_missing'
        """,
        (story_id, since_turn),
    )
    signal_visibility_retry_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'signal_visibility_missing'
          AND severity = 'warning'
        """,
        (story_id, since_turn),
    )
    objective_visibility_retry_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM generation_quality_issues
        WHERE story_id = ?
          AND created_turn >= ?
          AND issue_type = 'scene_objective_visibility_missing'
          AND severity = 'warning'
        """,
        (story_id, since_turn),
    )
    retried_logs = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM chat_logs
        WHERE story_id = ? AND turn_number >= ? AND generation_attempt > 1
        """,
        (story_id, since_turn),
    )
    scene_arcs_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_arc
        WHERE story_id = ?
          AND source_scene_id IS NOT NULL
          AND turn_from >= ?
        """,
        (story_id, since_turn),
    )
    closed_scenes_without_arc_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_scenes s
        LEFT JOIN story_arc a
          ON a.story_id = s.story_id
         AND a.source_scene_id = s.id
        WHERE s.story_id = ?
          AND s.scene_type = 'conversation'
          AND s.status = 'closed'
          AND COALESCE(s.closed_turn, s.opened_turn, 0) >= ?
          AND a.id IS NULL
        """,
        (story_id, since_turn),
    )
    scene_arcs_without_novel_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_arc a
        LEFT JOIN novel_output n
          ON n.story_id = a.story_id
         AND n.arc_id = a.id
        WHERE a.story_id = ?
          AND a.source_scene_id IS NOT NULL
          AND a.turn_from >= ?
          AND n.id IS NULL
        """,
        (story_id, since_turn),
    )
    scene_close_backlog_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_scenes s
        LEFT JOIN story_arc a
          ON a.story_id = s.story_id
         AND a.source_scene_id = s.id
        LEFT JOIN novel_output n
          ON n.story_id = a.story_id
         AND n.arc_id = a.id
        WHERE s.story_id = ?
          AND s.scene_type = 'conversation'
          AND s.status = 'closed'
          AND COALESCE(s.closed_turn, s.opened_turn, 0) >= ?
          AND (a.id IS NULL OR n.id IS NULL)
        """,
        (story_id, since_turn),
    )
    scene_close_missing_arc_recent = closed_scenes_without_arc_recent
    scene_close_missing_novel_recent = scene_arcs_without_novel_recent
    scene_close_backlog_recovered_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM story_scenes s
        JOIN story_arc a
          ON a.story_id = s.story_id
         AND a.source_scene_id = s.id
        JOIN novel_output n
          ON n.story_id = a.story_id
         AND n.arc_id = a.id
        WHERE s.story_id = ?
          AND s.scene_type = 'conversation'
          AND s.status = 'closed'
          AND COALESCE(s.closed_turn, s.opened_turn, 0) >= ?
        """,
        (story_id, since_turn),
    )
    novel_outputs_recent = await _fetch_scalar(
        db,
        """
        SELECT COUNT(*)
        FROM novel_output n
        JOIN story_arc a ON a.id = n.arc_id
        WHERE n.story_id = ?
          AND a.source_scene_id IS NOT NULL
          AND a.turn_from >= ?
        """,
        (story_id, since_turn),
    )
    cursor = await db._conn.execute(
        """
        WITH latest_activity AS (
            SELECT char_id, MAX(turn_number) AS last_turn
            FROM (
                SELECT char_id, turn_number
                FROM character_states
                WHERE story_id = ?
                UNION ALL
                SELECT char_id, turn_number
                FROM chat_logs
                WHERE story_id = ? AND char_id != '_narrator'
            )
            GROUP BY char_id
        )
        SELECT c.id AS char_id, latest_activity.last_turn
        FROM characters c
        LEFT JOIN latest_activity ON latest_activity.char_id = c.id
        WHERE c.story_id = ? AND c.is_active = 1
        ORDER BY c.id ASC
        """,
        (story_id, story_id, story_id),
    )
    activity_rows = await cursor.fetchall()
    max_character_turn_gap = 0
    stale_char_ids: list[str] = []
    for row in activity_rows:
        last_turn = row["last_turn"]
        if last_turn is None:
            turn_gap = 0 if latest_turn == 0 else latest_turn + 1
        else:
            turn_gap = max(0, latest_turn - int(last_turn))
        max_character_turn_gap = max(max_character_turn_gap, turn_gap)
        if turn_gap >= _TURN_GAP_WARN_THRESHOLD:
            stale_char_ids.append(str(row["char_id"]))

    # ── v2 upgrade: Chapter 進行状況 ──────────────────────────────────────
    active_chapter = await db.get_active_chapter(story_id)
    if active_chapter is not None:
        opened_turn = int(active_chapter.get("opened_turn") or 0)
        chapter_metrics: dict[str, Any] = {
            "active_chapter_id": active_chapter.get("chapter_id"),
            "active_chapter_beat": active_chapter.get("current_beat"),
            "active_chapter_opened_turn": opened_turn,
            "active_chapter_age": (
                max(0, latest_turn - opened_turn + 1)
                if latest_turn > 0 and opened_turn > 0
                else 0
            ),
        }
    else:
        chapter_metrics = {
            "active_chapter_id": None,
            "active_chapter_beat": None,
            "active_chapter_opened_turn": None,
            "active_chapter_age": 0,
        }

    # ── v2 upgrade: Director Persona 満足度 ──────────────────────────────
    active_persona = await db.get_active_director_persona(story_id)
    director_metrics: dict[str, Any] = {
        "active_director_persona_id": None,
        "director_satisfaction_overall": None,
        "director_satisfaction_trend": None,
        "director_satisfaction_tension": None,
        "director_satisfaction_pacing": None,
        "director_satisfaction_surprise": None,
    }
    if active_persona is not None:
        pid = active_persona["persona_id"]
        director_metrics["active_director_persona_id"] = pid
        latest_sat = await db.get_latest_director_satisfaction(story_id, pid)
        if latest_sat is not None:
            director_metrics.update({
                "director_satisfaction_overall": round(float(latest_sat.get("overall", 0.0)), 3),
                "director_satisfaction_trend": latest_sat.get("trend", "flat"),
                "director_satisfaction_tension": round(float(latest_sat.get("tension_sat", 0.0)), 3),
                "director_satisfaction_pacing": round(float(latest_sat.get("pacing_sat", 0.0)), 3),
                "director_satisfaction_surprise": round(float(latest_sat.get("surprise_sat", 0.0)), 3),
            })

    return {
        "story_intent_mode": intent_profile.primary_mode,
        "speaking_chars_per_turn_avg": (
            round(total_speakers / turn_count, 2) if turn_count else 0.0
        ),
        "all_chars_spoke_ratio": (
            round(all_chars_turns / turn_count, 2) if turn_count else 0.0
        ),
        "reply_ratio": (
            round(msg_type_counts.get("reply", 0) / total_recent_logs, 2)
            if total_recent_logs else 0.0
        ),
        "group_ratio": (
            round(msg_type_counts.get("group", 0) / total_recent_logs, 2)
            if total_recent_logs else 0.0
        ),
        "monologue_ratio": (
            round(msg_type_counts.get("monologue", 0) / total_recent_logs, 2)
            if total_recent_logs else 0.0
        ),
        "sessionless_monologues_recent": sessionless_monologues_recent,
        "sessionless_monologue_ratio": (
            round(sessionless_monologues_recent / total_recent_logs, 2)
            if total_recent_logs else 0.0
        ),
        "active_scenes": active_scenes,
        "closed_scenes_recent": closed_scenes_recent,
        "solo_scenes_recent": solo_scenes_recent,
        "open_hooks": open_hooks,
        "hooks_created_recent": hooks_created_recent,
        "hooks_resolved_recent": hooks_resolved_recent,
        "active_tensions": active_tensions,
        "intervention_eligible_now": intervention_eligible_now,
        "max_character_turn_gap": max_character_turn_gap,
        "stale_char_ids": stale_char_ids,
        "resolved_tensions_recent": resolved_tensions_recent,
        "live_interventions": live_interventions,
        "relationship_pairs_changed_recent": relationship_pairs_changed_recent,
        "active_relationship_modes": active_relationship_modes,
        "relationship_mode_reinforcements_recent": relationship_mode_reinforcements_recent,
        "relationship_mode_decay_updates_recent": relationship_mode_decay_updates_recent,
        "multi_mode_pairs_active": multi_mode_pairs_active,
        "complementary_mode_pairs_active": complementary_mode_pairs_active,
        "single_mode_pairs_active": single_mode_pairs_active,
        "relationship_mode_conflict_attention_pairs": relationship_mode_conflict_attention_pairs,
        "complementary_mode_reinforcements_recent": complementary_mode_reinforcements_recent,
        "complementary_mode_decay_updates_recent": complementary_mode_decay_updates_recent,
        "complementary_mode_deactivations_recent": complementary_mode_deactivations_recent,
        "dominant_relationship_modes": dominant_relationship_modes,
        "active_canon_bits": active_canon_bits,
        "canon_reinforcements_recent": canon_reinforcements_recent,
        "canon_promotions_recent": canon_promotions_recent,
        "canon_reignitions_recent": canon_reignitions_recent,
        "canon_triggered_hooks_open": canon_triggered_hooks_open,
        "active_canon_profile_overlays": active_canon_profile_overlays,
        "canon_writebacks_recent": canon_writebacks_recent,
        "canon_overlay_chars_recent": canon_overlay_chars_recent,
        "dominant_canon_levels": dominant_canon_levels,
        "dominant_canon_motifs": dominant_canon_motifs,
        "active_dramatic_pressures": active_dramatic_pressures,
        "pressure_reinforcements_recent": pressure_reinforcements_recent,
        "dominant_pressure_types": dominant_pressure_types,
        "max_pressure_score": max_pressure_score,
        "active_interaction_patterns": active_interaction_patterns,
        "patterns_detected_recent": patterns_detected_recent,
        "pattern_recurrences_recent": pattern_recurrences_recent,
        "dominant_pattern_types": dominant_pattern_types,
        "active_episode_id": (
            int(active_episode["id"])
            if active_episode is not None and active_episode.get("id") is not None
            else None
        ),
        "active_episode_type": (
            str(active_episode.get("episode_type") or "") or None
            if active_episode is not None
            else None
        ),
        "active_episode_goal": (
            str(active_episode.get("goal") or "") or None
            if active_episode is not None
            else None
        ),
        "active_episode_age": (
            max(0, latest_turn - int(active_episode.get("opened_turn") or latest_turn) + 1)
            if active_episode is not None and latest_turn > 0
            else 0
        ),
        "episodes_closed_recent": episodes_closed_recent,
        "growth_candidates_pending": growth_candidates_pending,
        "growth_commits_recent": growth_commits_recent,
        "growth_empty_recent": growth_empty_recent,
        "growth_quality_rejections_recent": growth_quality_rejections_recent,
        "growth_parse_failures_recent": growth_parse_failures_recent,
        "growth_rollbacks_recent": growth_rollbacks_recent,
        "growth_superseded_recent": growth_superseded_recent,
        "growth_expired_recent": growth_expired_recent,
        "quality_issues_recent": quality_issues_recent,
        "quality_normalizations_recent": quality_normalizations_recent,
        "quality_fallbacks_recent": quality_fallbacks_recent,
        "reply_quality_normalizations_recent": reply_quality_normalizations_recent,
        "reply_quality_fallbacks_recent": reply_quality_fallbacks_recent,
        "reply_fallback_focus_missing_recent": reply_fallback_focus_missing_recent,
        "reply_fallback_direct_reaction_recent": reply_fallback_direct_reaction_recent,
        "reply_retry_kept_recent": reply_retry_kept_recent,
        "reply_focus_misses_recent": reply_focus_misses_recent,
        "generic_reply_tails_recent": generic_reply_tails_recent,
        "voice_flat_replies_recent": voice_flat_replies_recent,
        "reply_flat_generic_tail_recent": reply_flat_generic_tail_recent,
        "reply_flat_voice_recent": reply_flat_voice_recent,
        "reply_flat_reused_tail_recent": reply_flat_reused_tail_recent,
        "reply_variety_press_recent": reply_variety_press_recent,
        "reply_variety_condition_recent": reply_variety_condition_recent,
        "reply_variety_redirect_recent": reply_variety_redirect_recent,
        "reply_dramatic_move_missing_recent": reply_dramatic_move_missing_recent,
        "reply_soft_landing_recent": reply_soft_landing_recent,
        "reply_shape_dominance_recent": reply_shape_dominance_recent,
        "reply_bland_shape_reused_recent": reply_bland_shape_reused_recent,
        "reply_pressure_shift_missing_recent": reply_pressure_shift_missing_recent,
        "reply_story_flavor_weak_recent": reply_story_flavor_weak_recent,
        "reply_second_beat_reused_recent": reply_second_beat_reused_recent,
        "reply_quality_keep_blocked_recent": reply_quality_keep_blocked_recent,
        "reply_story_quality_keep_blocked_recent": reply_story_quality_keep_blocked_recent,
        "reply_residual_keep_blocked_recent": reply_residual_keep_blocked_recent,
        "reply_bland_keep_blocked_recent": reply_bland_keep_blocked_recent,
        "reply_reused_opening_recent": reply_reused_opening_recent,
        "reply_reused_ending_recent": reply_reused_ending_recent,
        "reply_reused_second_beat_recent": reply_reused_second_beat_recent,
        "signal_visibility_misses_recent": signal_visibility_misses_recent,
        "objective_visibility_misses_recent": objective_visibility_misses_recent,
        "signal_visibility_retry_recent": signal_visibility_retry_recent,
        "objective_visibility_retry_recent": objective_visibility_retry_recent,
        "quality_retry_rate": (
            round(retried_logs / total_recent_logs, 2) if total_recent_logs else 0.0
        ),
        "scene_close_completion_rate": (
            round(scene_arcs_recent / closed_scenes_recent, 2)
            if closed_scenes_recent
            else 0.0
        ),
        "growth_candidate_rate": (
            round(growth_candidates_pending / active_characters, 2) if active_characters else 0.0
        ),
        "growth_commit_rate": (
            round(growth_commits_recent / active_characters, 2) if active_characters else 0.0
        ),
        "episode_close_completion_rate": (
            round(episode_arcs_recent / episodes_closed_recent, 2)
            if episodes_closed_recent
            else 0.0
        ),
        "episode_arcs_recent": episode_arcs_recent,
        "scene_arcs_recent": scene_arcs_recent,
        "closed_scenes_without_arc_recent": closed_scenes_without_arc_recent,
        "scene_arcs_without_novel_recent": scene_arcs_without_novel_recent,
        "scene_close_backlog_recent": scene_close_backlog_recent,
        "scene_close_missing_arc_recent": scene_close_missing_arc_recent,
        "scene_close_missing_novel_recent": scene_close_missing_novel_recent,
        "scene_close_backlog_recovered_recent": scene_close_backlog_recovered_recent,
        "novel_outputs_recent": novel_outputs_recent,
        **chapter_metrics,
        **director_metrics,
    }


def _build_monitor_warnings(
    snapshot: dict[str, Any],
    *,
    stall_samples: int,
    reply_ratio_warn_below: float,
    all_chars_spoke_warn_above: float,
    quality_retry_warn_above: float,
) -> list[str]:
    """snapshot から deterministic な warning 一覧を返す。"""
    warnings: list[str] = []
    if snapshot.get("engine_alive") is False:
        warnings.append("engine_stopped")
    if not snapshot.get("ollama_ok", True):
        warnings.append("ollama_failed")
    if int(snapshot.get("stall_samples_seen") or 0) >= stall_samples:
        warnings.append("turn_stalled")
    if float(snapshot.get("reply_ratio") or 0.0) < reply_ratio_warn_below:
        warnings.append("reply_ratio_low")
    if float(snapshot.get("all_chars_spoke_ratio") or 0.0) > all_chars_spoke_warn_above:
        warnings.append("all_chars_spoke_high")
    if float(snapshot.get("sessionless_monologue_ratio") or 0.0) > 0.45:
        warnings.append("sessionless_monologue_high")
    if int(snapshot.get("max_character_turn_gap") or 0) >= _TURN_GAP_WARN_THRESHOLD:
        warnings.append("turn_gap_high")
    if int(snapshot.get("open_hooks") or 0) > 0 and int(snapshot.get("hooks_resolved_recent") or 0) == 0:
        warnings.append("hook_resolution_stalled")
    if (
        int(snapshot.get("intervention_eligible_now") or 0) > 0
        and int(snapshot.get("live_interventions") or 0) == 0
    ):
        warnings.append("tension_without_intervention")
    if float(snapshot.get("quality_retry_rate") or 0.0) > quality_retry_warn_above:
        warnings.append("quality_retry_high")
    if int(snapshot.get("reply_quality_fallbacks_recent") or 0) > 0:
        warnings.append("reply_quality_fallback_high")
    if (
        int(snapshot.get("reply_focus_misses_recent") or 0) > 0
        or int(snapshot.get("generic_reply_tails_recent") or 0) > 0
        or int(snapshot.get("voice_flat_replies_recent") or 0) > 0
    ):
        warnings.append("reply_quality_flat")
    if (
        int(snapshot.get("closed_scenes_recent") or 0) > 0
        and float(snapshot.get("scene_close_completion_rate") or 0.0) < 0.5
    ):
        warnings.append("scene_close_weak")
    if (
        int(snapshot.get("active_relationship_modes") or 0) > 0
        and int(
            snapshot.get(
                "complementary_mode_pairs_active",
                snapshot.get("multi_mode_pairs_active") or 0,
            )
            or 0
        ) == 0
        and int(snapshot.get("single_mode_pairs_active") or 1) > 0
    ):
        warnings.append("relationship_mode_flat")
    if (
        snapshot.get("active_chapter_id") is not None
        and int(snapshot.get("active_chapter_age") or 0) >= _CHAPTER_PROGRESS_STALL_TURNS
    ):
        warnings.append("chapter_progress_stalled")
    overall_sat = snapshot.get("director_satisfaction_overall")
    if overall_sat is not None and float(overall_sat) < 0.4:
        warnings.append("director_satisfaction_low")
    return warnings


async def _collect_runtime_snapshot(
    db: DatabaseManager,
    cfg: Config,
    story_id: str,
    *,
    engine_pid: int | None,
    log_path: Path,
    window_turns: int,
) -> dict[str, Any]:
    progress = await _collect_story_progress(db, story_id)
    quality_metrics = await _collect_story_quality_metrics(
        db,
        story_id,
        window_turns=window_turns,
    )
    ollama_cfg = cfg.llm.providers.get("ollama")
    ollama_base_url = getattr(ollama_cfg, "base_url", "http://localhost:11434")
    ollama_ok, ollama_status = await _probe_ollama(ollama_base_url)

    return {
        "captured_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "story_id": story_id,
        "engine_alive": _is_process_alive(engine_pid),
        "ollama_ok": ollama_ok,
        "ollama_status": ollama_status,
        "gpu_summary": await _probe_gpu(),
        "recent_errors": _read_recent_errors(log_path),
        **progress,
        **quality_metrics,
    }


def render_markdown_entry(snapshot: dict[str, Any]) -> str:
    """監視スナップショット 1 件を Markdown へ変換する。"""
    engine_alive = snapshot.get("engine_alive")
    if engine_alive is None:
        engine_state = "not tracked"
    else:
        engine_state = "alive" if engine_alive else "stopped"

    ollama_state = "OK" if snapshot.get("ollama_ok") else "FAILED"
    gpu_summary = snapshot.get("gpu_summary") or "unavailable"
    recent_errors = snapshot.get("recent_errors") or []
    error_summary = "none" if not recent_errors else " | ".join(recent_errors)
    warnings = snapshot.get("warnings") or []
    warning_summary = "none" if not warnings else " | ".join(warnings)
    stale_char_ids = snapshot.get("stale_char_ids") or []
    stale_char_summary = "none" if not stale_char_ids else ", ".join(stale_char_ids)
    director_overall = snapshot.get("director_satisfaction_overall")
    director_overall_str = (
        f"{float(director_overall):.3f}" if director_overall is not None else "none"
    )
    director_tension = snapshot.get("director_satisfaction_tension")
    director_pacing = snapshot.get("director_satisfaction_pacing")
    director_surprise = snapshot.get("director_satisfaction_surprise")
    director_tension_str = f"{float(director_tension):.3f}" if director_tension is not None else "none"
    director_pacing_str = f"{float(director_pacing):.3f}" if director_pacing is not None else "none"
    director_surprise_str = (
        f"{float(director_surprise):.3f}" if director_surprise is not None else "none"
    )

    return (
        f"## Snapshot {snapshot.get('captured_at')}\n"
        f"### Health\n"
        f"- Story: `{snapshot.get('story_id')}`\n"
        f"- Engine process: {engine_state}\n"
        f"- Ollama: {ollama_state} ({snapshot.get('ollama_status')})\n"
        f"- GPU: {gpu_summary}\n"
        f"\n### Progress\n"
        f"- Progress: turn={snapshot.get('max_turn_number')}, "
        f"chat_logs={snapshot.get('chat_log_count')}, "
        f"last_char={snapshot.get('last_char_id')}, "
        f"last_message_len={snapshot.get('last_message_len')}\n"
        f"- Story clock: {snapshot.get('last_sim_datetime')}\n"
        f"\n### Story Quality\n"
        f"- story_intent_mode={snapshot.get('story_intent_mode', 'default')}\n"
        f"- speaking_chars_per_turn_avg={snapshot.get('speaking_chars_per_turn_avg', 0.0):.2f}, "
        f"all_chars_spoke_ratio={snapshot.get('all_chars_spoke_ratio', 0.0):.2f}\n"
        f"- reply_ratio={snapshot.get('reply_ratio', 0.0):.2f}, "
        f"group_ratio={snapshot.get('group_ratio', 0.0):.2f}, "
        f"monologue_ratio={snapshot.get('monologue_ratio', 0.0):.2f}, "
        f"sessionless_monologue_ratio={snapshot.get('sessionless_monologue_ratio', 0.0):.2f}\n"
        f"- active_scenes={snapshot.get('active_scenes')}, "
        f"closed_scenes_recent={snapshot.get('closed_scenes_recent')}, "
        f"solo_scenes_recent={snapshot.get('solo_scenes_recent')}\n"
        f"- closed_scenes_without_arc_recent={snapshot.get('closed_scenes_without_arc_recent', 0)}, "
        f"scene_arcs_without_novel_recent={snapshot.get('scene_arcs_without_novel_recent', 0)}, "
        f"scene_close_backlog_recent={snapshot.get('scene_close_backlog_recent', 0)}, "
        f"scene_close_missing_arc_recent={snapshot.get('scene_close_missing_arc_recent', 0)}, "
        f"scene_close_missing_novel_recent={snapshot.get('scene_close_missing_novel_recent', 0)}, "
        f"scene_close_backlog_recovered_recent={snapshot.get('scene_close_backlog_recovered_recent', 0)}\n"
        f"- open_hooks={snapshot.get('open_hooks')}, "
        f"hooks_created_recent={snapshot.get('hooks_created_recent')}, "
        f"hooks_resolved_recent={snapshot.get('hooks_resolved_recent')}\n"
        f"- active_tensions={snapshot.get('active_tensions')}, "
        f"intervention_eligible_now={snapshot.get('intervention_eligible_now')}, "
        f"max_character_turn_gap={snapshot.get('max_character_turn_gap')}, "
        f"stale_char_ids={stale_char_summary}, "
        f"resolved_tensions_recent={snapshot.get('resolved_tensions_recent')}, "
        f"live_interventions={snapshot.get('live_interventions')}\n"
        f"- relationship_pairs_changed_recent={snapshot.get('relationship_pairs_changed_recent')}\n"
        f"- active_relationship_modes={snapshot.get('active_relationship_modes', 0)}, "
        f"relationship_mode_reinforcements_recent={snapshot.get('relationship_mode_reinforcements_recent', 0)}, "
        f"relationship_mode_decay_updates_recent={snapshot.get('relationship_mode_decay_updates_recent', 0)}, "
        f"multi_mode_pairs_active={snapshot.get('multi_mode_pairs_active', 0)}, "
        f"complementary_mode_pairs_active={snapshot.get('complementary_mode_pairs_active', 0)}, "
        f"single_mode_pairs_active={snapshot.get('single_mode_pairs_active', 0)}, "
        f"complementary_mode_reinforcements_recent={snapshot.get('complementary_mode_reinforcements_recent', 0)}, "
        f"complementary_mode_decay_updates_recent={snapshot.get('complementary_mode_decay_updates_recent', 0)}, "
        f"complementary_mode_deactivations_recent={snapshot.get('complementary_mode_deactivations_recent', 0)}, "
        f"relationship_mode_conflict_attention_pairs={snapshot.get('relationship_mode_conflict_attention_pairs', 0)}, "
        f"dominant_relationship_modes={', '.join(snapshot.get('dominant_relationship_modes', [])) or 'none'}\n"
        f"- active_canon_bits={snapshot.get('active_canon_bits', 0)}, "
        f"canon_reinforcements_recent={snapshot.get('canon_reinforcements_recent', 0)}, "
        f"canon_promotions_recent={snapshot.get('canon_promotions_recent', 0)}, "
        f"canon_reignitions_recent={snapshot.get('canon_reignitions_recent', 0)}, "
        f"canon_triggered_hooks_open={snapshot.get('canon_triggered_hooks_open', 0)}, "
        f"dominant_canon_levels={', '.join(snapshot.get('dominant_canon_levels', [])) or 'none'}, "
        f"dominant_canon_motifs={', '.join(snapshot.get('dominant_canon_motifs', [])) or 'none'}\n"
        f"- active_dramatic_pressures={snapshot.get('active_dramatic_pressures', 0)}, "
        f"pressure_reinforcements_recent={snapshot.get('pressure_reinforcements_recent', 0)}, "
        f"dominant_pressure_types={', '.join(snapshot.get('dominant_pressure_types', [])) or 'none'}, "
        f"max_pressure_score={snapshot.get('max_pressure_score', 0.0):.2f}\n"
        f"- active_interaction_patterns={snapshot.get('active_interaction_patterns', 0)}, "
        f"patterns_detected_recent={snapshot.get('patterns_detected_recent', 0)}, "
        f"pattern_recurrences_recent={snapshot.get('pattern_recurrences_recent', 0)}, "
        f"dominant_pattern_types={', '.join(snapshot.get('dominant_pattern_types', [])) or 'none'}\n"
        f"- active_episode_id={snapshot.get('active_episode_id')}, "
        f"active_episode_type={snapshot.get('active_episode_type') or 'none'}, "
        f"active_episode_age={snapshot.get('active_episode_age', 0)}, "
        f"episodes_closed_recent={snapshot.get('episodes_closed_recent', 0)}\n"
        f"- active_chapter_id={snapshot.get('active_chapter_id') or 'none'}, "
        f"active_chapter_beat={snapshot.get('active_chapter_beat') or 'none'}, "
        f"active_chapter_age={snapshot.get('active_chapter_age', 0)}\n"
        f"- active_director_persona_id={snapshot.get('active_director_persona_id') or 'none'}, "
        f"director_satisfaction_overall={director_overall_str}, "
        f"director_satisfaction_trend={snapshot.get('director_satisfaction_trend') or 'none'}, "
        f"director_satisfaction_tension={director_tension_str}, "
        f"director_satisfaction_pacing={director_pacing_str}, "
        f"director_satisfaction_surprise={director_surprise_str}\n"
        f"- growth_candidates_pending={snapshot.get('growth_candidates_pending')}, "
        f"growth_commits_recent={snapshot.get('growth_commits_recent')}, "
        f"growth_empty_recent={snapshot.get('growth_empty_recent', 0)}, "
        f"growth_quality_rejections_recent={snapshot.get('growth_quality_rejections_recent', 0)}, "
        f"growth_parse_failures_recent={snapshot.get('growth_parse_failures_recent', 0)}, "
        f"growth_rollbacks_recent={snapshot.get('growth_rollbacks_recent', 0)}, "
        f"growth_superseded_recent={snapshot.get('growth_superseded_recent', 0)}, "
        f"growth_expired_recent={snapshot.get('growth_expired_recent', 0)}, "
        f"sessionless_monologues_recent={snapshot.get('sessionless_monologues_recent')}\n"
        f"- quality_issues_recent={snapshot.get('quality_issues_recent')}, "
        f"quality_normalizations_recent={snapshot.get('quality_normalizations_recent', 0)}, "
        f"quality_fallbacks_recent={snapshot.get('quality_fallbacks_recent', 0)}, "
        f"reply_quality_normalizations_recent={snapshot.get('reply_quality_normalizations_recent', 0)}, "
        f"reply_quality_fallbacks_recent={snapshot.get('reply_quality_fallbacks_recent', 0)}, "
        f"reply_fallback_focus_missing_recent={snapshot.get('reply_fallback_focus_missing_recent', 0)}, "
        f"reply_fallback_direct_reaction_recent={snapshot.get('reply_fallback_direct_reaction_recent', 0)}, "
        f"reply_retry_kept_recent={snapshot.get('reply_retry_kept_recent', 0)}, "
        f"reply_focus_misses_recent={snapshot.get('reply_focus_misses_recent', 0)}, "
        f"generic_reply_tails_recent={snapshot.get('generic_reply_tails_recent', 0)}, "
        f"voice_flat_replies_recent={snapshot.get('voice_flat_replies_recent', 0)}, "
        f"reply_flat_generic_tail_recent={snapshot.get('reply_flat_generic_tail_recent', 0)}, "
        f"reply_flat_voice_recent={snapshot.get('reply_flat_voice_recent', 0)}, "
        f"reply_flat_reused_tail_recent={snapshot.get('reply_flat_reused_tail_recent', 0)}, "
        f"reply_variety_press_recent={snapshot.get('reply_variety_press_recent', 0)}, "
        f"reply_variety_condition_recent={snapshot.get('reply_variety_condition_recent', 0)}, "
        f"reply_variety_redirect_recent={snapshot.get('reply_variety_redirect_recent', 0)}, "
        f"reply_dramatic_move_missing_recent={snapshot.get('reply_dramatic_move_missing_recent', 0)}, "
        f"reply_soft_landing_recent={snapshot.get('reply_soft_landing_recent', 0)}, "
        f"reply_shape_dominance_recent={snapshot.get('reply_shape_dominance_recent', 0)}, "
        f"reply_second_beat_reused_recent={snapshot.get('reply_second_beat_reused_recent', 0)}, "
        f"reply_bland_shape_reused_recent={snapshot.get('reply_bland_shape_reused_recent', 0)}, "
        f"reply_pressure_shift_missing_recent={snapshot.get('reply_pressure_shift_missing_recent', 0)}, "
        f"reply_story_flavor_weak_recent={snapshot.get('reply_story_flavor_weak_recent', 0)}, "
        f"reply_quality_keep_blocked_recent={snapshot.get('reply_quality_keep_blocked_recent', 0)}, "
        f"reply_story_quality_keep_blocked_recent={snapshot.get('reply_story_quality_keep_blocked_recent', 0)}, "
        f"reply_residual_keep_blocked_recent={snapshot.get('reply_residual_keep_blocked_recent', 0)}, "
        f"reply_bland_keep_blocked_recent={snapshot.get('reply_bland_keep_blocked_recent', 0)}, "
        f"reply_reused_opening_recent={snapshot.get('reply_reused_opening_recent', 0)}, "
        f"reply_reused_ending_recent={snapshot.get('reply_reused_ending_recent', 0)}, "
        f"reply_reused_second_beat_recent={snapshot.get('reply_reused_second_beat_recent', 0)}, "
        f"signal_visibility_misses_recent={snapshot.get('signal_visibility_misses_recent', 0)}, "
        f"objective_visibility_misses_recent={snapshot.get('objective_visibility_misses_recent', 0)}, "
        f"signal_visibility_retry_recent={snapshot.get('signal_visibility_retry_recent', 0)}, "
        f"objective_visibility_retry_recent={snapshot.get('objective_visibility_retry_recent', 0)}, "
        f"quality_retry_rate={snapshot.get('quality_retry_rate', 0.0):.2f}\n"
        f"- scene_close_completion_rate={snapshot.get('scene_close_completion_rate', 0.0):.2f}, "
        f"growth_candidate_rate={snapshot.get('growth_candidate_rate', 0.0):.2f}, "
        f"growth_commit_rate={snapshot.get('growth_commit_rate', 0.0):.2f}, "
        f"episode_close_completion_rate={snapshot.get('episode_close_completion_rate', 0.0):.2f}\n"
        f"- episode_arcs_recent={snapshot.get('episode_arcs_recent', 0)}, "
        f"scene_arcs_recent={snapshot.get('scene_arcs_recent')}, "
        f"novel_outputs_recent={snapshot.get('novel_outputs_recent')}\n"
        f"\n### Warnings\n"
        f"- {warning_summary}\n"
        f"- Recent warnings/errors: {error_summary}\n\n"
    )


def _ensure_monitor_file(
    output_path: Path,
    *,
    story_id: str,
    db_path: str | Path,
    engine_pid: int | None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        return

    header = (
        "# Runtime Monitor\n\n"
        f"- story_id: `{story_id}`\n"
        f"- db_path: `{db_path}`\n"
        f"- engine_pid: `{engine_pid}`\n"
        f"- started_at: `{datetime.now(UTC).astimezone().isoformat(timespec='seconds')}`\n\n"
    )
    output_path.write_text(header, encoding="utf-8")


async def run_monitor(
    story_id: str,
    db_path: str | Path = "db/pocketrole.db",
    *,
    config_path: str = "config.yaml",
    env_path: str = ".env",
    output_path: str | Path | None = None,
    interval_sec: float = 15.0,
    max_entries: int | None = None,
    engine_pid: int | None = None,
    log_path: str | Path = "logs/engine.log",
    window_turns: int = 20,
    stall_samples: int = 3,
    reply_ratio_warn_below: float = 0.20,
    all_chars_spoke_warn_above: float = 0.60,
    quality_retry_warn_above: float = 0.15,
) -> dict[str, Any]:
    """engine 監視を実行し Markdown に追記する。"""
    if max_entries is not None and max_entries < 1:
        raise ValueError("max_entries must be >= 1")

    cfg = load_config(config_path, env_path)
    output = Path(output_path) if output_path is not None else _default_output_path(story_id)
    log_file = Path(log_path)
    _ensure_monitor_file(output, story_id=story_id, db_path=db_path, engine_pid=engine_pid)

    entries_written = 0
    last_logged_turn: int | None = None
    last_seen_turn: int | None = None
    stall_samples_seen = 0
    last_warning_set: set[str] = set()

    async with DatabaseManager(db_path, _MIGRATIONS_DIR) as db:
        while max_entries is None or entries_written < max_entries:
            snapshot = await _collect_runtime_snapshot(
                db,
                cfg,
                story_id,
                engine_pid=engine_pid,
                log_path=log_file,
                window_turns=window_turns,
            )
            current_turn = snapshot.get("max_turn_number")
            if current_turn is not None and current_turn == last_seen_turn:
                stall_samples_seen += 1
            else:
                stall_samples_seen = 1
            last_seen_turn = current_turn
            snapshot["stall_samples_seen"] = stall_samples_seen
            snapshot["warnings"] = _build_monitor_warnings(
                snapshot,
                stall_samples=stall_samples,
                reply_ratio_warn_below=reply_ratio_warn_below,
                all_chars_spoke_warn_above=all_chars_spoke_warn_above,
                quality_retry_warn_above=quality_retry_warn_above,
            )
            warning_set = set(snapshot["warnings"])
            should_write = entries_written == 0 or current_turn != last_logged_turn
            if snapshot.get("engine_alive") is False:
                should_write = True
            if warning_set != last_warning_set:
                should_write = True
            if should_write:
                with output.open("a", encoding="utf-8") as handle:
                    handle.write(render_markdown_entry(snapshot))
                entries_written += 1
                last_logged_turn = current_turn
                last_warning_set = warning_set

            if snapshot.get("engine_alive") is False:
                break
            if max_entries is not None and entries_written >= max_entries:
                break
            await asyncio.sleep(interval_sec)

    return {
        "story_id": story_id,
        "output_path": str(output),
        "entries_written": entries_written,
        "last_turn_number": last_logged_turn,
    }


def main() -> int:
    """CLI エントリーポイント。"""
    args = parse_args()
    try:
        result = asyncio.run(
            run_monitor(
                story_id=args.story,
                db_path=args.db,
                config_path=args.config,
                env_path=args.env,
                output_path=args.output,
                interval_sec=args.interval_sec,
                max_entries=args.max_entries,
                engine_pid=args.engine_pid,
                log_path=args.log_path,
                window_turns=args.window_turns,
                stall_samples=args.stall_samples,
                reply_ratio_warn_below=args.reply_ratio_warn_below,
                all_chars_spoke_warn_above=args.all_chars_spoke_warn_above,
                quality_retry_warn_above=args.quality_retry_warn_above,
            )
        )
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as exc:
        print(f"monitor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(json.dumps(result, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
