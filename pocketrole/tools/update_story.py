#!/usr/bin/env python3
"""tools/update_story.py — 進行中ストーリーの定義更新 CLI。

Usage:
    python -m tools.update_story stories/ankoku_gakuen
    python -m tools.update_story stories/ankoku_gakuen --db db/pocketrole.db
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from story_map_scaffold import ensure_story_map_scaffold
from tools.import_story import (
    EXIT_ERROR,
    EXIT_FILE_NOT_FOUND,
    EXIT_OK,
    EXIT_YAML_PARSE_ERROR,
    _build_db,
    _map_anomaly,
    _map_character,
    _map_event,
    _map_place,
    _map_schedule,
    _map_story,
)
from tools.validate_story import StoryValidationError, validate_story

logger = logging.getLogger(__name__)


def _load_story_data(story_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    story_path = Path(story_dir)
    errors = validate_story(story_path)
    if errors:
        raise StoryValidationError(errors)
    world_data = yaml.safe_load(
        (story_path / "world_config.yaml").read_text(encoding="utf-8")
    )
    char_data = yaml.safe_load(
        (story_path / "characters.yaml").read_text(encoding="utf-8")
    )
    return world_data, char_data


def _turn_number_for(sim_datetime: str, season_start: str, turn_minutes: int) -> int:
    current_dt = datetime.fromisoformat(sim_datetime)
    start_dt = datetime.fromisoformat(f"{season_start}T00:00")
    elapsed_minutes = int((current_dt - start_dt).total_seconds() // 60)
    return elapsed_minutes // turn_minutes


def _default_sim_datetime(existing_story: dict[str, Any], season_start: str) -> str:
    return existing_story.get("last_sim_time") or f"{season_start}T00:00:00"


def _pick_active_place(
    favorite_places: list[str],
    active_places: list[dict[str, Any]],
) -> str:
    active_ids = {place["id"] for place in active_places}
    for place_id in favorite_places:
        if place_id in active_ids:
            return place_id

    home_places = sorted(place["id"] for place in active_places if place["zone"] == "home")
    if home_places:
        return home_places[0]

    active_sorted = sorted(place["id"] for place in active_places)
    if not active_sorted:
        raise ValueError("update_story requires at least one active place")
    return active_sorted[0]


async def _story_has_progress(conn, sid: str, story_row: dict[str, Any]) -> bool:
    if story_row.get("last_sim_time"):
        return True
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM chat_logs WHERE story_id = ?",
        (sid,),
    )
    row = await cursor.fetchone()
    return bool(row and row[0] > 0)


def _assert_safe_story_update(
    existing_story: dict[str, Any],
    new_story: dict[str, Any],
    has_progress: bool,
) -> None:
    if existing_story["id"] != new_story["id"]:
        raise ValueError("story.id cannot change during update_story")
    if not has_progress:
        return

    guarded_fields = ("season_start", "turn_minutes")
    for field in guarded_fields:
        if existing_story.get(field) != new_story.get(field):
            raise ValueError(
                f"{field} cannot change after progress exists; use import_story --force-replace"
            )


async def _replace_story_scoped_rows(
    conn,
    sid: str,
    schedules: list[dict[str, Any]],
    events: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
) -> dict[str, int]:
    await conn.execute("DELETE FROM time_schedules WHERE story_id = ?", (sid,))
    for schedule in schedules:
        await conn.execute(
            """
            INSERT INTO time_schedules
                (story_id, day_type, time_from, time_to, label,
                 expected_places, mood_modifier, anomaly_note)
            VALUES
                (:story_id, :day_type, :time_from, :time_to, :label,
                 :expected_places, :mood_modifier, :anomaly_note)
            """,
            _map_schedule(schedule, sid),
        )

    await conn.execute("DELETE FROM event_calendar WHERE story_id = ?", (sid,))
    for event in events:
        await conn.execute(
            """
            INSERT INTO event_calendar
                (story_id, event_date, name, duration_days,
                 atmosphere, emotion_impact, force_place)
            VALUES
                (:story_id, :event_date, :name, :duration_days,
                 :atmosphere, :emotion_impact, :force_place)
            """,
            _map_event(event, sid),
        )

    await conn.execute("DELETE FROM anomaly_rules WHERE story_id = ?", (sid,))
    for anomaly in anomalies:
        await conn.execute(
            """
            INSERT INTO anomaly_rules
                (story_id, label, condition_json, drama_potential, suggested_reasons)
            VALUES
                (:story_id, :label, :condition_json, :drama_potential, :suggested_reasons)
            """,
            _map_anomaly(anomaly, sid),
        )

    return {
        "time_schedules": len(schedules),
        "event_calendar": len(events),
        "anomaly_rules": len(anomalies),
    }


async def _upsert_places(
    conn,
    sid: str,
    places: list[dict[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    cursor = await conn.execute(
        "SELECT id, is_active FROM places WHERE story_id = ?",
        (sid,),
    )
    existing_rows = {
        row["id"]: dict(row)
        for row in await cursor.fetchall()
    }

    counts = {"places_added": 0, "places_updated": 0, "places_deactivated": 0}
    for place in places:
        mapped = _map_place(place, sid)
        if place["id"] in existing_rows:
            await conn.execute(
                """
                UPDATE places
                SET label = :label,
                    zone = :zone,
                    atmosphere = :atmosphere,
                    who_gathers = :who_gathers,
                    events_likely = :events_likely,
                    access_note = :access_note,
                    adjacent_places = :adjacent_places,
                    is_active = 1
                WHERE story_id = :story_id AND id = :id
                """,
                mapped,
            )
            counts["places_updated"] += 1
        else:
            await conn.execute(
                """
                INSERT INTO places
                    (id, story_id, label, zone, atmosphere,
                     who_gathers, events_likely, access_note, adjacent_places,
                     is_active)
                VALUES
                    (:id, :story_id, :label, :zone, :atmosphere,
                     :who_gathers, :events_likely, :access_note, :adjacent_places,
                     :is_active)
                """,
                mapped,
            )
            counts["places_added"] += 1

    yaml_place_ids = {place["id"] for place in places}
    for place_id, existing in existing_rows.items():
        if place_id not in yaml_place_ids:
            if existing["is_active"] != 0:
                counts["places_deactivated"] += 1
            await conn.execute(
                "UPDATE places SET is_active = 0 WHERE story_id = ? AND id = ?",
                (sid, place_id),
            )

    return counts, places


async def _upsert_characters(
    conn,
    sid: str,
    characters: list[dict[str, Any]],
) -> tuple[dict[str, int], list[str]]:
    cursor = await conn.execute(
        "SELECT id, is_active FROM characters WHERE story_id = ?",
        (sid,),
    )
    existing_rows = {
        row["id"]: dict(row)
        for row in await cursor.fetchall()
    }

    counts = {
        "characters_added": 0,
        "characters_updated": 0,
        "characters_deactivated": 0,
    }
    new_char_ids: list[str] = []

    for char in characters:
        mapped = _map_character(char, sid)
        mapped["is_active"] = 1
        if char["id"] in existing_rows:
            await conn.execute(
                """
                UPDATE characters
                SET name_ja = :name_ja,
                    name_read = :name_read,
                    appearance = :appearance,
                    personality_core = :personality_core,
                    speech = :speech,
                    strengths = :strengths,
                    weaknesses = :weaknesses,
                    current_goal = :current_goal,
                    current_worry = :current_worry,
                    secret = :secret,
                    secret_reveal_condition = :secret_reveal_condition,
                    emotion_default = :emotion_default,
                    favorite_places = :favorite_places,
                    move_tendency = :move_tendency,
                    expressions_available = :expressions_available,
                    image_path = :image_path,
                    is_active = :is_active,
                    updated_at = datetime('now')
                WHERE story_id = :story_id AND id = :id
                """,
                mapped,
            )
            counts["characters_updated"] += 1
        else:
            await conn.execute(
                """
                INSERT INTO characters
                    (id, story_id, name_ja, name_read, appearance,
                     personality_core, speech, strengths, weaknesses,
                     current_goal, current_worry, secret, secret_reveal_condition,
                     emotion_default, favorite_places, move_tendency,
                     expressions_available, image_path, is_active)
                VALUES
                    (:id, :story_id, :name_ja, :name_read, :appearance,
                     :personality_core, :speech, :strengths, :weaknesses,
                     :current_goal, :current_worry, :secret, :secret_reveal_condition,
                     :emotion_default, :favorite_places, :move_tendency,
                     :expressions_available, :image_path, :is_active)
                """,
                mapped,
            )
            counts["characters_added"] += 1
            new_char_ids.append(char["id"])

    yaml_char_ids = {char["id"] for char in characters}
    for char_id, existing in existing_rows.items():
        if char_id not in yaml_char_ids:
            if existing["is_active"] != 0:
                counts["characters_deactivated"] += 1
            await conn.execute(
                """
                UPDATE characters
                SET is_active = 0, updated_at = datetime('now')
                WHERE story_id = ? AND id = ?
                """,
                (sid, char_id),
            )

    return counts, new_char_ids


async def _insert_initial_state(
    conn,
    sid: str,
    char: dict[str, Any],
    sim_datetime: str,
    turn_number: int,
    active_places: list[dict[str, Any]],
) -> None:
    emotion = char.get("emotion_default") or {}
    current_place = _pick_active_place(char.get("favorite_places") or [], active_places)
    await conn.execute(
        """
        INSERT INTO character_states
            (char_id, story_id, sim_datetime, turn_number, current_place,
             stress, motivation, loneliness, excitement)
        VALUES
            (:char_id, :story_id, :sim_datetime, :turn_number, :current_place,
             :stress, :motivation, :loneliness, :excitement)
        """,
        {
            "char_id": char["id"],
            "story_id": sid,
            "sim_datetime": sim_datetime,
            "turn_number": turn_number,
            "current_place": current_place,
            "stress": float(emotion.get("stress", 0.3)),
            "motivation": float(emotion.get("motivation", 0.7)),
            "loneliness": float(emotion.get("loneliness", 0.2)),
            "excitement": float(emotion.get("excitement", 0.5)),
        },
    )


async def _ensure_relationships(
    conn,
    sid: str,
    active_char_ids: list[str],
) -> int:
    added = 0
    for from_id, to_id in itertools.permutations(active_char_ids, 2):
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO relationships (story_id, char_id_from, char_id_to, trust)
            VALUES (?, ?, ?, 0.5)
            """,
            (sid, from_id, to_id),
        )
        added += cursor.rowcount or 0
    return added


