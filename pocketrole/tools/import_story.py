#!/usr/bin/env python3
"""tools/import_story.py — ストーリーYAML → DB インポートCLI

Usage:
    python -m tools.import_story stories/ankoku_gakuen
    python -m tools.import_story stories/ankoku_gakuen --force-replace
    python -m tools.import_story stories/ankoku_gakuen --db db/pocketrole.db

終了コード:
    0 — インポート成功
    1 — バリデーションエラー・DB エラー
    2 — ファイルが見つからない
    3 — YAML 解析エラー
"""

import argparse
import asyncio
import itertools
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from db.db_manager import DatabaseManager
from db.backends import SupportsAsyncConnection
from story_map_scaffold import ensure_story_map_scaffold
from tools.validate_story import StoryValidationError, validate_story

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_YAML_PARSE_ERROR = 3

_MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


# ──────────────────────────────────────────────────────────────────────────────
# DB 初期化
# ──────────────────────────────────────────────────────────────────────────────

async def _build_db(db_path: str | Path) -> SupportsAsyncConnection:
    """DBファイルにスキーマを適用し、aiosqlite 接続を返す。

    DatabaseManager で migrations を適用後、接続を閉じ、
    新たな aiosqlite.Connection を開いて返す。
    """
    async with DatabaseManager(db_path, _MIGRATIONS_DIR):
        pass  # migrations 適用のみ（接続はここで閉じる）

    db = DatabaseManager(db_path, _MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    return db._conn


# ──────────────────────────────────────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────────────────────────────────────

def _to_json(val: Any) -> str | None:
    """dict / list / str などを JSON 文字列に変換する。None はそのまま None。"""
    if val is None:
        return None
    return json.dumps(val, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────────────
# YAML → DB マッピング
# ──────────────────────────────────────────────────────────────────────────────

def _map_story(story: dict) -> dict:
    """world_config.story セクション → stories テーブル dict"""
    return {
        "id": story["id"],
        "title": story["title"],
        "description": story.get("description"),
        "world_rules": _to_json(story.get("world_rules")),
        "season_start": story.get("season_start"),
        "turn_minutes": story.get("turn_minutes", 30),
        "turn_interval_sec": story.get("turn_interval_sec", 30),
        "story_mode": story.get("story_mode", "drama"),
        "llm_provider": "",
        "llm_model": "",
    }


def _map_place(place: dict, sid: str) -> dict:
    """places[] エントリ → places テーブル dict"""
    return {
        "id": place["id"],
        "story_id": sid,
        "label": place["label"],
        "zone": place["zone"],
        "atmosphere": place.get("atmosphere"),
        "who_gathers": _to_json(place.get("who_gathers")),
        "events_likely": _to_json(place.get("events_likely")),
        "access_note": place.get("access_note"),
        "adjacent_places": _to_json(place.get("adjacent_places")),
        "is_active": 1,
    }


def _map_schedule(s: dict, sid: str) -> dict:
    """time_schedules[] エントリ → time_schedules テーブル dict"""
    return {
        "story_id": sid,
        "day_type": s["day_type"],
        "time_from": s["time_from"],
        "time_to": s["time_to"],
        "label": s["label"],
        "expected_places": _to_json(s.get("expected_places")),
        "mood_modifier": s.get("mood_modifier"),
        "anomaly_note": s.get("anomaly_note"),
    }


def _map_event(e: dict, sid: str) -> dict:
    """event_calendar[] エントリ → event_calendar テーブル dict"""
    return {
        "story_id": sid,
        "event_date": e["event_date"],
        "name": e["name"],
        "duration_days": e.get("duration_days", 1),
        "atmosphere": e.get("atmosphere"),
        "emotion_impact": _to_json(e.get("emotion_impact")),
        "force_place": e.get("force_place"),
    }


def _map_anomaly(r: dict, sid: str) -> dict:
    """anomaly_rules[] エントリ → anomaly_rules テーブル dict"""
    return {
        "story_id": sid,
        "label": r["label"],
        "condition_json": _to_json(r.get("condition_json") or {}),
        "drama_potential": r.get("drama_potential"),
        "suggested_reasons": _to_json(r.get("suggested_reasons")),
    }


def _map_character(c: dict, sid: str) -> dict:
    """characters[] エントリ → characters テーブル dict"""
    personality = c.get("personality") or {}
    speech = {
        "first_person": personality.get("first_person"),
        "speech_style": personality.get("speech_style"),
        "examples": personality.get("speech_examples") or [],
        "never_say": personality.get("never_say") or [],
    }
    secret = c.get("secret")
    return {
        "id": c["id"],
        "story_id": sid,
        "name_ja": c["name"],
        "name_read": c.get("name_reading"),
        "appearance": _to_json(c.get("appearance")),
        "personality_core": personality.get("type"),
        "speech": _to_json(speech),
        "strengths": _to_json(personality.get("strengths")),
        "weaknesses": _to_json(personality.get("weaknesses")),
        "current_goal": c.get("goal"),
        "current_worry": c.get("worry"),
        "secret": secret.get("content") if isinstance(secret, dict) else None,
        "secret_reveal_condition": (
            secret.get("unlock_condition") if isinstance(secret, dict) else None
        ),
        "emotion_default": _to_json(c.get("emotion_default")),
        "favorite_places": _to_json(c.get("favorite_places")),
        "move_tendency": _to_json(c.get("behavior_notes")),
        "expressions_available": _to_json(c.get("expressions")),
        "image_path": f"images/{c['id']}/neutral.png",
    }


def _map_char_state(c: dict, sid: str, t0: str) -> dict:
    """characters[] エントリ → character_states テーブルの初期行 dict"""
    fav = c.get("favorite_places") or []
    current_place = fav[0] if fav else "school_gate"
    emotion = c.get("emotion_default") or {}
    return {
        "char_id": c["id"],
        "story_id": sid,
        "sim_datetime": t0,
        "turn_number": 0,
        "current_place": current_place,
        "stress": float(emotion.get("stress", 0.3)),
        "motivation": float(emotion.get("motivation", 0.7)),
        "loneliness": float(emotion.get("loneliness", 0.2)),
        "excitement": float(emotion.get("excitement", 0.5)),
    }


# ──────────────────────────────────────────────────────────────────────────────
# メイン async 関数
# ──────────────────────────────────────────────────────────────────────────────

async def import_story(
    story_dir: str | Path,
    db_path: str | Path = "db/pocketrole.db",
    force_replace: bool = False,
    story_maps_root: str | Path | None = None,
) -> dict[str, int]:
    """ストーリーYAMLをDBにインポートする。

    Args:
        story_dir: world_config.yaml と characters.yaml を含むディレクトリ
        db_path: SQLite DB ファイルパス

    Returns:
        各テーブルへの挿入行数 dict
        例: {"stories": 1, "places": 11, "characters": 5, ...}

    Raises:
        FileNotFoundError: YAML ファイルが見つからない
        StoryValidationError: YAML 解析エラー または バリデーションエラー
        aiosqlite.Error: DB エラー
    """
    story_dir = Path(story_dir)

    # ── バリデーション ────────────────────────────────────────────────────────
    # FileNotFoundError / StoryValidationError（YAML解析エラー）を送出する場合あり
    errors = validate_story(story_dir)
    if errors:
        raise StoryValidationError(errors)

    # ── YAML ロード ───────────────────────────────────────────────────────────
    world_data: dict = yaml.safe_load(
        (story_dir / "world_config.yaml").read_text(encoding="utf-8")
    )
    char_data: dict = yaml.safe_load(
        (story_dir / "characters.yaml").read_text(encoding="utf-8")
    )

    story = world_data["story"]
    sid = story["id"]
    season_start = story.get("season_start", "2000-01-01")
    t0 = f"{season_start}T00:00:00"

    places = world_data.get("places") or []
    schedules = world_data.get("time_schedules") or []
    events = world_data.get("event_calendar") or []
    anomalies = world_data.get("anomaly_rules") or []
    characters = char_data.get("characters") or []
    char_ids = [c["id"] for c in characters]

    # ── DB 接続・初期化 ───────────────────────────────────────────────────────
    conn = await _build_db(db_path)

    counts: dict[str, int] = {}

    try:
        # 既存ストーリーは明示的な force 指定がある場合のみ再構築する。
        cursor = await conn.execute(
            "SELECT id FROM stories WHERE id = ?", (sid,)
        )
        if await cursor.fetchone() is not None:
            if not force_replace:
                raise RuntimeError(
                    f"story_id '{sid}' already exists. "
                    "Use python -m tools.update_story ... to preserve progress, "
                    "or rerun import_story with --force-replace."
                )
            logger.warning(
                "既存ストーリーを削除して再インポートします: story_id=%s", sid
            )
            await conn.execute("DELETE FROM stories WHERE id = ?", (sid,))

        # ── stories ──────────────────────────────────────────────────────────
        await conn.execute(
            """
            INSERT INTO stories
                (id, title, description, world_rules, season_start,
                 turn_minutes, turn_interval_sec, story_mode, llm_provider, llm_model)
            VALUES
                (:id, :title, :description, :world_rules, :season_start,
                 :turn_minutes, :turn_interval_sec, :story_mode, :llm_provider, :llm_model)
            """,
            _map_story(story),
        )
        counts["stories"] = 1

        # ── places ───────────────────────────────────────────────────────────
        for place in places:
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
                _map_place(place, sid),
            )
        counts["places"] = len(places)

        # ── time_schedules ───────────────────────────────────────────────────
        for s in schedules:
            await conn.execute(
                """
                INSERT INTO time_schedules
                    (story_id, day_type, time_from, time_to, label,
                     expected_places, mood_modifier, anomaly_note)
                VALUES
                    (:story_id, :day_type, :time_from, :time_to, :label,
                     :expected_places, :mood_modifier, :anomaly_note)
                """,
                _map_schedule(s, sid),
            )
        counts["time_schedules"] = len(schedules)

        # ── event_calendar ───────────────────────────────────────────────────
        for e in events:
            await conn.execute(
                """
                INSERT INTO event_calendar
                    (story_id, event_date, name, duration_days,
                     atmosphere, emotion_impact, force_place)
                VALUES
                    (:story_id, :event_date, :name, :duration_days,
                     :atmosphere, :emotion_impact, :force_place)
                """,
                _map_event(e, sid),
            )
        counts["event_calendar"] = len(events)

        # ── anomaly_rules ────────────────────────────────────────────────────
        for r in anomalies:
            await conn.execute(
                """
                INSERT INTO anomaly_rules
                    (story_id, label, condition_json, drama_potential, suggested_reasons)
                VALUES
                    (:story_id, :label, :condition_json, :drama_potential, :suggested_reasons)
                """,
                _map_anomaly(r, sid),
            )
        counts["anomaly_rules"] = len(anomalies)

        # ── characters ───────────────────────────────────────────────────────
        for c in characters:
            await conn.execute(
                """
                INSERT INTO characters
                    (id, story_id, name_ja, name_read, appearance,
                     personality_core, speech, strengths, weaknesses,
                     current_goal, current_worry, secret, secret_reveal_condition,
                     emotion_default, favorite_places, move_tendency,
                     expressions_available, image_path)
                VALUES
                    (:id, :story_id, :name_ja, :name_read, :appearance,
                     :personality_core, :speech, :strengths, :weaknesses,
                     :current_goal, :current_worry, :secret, :secret_reveal_condition,
                     :emotion_default, :favorite_places, :move_tendency,
                     :expressions_available, :image_path)
                """,
                _map_character(c, sid),
            )
        counts["characters"] = len(characters)

        # ── character_states（初期行）────────────────────────────────────────
        for c in characters:
            await conn.execute(
                """
                INSERT INTO character_states
                    (char_id, story_id, sim_datetime, turn_number, current_place,
                     stress, motivation, loneliness, excitement)
                VALUES
                    (:char_id, :story_id, :sim_datetime, :turn_number, :current_place,
                     :stress, :motivation, :loneliness, :excitement)
                """,
                _map_char_state(c, sid, t0),
            )
        counts["character_states"] = len(characters)

        # ── relationships（全キャラ順列）────────────────────────────────────
        rel_count = 0
        for from_id, to_id in itertools.permutations(char_ids, 2):
            await conn.execute(
                """
                INSERT INTO relationships (story_id, char_id_from, char_id_to, trust)
                VALUES (?, ?, ?, 0.5)
                """,
                (sid, from_id, to_id),
            )
            rel_count += 1
        counts["relationships"] = rel_count

        await conn.commit()
        ensure_story_map_scaffold(sid, places, story_maps_root)
        logger.info("インポート完了: story_id=%s, counts=%s", sid, counts)
        return counts

    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# CLI エントリーポイント
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    """CLIエントリーポイント。引数でストーリーディレクトリと DB パスを受け取る。"""
    parser = argparse.ArgumentParser(
        description="ストーリーYAMLをSQLite DBにインポートする"
    )
    parser.add_argument("story_dir", help="ストーリーディレクトリパス")
    parser.add_argument(
        "--db",
        default="db/pocketrole.db",
        help="SQLite DB ファイルパス（デフォルト: db/pocketrole.db）",
    )
    parser.add_argument(
        "--force-replace",
        action="store_true",
        help="既存 story_id を削除してから再インポートする",
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

    story_dir = Path(args.story_dir)

    # ── バリデーション（exit code を正確に決定するため import 前に実行）───────
    try:
        errors = validate_story(story_dir)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_FILE_NOT_FOUND
    except StoryValidationError as e:
        for err in e.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return EXIT_YAML_PARSE_ERROR

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        print(f"{len(errors)} validation error(s) found.", file=sys.stderr)
        return EXIT_ERROR

    # ── インポート実行 ────────────────────────────────────────────────────────
    try:
        counts = asyncio.run(
            import_story(
                args.story_dir,
                args.db,
                force_replace=args.force_replace,
                story_maps_root=args.story_maps_root,
            )
        )
    except Exception as e:
        logger.exception("インポートエラー")
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR

    print(f"OK: {story_dir.name} imported — {counts}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
