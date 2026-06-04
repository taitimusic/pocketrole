from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

from tests._async_harness import async_to_sync
from tools.import_story import import_story
from tools.update_story import update_story


BASE_WORLD: dict = {
    "story": {
        "id": "test_story",
        "title": "テストストーリー",
        "season_start": "2025-04-01",
        "turn_minutes": 30,
        "turn_interval_sec": 45,
        "llm_provider": "ollama",
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

BASE_CHARACTERS: dict = {
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


def write_story_dir(story_dir: Path, world: dict, chars: dict) -> None:
    story_dir.mkdir(exist_ok=True)
    (story_dir / "world_config.yaml").write_text(
        yaml.dump(world, allow_unicode=True), encoding="utf-8"
    )
    (story_dir / "characters.yaml").write_text(
        yaml.dump(chars, allow_unicode=True), encoding="utf-8"
    )


@async_to_sync
async def test_import_story_creates_story_map_scaffold(tmp_path: Path) -> None:
    story_dir = tmp_path / "story"
    write_story_dir(story_dir, copy.deepcopy(BASE_WORLD), copy.deepcopy(BASE_CHARACTERS))
    db_path = tmp_path / "test.db"
    story_maps_root = tmp_path / "web" / "assets" / "story_maps"

    await import_story(story_dir, db_path, story_maps_root=story_maps_root)

    manifest = json.loads(
        (story_maps_root / "test_story" / "place_manifest.json").read_text(encoding="utf-8")
    )
    bgm_manifest = json.loads(
        (story_maps_root / "test_story" / "bgm_manifest.json").read_text(encoding="utf-8")
    )
    assert [row["place_id"] for row in manifest["places"]] == ["classroom", "corridor", "home"]
    assert (story_maps_root / "test_story" / "images" / "index.html").read_bytes() == b""
    assert (story_maps_root / "test_story" / "bgm" / "index.html").read_bytes() == b""
    assert bgm_manifest == {
        "story_id": "test_story",
        "enabled": True,
        "story_default": None,
        "place_overrides": {},
        "mood_overrides": {},
    }


@async_to_sync
async def test_update_story_refreshes_story_map_manifest_without_overwriting_manual_assets(
    tmp_path: Path,
) -> None:
    story_dir = tmp_path / "story"
    world = copy.deepcopy(BASE_WORLD)
    chars = copy.deepcopy(BASE_CHARACTERS)
    write_story_dir(story_dir, world, chars)
    db_path = tmp_path / "test.db"
    story_maps_root = tmp_path / "web" / "assets" / "story_maps"

    await import_story(story_dir, db_path, story_maps_root=story_maps_root)

    story_map_dir = story_maps_root / "test_story"
    images_dir = story_map_dir / "images"
    bgm_dir = story_map_dir / "bgm"
    (story_map_dir / "map.json").write_text('{"keep": true}\n', encoding="utf-8")
    (images_dir / "classroom_320.png").write_bytes(b"png")
    (story_map_dir / "bgm_manifest.json").write_text(
        '{"story_id":"test_story","enabled":true,"story_default":"bgm/custom.mp3","place_overrides":{},"mood_overrides":{}}\n',
        encoding="utf-8",
    )
    (bgm_dir / "custom.mp3").write_bytes(b"mp3")

    updated_world = copy.deepcopy(BASE_WORLD)
    updated_world["places"].append(
        {
            "id": "stage",
            "label": "ステージ",
            "zone": "school",
            "adjacent_places": {"corridor": 1},
        }
    )
    write_story_dir(story_dir, updated_world, chars)

    await update_story(story_dir, db_path, story_maps_root=story_maps_root)

    manifest = json.loads((story_map_dir / "place_manifest.json").read_text(encoding="utf-8"))
    assert [row["place_id"] for row in manifest["places"]] == ["classroom", "corridor", "home", "stage"]
    assert (story_map_dir / "map.json").read_text(encoding="utf-8") == '{"keep": true}\n'
    assert (images_dir / "classroom_320.png").read_bytes() == b"png"
    assert (story_map_dir / "bgm_manifest.json").read_text(encoding="utf-8") == (
        '{"story_id":"test_story","enabled":true,"story_default":"bgm/custom.mp3","place_overrides":{},"mood_overrides":{}}\n'
    )
    assert (bgm_dir / "custom.mp3").read_bytes() == b"mp3"