async def _latest_state(conn, sid: str, char_id: str):
    cursor = await conn.execute(
        """
        SELECT *
        FROM character_states
        WHERE story_id = ? AND char_id = ?
        ORDER BY recorded_at DESC, id DESC
        LIMIT 1
        """,
        (sid, char_id),
    )
    return await cursor.fetchone()


async def _relocate_characters_on_inactive_places(
    conn,
    sid: str,
    active_characters: list[dict[str, Any]],
    active_places: list[dict[str, Any]],
    sim_datetime: str,
    turn_number: int,
) -> int:
    active_place_ids = {place["id"] for place in active_places}
    relocated = 0
    for char in active_characters:
        latest = await _latest_state(conn, sid, char["id"])
        if latest is None:
            await _insert_initial_state(conn, sid, char, sim_datetime, turn_number, active_places)
            continue

        current_place = latest["current_place"]
        if current_place in active_place_ids:
            continue

        destination = _pick_active_place(char.get("favorite_places") or [], active_places)
        await conn.execute(
            """
            INSERT INTO character_states
                (char_id, story_id, sim_datetime, turn_number,
                 current_place, previous_place, move_reason,
                 current_action, current_expression,
                 stress, motivation, loneliness, excitement)
            VALUES
                (:char_id, :story_id, :sim_datetime, :turn_number,
                 :current_place, :previous_place, :move_reason,
                 :current_action, :current_expression,
                 :stress, :motivation, :loneliness, :excitement)
            """,
            {
                "char_id": char["id"],
                "story_id": sid,
                "sim_datetime": sim_datetime,
                "turn_number": turn_number,
                "current_place": destination,
                "previous_place": current_place,
                "move_reason": "update_story relocated from inactive place",
                "current_action": latest["current_action"],
                "current_expression": latest["current_expression"],
                "stress": latest["stress"],
                "motivation": latest["motivation"],
                "loneliness": latest["loneliness"],
                "excitement": latest["excitement"],
            },
        )
        relocated += 1
    return relocated


