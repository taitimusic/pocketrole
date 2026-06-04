"""tests/test_validate_story.py — validate_story.py のユニットテスト

tmp_path フィクスチャで一時ディレクトリにYAMLを書いてテストする。
"""

import copy
from pathlib import Path

import pytest
import yaml

from tools.validate_story import StoryValidationError, validate_story

# ─── 最小限の正常YAML テンプレート ──────────────────────────────────────────────

MINIMAL_WORLD_CONFIG: dict = {
    "story": {
        "id": "test_story",
        "title": "テストストーリー",
        "season_start": "2025-04-01",
        "turn_minutes": 30,
        "turn_interval_sec": 45,
    },
    "places": [
        {
            "id": "classroom",
            "label": "教室",
            "zone": "school",
            "adjacent_places": {"corridor": 1},
        },
        {
            "id": "corridor",
            "label": "廊下",
            "zone": "school",
            "adjacent_places": {"classroom": 1},
        },
        {
            "id": "home",
            "label": "自宅",
            "zone": "home",
        },
    ],
    "time_schedules": [
        {
            "day_type": "weekday",
            "time_from": "08:00",
            "time_to": "17:00",
            "label": "授業",
            "expected_places": ["classroom"],
        }
    ],
    "event_calendar": [
        {
            "event_date": "04-01",
            "name": "入学式",
            "duration_days": 1,
            "force_place": "classroom",
        }
    ],
    "anomaly_rules": [
        {
            "label": "深夜の廊下",
            "condition_json": {
                "place": "corridor",
                "time_from": "22:00",
            },
        }
    ],
}

MINIMAL_CHARACTERS: dict = {
    "characters": [
        {
            "id": "test_char",
            "name": "テストキャラ",
            "story_id": "test_story",
            "emotion_default": {
                "stress": 0.3,
                "motivation": 0.7,
                "loneliness": 0.2,
                "excitement": 0.5,
            },
            "favorite_places": ["classroom"],
            "secret": {
                "content": "秘密の内容",
                "unlock_threshold": 0.8,
            },
        }
    ]
}


# ─── ヘルパー ─────────────────────────────────────────────────────────────────


def write_yamls(
    tmp_path: Path,
    world: dict | None = None,
    chars: dict | None = None,
) -> None:
    """YAMLファイルを tmp_path に書き出す。None を渡したファイルは作成しない。"""
    if world is not None:
        (tmp_path / "world_config.yaml").write_text(
            yaml.dump(world, allow_unicode=True), encoding="utf-8"
        )
    if chars is not None:
        (tmp_path / "characters.yaml").write_text(
            yaml.dump(chars, allow_unicode=True), encoding="utf-8"
        )


# ─── テストケース ─────────────────────────────────────────────────────────────


def test_valid_story(tmp_path: Path) -> None:
    """正常なYAMLではエラーが返らないこと。"""
    write_yamls(tmp_path, MINIMAL_WORLD_CONFIG, MINIMAL_CHARACTERS)
    errors = validate_story(tmp_path)
    assert errors == []


def test_story_without_llm_fields_is_valid(tmp_path: Path) -> None:
    """world_config.yaml に LLM runtime 指定が無くても通ること。"""
    write_yamls(tmp_path, copy.deepcopy(MINIMAL_WORLD_CONFIG), MINIMAL_CHARACTERS)
    errors = validate_story(tmp_path)
    assert errors == []


def test_unknown_place_in_expected_places(tmp_path: Path) -> None:
    """time_schedules の expected_places に存在しない place_id を指定するとエラーになること。"""
    world = copy.deepcopy(MINIMAL_WORLD_CONFIG)
    world["time_schedules"][0]["expected_places"] = ["nonexistent_place"]
    write_yamls(tmp_path, world, MINIMAL_CHARACTERS)
    errors = validate_story(tmp_path)
    assert any("nonexistent_place" in e for e in errors)


def test_unknown_place_in_favorite_places(tmp_path: Path) -> None:
    """characters の favorite_places に存在しない place_id を指定するとエラーになること。"""
    chars = copy.deepcopy(MINIMAL_CHARACTERS)
    chars["characters"][0]["favorite_places"] = ["ghost_place"]
    write_yamls(tmp_path, MINIMAL_WORLD_CONFIG, chars)
    errors = validate_story(tmp_path)
    assert any("ghost_place" in e for e in errors)


