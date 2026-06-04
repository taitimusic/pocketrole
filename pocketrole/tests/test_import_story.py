"""tests/test_import_story.py — import_story.py のユニットテスト

tmp_path フィクスチャで一時 DB ファイルを作成し、import_story() を呼び出す。
実物の ankoku_gakuen YAML を使うテストと、ミニマル YAML を使うテストを使い分ける。
"""

import copy
from pathlib import Path
import sqlite3

import aiosqlite
import pytest
import yaml

from db.db_manager import DatabaseManager
from tests._async_harness import async_to_sync
from tools.import_story import import_story
from tools.validate_story import StoryValidationError

# ──────────────────────────────────────────────────────────────────────────────
# パス定数
# ──────────────────────────────────────────────────────────────────────────────

STORY_DIR = Path(__file__).parent.parent / "stories" / "ankoku_gakuen"
MYSTERY_STORY_DIR = Path(__file__).parent.parent / "stories" / "ankoku_gakuen_mystery"
MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


# ──────────────────────────────────────────────────────────────────────────────
# ミニマル YAML テンプレート（エラー系テスト用）
# ──────────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────────────────────────────────────


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


def story_maps_root_for(tmp_path: Path) -> Path:
    return tmp_path / "web" / "assets" / "story_maps"


async def count_rows(db_path: Path, table: str, story_id: str) -> int:
    """指定テーブルの story_id に対応する行数を返す。"""
    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE story_id = ?",  # noqa: S608
            (story_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


def create_dirty_db_with_missing_v2(db_path: Path) -> None:
    """schema_version=1 のみだが places.is_active は存在する DB を作る。"""
    conn = sqlite3.connect(db_path)
    try:
        sql = (MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(
            "ALTER TABLE places ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;"
        )
        conn.commit()
    finally:
        conn.close()


def create_dirty_db_with_missing_v3(db_path: Path) -> None:
    """schema_version=1,2 だが conversation schema は存在する DB を作る。"""
    conn = sqlite3.connect(db_path)
    try:
        sql = (MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(
            "ALTER TABLE places ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, description) VALUES (2, ?)",
            ("places.is_active added",),
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversation_sessions (
                id                         INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id                   TEXT    NOT NULL,
                place_id                   TEXT    NOT NULL,
                participant_ids            JSON    NOT NULL DEFAULT '[]',
                status                     TEXT    NOT NULL DEFAULT 'active',
                last_speaker_id            TEXT,
                last_log_id                INTEGER,
                started_at_sim_datetime    TEXT    NOT NULL,
                last_activity_sim_datetime TEXT    NOT NULL,
                last_turn_number           INTEGER,
                created_at                 TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at                 TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_sessions_active
                ON conversation_sessions(story_id, place_id, status);
            """
        )
        conn.execute("ALTER TABLE chat_logs ADD COLUMN conversation_session_id INTEGER")
        conn.execute("ALTER TABLE chat_logs ADD COLUMN reply_to_log_id INTEGER")
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# テストケース
# ──────────────────────────────────────────────────────────────────────────────


@async_to_sync
async def test_import_success(tmp_path: Path) -> None:
    """正常YAML → 全テーブルにデータあり、返り値 dict を確認する。"""
    db_path = tmp_path / "test.db"
    # DB を事前初期化（migrations 適用）
    async with DatabaseManager(str(db_path), MIGRATIONS_DIR):
        pass

    counts = await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))

    assert isinstance(counts, dict)
    assert counts["stories"] == 1
    assert counts["places"] > 0
    assert counts["characters"] > 0
    assert counts["character_states"] > 0
    assert counts["relationships"] > 0


@async_to_sync
async def test_place_count(tmp_path: Path) -> None:
    """ankoku_gakuen の places が 17 件インポートされること。"""
    db_path = tmp_path / "test.db"
    counts = await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))
    assert counts["places"] == 15


@async_to_sync
async def test_character_count(tmp_path: Path) -> None:
    """ankoku_gakuen の characters が 7 件インポートされること。"""
    db_path = tmp_path / "test.db"
    counts = await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))
    assert counts["characters"] == 7


@async_to_sync
async def test_character_states_created(tmp_path: Path) -> None:
    """character_states の初期行が 7 件（キャラ数と一致）作成されること。"""
    db_path = tmp_path / "test.db"
    counts = await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))
    assert counts["character_states"] == 7

    n = await count_rows(db_path, "character_states", "ankoku_gakuen")
    assert n == 7


@async_to_sync
async def test_relationships_created(tmp_path: Path) -> None:
    """7キャラの全順列 7×6=42 件の relationships が作成されること。"""
    db_path = tmp_path / "test.db"
    counts = await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))
    assert counts["relationships"] == 42

    n = await count_rows(db_path, "relationships", "ankoku_gakuen")
    assert n == 42


@async_to_sync
async def test_idempotent(tmp_path: Path) -> None:
    """force 指定で2回インポートしても重複せず、同じ件数になること。"""
    db_path = tmp_path / "test.db"

    counts1 = await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))
    counts2 = await import_story(
        STORY_DIR,
        db_path,
        force_replace=True,
        story_maps_root=story_maps_root_for(tmp_path),
    )

    assert counts1 == counts2

    # DB 上の実件数も一致すること
    n_places = await count_rows(db_path, "places", "ankoku_gakuen")
    assert n_places == counts1["places"]

    n_chars = await count_rows(db_path, "characters", "ankoku_gakuen")
    assert n_chars == counts1["characters"]


@async_to_sync
async def test_existing_story_requires_force_replace(tmp_path: Path) -> None:
    """既存 story_id への再インポートは force 指定なしでは失敗すること。"""
    db_path = tmp_path / "test.db"

    await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "UPDATE stories SET last_sim_time = ? WHERE id = ?",
            ("2025-04-01T01:00", "ankoku_gakuen"),
        )
        await conn.commit()

    with pytest.raises(RuntimeError, match="force-replace"):
        await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))

    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            "SELECT last_sim_time FROM stories WHERE id = ?",
            ("ankoku_gakuen",),
        )
        row = await cursor.fetchone()

    assert row is not None
    assert row[0] == "2025-04-01T01:00"


@async_to_sync
async def test_force_replace_allows_reimport(tmp_path: Path) -> None:
    """force 指定時は既存 story_id を破壊的に再インポートできること。"""
    db_path = tmp_path / "test.db"

    await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))

    counts = await import_story(
        STORY_DIR,
        db_path,
        force_replace=True,
        story_maps_root=story_maps_root_for(tmp_path),
    )

    assert counts["stories"] == 1
    assert counts["characters"] == 7


@async_to_sync
async def test_force_replace_allows_reimport_after_migration_repair(
    tmp_path: Path,
) -> None:
    """version 2 未記録 DB でも force-replace で再構築できること。"""
    db_path = tmp_path / "test.db"
    create_dirty_db_with_missing_v2(db_path)

    await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))
    counts = await import_story(
        STORY_DIR,
        db_path,
        force_replace=True,
        story_maps_root=story_maps_root_for(tmp_path),
    )

    assert counts["stories"] == 1
    assert counts["characters"] == 7


@async_to_sync
async def test_mystery_story_import_success(tmp_path: Path) -> None:
    """ankoku_gakuen_mystery も実サンプルとして正常インポートできること。"""
    db_path = tmp_path / "mystery.db"

    counts = await import_story(
        MYSTERY_STORY_DIR,
        db_path,
        story_maps_root=story_maps_root_for(tmp_path),
    )

    assert counts["stories"] == 1
    assert counts["places"] > 0
    assert counts["characters"] == 5
    assert counts["character_states"] == 5
    assert counts["relationships"] == 20

    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        )
        rows = await cursor.fetchall()

    assert [row[0] for row in rows] == list(range(1, 36))


@async_to_sync
async def test_force_replace_allows_reimport_after_conversation_migration_repair(
    tmp_path: Path,
) -> None:
    """version 3 未記録 DB でも force-replace で再構築できること。"""
    db_path = tmp_path / "test-v3.db"
    create_dirty_db_with_missing_v3(db_path)

    await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))
    counts = await import_story(
        STORY_DIR,
        db_path,
        force_replace=True,
        story_maps_root=story_maps_root_for(tmp_path),
    )

    assert counts["stories"] == 1
    assert counts["characters"] == 7

    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        )
        rows = await cursor.fetchall()

    assert [row[0] for row in rows] == list(range(1, 36))


@async_to_sync
async def test_validation_error_aborts(tmp_path: Path) -> None:
    """バリデーション失敗時は stories テーブルに何も挿入されないこと。"""
    story_dir = tmp_path / "bad_story"
    story_dir.mkdir()

    bad_world = copy.deepcopy(MINIMAL_WORLD_CONFIG)
    bad_world["story"]["season_start"] = "invalid-date"
    write_yamls(story_dir, bad_world, MINIMAL_CHARACTERS)

    db_path = tmp_path / "test.db"
    async with DatabaseManager(str(db_path), MIGRATIONS_DIR):
        pass

    with pytest.raises(StoryValidationError):
        await import_story(story_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))

    # DB には何も残っていないこと（stories テーブルは id が主キー）
    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM stories WHERE id = ?", ("test_story",)
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] == 0


@async_to_sync
async def test_missing_yaml_raises(tmp_path: Path) -> None:
    """world_config.yaml が存在しない場合は FileNotFoundError が送出されること。"""
    empty_dir = tmp_path / "empty_story"
    empty_dir.mkdir()

    db_path = tmp_path / "test.db"

    with pytest.raises(FileNotFoundError):
        await import_story(empty_dir, db_path, story_maps_root=story_maps_root_for(tmp_path))


@async_to_sync
async def test_story_row_values(tmp_path: Path) -> None:
    """stories テーブルの id / title が正しくインポートされること。"""
    db_path = tmp_path / "test.db"
    await import_story(STORY_DIR, db_path, story_maps_root=story_maps_root_for(tmp_path))

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT id, title, llm_provider, llm_model FROM stories WHERE id = ?",
            ("ankoku_gakuen",),
        )
        row = await cursor.fetchone()

    assert row is not None
    assert row["id"] == "ankoku_gakuen"
    assert row["title"] == "逢魔ヶ刻学園 — トージョー大サーカスへようこそ"
    assert row["llm_provider"] == ""
    assert row["llm_model"] == ""