async def update_story(
    story_dir: str | Path,
    db_path: str | Path = "db/pocketrole.db",
    story_maps_root: str | Path | None = None,
) -> dict[str, int]:
    world_data, char_data = _load_story_data(story_dir)

    story = world_data["story"]
    sid = story["id"]
    places = world_data.get("places") or []
    schedules = world_data.get("time_schedules") or []
    events = world_data.get("event_calendar") or []
    anomalies = world_data.get("anomaly_rules") or []
    characters = char_data.get("characters") or []
    char_map = {char["id"]: char for char in characters}

    conn = await _build_db(db_path)
    counts: dict[str, int] = {"stories": 1}

    try:
        cursor = await conn.execute(
            "SELECT * FROM stories WHERE id = ?",
            (sid,),
        )
        existing_story_row = await cursor.fetchone()
        if existing_story_row is None:
            raise RuntimeError(
                f"story_id '{sid}' is not imported yet. "
                "Run python -m tools.import_story ... first."
            )

        existing_story = dict(existing_story_row)
        has_progress = await _story_has_progress(conn, sid, existing_story)
        _assert_safe_story_update(existing_story, story, has_progress)

        current_sim_datetime = _default_sim_datetime(
            existing_story, story.get("season_start", existing_story["season_start"])
        )
        turn_basis_season_start = (
            existing_story["season_start"] if has_progress else story["season_start"]
        )
        turn_basis_minutes = (
            existing_story["turn_minutes"] if has_progress else story["turn_minutes"]
        )
        current_turn_number = _turn_number_for(
            current_sim_datetime,
            turn_basis_season_start,
            turn_basis_minutes,
        )

        await conn.execute("BEGIN")

        mapped_story = _map_story(story)
        await conn.execute(
            """
            UPDATE stories
            SET title = :title,
                description = :description,
                world_rules = :world_rules,
                season_start = :season_start,
                turn_minutes = :turn_minutes,
                turn_interval_sec = :turn_interval_sec,
                llm_provider = :llm_provider,
                llm_model = :llm_model,
                updated_at = datetime('now')
            WHERE id = :id
            """,
            mapped_story,
        )

        place_counts, active_places = await _upsert_places(conn, sid, places)
        counts.update(place_counts)

        counts.update(
            await _replace_story_scoped_rows(conn, sid, schedules, events, anomalies)
        )

        char_counts, new_char_ids = await _upsert_characters(conn, sid, characters)
        counts.update(char_counts)

        for char_id in new_char_ids:
            await _insert_initial_state(
                conn,
                sid,
                char_map[char_id],
                current_sim_datetime,
                current_turn_number,
                active_places,
            )
        counts["character_states_added"] = len(new_char_ids)

        counts["relationships_added"] = await _ensure_relationships(
            conn,
            sid,
            [char["id"] for char in characters],
        )

        counts["characters_relocated"] = await _relocate_characters_on_inactive_places(
            conn,
            sid,
            characters,
            active_places,
            current_sim_datetime,
            current_turn_number,
        )

        await conn.commit()
        ensure_story_map_scaffold(sid, places, story_maps_root)
        logger.info(
            "ストーリー更新完了: story_id=%s, counts=%s",
            sid,
            counts,
        )
        return counts
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="進行中ストーリーの YAML 定義を DB に反映する"
    )
    parser.add_argument("story_dir", help="ストーリーディレクトリパス")
    parser.add_argument(
        "--db",
        default="db/pocketrole.db",
        help="SQLite DB ファイルパス（デフォルト: db/pocketrole.db）",
    )
    parser.add_argument(
        "--story-maps-root",
        default=None,
        help="story_maps scaffold 出力先（既定: web/assets/story_maps）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        counts = asyncio.run(update_story(args.story_dir, args.db, args.story_maps_root))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FILE_NOT_FOUND
    except StoryValidationError as exc:
        for err in exc.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return EXIT_YAML_PARSE_ERROR
    except Exception as exc:
        logger.exception("ストーリー更新エラー")
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"OK: {Path(args.story_dir).name} updated — {counts}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
