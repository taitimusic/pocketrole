"""tests/test_update_story.py — 進行保持 update_story のテスト。"""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import aiosqlite
import pytest
import yaml

from db.db_manager import DatabaseManager
from tests._async_harness import async_to_sync
from tools.import_story import import_story

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

BASE_WORLD: dict = {
    "story": {
        "id": "test_story",
        "title": "テストストーリー",
        "description": "初期説明",
        "world_rules": ["rule-a"],
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
            "adjacent_places": {"classroom": 1, "home": 1},
        },
        {
            "id": "home",
            "label": "自宅",
            "zone": "home",
            "adjacent_places": {"corridor": 1},
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
            "name": "始業式",
            "duration_days": 1,
            "force_place": "classroom",
        }
    ],
    "anomaly_rules": [
        {
            "label": "夜の廊下",
            "condition_json": {
                "place": "corridor",
                "time_from": "22:00",
                "time_to": "23:59",
            },
        }
    ],
}

BASE_CHARACTERS: dict = {
    "characters": [
        {
            "id": "char_a",
            "name": "キャラA",
            "story_id": "test_story",
            "goal": "最初の目標",
            "worry": "最初の不安",
            "emotion_default": {
                "stress": 0.3,
                "motivation": 0.7,
                "loneliness": 0.2,
                "excitement": 0.5,
            },
            "favorite_places": ["classroom"],
            "secret": {
                "content": "秘密A",
                "unlock_threshold": 0.8,
            },
        },
        {
            "id": "char_b",
            "name": "キャラB",
            "story_id": "test_story",
            "goal": "二人目の目標",
            "worry": "二人目の不安",
            "emotion_default": {
                "stress": 0.4,
                "motivation": 0.6,
                "loneliness": 0.3,
                "excitement": 0.4,
            },
            "favorite_places": ["corridor"],
            "secret": {
                "content": "秘密B",
                "unlock_threshold": 0.7,
            },
        },
    ]
}


def write_story_dir(story_dir: Path, world: dict, chars: dict) -> None:
    story_dir.mkdir(exist_ok=True)
    (story_dir / "world_config.yaml").write_text(
        yaml.dump(world, allow_unicode=True), encoding="utf-8"
    )
    (story_dir / "characters.yaml").write_text(
        yaml.dump(chars, allow_unicode=True), encoding="utf-8"
    )


def story_maps_root_for(tmp_path: Path) -> Path:
    return tmp_path / "web" / "assets" / "story_maps"


def load_update_story():
    try:
        module = importlib.import_module("tools.update_story")
    except ModuleNotFoundError:
        pytest.fail("tools.update_story module is missing")
    update_story = getattr(module, "update_story", None)
    if update_story is None:
        pytest.fail("tools.update_story.update_story is missing")
    return update_story


async def seed_progress(db_path: Path) -> None:
    async with DatabaseManager(str(db_path), MIGRATIONS_DIR) as db:
        await db.update_last_sim_time("test_story", "2025-04-01T01:00")
        await db.insert_chat_log(
            "test_story",
            {
                "sim_datetime": "2025-04-01T01:00",
                "turn_number": 2,
                "char_id": "char_a",
                "msg_type": "monologue",
                "place_id": "classroom",
                "expression": "neutral",
                "message": "既存ログ",
                "llm_provider": "ollama",
                "llm_model": "ministral-3:14b",
                "posted_to_web": 1,
            },
        )
        await db.insert_character_state(
            "test_story",
            {
                "char_id": "char_a",
                "sim_datetime": "2025-04-01T01:00",
                "turn_number": 2,
                "current_place": "classroom",
                "previous_place": "corridor",
                "current_expression": "neutral",
                "stress": 0.41,
                "motivation": 0.72,
                "loneliness": 0.24,
                "excitement": 0.58,
            },
        )
        await db.insert_character_state(
            "test_story",
            {
                "char_id": "char_b",
                "sim_datetime": "2025-04-01T01:00",
                "turn_number": 2,
                "current_place": "corridor",
                "previous_place": "classroom",
                "current_expression": "neutral",
                "stress": 0.35,
                "motivation": 0.61,
                "loneliness": 0.29,
                "excitement": 0.43,
            },
        )
        assert db._conn is not None
        await db._conn.execute(
            """
            UPDATE relationships
            SET trust = ?, updated_at = datetime('now')
            WHERE story_id = ? AND char_id_from = ? AND char_id_to = ?
            """,
            (0.9, "test_story", "char_a", "char_b"),
        )
        await db._conn.commit()