def test_emotion_out_of_range(tmp_path: Path) -> None:
    """emotion_default の値が 1.0 を超えるとエラーになること。"""
    chars = copy.deepcopy(MINIMAL_CHARACTERS)
    chars["characters"][0]["emotion_default"]["stress"] = 1.5
    write_yamls(tmp_path, MINIMAL_WORLD_CONFIG, chars)
    errors = validate_story(tmp_path)
    assert any("stress" in e for e in errors)


def test_story_id_mismatch(tmp_path: Path) -> None:
    """characters の story_id が world_config の story.id と異なるとエラーになること。"""
    chars = copy.deepcopy(MINIMAL_CHARACTERS)
    chars["characters"][0]["story_id"] = "wrong_story_id"
    write_yamls(tmp_path, MINIMAL_WORLD_CONFIG, chars)
    errors = validate_story(tmp_path)
    assert any("story_id" in e for e in errors)


def test_invalid_time_format(tmp_path: Path) -> None:
    """time_from に不正な時刻（25:00）を指定するとエラーになること。"""
    world = copy.deepcopy(MINIMAL_WORLD_CONFIG)
    world["time_schedules"][0]["time_from"] = "25:00"
    write_yamls(tmp_path, world, MINIMAL_CHARACTERS)
    errors = validate_story(tmp_path)
    assert any("time_from" in e for e in errors)


def test_missing_world_config(tmp_path: Path) -> None:
    """world_config.yaml が存在しない場合は FileNotFoundError が送出されること。"""
    write_yamls(tmp_path, world=None, chars=MINIMAL_CHARACTERS)
    with pytest.raises(FileNotFoundError):
        validate_story(tmp_path)


def test_yaml_syntax_error(tmp_path: Path) -> None:
    """不正なYAML文法の場合は StoryValidationError が送出されること。"""
    (tmp_path / "world_config.yaml").write_text("[unclosed bracket\n", encoding="utf-8")
    (tmp_path / "characters.yaml").write_text(
        yaml.dump(MINIMAL_CHARACTERS, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(StoryValidationError):
        validate_story(tmp_path)


def test_emotion_negative_value(tmp_path: Path) -> None:
    """emotion_default の値が負数の場合もエラーになること。"""
    chars = copy.deepcopy(MINIMAL_CHARACTERS)
    chars["characters"][0]["emotion_default"]["motivation"] = -0.1
    write_yamls(tmp_path, MINIMAL_WORLD_CONFIG, chars)
    errors = validate_story(tmp_path)
    assert any("motivation" in e for e in errors)


def test_invalid_zone(tmp_path: Path) -> None:
    """places の zone に不正な値を指定するとエラーになること。"""
    world = copy.deepcopy(MINIMAL_WORLD_CONFIG)
    world["places"][0]["zone"] = "space"
    write_yamls(tmp_path, world, MINIMAL_CHARACTERS)
    errors = validate_story(tmp_path)
    assert any("zone" in e for e in errors)


def test_unlock_threshold_out_of_range(tmp_path: Path) -> None:
    """secret.unlock_threshold が 1.0 を超えるとエラーになること。"""
    chars = copy.deepcopy(MINIMAL_CHARACTERS)
    chars["characters"][0]["secret"]["unlock_threshold"] = 1.5
    write_yamls(tmp_path, MINIMAL_WORLD_CONFIG, chars)
    errors = validate_story(tmp_path)
    assert any("unlock_threshold" in e for e in errors)


def test_invalid_event_date(tmp_path: Path) -> None:
    """event_calendar の event_date に不正な月日（13-01）を指定するとエラーになること。"""
    world = copy.deepcopy(MINIMAL_WORLD_CONFIG)
    world["event_calendar"][0]["event_date"] = "13-01"
    write_yamls(tmp_path, world, MINIMAL_CHARACTERS)
    errors = validate_story(tmp_path)
    assert any("event_date" in e for e in errors)


def test_unknown_force_place(tmp_path: Path) -> None:
    """event_calendar の force_place に存在しない place_id を指定するとエラーになること。"""
    world = copy.deepcopy(MINIMAL_WORLD_CONFIG)
    world["event_calendar"][0]["force_place"] = "nonexistent_place"
    write_yamls(tmp_path, world, MINIMAL_CHARACTERS)
    errors = validate_story(tmp_path)
    assert any("force_place" in e for e in errors)
