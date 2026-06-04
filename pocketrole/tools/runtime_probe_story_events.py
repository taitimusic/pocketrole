#!/usr/bin/env python3
"""tools/runtime_probe_story_events.py — post-round artifact を観測する deterministic probe."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from db.db_manager import DatabaseManager
from engine.config import load_config
from engine.llm.router import LLMRouter
from engine.llm_runtime import (
    ensure_story_llm_runtime_requirements,
    required_story_llm_providers,
)
from engine.story_engine import StoryEngine

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
ProbeGoal = Literal["round_end", "scene_close", "growth"]
_SCENE_CLOSE_STAGE_RANK = {
    "no_close": 0,
    "closed_only": 1,
    "arc_created": 2,
    "complete": 3,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """コマンドライン引数を解析する。"""
    parser = argparse.ArgumentParser(
        description="指定 story を /tmp の DB コピーで round 単位に probe 実行する"
    )
    parser.add_argument("--story", required=True, help="対象ストーリーID")
    parser.add_argument(
        "--db",
        default="db/pocketrole.db",
        help="SQLite DB ファイルパス（デフォルト: db/pocketrole.db）",
    )
    parser.add_argument(
        "--goal",
        required=True,
        choices=("round_end", "scene_close", "growth"),
        help="停止条件",
    )
    parser.add_argument(
        "--max-character-turns",
        type=int,
        required=True,
        dest="max_character_turns",
        help="最大キャラターン数",
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
        "--llm-runtime",
        default="config/llm_runtime.local.yaml",
        dest="llm_runtime",
        help="runtime LLM 設定ファイル（デフォルト: config/llm_runtime.local.yaml）",
    )
    return parser.parse_args(argv)


def _copy_db_to_tmp(db_path: str | Path, story_id: str) -> Path:
    """作業用 DB を /tmp に複製して返す。"""
    source = Path(db_path)
    suffix = f"{story_id}-{uuid.uuid4().hex}.db"
    work_db = Path(tempfile.gettempdir()) / f"pocketrole-probe-{suffix}"
    shutil.copy2(source, work_db)
    return work_db


async def _collect_probe_counts(db: DatabaseManager, story_id: str) -> dict[str, Any]:
    """probe 用のカウンタ群を返す。"""
    assert db._conn is not None

    count_queries = {
        "scene_arc_count": "SELECT COUNT(*) FROM story_arc WHERE story_id = ?",
        "episode_arc_count": "SELECT COUNT(*) FROM story_arc WHERE story_id = ? AND arc_type = 'episode'",
        "novel_output_count": "SELECT COUNT(*) FROM novel_output WHERE story_id = ?",
        "growth_candidate_count": "SELECT COUNT(*) FROM character_growth_candidates WHERE story_id = ?",
        "growth_commit_count": "SELECT COUNT(*) FROM character_evolution WHERE story_id = ?",
        "active_pattern_count": "SELECT COUNT(*) FROM story_interaction_patterns WHERE story_id = ? AND status = 'active'",
        "episode_count": "SELECT COUNT(*) FROM story_episodes WHERE story_id = ?",
        "active_relationship_mode_count": "SELECT COUNT(*) FROM relationship_modes WHERE story_id = ? AND status = 'active'",
        "active_canon_bit_count": "SELECT COUNT(*) FROM story_canon_bits WHERE story_id = ? AND status = 'active'",
        "canon_reignition_total": "SELECT COALESCE(SUM(reignition_count), 0) FROM story_canon_bits WHERE story_id = ?",
        "canon_writeback_total": "SELECT COALESCE(SUM(writeback_count), 0) FROM story_canon_bits WHERE story_id = ?",
        "canon_triggered_hook_count": "SELECT COUNT(*) FROM story_hooks WHERE story_id = ? AND status = 'open' AND source_canon_bit_id IS NOT NULL",
        "active_canon_profile_overlay_count": "SELECT COUNT(*) FROM character_canon_overlays WHERE story_id = ?",
        "active_dramatic_pressure_count": "SELECT COUNT(*) FROM story_dramatic_pressures WHERE story_id = ? AND status = 'active'",
    }
    counts: dict[str, Any] = {}
    for key, query in count_queries.items():
        cursor = await db._conn.execute(query, (story_id,))
        row = await cursor.fetchone()
        counts[key] = int(row[0]) if row is not None else 0

    last_cursor = await db._conn.execute(
        """
        SELECT MAX(turn_number)
        FROM chat_logs
        WHERE story_id = ?
        """,
        (story_id,),
    )
    last_row = await last_cursor.fetchone()
    counts["last_turn_number"] = int(last_row[0]) if last_row and last_row[0] is not None else None

    closed_scene_cursor = await db._conn.execute(
        """
        SELECT id
        FROM story_scenes
        WHERE story_id = ? AND scene_type = 'conversation' AND status = 'closed'
        ORDER BY id
        """,
        (story_id,),
    )
    counts["closed_scene_ids"] = [int(row[0]) for row in await closed_scene_cursor.fetchall()]
    scene_arc_scene_cursor = await db._conn.execute(
        """
        SELECT DISTINCT source_scene_id
        FROM story_arc
        WHERE story_id = ? AND source_scene_id IS NOT NULL
        ORDER BY source_scene_id
        """,
        (story_id,),
    )
    counts["scene_arc_scene_ids"] = [
        int(row[0]) for row in await scene_arc_scene_cursor.fetchall() if row[0] is not None
    ]
    novel_output_scene_cursor = await db._conn.execute(
        """
        SELECT DISTINCT a.source_scene_id
        FROM novel_output n
        JOIN story_arc a ON a.id = n.arc_id
        WHERE n.story_id = ? AND a.source_scene_id IS NOT NULL
        ORDER BY a.source_scene_id
        """,
        (story_id,),
    )
    counts["novel_output_scene_ids"] = [
        int(row[0]) for row in await novel_output_scene_cursor.fetchall() if row[0] is not None
    ]
    pattern_cursor = await db._conn.execute(
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
    counts["dominant_pattern_types"] = [str(row[0]) for row in await pattern_cursor.fetchall()]
    relationship_mode_cursor = await db._conn.execute(
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
    counts["dominant_relationship_modes"] = [
        str(row[0]) for row in await relationship_mode_cursor.fetchall()
    ]
    canon_cursor = await db._conn.execute(
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
    counts["dominant_canon_motifs"] = [str(row[0]) for row in await canon_cursor.fetchall()]
    pressure_cursor = await db._conn.execute(
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
    counts["dominant_pressure_types"] = [str(row[0]) for row in await pressure_cursor.fetchall()]
    max_pressure_score_cursor = await db._conn.execute(
        """
        SELECT MAX(score)
        FROM story_dramatic_pressures
        WHERE story_id = ? AND status = 'active'
        """,
        (story_id,),
    )
    max_pressure_score_row = await max_pressure_score_cursor.fetchone()
    counts["max_pressure_score"] = (
        round(float(max_pressure_score_row[0]), 2)
        if max_pressure_score_row is not None and max_pressure_score_row[0] is not None
        else 0.0
    )
    active_episode = await db.get_active_story_episode(story_id)
    counts["active_episode_id"] = (
        int(active_episode["id"])
        if active_episode is not None and active_episode.get("id") is not None
        else None
    )
    counts["active_episode_type"] = (
        str(active_episode.get("episode_type") or "") or None
        if active_episode is not None
        else None
    )
    counts["active_episode_goal"] = (
        str(active_episode.get("goal") or "") or None
        if active_episode is not None
        else None
    )
    return counts


def _resolve_scene_close_goal_state(
    start_counts: dict[str, Any],
    end_counts: dict[str, Any],
    new_closed_scene_ids: list[int],
) -> tuple[str, list[int], list[int], bool]:
    new_closed_ids = [int(scene_id) for scene_id in new_closed_scene_ids]
    if not new_closed_ids:
        return "no_close", [], [], False

    end_arc_ids = {int(scene_id) for scene_id in list(end_counts.get("scene_arc_scene_ids") or [])}
    end_output_ids = {int(scene_id) for scene_id in list(end_counts.get("novel_output_scene_ids") or [])}
    if not end_arc_ids and int(end_counts.get("scene_arc_count", 0)) > int(start_counts.get("scene_arc_count", 0)):
        end_arc_ids = set(new_closed_ids)
    if not end_output_ids and int(end_counts.get("novel_output_count", 0)) > int(start_counts.get("novel_output_count", 0)):
        end_output_ids = set(new_closed_ids)

    artifact_ready_scene_ids = [scene_id for scene_id in new_closed_ids if scene_id in end_output_ids]
    artifact_missing_scene_ids = [scene_id for scene_id in new_closed_ids if scene_id not in end_output_ids]
    arc_created_scene_ids = [scene_id for scene_id in new_closed_ids if scene_id in end_arc_ids]

    if len(artifact_ready_scene_ids) == len(new_closed_ids):
        return "complete", artifact_ready_scene_ids, [], True
    if arc_created_scene_ids:
        return "arc_created", artifact_ready_scene_ids, artifact_missing_scene_ids, False
    return "closed_only", artifact_ready_scene_ids, artifact_missing_scene_ids, False


def _resolve_scene_close_status_map(
    base_counts: dict[str, Any],
    current_counts: dict[str, Any],
    scene_ids: list[int],
) -> dict[int, str]:
    tracked_scene_ids = [int(scene_id) for scene_id in scene_ids]
    if not tracked_scene_ids:
        return {}
    arc_ids = {
        int(scene_id)
        for scene_id in list(current_counts.get("scene_arc_scene_ids") or [])
    }
    output_ids = {
        int(scene_id)
        for scene_id in list(current_counts.get("novel_output_scene_ids") or [])
    }
    if (
        not arc_ids
        and int(current_counts.get("scene_arc_count", 0)) > int(base_counts.get("scene_arc_count", 0))
    ):
        arc_ids = set(tracked_scene_ids)
    if (
        not output_ids
        and int(current_counts.get("novel_output_count", 0)) > int(base_counts.get("novel_output_count", 0))
    ):
        output_ids = set(tracked_scene_ids)

    status_map: dict[int, str] = {}
    for scene_id in tracked_scene_ids:
        if scene_id in output_ids:
            status_map[scene_id] = "complete"
        elif scene_id in arc_ids:
            status_map[scene_id] = "arc_created"
        else:
            status_map[scene_id] = "closed_only"
    return status_map


def _extract_evidence_bearing_char_ids(growth_results: dict[str, dict[str, Any]]) -> list[str]:
    """growth 観測結果から evidence-bearing char を抽出する。"""
    return [
        char_id
        for char_id, summary in growth_results.items()
        if int(summary.get("evidence_count", 0)) > 0
    ]


async def run_probe(
    story_id: str,
    db_path: str | Path = "db/pocketrole.db",
    *,
    goal: ProbeGoal,
    max_character_turns: int,
    config_path: str = "config.yaml",
    env_path: str = ".env",
    llm_runtime_path: str = "config/llm_runtime.local.yaml",
) -> dict[str, Any]:
    """指定 story を /tmp コピー DB で round 単位に probe 実行し、要約を返す。"""
    if max_character_turns < 1:
        raise ValueError("max_character_turns must be >= 1")

    cfg = load_config(config_path, env_path, llm_runtime_path=llm_runtime_path)
    work_db = _copy_db_to_tmp(db_path, story_id)

    async with DatabaseManager(work_db, _MIGRATIONS_DIR) as db:
        story = await db.get_story(story_id)
        if story is None:
            raise ValueError(f"Story not found: {story_id}")
        resolved_llm_configs = ensure_story_llm_runtime_requirements(cfg, [story_id])
        required_providers = required_story_llm_providers(cfg, [story_id])
        start_counts = await _collect_probe_counts(db, story_id)

        router = LLMRouter(cfg.llm, required_providers=required_providers)
        await router.start()
        try:
            engine = StoryEngine(
                story_id,
                db,
                router,
                config=cfg,
                resolved_llm_config=resolved_llm_configs.get(story_id),
            )
            await engine.initialize()

            rounds_advanced = 0
            character_turns_advanced = 0
            goal_reached = False
            new_closed_scene_ids: list[int] = []
            evidence_bearing_char_ids: list[str] = []
            start_closed_scene_ids = [
                int(scene_id) for scene_id in list(start_counts.get("closed_scene_ids") or [])
            ]
            scene_close_stage_by_id: dict[int, str] = _resolve_scene_close_status_map(
                start_counts,
                start_counts,
                start_closed_scene_ids,
            )
            pending_scene_close_ids: set[int] = {
                scene_id
                for scene_id, stage in scene_close_stage_by_id.items()
                if stage != "complete"
            }
            backlog_recovered_scene_ids: set[int] = set()
            closed_only_count = 0
            arc_created_count = 0
            complete_count = 0
            current_counts = start_counts

            while character_turns_advanced < max_character_turns and not goal_reached:
                round_summary = await engine._run_single_round()
                rounds_advanced += 1
                character_turns_advanced += int(round_summary["planned_turns"])

                growth_results = dict(round_summary.get("growth_results") or {})
                evidence_bearing_char_ids = _extract_evidence_bearing_char_ids(growth_results)
                processed_closed_scene_ids = [
                    int(scene_id)
                    for scene_id in list(round_summary.get("processed_closed_scene_ids") or [])
                ]
                if processed_closed_scene_ids:
                    new_closed_scene_ids = processed_closed_scene_ids
                    pending_scene_close_ids.update(processed_closed_scene_ids)

                if goal == "scene_close" and (processed_closed_scene_ids or pending_scene_close_ids):
                    current_counts = await _collect_probe_counts(db, story_id)
                    tracked_scene_ids = sorted(
                        set(new_closed_scene_ids) | set(pending_scene_close_ids)
                    )
                    status_map = _resolve_scene_close_status_map(
                        start_counts,
                        current_counts,
                        tracked_scene_ids,
                    )
                    for scene_id in tracked_scene_ids:
                        previous_stage = scene_close_stage_by_id.get(scene_id, "no_close")
                        current_stage = status_map.get(scene_id, "no_close")
                        if current_stage == previous_stage:
                            continue
                        if current_stage == "closed_only":
                            closed_only_count += 1
                        elif current_stage == "arc_created":
                            arc_created_count += 1
                        elif current_stage == "complete":
                            complete_count += 1
                            if previous_stage != "no_close" and scene_id not in processed_closed_scene_ids:
                                backlog_recovered_scene_ids.add(scene_id)
                            pending_scene_close_ids.discard(scene_id)
                        scene_close_stage_by_id[scene_id] = current_stage

                if goal == "round_end":
                    goal_reached = True
                elif goal == "scene_close" and bool(new_closed_scene_ids):
                    goal_reached = all(
                        scene_close_stage_by_id.get(scene_id) == "complete"
                        for scene_id in new_closed_scene_ids
                    )
                elif goal == "growth" and bool(evidence_bearing_char_ids):
                    goal_reached = True

            end_counts = (
                current_counts
                if goal == "scene_close" and current_counts is not start_counts
                else await _collect_probe_counts(db, story_id)
            )
        finally:
            await router.stop()

    if not new_closed_scene_ids:
        start_closed = set(int(scene_id) for scene_id in start_counts["closed_scene_ids"])
        end_closed = [int(scene_id) for scene_id in end_counts["closed_scene_ids"]]
        new_closed_scene_ids = [scene_id for scene_id in end_closed if scene_id not in start_closed]

    scene_close_goal_stage = "no_close"
    artifact_ready_scene_ids: list[int] = []
    artifact_missing_scene_ids: list[int] = []
    if goal == "scene_close":
        (
            scene_close_goal_stage,
            artifact_ready_scene_ids,
            artifact_missing_scene_ids,
            goal_reached,
        ) = _resolve_scene_close_goal_state(start_counts, end_counts, new_closed_scene_ids)
    else:
        backlog_recovered_scene_ids = set()
        closed_only_count = 0
        arc_created_count = 0
        complete_count = 0

    return {
        "story_id": story_id,
        "goal": goal,
        "goal_reached": goal_reached,
        "max_character_turns": max_character_turns,
        "character_turns_advanced": character_turns_advanced,
        "start_turn_number": start_counts["last_turn_number"],
        "end_turn_number": end_counts["last_turn_number"],
        "rounds_advanced": rounds_advanced,
        "new_closed_scene_ids": new_closed_scene_ids,
        "scene_close_goal_stage": scene_close_goal_stage,
        "artifact_ready_scene_ids": artifact_ready_scene_ids,
        "artifact_missing_scene_ids": artifact_missing_scene_ids,
        "backlog_recovered_scene_ids": sorted(backlog_recovered_scene_ids),
        "closed_only_count": closed_only_count,
        "arc_created_count": arc_created_count,
        "complete_count": complete_count,
        "scene_arc_delta": end_counts["scene_arc_count"] - start_counts["scene_arc_count"],
        "novel_output_delta": end_counts["novel_output_count"] - start_counts["novel_output_count"],
        "growth_candidate_delta": (
            end_counts["growth_candidate_count"] - start_counts["growth_candidate_count"]
        ),
        "growth_commit_delta": (
            end_counts["growth_commit_count"] - start_counts["growth_commit_count"]
        ),
        "pattern_delta": int(end_counts.get("active_pattern_count", 0)) - int(start_counts.get("active_pattern_count", 0)),
        "active_pattern_count": int(end_counts.get("active_pattern_count", 0)),
        "dominant_pattern_types": list(end_counts.get("dominant_pattern_types", [])),
        "relationship_mode_delta": (
            int(end_counts.get("active_relationship_mode_count", 0))
            - int(start_counts.get("active_relationship_mode_count", 0))
        ),
        "active_relationship_mode_count": int(end_counts.get("active_relationship_mode_count", 0)),
        "dominant_relationship_modes": list(end_counts.get("dominant_relationship_modes", [])),
        "canon_delta": (
            int(end_counts.get("active_canon_bit_count", 0))
            - int(start_counts.get("active_canon_bit_count", 0))
        ),
        "active_canon_bit_count": int(end_counts.get("active_canon_bit_count", 0)),
        "canon_reignitions_delta": (
            int(end_counts.get("canon_reignition_total", 0))
            - int(start_counts.get("canon_reignition_total", 0))
        ),
        "canon_writeback_delta": (
            int(end_counts.get("canon_writeback_total", 0))
            - int(start_counts.get("canon_writeback_total", 0))
        ),
        "canon_triggered_hook_count": int(end_counts.get("canon_triggered_hook_count", 0)),
        "active_canon_profile_overlay_count": int(end_counts.get("active_canon_profile_overlay_count", 0)),
        "dominant_canon_motifs": list(end_counts.get("dominant_canon_motifs", [])),
        "pressure_delta": (
            int(end_counts.get("active_dramatic_pressure_count", 0))
            - int(start_counts.get("active_dramatic_pressure_count", 0))
        ),
        "active_dramatic_pressure_count": int(end_counts.get("active_dramatic_pressure_count", 0)),
        "dominant_pressure_types": list(end_counts.get("dominant_pressure_types", [])),
        "max_pressure_score": float(end_counts.get("max_pressure_score", 0.0)),
        "episode_delta": int(end_counts.get("episode_count", 0)) - int(start_counts.get("episode_count", 0)),
        "active_episode_id": end_counts.get("active_episode_id"),
        "active_episode_type": end_counts.get("active_episode_type"),
        "active_episode_goal": end_counts.get("active_episode_goal"),
        "evidence_bearing_char_ids": evidence_bearing_char_ids,
        "work_db_path": str(work_db),
    }


def main() -> int:
    """CLI エントリーポイント。"""
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        summary = asyncio.run(
            run_probe(
                args.story,
                args.db,
                goal=args.goal,
                max_character_turns=args.max_character_turns,
                config_path=args.config,
                env_path=args.env,
                llm_runtime_path=args.llm_runtime,
            )
        )
    except Exception:
        logger.exception("runtime probe failed")
        return EXIT_ERROR

    print(json.dumps(summary, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