async def fetch_one(db_path: Path, query: str, params: tuple = ()) -> aiosqlite.Row | None:
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(query, params)
        return await cursor.fetchone()


async def fetch_value(db_path: Path, query: str, params: tuple = ()) -> object:
    row = await fetch_one(db_path, query, params)
    assert row is not None
    return row[0]


@async_to_sync
async def test_update_story_preserves_progress_and_adds_new_character(tmp_path: Path) -> None:
    """進行状況を保持したまま設定更新と新規キャラ追加ができること。"""
    story_dir = tmp_path / "story"
    write_story_dir(story_dir, copy.deepcopy(BASE_WORLD), copy.deepcopy(BASE_CHARACTERS))
    db_path = tmp_path / "test.db"

    await import_story(story_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))
    await seed_progress(db_path)

    updated_world = copy.deepcopy(BASE_WORLD)
    updated_world["story"]["title"] = "更新後ストーリー"
    updated_world["story"]["turn_interval_sec"] = 60
    updated_world["story"]["world_rules"] = ["rule-b"]
    updated_world["places"].append(
        {
            "id": "stage",
            "label": "ステージ",
            "zone": "school",
            "adjacent_places": {"corridor": 1},
        }
    )

    updated_chars = copy.deepcopy(BASE_CHARACTERS)
    updated_chars["characters"][0]["goal"] = "更新後の目標"
    updated_chars["characters"].append(
        {
            "id": "char_c",
            "name": "キャラC",
            "story_id": "test_story",
            "goal": "新規参加",
            "worry": "まだ様子見",
            "emotion_default": {
                "stress": 0.2,
                "motivation": 0.8,
                "loneliness": 0.1,
                "excitement": 0.7,
            },
            "favorite_places": ["stage"],
            "secret": {
                "content": "秘密C",
                "unlock_threshold": 0.6,
            },
        }
    )
    write_story_dir(story_dir, updated_world, updated_chars)

    update_story = load_update_story()
    counts = await update_story(story_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))

    assert counts["stories"] == 1
    assert counts["characters_added"] == 1
    assert counts["characters_deactivated"] == 0

    story = await fetch_one(
        db_path,
        """
        SELECT title, turn_interval_sec, llm_provider, llm_model, last_sim_time
        FROM stories WHERE id = ?
        """,
        ("test_story",),
    )
    assert story is not None
    assert story["title"] == "更新後ストーリー"
    assert story["turn_interval_sec"] == 60
    assert story["llm_provider"] == ""
    assert story["llm_model"] == ""
    assert story["last_sim_time"] == "2025-04-01T01:00"

    assert await fetch_value(
        db_path,
        "SELECT COUNT(*) FROM chat_logs WHERE story_id = ?",
        ("test_story",),
    ) == 1
    assert await fetch_value(
        db_path,
        "SELECT current_goal FROM characters WHERE story_id = ? AND id = ?",
        ("test_story", "char_a"),
    ) == "更新後の目標"
    assert await fetch_value(
        db_path,
        "SELECT is_active FROM characters WHERE story_id = ? AND id = ?",
        ("test_story", "char_c"),
    ) == 1

    new_state = await fetch_one(
        db_path,
        """
        SELECT sim_datetime, turn_number, current_place
        FROM character_states
        WHERE story_id = ? AND char_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        ("test_story", "char_c"),
    )
    assert new_state is not None
    assert new_state["sim_datetime"] == "2025-04-01T01:00"
    assert new_state["turn_number"] == 2
    assert new_state["current_place"] == "stage"

    assert await fetch_value(
        db_path,
        """
        SELECT trust FROM relationships
        WHERE story_id = ? AND char_id_from = ? AND char_id_to = ?
        """,
        ("test_story", "char_a", "char_b"),
    ) == pytest.approx(0.9)
    assert await fetch_value(
        db_path,
        """
        SELECT COUNT(*) FROM relationships
        WHERE story_id = ? AND char_id_from = ? AND char_id_to = ?
        """,
        ("test_story", "char_c", "char_a"),
    ) == 1


@async_to_sync
async def test_update_story_deactivates_missing_rows_and_relocates_character(tmp_path: Path) -> None:
    """YAML から消えたキャラ・場所は inactive 化され、占有中キャラは再配置される。"""
    story_dir = tmp_path / "story"
    write_story_dir(story_dir, copy.deepcopy(BASE_WORLD), copy.deepcopy(BASE_CHARACTERS))
    db_path = tmp_path / "test.db"

    await import_story(story_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))
    await seed_progress(db_path)

    updated_world = copy.deepcopy(BASE_WORLD)
    updated_world["places"] = [
        place for place in updated_world["places"] if place["id"] != "classroom"
    ]
    updated_world["time_schedules"][0]["expected_places"] = ["corridor"]
    updated_world["event_calendar"][0]["force_place"] = "corridor"

    updated_chars = copy.deepcopy(BASE_CHARACTERS)
    updated_chars["characters"] = [updated_chars["characters"][0]]
    updated_chars["characters"][0]["favorite_places"] = ["corridor"]
    write_story_dir(story_dir, updated_world, updated_chars)

    update_story = load_update_story()
    counts = await update_story(story_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))

    assert counts["characters_deactivated"] == 1
    assert counts["places_deactivated"] == 1
    assert await fetch_value(
        db_path,
        "SELECT is_active FROM characters WHERE story_id = ? AND id = ?",
        ("test_story", "char_b"),
    ) == 0
    assert await fetch_value(
        db_path,
        "SELECT is_active FROM places WHERE story_id = ? AND id = ?",
        ("test_story", "classroom"),
    ) == 0

    latest_state = await fetch_one(
        db_path,
        """
        SELECT sim_datetime, current_place, previous_place
        FROM character_states
        WHERE story_id = ? AND char_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        ("test_story", "char_a"),
    )
    assert latest_state is not None
    assert latest_state["sim_datetime"] == "2025-04-01T01:00"
    assert latest_state["current_place"] == "corridor"
    assert latest_state["previous_place"] == "classroom"


