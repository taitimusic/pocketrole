#!/usr/bin/env python3
"""tools/validate_story.py — ストーリーYAML バリデーションCLI

Usage:
    python -m tools.validate_story stories/ankoku_gakuen
    python tools/validate_story.py stories/ankoku_gakuen

終了コード:
    0 — バリデーション成功
    1 — バリデーションエラー
    2 — ファイルが見つからない
    3 — YAML 解析エラー
"""

import re
import sys
from pathlib import Path
from typing import Any

import yaml

VALID_ZONES: frozenset = frozenset({"school", "town", "home"})
VALID_DAY_TYPES: frozenset = frozenset({"weekday", "weekend"})
EMOTION_KEYS: tuple = ("stress", "motivation", "loneliness", "excitement")

EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1
EXIT_FILE_NOT_FOUND = 2
EXIT_YAML_PARSE_ERROR = 3


class StoryValidationError(Exception):
    """YAML 解析エラー時に送出される例外。エラー一覧を保持する。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s)")


def _load_yaml(path: Path) -> Any:
    """YAMLファイルをロードする。yaml.YAMLError を StoryValidationError に変換する。"""
    with path.open(encoding="utf-8") as f:
        try:
            return yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise StoryValidationError([f"[YAML] {path.name}: 解析エラー — {e}"])


def _is_valid_time(s: str) -> bool:
    """HH:MM 形式かつ有効な時刻かチェックする。"""
    m = re.fullmatch(r"(\d{2}):(\d{2})", s)
    if not m:
        return False
    h, mm = int(m.group(1)), int(m.group(2))
    return 0 <= h <= 23 and 0 <= mm <= 59


def _is_valid_mmdd(s: str) -> bool:
    """MM-DD 形式かつ実在する日付かチェックする（うるう年は29日まで許容）。"""
    m = re.fullmatch(r"(\d{2})-(\d{2})", s)
    if not m:
        return False
    month, day = int(m.group(1)), int(m.group(2))
    if month < 1 or month > 12:
        return False
    days_in_month = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return 1 <= day <= days_in_month[month]


def _is_snake_case(s: str) -> bool:
    """^[a-z0-9_]+$ パターンかチェックする。"""
    if not isinstance(s, str):
        return False
    return bool(re.fullmatch(r"[a-z0-9_]+", s))


def _validate_world_config(data: Any) -> tuple[list[str], set[str], str]:
    """world_config.yaml の内容を検証する。

    Returns:
        (errors, place_ids, story_id) のタプル
    """
    errors: list[str] = []
    place_ids: set[str] = set()
    story_id: str = ""

    if not isinstance(data, dict):
        errors.append("[world_config] ルートがマッピングではありません")
        return errors, place_ids, story_id

    # ─── story セクション ─────────────────────────────────────────────────────
    story = data.get("story")
    if not isinstance(story, dict):
        errors.append("[story] セクションが存在しないか不正です")
    else:
        sid = story.get("id", "")
        if not sid:
            errors.append("[story] id: 必須フィールドが空です")
        elif not _is_snake_case(sid):
            errors.append(f"[story] id: snake_case でなければなりません — '{sid}'")
        elif not (1 <= len(sid) <= 128):
            errors.append(f"[story] id: 1〜128文字でなければなりません — '{sid}'")
        else:
            story_id = sid

        title = story.get("title", "")
        if not isinstance(title, str) or not title.strip():
            errors.append("[story] title: 必須・非空文字列でなければなりません")

        season_start = story.get("season_start")
        if season_start is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(season_start)):
            errors.append(f"[story] season_start: YYYY-MM-DD 形式でなければなりません — '{season_start}'")

        turn_minutes = story.get("turn_minutes")
        if not isinstance(turn_minutes, int) or isinstance(turn_minutes, bool) or turn_minutes <= 0:
            errors.append(f"[story] turn_minutes: 正の整数でなければなりません — '{turn_minutes}'")

        turn_interval_sec = story.get("turn_interval_sec")
        if not isinstance(turn_interval_sec, int) or isinstance(turn_interval_sec, bool) or turn_interval_sec <= 0:
            errors.append(f"[story] turn_interval_sec: 正の整数でなければなりません — '{turn_interval_sec}'")

        story_mode = story.get("story_mode", "drama")
        _VALID_STORY_MODES = {"drama", "comedy", "romance", "action"}
        if str(story_mode) not in _VALID_STORY_MODES:
            errors.append(
                f"[story] story_mode: 無効な値 '{story_mode}'。"
                f"有効値: {', '.join(sorted(_VALID_STORY_MODES))}"
            )

    # ─── places セクション ────────────────────────────────────────────────────
    places = data.get("places")
    if not isinstance(places, list) or not places:
        errors.append("[places] セクションが存在しないかリストではありません")
    else:
        seen_ids: set[str] = set()
        for i, place in enumerate(places):
            if not isinstance(place, dict):
                errors.append(f"[places][{i}] 各エントリはマッピングでなければなりません")
                continue

            pid = place.get("id", "")
            if not pid:
                errors.append(f"[places][{i}] id: 必須フィールドが空です")
            elif not _is_snake_case(pid):
                errors.append(f"[places][{i}] id: snake_case でなければなりません — '{pid}'")
            elif pid in seen_ids:
                errors.append(f"[places] id: '{pid}' が重複しています")
            else:
                seen_ids.add(pid)
                place_ids.add(pid)

            zone = place.get("zone")
            if zone not in VALID_ZONES:
                errors.append(
                    f"[places][{i}] zone: 無効な値 '{zone}' — 有効値: {sorted(VALID_ZONES)}"
                )

            adjacent = place.get("adjacent_places")
            if adjacent is not None:
                if not isinstance(adjacent, dict):
                    errors.append(f"[places][{i}] adjacent_places: マッピングでなければなりません")
                else:
                    for adj_id, cost in adjacent.items():
                        if not isinstance(cost, int) or isinstance(cost, bool) or cost <= 0:
                            errors.append(
                                f"[places][{i}] adjacent_places.{adj_id}: "
                                f"正の整数でなければなりません — '{cost}'"
                            )

    # ─── time_schedules セクション ────────────────────────────────────────────
    schedules = data.get("time_schedules")
    if not isinstance(schedules, list) or not schedules:
        errors.append("[time_schedules] セクションが存在しないかリストではありません")
    else:
        for i, sched in enumerate(schedules):
            if not isinstance(sched, dict):
                errors.append(f"[time_schedules][{i}] マッピングでなければなりません")
                continue

            day_type = sched.get("day_type")
            if day_type not in VALID_DAY_TYPES:
                errors.append(
                    f"[time_schedules][{i}] day_type: 無効な値 '{day_type}' "
                    f"— 有効値: {sorted(VALID_DAY_TYPES)}"
                )

            time_from = sched.get("time_from")
            if not _is_valid_time(str(time_from) if time_from is not None else ""):
                errors.append(
                    f"[time_schedules][{i}] time_from: HH:MM 形式でなければなりません — '{time_from}'"
                )

            time_to = sched.get("time_to")
            if not _is_valid_time(str(time_to) if time_to is not None else ""):
                errors.append(
                    f"[time_schedules][{i}] time_to: HH:MM 形式でなければなりません — '{time_to}'"
                )

            expected = sched.get("expected_places")
            if expected is not None:
                if not isinstance(expected, list):
                    errors.append(f"[time_schedules][{i}] expected_places: リストでなければなりません")
                else:
                    for ep in expected:
                        if ep not in place_ids:
                            errors.append(
                                f"[time_schedules][{i}] expected_places: 未定義の place_id '{ep}'"
                            )

    # ─── event_calendar セクション ────────────────────────────────────────────
    events = data.get("event_calendar")
    if not isinstance(events, list) or not events:
        errors.append("[event_calendar] セクションが存在しないかリストではありません")
    else:
        for i, event in enumerate(events):
            if not isinstance(event, dict):
                errors.append(f"[event_calendar][{i}] マッピングでなければなりません")
                continue

            event_date = event.get("event_date")
            date_str = str(event_date) if event_date is not None else ""
            if not _is_valid_mmdd(date_str):
                errors.append(
                    f"[event_calendar][{i}] event_date: MM-DD 形式でなければなりません — '{event_date}'"
                )

            duration = event.get("duration_days")
            if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
                errors.append(
                    f"[event_calendar][{i}] duration_days: 正の整数でなければなりません — '{duration}'"
                )

            force_place = event.get("force_place")
            if force_place is not None and force_place not in place_ids:
                errors.append(
                    f"[event_calendar][{i}] force_place: 未定義の place_id '{force_place}'"
                )

    # ─── anomaly_rules セクション ─────────────────────────────────────────────
    anomaly_rules = data.get("anomaly_rules")
    if not isinstance(anomaly_rules, list) or not anomaly_rules:
        errors.append("[anomaly_rules] セクションが存在しないかリストではありません")
    else:
        for i, rule in enumerate(anomaly_rules):
            if not isinstance(rule, dict):
                errors.append(f"[anomaly_rules][{i}] マッピングでなければなりません")
                continue

            label = rule.get("label", "")
            if not isinstance(label, str) or not label.strip():
                errors.append(f"[anomaly_rules][{i}] label: 非空文字列でなければなりません")

            cond = rule.get("condition_json")
            if isinstance(cond, dict):
                place = cond.get("place")
                if place is not None and place not in place_ids:
                    errors.append(
                        f"[anomaly_rules][{i}] condition_json.place: 未定義の place_id '{place}'"
                    )

    return errors, place_ids, story_id


def _validate_characters(data: Any, place_ids: set[str], story_id: str) -> list[str]:
    """characters.yaml の内容を検証する。"""
    errors: list[str] = []

    if not isinstance(data, dict):
        errors.append("[characters] ルートがマッピングではありません")
        return errors

    characters = data.get("characters")
    if not isinstance(characters, list) or not characters:
        errors.append("[characters] セクションが存在しないかリストではありません")
        return errors

    for i, char in enumerate(characters):
        if not isinstance(char, dict):
            errors.append(f"[characters][{i}] マッピングでなければなりません")
            continue

        cid = char.get("id", "")
        tag = f"[characters][{cid or i}]"

        if not cid:
            errors.append(f"{tag} id: 必須フィールドが空です")
        elif not _is_snake_case(cid):
            errors.append(f"{tag} id: snake_case でなければなりません — '{cid}'")

        char_story_id = char.get("story_id", "")
        if char_story_id != story_id:
            errors.append(
                f"{tag} story_id: world_config の story.id '{story_id}' と一致しません "
                f"— '{char_story_id}'"
            )

        name = char.get("name", "")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{tag} name: 非空文字列でなければなりません")

        emotion = char.get("emotion_default")
        if not isinstance(emotion, dict):
            errors.append(f"{tag} emotion_default: マッピングでなければなりません")
        else:
            for key in EMOTION_KEYS:
                if key not in emotion:
                    errors.append(f"{tag} emotion_default.{key}: 必須フィールドが存在しません")
                else:
                    val = emotion[key]
                    if not isinstance(val, (int, float)) or isinstance(val, bool):
                        errors.append(
                            f"{tag} emotion_default.{key}: 数値でなければなりません — '{val}'"
                        )
                    elif not (0.0 <= float(val) <= 1.0):
                        errors.append(
                            f"{tag} emotion_default.{key}: 0.0〜1.0 の範囲でなければなりません — '{val}'"
                        )

        fav = char.get("favorite_places")
        if fav is not None:
            if not isinstance(fav, list):
                errors.append(f"{tag} favorite_places: リストでなければなりません")
            else:
                for fp in fav:
                    if fp not in place_ids:
                        errors.append(f"{tag} favorite_places: 未定義の place_id '{fp}'")

        secret = char.get("secret")
        if isinstance(secret, dict):
            threshold = secret.get("unlock_threshold")
            if threshold is not None:
                if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
                    errors.append(
                        f"{tag} secret.unlock_threshold: 数値でなければなりません — '{threshold}'"
                    )
                elif not (0.0 <= float(threshold) <= 1.0):
                    errors.append(
                        f"{tag} secret.unlock_threshold: 0.0〜1.0 の範囲でなければなりません "
                        f"— '{threshold}'"
                    )

    return errors


def validate_story(story_dir: str | Path) -> list[str]:
    """ストーリーディレクトリを検証し、エラー一覧を返す。

    エラーがなければ空リストを返す。#1-11 の import_story.py からも呼び出し可能。

    Args:
        story_dir: world_config.yaml と characters.yaml を含むディレクトリ

    Returns:
        エラーメッセージのリスト（空リストは成功を意味する）

    Raises:
        FileNotFoundError: world_config.yaml または characters.yaml が存在しない
        StoryValidationError: YAML 解析エラー
    """
    story_dir = Path(story_dir)
    world_config_path = story_dir / "world_config.yaml"
    characters_path = story_dir / "characters.yaml"

    if not world_config_path.exists():
        raise FileNotFoundError(f"world_config.yaml が見つかりません: {world_config_path}")
    if not characters_path.exists():
        raise FileNotFoundError(f"characters.yaml が見つかりません: {characters_path}")

    world_data = _load_yaml(world_config_path)
    char_data = _load_yaml(characters_path)

    world_errors, place_ids, story_id = _validate_world_config(world_data)
    char_errors = _validate_characters(char_data, place_ids, story_id)

    return world_errors + char_errors


def main() -> int:
    """CLIエントリーポイント。引数にストーリーディレクトリを受け取る。"""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <story_dir>", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    story_dir = Path(sys.argv[1])

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
        print(f"\n{len(errors)} error(s) found.", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    # 成功時のサマリー表示
    world_data = yaml.safe_load((story_dir / "world_config.yaml").read_text(encoding="utf-8"))
    char_data = yaml.safe_load((story_dir / "characters.yaml").read_text(encoding="utf-8"))
    place_count = len(world_data.get("places", []))
    char_count = len(char_data.get("characters", []))
    sid = world_data.get("story", {}).get("id", story_dir.name)
    print(f"OK: {sid} ({place_count} places, {char_count} characters)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
