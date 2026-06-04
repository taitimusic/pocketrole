"""tests/test_story_onboarding.py — story clone onboarding service tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from admin.story_onboarding import (
    CharacterEditingError,
    CharacterEditingService,
    ChapterDefinitionEditingError,
    ChapterDefinitionEditingService,
    DirectorPersonaEditingError,
    DirectorPersonaEditingService,
    EventAnomalyEditingError,
    EventAnomalyEditingService,
    PlaceEditingError,
    PlaceEditingService,
    StoryDefinitionEditingError,
    StoryDefinitionEditingService,
    StoryOnboardingError,
    StoryOnboardingService,
)
from db.db_manager import DatabaseManager
from tools.import_story import import_story
from tests.test_validate_story import MINIMAL_CHARACTERS, MINIMAL_WORLD_CONFIG


MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"
TEMPLATE_STORY_ID = "template_story"


def _write_story_bundle(stories_root: Path, story_id: str) -> Path:
    story_dir = stories_root / story_id
    story_dir.mkdir(parents=True, exist_ok=True)

    world = copy.deepcopy(MINIMAL_WORLD_CONFIG)
    world["story"]["id"] = story_id
    world["story"]["title"] = "テンプレート"
    world["story"]["description"] = "テンプレート説明"
    (story_dir / "world_config.yaml").write_text(
        yaml.safe_dump(world, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    chars = copy.deepcopy(MINIMAL_CHARACTERS)
    chars["characters"][0]["story_id"] = story_id
    chars["characters"][0]["name"] = "テンプレキャラ"
    chars["characters"][0]["personality"] = {"type": "静かな主人公"}
    chars["characters"][0]["goal"] = "毎日を無事に終える"
    chars["characters"][0]["worry"] = "少し緊張しやすい"
    chars["characters"][0]["expressions"] = ["neutral", "happy"]
    (story_dir / "characters.yaml").write_text(
        yaml.safe_dump(chars, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    (story_dir / "chapters.yaml").write_text(
        yaml.safe_dump(
            {
                "chapters": [
                    {
                        "id": "ch001",
                        "title": "導入",
                        "theme": "日常",
                        "world_injection": "最初の朝",
                        "beats": [
                            {
                                "phase": "setup",
                                "description": "始まり",
                                "goal": "顔合わせ",
                                "events": [],
                            }
                        ],
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (story_dir / "director.yaml").write_text(
        yaml.safe_dump(
            {
                "default_active": "default_director",
                "personas": {
                    "default_director": {
                        "name": "標準監督",
                        "aesthetic": {"tension_preference": 0.5},
                        "values": ["誠実"],
                        "traits": ["観察型"],
                    }
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    image_sources = story_dir / "image_sources"
    image_sources.mkdir()
    (image_sources / "test_char.png").write_bytes(b"template-source")
    return story_dir


def _write_asset_bundle(
    character_images_root: Path,
    story_maps_root: Path,
    story_metadata_root: Path,
    story_id: str,
) -> None:
    char_dir = character_images_root / story_id / "test_char"
    char_dir.mkdir(parents=True, exist_ok=True)
    (char_dir / "neutral.png").write_bytes(b"template-neutral")
    (char_dir / "happy.png").write_bytes(b"template-happy")

    map_dir = story_maps_root / story_id
    (map_dir / "images").mkdir(parents=True, exist_ok=True)
    (map_dir / "bgm").mkdir(parents=True, exist_ok=True)
    (map_dir / "images" / "classroom_320.png").write_bytes(b"place-image")
    (map_dir / "bgm" / "theme.ogg").write_bytes(b"bgm")
    (map_dir / "map.json").write_text("{}", encoding="utf-8")
    (map_dir / "background.svg").write_text("<svg></svg>", encoding="utf-8")
    (map_dir / "place_manifest.json").write_text(
        json.dumps({"story_id": story_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    story_metadata_root.mkdir(parents=True, exist_ok=True)
    (story_metadata_root / f"{story_id}.json").write_text(
        json.dumps({"story_id": story_id}, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_clone_story_creates_new_story_and_imports_assets(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, TEMPLATE_STORY_ID)
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, TEMPLATE_STORY_ID)

    service = StoryOnboardingService(
        db=db,
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.clone_story(
        TEMPLATE_STORY_ID,
        {
            "story_id": "my_story",
            "title": "自分の物語",
            "description": "自分用に差し替えた説明",
            "characters": [
                {
                    "char_id": "test_char",
                    "name": "自分の主人公",
                    "short_description": "少し不器用な観測者",
                    "goal": "うまく友達を作る",
                    "worry": "自分だけ空回りしそう",
                }
            ],
        },
    )

    assert result["story_id"] == "my_story"
    assert result["character_count"] == 1
    assert result["import_mode"] == "created"
    assert result["chapters_imported"] == 1
    assert result["director_personas_imported"] == 1

    story_row = await db.get_story("my_story")
    assert story_row is not None
    assert story_row["title"] == "自分の物語"

    character_row = await db.get_character("my_story", "test_char")
    assert character_row is not None
    assert character_row["name_ja"] == "自分の主人公"
    assert character_row["personality_core"] == "少し不器用な観測者"
    assert character_row["current_goal"] == "うまく友達を作る"

    cloned_world = yaml.safe_load((stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8"))
    cloned_chars = yaml.safe_load((stories_root / "my_story" / "characters.yaml").read_text(encoding="utf-8"))
    assert cloned_world["story"]["id"] == "my_story"
    assert cloned_chars["characters"][0]["story_id"] == "my_story"
    assert cloned_chars["characters"][0]["name"] == "自分の主人公"

    assert (stories_root / "my_story" / "image_sources" / "test_char.png").exists()
    assert (character_images_root / "my_story" / "test_char" / "neutral.png").read_bytes() == b"template-neutral"
    assert (story_maps_root / "my_story" / "images" / "classroom_320.png").exists()

    metadata = json.loads((story_metadata_root / "my_story.json").read_text(encoding="utf-8"))
    assert metadata["story_id"] == "my_story"
    assert metadata["characters"][0]["name"] == "自分の主人公"

    await db.close()


@pytest.mark.asyncio
async def test_clone_story_can_replace_neutral_image(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, TEMPLATE_STORY_ID)
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, TEMPLATE_STORY_ID)

    service = StoryOnboardingService(
        db=db,
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    await service.clone_story(
        TEMPLATE_STORY_ID,
        {"story_id": "with_image", "title": "画像差し替え", "characters": []},
        uploaded_images={"test_char": b"replacement-neutral"},
    )

    assert (
        character_images_root / "with_image" / "test_char" / "neutral.png"
    ).read_bytes() == b"replacement-neutral"

    await db.close()


@pytest.mark.asyncio
async def test_clone_story_rejects_duplicate_story_id(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    assert db._conn is not None
    await db._conn.execute(
        """
        INSERT INTO stories (id, title, llm_provider, llm_model)
        VALUES (?, ?, ?, ?)
        """,
        ("duplicate_story", "既存", "openai", "gpt-4o"),
    )
    await db._conn.commit()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, TEMPLATE_STORY_ID)
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, TEMPLATE_STORY_ID)

    service = StoryOnboardingService(
        db=db,
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(StoryOnboardingError):
        await service.clone_story(
            TEMPLATE_STORY_ID,
            {"story_id": "duplicate_story", "title": "重複", "characters": []},
        )

    await db.close()


@pytest.mark.asyncio
async def test_character_editing_updates_yaml_db_metadata_and_neutral_image(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = CharacterEditingService(
        db=db,
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_characters(
        "my_story",
        {
            "characters": [
                {
                    "char_id": "test_char",
                    "name": "編集後キャラ",
                    "short_description": "明るい観察者",
                    "goal": "友達と物語を進める",
                    "worry": "失敗が怖い",
                }
            ]
        },
        uploaded_images={"test_char": b"edited-neutral"},
    )

    assert result["story_id"] == "my_story"
    assert result["character_count"] == 1
    assert result["update_counts"]["characters_updated"] == 1

    edited_chars = yaml.safe_load(
        (stories_root / "my_story" / "characters.yaml").read_text(encoding="utf-8")
    )
    edited_char = edited_chars["characters"][0]
    assert edited_char["name"] == "編集後キャラ"
    assert edited_char["personality"]["type"] == "明るい観察者"
    assert edited_char["goal"] == "友達と物語を進める"
    assert edited_char["worry"] == "失敗が怖い"

    character_row = await db.get_character("my_story", "test_char")
    assert character_row is not None
    assert character_row["name_ja"] == "編集後キャラ"
    assert character_row["personality_core"] == "明るい観察者"
    assert character_row["current_goal"] == "友達と物語を進める"
    assert (
        character_images_root / "my_story" / "test_char" / "neutral.png"
    ).read_bytes() == b"edited-neutral"

    metadata = json.loads((story_metadata_root / "my_story.json").read_text(encoding="utf-8"))
    assert metadata["characters"][0]["name"] == "編集後キャラ"

    await db.close()


@pytest.mark.asyncio
async def test_character_editing_updates_non_neutral_expression_images(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = CharacterEditingService(
        db=db,
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_characters(
        "my_story",
        {
            "characters": [
                {
                    "char_id": "test_char",
                    "new_char_id": "test_char",
                    "name": "テンプレキャラ",
                    "short_description": "静かな主人公",
                    "goal": "毎日を無事に終える",
                    "worry": "少し緊張しやすい",
                }
            ]
        },
        uploaded_images={"test_char": {"angry": b"edited-angry", "determined": b"edited-determined"}},
    )

    assert result["character_count"] == 1
    assert (character_images_root / "my_story" / "test_char" / "angry.png").read_bytes() == (
        b"edited-angry"
    )
    assert (
        character_images_root / "my_story" / "test_char" / "determined.png"
    ).read_bytes() == b"edited-determined"

    edited_chars = yaml.safe_load(
        (stories_root / "my_story" / "characters.yaml").read_text(encoding="utf-8")
    )
    assert "angry" in edited_chars["characters"][0]["expressions"]
    assert "determined" in edited_chars["characters"][0]["expressions"]

    character_row = await db.get_character("my_story", "test_char")
    assert character_row is not None
    assert "determined" in json.loads(character_row["expressions_available"])

    await db.close()


@pytest.mark.asyncio
async def test_character_editing_renames_character_id_and_assets(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = CharacterEditingService(
        db=db,
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_characters(
        "my_story",
        {
            "characters": [
                {
                    "char_id": "test_char",
                    "new_char_id": "main_hero",
                    "name": "主人公",
                    "short_description": "静かな主人公",
                    "goal": "友達と物語を進める",
                    "worry": "失敗が怖い",
                }
            ]
        },
    )

    assert result["characters_renamed"] == 1
    edited_chars = yaml.safe_load(
        (stories_root / "my_story" / "characters.yaml").read_text(encoding="utf-8")
    )
    assert edited_chars["characters"][0]["id"] == "main_hero"

    assert not (character_images_root / "my_story" / "test_char").exists()
    assert (character_images_root / "my_story" / "main_hero" / "neutral.png").read_bytes() == (
        b"template-neutral"
    )
    assert (character_images_root / "my_story" / "main_hero" / "happy.png").read_bytes() == (
        b"template-happy"
    )

    new_character_row = await db.get_character("my_story", "main_hero")
    old_character_row = await db.get_character("my_story", "test_char")
    assert new_character_row is not None
    assert new_character_row["is_active"] == 1
    assert old_character_row is not None
    assert old_character_row["is_active"] == 0

    metadata = json.loads((story_metadata_root / "my_story.json").read_text(encoding="utf-8"))
    assert metadata["characters"][0]["id"] == "main_hero"

    await db.close()


@pytest.mark.asyncio
async def test_character_editing_adds_and_removes_characters(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = CharacterEditingService(
        db=db,
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_characters(
        "my_story",
        {
            "characters": [
                {
                    "char_id": "",
                    "new_char_id": "new_friend",
                    "name": "新しい友人",
                    "short_description": "明るい新キャラ",
                    "goal": "主人公を助ける",
                    "worry": "まだ場所に慣れない",
                }
            ]
        },
        uploaded_images={"new_friend": b"new-neutral"},
    )

    assert result["character_count"] == 1
    assert result["characters_added"] == 1
    assert result["characters_removed"] == 1

    edited_chars = yaml.safe_load(
        (stories_root / "my_story" / "characters.yaml").read_text(encoding="utf-8")
    )
    assert [character["id"] for character in edited_chars["characters"]] == ["new_friend"]
    assert edited_chars["characters"][0]["name"] == "新しい友人"
    assert edited_chars["characters"][0]["expressions"] == [
        "neutral",
        "happy",
        "angry",
        "sad",
        "surprised",
        "worried",
        "content",
        "lonely",
        "tired",
        "determined",
    ]

    assert not (character_images_root / "my_story" / "test_char").exists()
    assert (character_images_root / "my_story" / "new_friend" / "neutral.png").read_bytes() == (
        b"new-neutral"
    )

    new_character_row = await db.get_character("my_story", "new_friend")
    old_character_row = await db.get_character("my_story", "test_char")
    assert new_character_row is not None
    assert new_character_row["is_active"] == 1
    assert old_character_row is not None
    assert old_character_row["is_active"] == 0

    assert db._conn is not None
    cursor = await db._conn.execute(
        "SELECT current_place FROM character_states WHERE story_id = ? AND char_id = ?",
        ("my_story", "new_friend"),
    )
    assert await cursor.fetchone() is not None

    metadata = json.loads((story_metadata_root / "my_story.json").read_text(encoding="utf-8"))
    assert metadata["characters"][0]["id"] == "new_friend"

    await db.close()


@pytest.mark.asyncio
async def test_character_editing_rejects_new_character_without_name(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = CharacterEditingService(
        db=db,
        stories_root=stories_root,
        character_images_root=character_images_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(CharacterEditingError, match="character name is required"):
        await service.update_characters(
            "my_story",
            {
                "characters": [
                    {
                        "char_id": "",
                        "new_char_id": "new_friend",
                        "name": "",
                    },
                    {
                        "char_id": "test_char",
                        "new_char_id": "test_char",
                        "name": "テンプレキャラ",
                    },
                ]
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_character_editing_rejects_template_story(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    service = CharacterEditingService(
        db=db,
        stories_root=tmp_path / "stories",
        character_images_root=tmp_path / "character_images",
        story_maps_root=tmp_path / "story_maps",
        story_metadata_root=tmp_path / "story_metadata",
    )

    with pytest.raises(CharacterEditingError, match="template story is read-only"):
        await service.update_characters("ankoku_gakuen", {"characters": []})

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_updates_yaml_and_db(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = PlaceEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_places(
        "my_story",
        {
            "places": [
                {
                    "place_id": "classroom",
                    "label": "朝の教室",
                    "zone": "school",
                    "atmosphere": "朝日が差し込む静かな空気",
                    "who_gathers": ["主人公", "友人"],
                    "events_likely": ["内緒話", "小さな衝突"],
                    "access_note": "授業前だけ人が少ない",
                },
                {"place_id": "corridor"},
                {"place_id": "home"},
            ]
        },
    )

    assert result["story_id"] == "my_story"
    assert result["place_count"] == 3
    assert result["places_added"] == 0
    assert result["places_removed"] == 0
    assert result["update_counts"]["places_updated"] == 3

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    classroom = next(place for place in edited_world["places"] if place["id"] == "classroom")
    assert classroom["label"] == "朝の教室"
    assert classroom["atmosphere"] == "朝日が差し込む静かな空気"
    assert classroom["who_gathers"] == ["主人公", "友人"]
    assert classroom["events_likely"] == ["内緒話", "小さな衝突"]
    assert classroom["access_note"] == "授業前だけ人が少ない"

    assert db._conn is not None
    cursor = await db._conn.execute(
        """
        SELECT label, zone, atmosphere, who_gathers, events_likely, access_note
        FROM places
        WHERE story_id = ? AND id = ?
        """,
        ("my_story", "classroom"),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["label"] == "朝の教室"
    assert row["zone"] == "school"
    assert row["atmosphere"] == "朝日が差し込む静かな空気"
    assert json.loads(row["who_gathers"]) == ["主人公", "友人"]
    assert json.loads(row["events_likely"]) == ["内緒話", "小さな衝突"]
    assert row["access_note"] == "授業前だけ人が少ない"

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_template_story(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    service = PlaceEditingService(
        db=db,
        stories_root=tmp_path / "stories",
        story_maps_root=tmp_path / "story_maps",
        story_metadata_root=tmp_path / "story_metadata",
    )

    with pytest.raises(PlaceEditingError, match="template story is read-only"):
        await service.update_places("ankoku_gakuen", {"places": []})

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_renames_place_and_updates_references(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = PlaceEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    result = await service.update_places(
        "my_story",
        {
            "places": [
                {
                    "place_id": "classroom",
                    "new_place_id": "homeroom",
                    "label": "ホームルーム",
                    "zone": "school",
                    "adjacent_places": {"corridor": 1},
                },
                {
                    "place_id": "corridor",
                    "label": "廊下",
                    "zone": "school",
                    "adjacent_places": {"homeroom": 1},
                },
                {"place_id": "home", "label": "自宅", "zone": "home"},
            ]
        },
    )

    assert result["places_renamed"] == 1
    assert result["places_added"] == 0
    assert result["places_removed"] == 0

    world_data = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    char_data = yaml.safe_load(
        (stories_root / "my_story" / "characters.yaml").read_text(encoding="utf-8")
    )
    assert [place["id"] for place in world_data["places"]] == ["homeroom", "corridor", "home"]
    assert world_data["time_schedules"][0]["expected_places"] == ["homeroom"]
    assert world_data["event_calendar"][0]["force_place"] == "homeroom"
    assert world_data["places"][1]["adjacent_places"] == {"homeroom": 1}
    assert char_data["characters"][0]["favorite_places"] == ["homeroom"]

    assert db._conn is not None
    cursor = await db._conn.execute(
        "SELECT id, is_active FROM places WHERE story_id = ? ORDER BY id",
        ("my_story",),
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    assert {"id": "homeroom", "is_active": 1} in rows
    assert {"id": "classroom", "is_active": 0} in rows
    cursor = await db._conn.execute(
        "SELECT favorite_places FROM characters WHERE story_id = ? AND id = ?",
        ("my_story", "test_char"),
    )
    char_row = await cursor.fetchone()
    assert json.loads(char_row["favorite_places"]) == ["homeroom"]

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_rename_to_existing_place_id(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = PlaceEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(PlaceEditingError, match="duplicate place_id"):
        await service.update_places(
            "my_story",
            {
                "places": [
                    {
                        "place_id": "classroom",
                        "new_place_id": "corridor",
                        "label": "教室",
                        "zone": "school",
                    },
                    {"place_id": "corridor", "label": "廊下", "zone": "school"},
                    {"place_id": "home", "label": "自宅", "zone": "home"},
                ]
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_story_definition_editing_updates_yaml_and_db(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = StoryDefinitionEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_definition(
        "my_story",
        {
            "story": {
                "title": "編集後タイトル",
                "description": "編集後の説明",
                "season_start": "2025-05-01",
                "turn_minutes": 15,
                "turn_interval_sec": 5,
                "world_rules": "編集後の世界ルール\nキャラ同士の衝突を重視する",
            }
        },
    )

    assert result["story_id"] == "my_story"
    assert result["update_counts"]["stories"] == 1

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    assert edited_world["story"]["title"] == "編集後タイトル"
    assert edited_world["story"]["description"] == "編集後の説明"
    assert edited_world["story"]["season_start"] == "2025-05-01"
    assert edited_world["story"]["turn_minutes"] == 15
    assert edited_world["story"]["turn_interval_sec"] == 5
    assert edited_world["story"]["world_rules"] == "編集後の世界ルール\nキャラ同士の衝突を重視する"

    story_row = await db.get_story("my_story")
    assert story_row is not None
    assert story_row["title"] == "編集後タイトル"
    assert story_row["description"] == "編集後の説明"
    assert story_row["season_start"] == "2025-05-01"
    assert story_row["turn_minutes"] == 15
    assert story_row["turn_interval_sec"] == 5
    assert json.loads(story_row["world_rules"]) == "編集後の世界ルール\nキャラ同士の衝突を重視する"

    await db.close()


@pytest.mark.asyncio
async def test_story_definition_editing_rejects_invalid_timing_values(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = StoryDefinitionEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(StoryDefinitionEditingError, match="season_start"):
        await service.update_definition(
            "my_story",
            {
                "story": {
                    "title": "テンプレート",
                    "season_start": "2025/05/01",
                }
            },
        )

    with pytest.raises(StoryDefinitionEditingError, match="turn_minutes"):
        await service.update_definition(
            "my_story",
            {
                "story": {
                    "title": "テンプレート",
                    "turn_minutes": 0,
                }
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_story_definition_editing_rejects_template_story(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    service = StoryDefinitionEditingService(
        db=db,
        stories_root=tmp_path / "stories",
        story_maps_root=tmp_path / "story_maps",
        story_metadata_root=tmp_path / "story_metadata",
    )

    with pytest.raises(StoryDefinitionEditingError, match="template story is read-only"):
        await service.update_definition("ankoku_gakuen", {"story": {}})

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_updates_yaml_and_db(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_event_anomalies(
        "my_story",
        {
            "events": [
                {
                    "event_key": "04-01:入学式",
                    "event_date": "04-02",
                    "name": "部活動紹介",
                    "duration_days": 2,
                    "atmosphere": "新しい出会いで校内が浮き立つ",
                    "emotion_impact": {"excitement": 0.4, "stress": 0.1},
                    "force_place": "classroom",
                }
            ],
            "anomalies": [
                {
                    "anomaly_key": "深夜の廊下",
                    "label": "深夜の教室",
                    "condition_json": {"place": "classroom", "time_from": "21:00"},
                    "drama_potential": "誰かの本音が漏れる",
                    "suggested_reasons": ["秘密の相談", "忘れ物を探している"],
                }
            ],
        },
    )

    assert result["story_id"] == "my_story"
    assert result["event_count"] == 1
    assert result["anomaly_count"] == 1
    assert result["update_counts"]["event_calendar"] == 1
    assert result["update_counts"]["anomaly_rules"] == 1

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    event = edited_world["event_calendar"][0]
    assert event["event_date"] == "04-02"
    assert event["name"] == "部活動紹介"
    assert event["duration_days"] == 2
    assert event["atmosphere"] == "新しい出会いで校内が浮き立つ"
    assert event["emotion_impact"] == {"excitement": 0.4, "stress": 0.1}

    anomaly = edited_world["anomaly_rules"][0]
    assert anomaly["label"] == "深夜の教室"
    assert anomaly["condition_json"]["place"] == "classroom"
    assert anomaly["drama_potential"] == "誰かの本音が漏れる"
    assert anomaly["suggested_reasons"] == ["秘密の相談", "忘れ物を探している"]

    assert db._conn is not None
    cursor = await db._conn.execute(
        """
        SELECT event_date, name, duration_days, atmosphere, emotion_impact, force_place
        FROM event_calendar
        WHERE story_id = ?
        """,
        ("my_story",),
    )
    event_row = await cursor.fetchone()
    assert event_row is not None
    assert event_row["event_date"] == "04-02"
    assert event_row["name"] == "部活動紹介"
    assert event_row["duration_days"] == 2
    assert json.loads(event_row["emotion_impact"]) == {"excitement": 0.4, "stress": 0.1}

    cursor = await db._conn.execute(
        """
        SELECT label, condition_json, drama_potential, suggested_reasons
        FROM anomaly_rules
        WHERE story_id = ?
        """,
        ("my_story",),
    )
    anomaly_row = await cursor.fetchone()
    assert anomaly_row is not None
    assert anomaly_row["label"] == "深夜の教室"
    assert json.loads(anomaly_row["condition_json"])["place"] == "classroom"
    assert json.loads(anomaly_row["suggested_reasons"]) == ["秘密の相談", "忘れ物を探している"]

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_rejects_template_story(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    service = EventAnomalyEditingService(
        db=db,
        stories_root=tmp_path / "stories",
        story_maps_root=tmp_path / "story_maps",
        story_metadata_root=tmp_path / "story_metadata",
    )

    with pytest.raises(EventAnomalyEditingError, match="template story is read-only"):
        await service.update_event_anomalies("ankoku_gakuen", {"events": [], "anomalies": []})

    await db.close()


@pytest.mark.asyncio
async def test_director_persona_editing_updates_yaml_and_db(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = DirectorPersonaEditingService(
        db=db,
        stories_root=stories_root,
        story_metadata_root=story_metadata_root,
    )
    result = await service.update_director_personas(
        "my_story",
        {
            "default_active": "bright_director",
            "personas": [
                {
                    "persona_id": "default_director",
                    "name": "標準監督 改",
                    "aesthetic": {"tension_preference": 0.65, "dialogue_wit": 0.7},
                    "values": ["誠実", "全員に見せ場"],
                    "traits": ["観察型"],
                },
                {
                    "persona_id": "bright_director",
                    "name": "明るい監督",
                    "aesthetic": {"tension_preference": 0.25, "curiosity": 0.8},
                    "values": ["軽やか"],
                    "traits": ["テンポ重視"],
                },
            ],
        },
    )

    assert result["persona_count"] == 2
    assert result["personas_added"] == 1
    assert result["personas_removed"] == 0
    assert result["default_active"] == "bright_director"

    director_data = yaml.safe_load(
        (stories_root / "my_story" / "director.yaml").read_text(encoding="utf-8")
    )
    assert director_data["default_active"] == "bright_director"
    assert director_data["personas"]["default_director"]["name"] == "標準監督 改"
    assert director_data["personas"]["bright_director"]["values"] == ["軽やか"]

    personas = await db.get_all_director_personas("my_story")
    assert [persona["persona_id"] for persona in personas] == [
        "bright_director",
        "default_director",
    ]
    active = await db.get_active_director_persona("my_story")
    assert active is not None
    assert active["persona_id"] == "bright_director"
    assert active["aesthetic_json"]["curiosity"] == 0.8

    await db.close()


@pytest.mark.asyncio
async def test_director_persona_editing_removes_omitted_persona(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = DirectorPersonaEditingService(
        db=db,
        stories_root=stories_root,
        story_metadata_root=story_metadata_root,
    )
    await service.update_director_personas(
        "my_story",
        {
            "default_active": "default_director",
            "personas": [
                {"persona_id": "default_director", "name": "標準監督"},
                {"persona_id": "bright_director", "name": "明るい監督"},
            ],
        },
    )
    result = await service.update_director_personas(
        "my_story",
        {
            "default_active": "default_director",
            "personas": [{"persona_id": "default_director", "name": "標準監督"}],
        },
    )

    assert result["personas_removed"] == 1
    personas = await db.get_all_director_personas("my_story")
    assert [persona["persona_id"] for persona in personas] == ["default_director"]

    await db.close()


@pytest.mark.asyncio
async def test_director_persona_editing_rejects_template_story(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "test.db", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    service = DirectorPersonaEditingService(
        db=db,
        stories_root=tmp_path / "stories",
        story_metadata_root=tmp_path / "story_metadata",
    )

    with pytest.raises(DirectorPersonaEditingError, match="template story is read-only"):
        await service.update_director_personas(
            "ankoku_gakuen",
            {"default_active": "default_director", "personas": []},
        )

    await db.close()


@pytest.mark.asyncio
async def test_chapter_definition_editing_updates_yaml_and_db(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = ChapterDefinitionEditingService(
        db=db,
        stories_root=stories_root,
        story_metadata_root=story_metadata_root,
    )
    result = await service.update_chapters(
        "my_story",
        {
            "chapters": [
                {
                    "chapter_id": "ch001",
                    "title": "導入 改",
                    "theme": "最初の緊張",
                    "world_injection": "朝の教室に小さな違和感がある。",
                    "start_condition": "manual",
                    "beats": [
                        {
                            "phase": "setup",
                            "description": "朝の空気を作る",
                            "goal": "全員が違和感に触れる",
                            "events": [
                                {
                                    "type": "notice",
                                    "desc": "掲示が変わる",
                                    "place_id": " classroom ",
                                    "priority": 2,
                                    "cooldown_turns": 3,
                                }
                            ],
                        }
                    ],
                },
                {
                    "chapter_id": "ch002",
                    "title": "放課後",
                    "theme": "小さな対立",
                    "start_condition": "manual",
                    "beats": [
                        {
                            "phase": "setup",
                            "description": "放課後の相談",
                            "goal": "次の争点が立つ",
                            "events": [],
                        }
                    ],
                },
            ]
        },
    )

    assert result["chapter_count"] == 2
    assert result["chapters_added"] == 1
    assert result["chapters_removed"] == 0
    assert result["beat_count"] == 2

    chapter_data = yaml.safe_load(
        (stories_root / "my_story" / "chapters.yaml").read_text(encoding="utf-8")
    )
    assert chapter_data["chapters"][0]["title"] == "導入 改"
    assert chapter_data["chapters"][1]["id"] == "ch002"

    chapters = await db.get_pending_chapters("my_story")
    assert [chapter["chapter_id"] for chapter in chapters] == ["ch001", "ch002"]
    ch001 = chapters[0]
    assert ch001["title"] == "導入 改"
    beats = await db.get_chapter_beats(ch001["id"])
    assert beats[0]["events_json"] == [
        {
            "type": "notice",
            "desc": "掲示が変わる",
            "place_id": "classroom",
            "priority": 2,
            "cooldown_turns": 3,
        }
    ]

    await db.close()


@pytest.mark.asyncio
async def test_chapter_definition_editing_removes_omitted_pending_chapter(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )
    service = ChapterDefinitionEditingService(
        db=db,
        stories_root=stories_root,
        story_metadata_root=story_metadata_root,
    )
    await service.update_chapters(
        "my_story",
        {
            "chapters": [
                {"chapter_id": "ch001", "title": "導入", "beats": [{"phase": "setup"}]},
                {"chapter_id": "ch002", "title": "追加", "beats": [{"phase": "setup"}]},
            ]
        },
    )
    result = await service.update_chapters(
        "my_story",
        {"chapters": [{"chapter_id": "ch001", "title": "導入", "beats": [{"phase": "setup"}]}]},
    )

    assert result["chapters_removed"] == 1
    chapters = await db.get_pending_chapters("my_story")
    assert [chapter["chapter_id"] for chapter in chapters] == ["ch001"]

    await db.close()


@pytest.mark.asyncio
async def test_chapter_definition_editing_validates_beat_events(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    try:
        service = ChapterDefinitionEditingService(
            db=db,
            stories_root=stories_root,
            story_metadata_root=story_metadata_root,
        )
        with pytest.raises(
            ChapterDefinitionEditingError, match="chapter beat event must be an object"
        ):
            await service.update_chapters(
                "my_story",
                {
                    "chapters": [
                        {
                            "chapter_id": "ch001",
                            "title": "導入",
                            "beats": [{"phase": "setup", "events": ["notice"]}],
                        }
                    ]
                },
            )
        with pytest.raises(ChapterDefinitionEditingError, match="event priority must be an integer"):
            await service.update_chapters(
                "my_story",
                {
                    "chapters": [
                        {
                            "chapter_id": "ch001",
                            "title": "導入",
                            "beats": [
                                {
                                    "phase": "setup",
                                    "events": [{"type": "notice", "priority": "high"}],
                                }
                            ],
                        }
                    ]
                },
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_chapter_definition_editing_rejects_template_story(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "test.db", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    service = ChapterDefinitionEditingService(
        db=db,
        stories_root=tmp_path / "stories",
        story_metadata_root=tmp_path / "story_metadata",
    )

    with pytest.raises(ChapterDefinitionEditingError, match="template story is read-only"):
        await service.update_chapters("ankoku_gakuen", {"chapters": []})

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_adds_new_event(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_event_anomalies(
        "my_story",
        {
            "events": [
                {"event_key": "04-01:入学式"},
                {
                    "event_key": "",
                    "event_date": "06-15",
                    "name": "梅雨祭",
                    "duration_days": 2,
                    "atmosphere": "じめじめとした季節の特別行事",
                },
            ],
            "anomalies": [{"anomaly_key": "深夜の廊下"}],
        },
    )

    assert result["event_count"] == 2
    assert result["events_added"] == 1
    assert result["events_removed"] == 0

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    assert len(edited_world["event_calendar"]) == 2
    names = [e["name"] for e in edited_world["event_calendar"]]
    assert "入学式" in names
    assert "梅雨祭" in names

    assert db._conn is not None
    cursor = await db._conn.execute(
        "SELECT count(*) AS cnt FROM event_calendar WHERE story_id = ?",
        ("my_story",),
    )
    row = await cursor.fetchone()
    assert row["cnt"] == 2

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_removes_event(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    story_dir = _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")

    world_path = story_dir / "world_config.yaml"
    world_data = yaml.safe_load(world_path.read_text(encoding="utf-8"))
    world_data["event_calendar"].append(
        {"event_date": "05-01", "name": "五月祭", "duration_days": 2}
    )
    world_path.write_text(
        yaml.safe_dump(world_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_event_anomalies(
        "my_story",
        {
            "events": [{"event_key": "05-01:五月祭"}],
            "anomalies": [{"anomaly_key": "深夜の廊下"}],
        },
    )

    assert result["event_count"] == 1
    assert result["events_removed"] == 1
    assert result["events_added"] == 0

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    assert len(edited_world["event_calendar"]) == 1
    assert edited_world["event_calendar"][0]["name"] == "五月祭"

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_adds_new_anomaly(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_event_anomalies(
        "my_story",
        {
            "events": [{"event_key": "04-01:入学式"}],
            "anomalies": [
                {"anomaly_key": "深夜の廊下"},
                {
                    "anomaly_key": "",
                    "label": "早朝の屋上",
                    "condition_json": {"place": "classroom", "time_from": "06:00"},
                    "drama_potential": "秘密の早朝練習",
                    "suggested_reasons": ["誰にも見られたくない"],
                },
            ],
        },
    )

    assert result["anomaly_count"] == 2
    assert result["anomalies_added"] == 1
    assert result["anomalies_removed"] == 0

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    assert len(edited_world["anomaly_rules"]) == 2
    labels = [a["label"] for a in edited_world["anomaly_rules"]]
    assert "深夜の廊下" in labels
    assert "早朝の屋上" in labels

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_removes_anomaly(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    story_dir = _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")

    world_path = story_dir / "world_config.yaml"
    world_data = yaml.safe_load(world_path.read_text(encoding="utf-8"))
    world_data["anomaly_rules"].append(
        {
            "label": "早朝の屋上",
            "condition_json": {"place": "classroom", "time_from": "06:00"},
        }
    )
    world_path.write_text(
        yaml.safe_dump(world_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_event_anomalies(
        "my_story",
        {
            "events": [{"event_key": "04-01:入学式"}],
            "anomalies": [{"anomaly_key": "早朝の屋上"}],
        },
    )

    assert result["anomaly_count"] == 1
    assert result["anomalies_removed"] == 1
    assert result["anomalies_added"] == 0

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    assert len(edited_world["anomaly_rules"]) == 1
    assert edited_world["anomaly_rules"][0]["label"] == "早朝の屋上"

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_rejects_duplicate_event_key(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(EventAnomalyEditingError, match="duplicate event"):
        await service.update_event_anomalies(
            "my_story",
            {
                "events": [
                    {"event_key": "", "event_date": "06-01", "name": "同名祭", "duration_days": 1},
                    {"event_key": "", "event_date": "06-01", "name": "同名祭", "duration_days": 2},
                ],
                "anomalies": [{"anomaly_key": "深夜の廊下"}],
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_rejects_duplicate_anomaly_label(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(EventAnomalyEditingError, match="duplicate anomaly"):
        await service.update_event_anomalies(
            "my_story",
            {
                "events": [{"event_key": "04-01:入学式"}],
                "anomalies": [
                    {"anomaly_key": "", "label": "同名異変", "condition_json": {"place": "classroom"}},
                    {"anomaly_key": "", "label": "同名異変", "condition_json": {"place": "corridor"}},
                ],
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_rejects_new_event_missing_name(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(EventAnomalyEditingError, match="name"):
        await service.update_event_anomalies(
            "my_story",
            {
                "events": [
                    {"event_key": "", "event_date": "07-01", "duration_days": 1},
                ],
                "anomalies": [{"anomaly_key": "深夜の廊下"}],
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_rejects_invalid_event_date_format(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(EventAnomalyEditingError, match="MM-DD"):
        await service.update_event_anomalies(
            "my_story",
            {
                "events": [
                    {"event_key": "", "event_date": "2025-07-01", "name": "変な日付", "duration_days": 1},
                ],
                "anomalies": [{"anomaly_key": "深夜の廊下"}],
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_rejects_new_event_with_unknown_force_place(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    story_dir = _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    original_world_yaml = (story_dir / "world_config.yaml").read_bytes()
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(EventAnomalyEditingError):
        await service.update_event_anomalies(
            "my_story",
            {
                "events": [
                    {
                        "event_key": "",
                        "event_date": "08-01",
                        "name": "謎の場所イベント",
                        "duration_days": 1,
                        "force_place": "nonexistent_place",
                    },
                ],
                "anomalies": [{"anomaly_key": "深夜の廊下"}],
            },
        )

    assert (story_dir / "world_config.yaml").read_bytes() == original_world_yaml

    await db.close()


@pytest.mark.asyncio
async def test_event_anomaly_editing_mixed_add_edit_delete(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    story_dir = _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")

    world_path = story_dir / "world_config.yaml"
    world_data = yaml.safe_load(world_path.read_text(encoding="utf-8"))
    world_data["event_calendar"].append(
        {"event_date": "09-01", "name": "文化祭", "duration_days": 2}
    )
    world_data["anomaly_rules"].append(
        {"label": "早朝の屋上", "condition_json": {"place": "classroom", "time_from": "06:00"}}
    )
    world_path.write_text(
        yaml.safe_dump(world_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    result = await service.update_event_anomalies(
        "my_story",
        {
            "events": [
                {"event_key": "04-01:入学式", "atmosphere": "新学期の緊張感"},
                {"event_key": "", "event_date": "11-01", "name": "体育祭", "duration_days": 1},
            ],
            "anomalies": [
                {"anomaly_key": "早朝の屋上", "drama_potential": "秘密の早朝作戦会議"},
            ],
        },
    )

    assert result["event_count"] == 2
    assert result["events_added"] == 1
    assert result["events_removed"] == 1
    assert result["anomaly_count"] == 1
    assert result["anomalies_added"] == 0
    assert result["anomalies_removed"] == 1

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    event_names = [e["name"] for e in edited_world["event_calendar"]]
    assert "入学式" in event_names
    assert "体育祭" in event_names
    assert "文化祭" not in event_names

    assert len(edited_world["anomaly_rules"]) == 1
    assert edited_world["anomaly_rules"][0]["label"] == "早朝の屋上"
    assert edited_world["anomaly_rules"][0]["drama_potential"] == "秘密の早朝作戦会議"

    await db.close()


# ─── Task 14: place 追加・削除・adjacent 編集テスト ─────────────────────────────


async def _setup_place_service(tmp_path: Path) -> tuple[DatabaseManager, PlaceEditingService, Path]:
    db = DatabaseManager(tmp_path / "test.db", migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )
    service = PlaceEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )
    return db, service, stories_root


_ALL_PLACES_PAYLOAD = [
    {"place_id": "classroom"},
    {"place_id": "corridor"},
    {"place_id": "home"},
]


@pytest.mark.asyncio
async def test_place_editing_adds_new_place(tmp_path: Path) -> None:
    db, service, stories_root = await _setup_place_service(tmp_path)

    result = await service.update_places(
        "my_story",
        {
            "places": [
                {"place_id": "classroom"},
                {"place_id": "corridor"},
                {"place_id": "home"},
                {
                    "place_id": "garden",
                    "label": "中庭",
                    "zone": "school",
                    "atmosphere": "花壇が綺麗な場所",
                },
            ]
        },
    )

    assert result["places_added"] == 1
    assert result["places_removed"] == 0
    assert result["place_count"] == 4

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    ids = [p["id"] for p in edited_world["places"]]
    assert "garden" in ids
    garden = next(p for p in edited_world["places"] if p["id"] == "garden")
    assert garden["label"] == "中庭"
    assert garden["zone"] == "school"

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_removes_unused_place(tmp_path: Path) -> None:
    db, service, stories_root = await _setup_place_service(tmp_path)

    result = await service.update_places(
        "my_story",
        {"places": [{"place_id": "classroom"}, {"place_id": "corridor"}]},
    )

    assert result["places_removed"] == 1
    assert result["places_added"] == 0
    assert result["place_count"] == 2

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    assert [p["id"] for p in edited_world["places"]] == ["classroom", "corridor"]

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_remove_referenced_by_favorite_places(tmp_path: Path) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="favorite_places"):
        await service.update_places(
            "my_story",
            {"places": [{"place_id": "corridor"}, {"place_id": "home"}]},
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_remove_referenced_by_time_schedules(tmp_path: Path) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="time_schedules"):
        await service.update_places(
            "my_story",
            {"places": [{"place_id": "corridor"}, {"place_id": "home"}]},
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_remove_referenced_by_force_place(tmp_path: Path) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="event_calendar"):
        await service.update_places(
            "my_story",
            {"places": [{"place_id": "corridor"}, {"place_id": "home"}]},
        )

    await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("condition_json", "message"),
    [
        ({"expected_place": "unknown_place"}, "expected_place"),
        ({"zone": "moon"}, "zone"),
        ({"time_from": "25:00", "time_to": "26:00"}, "time_from"),
        ({"time_to": "99:00"}, "time_to"),
        ({"multiple_characters": "yes"}, "multiple_characters"),
        ({"alone": "no"}, "alone"),
        ({"min_tension": 1.5}, "min_tension"),
        ({"stress_threshold_min": "high"}, "stress_threshold_min"),
    ],
)
async def test_event_anomaly_editing_rejects_invalid_condition_json_fields(
    tmp_path: Path,
    condition_json: dict[str, object],
    message: str,
) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()

    stories_root = tmp_path / "stories"
    character_images_root = tmp_path / "character_images"
    story_maps_root = tmp_path / "story_maps"
    story_metadata_root = tmp_path / "story_metadata"
    story_dir = _write_story_bundle(stories_root, "my_story")
    _write_asset_bundle(character_images_root, story_maps_root, story_metadata_root, "my_story")
    original_world_yaml = (story_dir / "world_config.yaml").read_bytes()
    await import_story(
        stories_root / "my_story",
        db_path=db.db_path,
        force_replace=False,
        story_maps_root=story_maps_root,
    )

    service = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root,
        story_maps_root=story_maps_root,
        story_metadata_root=story_metadata_root,
    )

    with pytest.raises(EventAnomalyEditingError, match=message):
        await service.update_event_anomalies(
            "my_story",
            {
                "events": [{"event_key": "04-01:入学式"}],
                "anomalies": [
                    {
                        "anomaly_key": "深夜の廊下",
                        "condition_json": condition_json,
                    }
                ],
            },
        )

    assert (story_dir / "world_config.yaml").read_bytes() == original_world_yaml

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_remove_referenced_by_condition_json_place(
    tmp_path: Path,
) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="anomaly_rules"):
        await service.update_places(
            "my_story",
            {"places": [{"place_id": "classroom"}, {"place_id": "home"}]},
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_remove_referenced_by_other_place_adjacent(
    tmp_path: Path,
) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="adjacent_places"):
        await service.update_places(
            "my_story",
            {"places": [{"place_id": "corridor"}, {"place_id": "home"}]},
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_duplicate_new_place_id(tmp_path: Path) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="duplicate place_id"):
        await service.update_places(
            "my_story",
            {
                "places": [
                    {"place_id": "classroom"},
                    {"place_id": "corridor"},
                    {"place_id": "home"},
                    {"place_id": "garden", "label": "中庭A", "zone": "school"},
                    {"place_id": "garden", "label": "中庭B", "zone": "school"},
                ]
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_invalid_place_id_format(tmp_path: Path) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="invalid place_id format"):
        await service.update_places(
            "my_story",
            {
                "places": [
                    {"place_id": "classroom"},
                    {"place_id": "corridor"},
                    {"place_id": "home"},
                    {"place_id": "Garden", "label": "中庭", "zone": "school"},
                ]
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_invalid_zone(tmp_path: Path) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="invalid zone"):
        await service.update_places(
            "my_story",
            {
                "places": [
                    {"place_id": "classroom"},
                    {"place_id": "corridor"},
                    {"place_id": "home"},
                    {"place_id": "garden", "label": "中庭", "zone": "forest"},
                ]
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_updates_adjacent_places(tmp_path: Path) -> None:
    db, service, stories_root = await _setup_place_service(tmp_path)

    result = await service.update_places(
        "my_story",
        {
            "places": [
                {"place_id": "classroom", "adjacent_places": {"home": 2}},
                {"place_id": "corridor", "adjacent_places": {"home": 1}},
                {"place_id": "home", "adjacent_places": {"classroom": 2, "corridor": 1}},
            ]
        },
    )

    assert result["places_added"] == 0
    assert result["places_removed"] == 0

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    classroom = next(p for p in edited_world["places"] if p["id"] == "classroom")
    assert classroom.get("adjacent_places") == {"home": 2}

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_self_referencing_adjacent(tmp_path: Path) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="cannot be adjacent to itself"):
        await service.update_places(
            "my_story",
            {
                "places": [
                    {"place_id": "classroom", "adjacent_places": {"classroom": 1}},
                    {"place_id": "corridor"},
                    {"place_id": "home"},
                ]
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_unknown_adjacent_place_id(tmp_path: Path) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="unknown place_id"):
        await service.update_places(
            "my_story",
            {
                "places": [
                    {"place_id": "classroom"},
                    {"place_id": "corridor"},
                    {"place_id": "home"},
                    {
                        "place_id": "garden",
                        "label": "中庭",
                        "zone": "school",
                        "adjacent_places": {"ghost_place": 1},
                    },
                ]
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_rejects_invalid_adjacent_cost(tmp_path: Path) -> None:
    db, service, _ = await _setup_place_service(tmp_path)

    with pytest.raises(PlaceEditingError, match="cost must be int >= 1"):
        await service.update_places(
            "my_story",
            {
                "places": [
                    {"place_id": "classroom", "adjacent_places": {"corridor": 0}},
                    {"place_id": "corridor"},
                    {"place_id": "home"},
                ]
            },
        )

    await db.close()


@pytest.mark.asyncio
async def test_place_editing_mixed_add_edit_delete(tmp_path: Path) -> None:
    db, service, stories_root = await _setup_place_service(tmp_path)

    result = await service.update_places(
        "my_story",
        {
            "places": [
                {"place_id": "classroom", "label": "特別教室", "zone": "school"},
                {"place_id": "corridor"},
                {
                    "place_id": "garden",
                    "label": "中庭",
                    "zone": "school",
                    "adjacent_places": {"classroom": 1},
                },
            ]
        },
    )

    assert result["places_added"] == 1
    assert result["places_removed"] == 1
    assert result["place_count"] == 3

    edited_world = yaml.safe_load(
        (stories_root / "my_story" / "world_config.yaml").read_text(encoding="utf-8")
    )
    ids = [p["id"] for p in edited_world["places"]]
    assert "garden" in ids
    assert "home" not in ids
    classroom = next(p for p in edited_world["places"] if p["id"] == "classroom")
    assert classroom["label"] == "特別教室"

    await db.close()