@async_to_sync
async def test_update_story_rejects_turn_minutes_change_after_progress(tmp_path: Path) -> None:
    """進行済み story では turn_minutes 変更を拒否すること。"""
    story_dir = tmp_path / "story"
    write_story_dir(story_dir, copy.deepcopy(BASE_WORLD), copy.deepcopy(BASE_CHARACTERS))
    db_path = tmp_path / "test.db"

    await import_story(story_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))
    await seed_progress(db_path)

    updated_world = copy.deepcopy(BASE_WORLD)
    updated_world["story"]["turn_minutes"] = 60
    write_story_dir(story_dir, updated_world, copy.deepcopy(BASE_CHARACTERS))

    update_story = load_update_story()
    with pytest.raises(ValueError, match="turn_minutes"):
        await update_story(story_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))


@async_to_sync
async def test_update_story_persists_speech_examples_and_never_say(tmp_path: Path) -> None:
    """personality の追加口調情報を speech JSON に保持すること。"""
    story_dir = tmp_path / "story"
    write_story_dir(story_dir, copy.deepcopy(BASE_WORLD), copy.deepcopy(BASE_CHARACTERS))
    db_path = tmp_path / "test.db"

    await import_story(story_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))

    updated_chars = copy.deepcopy(BASE_CHARACTERS)
    updated_chars["characters"][0]["personality"] = {
        "type": "冷静なツッコミ役",
        "first_person": "わたし",
        "speech_style": "否定してから整理して説明する",
        "strengths": ["整理力"],
        "weaknesses": ["少し皮肉っぽい"],
        "speech_examples": [
            "いや、それは違うわ",
            "普通に考えてそうでしょ",
        ],
        "never_say": ["〜だぜ", "全部解決だな！"],
    }
    write_story_dir(story_dir, copy.deepcopy(BASE_WORLD), updated_chars)

    update_story = load_update_story()
    await update_story(story_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))

    speech_json = await fetch_value(
        db_path,
        "SELECT speech FROM characters WHERE story_id = ? AND id = ?",
        ("test_story", "char_a"),
    )
    assert speech_json is not None
    speech = json.loads(str(speech_json))
    assert speech["examples"] == [
        "いや、それは違うわ",
        "普通に考えてそうでしょ",
    ]
    assert speech["never_say"] == ["〜だぜ", "全部解決だな！"]
