"""
tests/test_db_manager.py — DatabaseManager のテスト

in-memory SQLite を使用。migrations/ ディレクトリは実ファイルを参照する。
real aiosqlite は pytest-asyncio を介さず asyncio.Runner で実行する。
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db.db_manager import DatabaseConnectionTimeoutError, DatabaseManager
from tests._async_harness import async_to_sync

# 実際の migrations ディレクトリを絶対パスで指定
MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

# ------------------------------------------------------------------
# フィクスチャ
# ------------------------------------------------------------------

_STORY_ID = "test_story"
_CHAR_ID = "char_a"


def _create_dirty_db_with_places_is_active(db_path: Path) -> None:
    """version 2 未記録だが places.is_active は存在する DB を作る。"""
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


def _create_dirty_db_with_conversation_schema(db_path: Path) -> None:
    """version 3 未記録だが会話 schema は存在する DB を作る。"""
    conn = sqlite3.connect(db_path)
    try:
        sql = (MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(
            "ALTER TABLE places ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, description) VALUES (2, ?);",
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
        conn.execute("ALTER TABLE chat_logs ADD COLUMN conversation_session_id INTEGER;")
        conn.execute("ALTER TABLE chat_logs ADD COLUMN reply_to_log_id INTEGER;")
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def _make_db() -> AsyncIterator[DatabaseManager]:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()

    # テスト用ストーリーを挿入
    assert manager._conn is not None
    await manager._conn.execute(
        "INSERT INTO stories (id, title) VALUES (?, ?);",
        (_STORY_ID, "テストストーリー"),
    )
    await manager._conn.commit()

    try:
        yield manager
    finally:
        await manager.close()


# ------------------------------------------------------------------
# テスト 1: 全テーブルが作成される
# ------------------------------------------------------------------

@async_to_sync
async def test_initialize_creates_tables() -> None:
    """initialize() 後に全テーブルが sqlite_master に存在すること。"""
    async with _make_db() as db:
        expected_tables = {
            "schema_version",
            "stories",
            "places",
            "time_schedules",
            "event_calendar",
            "anomaly_rules",
            "characters",
            "character_states",
            "relationships",
            "chat_logs",
            "conversation_sessions",
            "memories",
            "story_memory",
            "character_evolution",       # B-3 追加
            "narrative_tensions",        # B-4 追加
            "director_interventions",    # B-5 追加
            "story_arc",                 # C-3 追加
            "novel_output",              # C-3 追加
            "ambient_states",
            "story_scenes",
            "scene_participants",
            "story_hooks",
            "story_interaction_patterns",
            "story_episodes",
            "relationship_modes",
            "story_canon_bits",
            "story_dramatic_pressures",
            "character_drives",
            "relationship_events",
            "generation_quality_issues",
            "character_profile_overlays",
            "character_canon_overlays",
            "character_growth_candidates",
            "story_chapters",            # v2 upgrade: Chapter System
            "story_chapter_beats",       # v2 upgrade: Chapter System
            "director_personas",         # v2 upgrade: Director Persona
            "director_satisfaction",     # v2 upgrade: Director Persona
            "director_swap_log",         # v2 upgrade: Director Swap
            "conversation_motif_settings",
            "conversation_motif_runs",
        }
        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        )
        rows = await cursor.fetchall()
        actual_tables = {row[0] for row in rows}
        assert expected_tables.issubset(actual_tables)


@async_to_sync
async def test_conversation_motif_settings_and_runs_roundtrip() -> None:
    """会話モチーフ設定と進行中 run を story 単位で保存・取得できること。"""
    async with _make_db() as db:
        defaults = await db.get_conversation_motif_settings(_STORY_ID)
        assert defaults[0]["motif_id"] == "solo_seed_rondo"
        assert defaults[0]["enabled"] is False

        await db.upsert_conversation_motif_settings(
            _STORY_ID,
            [
                {
                    "motif_id": "solo_seed_rondo",
                    "enabled": True,
                    "strength": "strong",
                    "cooldown_turns": 3,
                }
            ],
        )
        settings = await db.get_conversation_motif_settings(_STORY_ID)
        assert settings[0]["enabled"] is True
        assert settings[0]["strength"] == "strong"
        assert settings[0]["cooldown_turns"] == 3

        run_id = await db.insert_conversation_motif_run(
            _STORY_ID,
            {
                "motif_id": "solo_seed_rondo",
                "status": "active",
                "stage": "seeded",
                "seed_hook_id": None,
                "seed_log_id": None,
                "owner_char_id": _CHAR_ID,
                "pickup_char_id": None,
                "place_id": "music_room",
                "title": "聞き逃せない独り言",
                "description": "灯里の譜面がまだ残っている。",
                "started_turn": 12,
                "last_advanced_turn": 12,
                "cooldown_until_turn": None,
            },
        )
        runs = await db.get_active_conversation_motif_runs(_STORY_ID)
        assert [run["id"] for run in runs] == [run_id]
        assert runs[0]["stage"] == "seeded"
        assert runs[0]["owner_char_id"] == _CHAR_ID

        await db.update_conversation_motif_run(
            run_id,
            {
                "stage": "picked_up",
                "pickup_char_id": "char_b",
                "last_advanced_turn": 13,
            },
        )
        updated_runs = await db.get_active_conversation_motif_runs(_STORY_ID)
        assert updated_runs[0]["stage"] == "picked_up"
        assert updated_runs[0]["pickup_char_id"] == "char_b"


@async_to_sync
async def test_reset_story_progress_preserves_definitions_and_reseeds_runtime_baseline() -> None:
    """reset_story_progress は定義を残し、進行系データだけを初期化すること。"""
    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            """
            UPDATE stories
            SET season_start = '2026-04-01',
                turn_minutes = 10,
                last_sim_time = '2026-04-02T12:00:00'
            WHERE id = ?;
            """,
            (_STORY_ID,),
        )
        await db._conn.execute(
            """
            INSERT INTO places (id, story_id, label, zone, is_active)
            VALUES ('classroom', ?, '教室', 'school', 1);
            """,
            (_STORY_ID,),
        )
        for char_id in ("char_a", "char_b"):
            await db._conn.execute(
                """
                INSERT INTO characters (
                    id, story_id, name_ja, emotion_default, favorite_places,
                    expressions_available, is_active
                )
                VALUES (?, ?, ?, ?, ?, '["neutral"]', 1);
                """,
                (
                    char_id,
                    _STORY_ID,
                    char_id,
                    '{"stress": 0.12, "motivation": 0.8}',
                    '["classroom"]',
                ),
            )
        await db._conn.execute(
            """
            INSERT INTO character_states (
                char_id, story_id, sim_datetime, turn_number, current_place
            )
            VALUES ('char_a', ?, '2026-04-02T12:00:00', 99, 'old_room');
            """,
            (_STORY_ID,),
        )
        await db._conn.execute(
            """
            INSERT INTO relationships (
                story_id, char_id_from, char_id_to, trust, affinity, tension, familiarity
            )
            VALUES (?, 'char_a', 'char_b', 0.1, 0.2, 0.9, 0.8);
            """,
            (_STORY_ID,),
        )
        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2026-04-02T12:00:00",
                "turn_number": 99,
                "char_id": "char_a",
                "msg_type": "talk",
                "message": "old",
            },
        )
        await db.insert_scene_script(
            _STORY_ID,
            {
                "turn_number": 99,
                "script_text": "古いシーンスクリプト",
            },
        )
        await db._conn.execute(
            """
            INSERT INTO story_memory (story_id, memory_type, summary, trigger_turn)
            VALUES (?, 'event', 'old memory', 99);
            """,
            (_STORY_ID,),
        )
        await db._conn.execute(
            """
            INSERT INTO story_chapters (story_id, chapter_id, title, status, current_beat, opened_turn)
            VALUES (?, 'ch001', '導入', 'active', 'complication', 99);
            """,
            (_STORY_ID,),
        )
        chapter_id = (await (await db._conn.execute("SELECT id FROM story_chapters")).fetchone())[0]
        await db._conn.execute(
            """
            INSERT INTO story_chapter_beats (chapter_db_id, phase, description, status, reached_turn)
            VALUES (?, 'setup', '開始', 'reached', 99);
            """,
            (chapter_id,),
        )
        await db._conn.commit()

        counts = await db.reset_story_progress(_STORY_ID)

        assert counts["chat_logs"] == 1
        assert counts["scene_scripts"] == 1
        story = await db.get_story(_STORY_ID)
        assert story is not None
        assert story["last_sim_time"] is None
        assert len(await db.get_characters(_STORY_ID)) == 2
        assert await db.get_recent_chat_logs(_STORY_ID, limit=10) == []
        assert await db.get_recent_scene_scripts(_STORY_ID, limit=10) == []
        state = await db.get_latest_character_state(_STORY_ID, "char_a")
        assert state is not None
        assert state["sim_datetime"] == "2026-04-01T00:00:00"
        assert state["turn_number"] == 0
        assert state["current_place"] == "classroom"
        assert state["stress"] == pytest.approx(0.12)
        rel = await (
            await db._conn.execute(
                """
                SELECT trust, affinity, tension, familiarity
                FROM relationships
                WHERE story_id = ? AND char_id_from = 'char_a' AND char_id_to = 'char_b';
                """,
                (_STORY_ID,),
            )
        ).fetchone()
        assert dict(rel) == {"trust": 0.5, "affinity": 0.5, "tension": 0.0, "familiarity": 0.5}
        chapter = await db.get_story_chapter_by_chapter_id(_STORY_ID, "ch001")
        assert chapter is not None
        assert chapter["status"] == "pending"
        assert chapter["current_beat"] == "setup"
        beat = (await db.get_chapter_beats(chapter["id"]))[0]
        assert beat["status"] == "pending"
        assert beat["reached_turn"] is None


@async_to_sync
async def test_initialize_uses_fallback_when_connect_hangs() -> None:
    """aiosqlite.connect が停止した場合、threaded backend へ fallback する。"""
    manager = DatabaseManager(
        ":memory:", migrations_dir=MIGRATIONS_DIR, connect_timeout_sec=0.01
    )

    async def _hung_connect(*_args: object, **_kwargs: object) -> object:
        await asyncio.sleep(60)
        return object()

    with patch("db.db_manager.aiosqlite.connect", new=AsyncMock(side_effect=_hung_connect)):
        await manager.initialize()

    assert manager.backend_kind == "sync_sqlite"
    assert manager._conn is not None
    await manager.close()


# ------------------------------------------------------------------
# テスト 2: マイグレーションは重複適用されない
# ------------------------------------------------------------------

@async_to_sync
async def test_migration_applied_once() -> None:
    """2回 initialize() しても schema_version は migration 件数のまま増えない。"""
    async with _make_db() as db:
        # 同じ DB 接続で _apply_migrations を再実行
        await db._apply_migrations()

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT version FROM schema_version ORDER BY version;"
        )
        rows = await cursor.fetchall()
        assert [row[0] for row in rows] == list(range(1, 36))


@async_to_sync
async def test_initialize_repairs_missing_version_for_existing_places_column(
    tmp_path: Path,
) -> None:
    """places.is_active が既にある DB では version 2 を補完して起動できること。"""
    db_path = tmp_path / "dirty.db"
    _create_dirty_db_with_places_is_active(db_path)

    manager = DatabaseManager(str(db_path), migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    try:
        assert manager._conn is not None
        cursor = await manager._conn.execute(
            "SELECT version FROM schema_version ORDER BY version;"
        )
        rows = await cursor.fetchall()
        assert [row[0] for row in rows] == list(range(1, 36))

        cursor = await manager._conn.execute("PRAGMA table_info(places);")
        columns = await cursor.fetchall()
        is_active_columns = [row[1] for row in columns if row[1] == "is_active"]
        assert is_active_columns == ["is_active"]
    finally:
        await manager.close()


@async_to_sync
async def test_initialize_repairs_missing_version_for_existing_conversation_schema(
    tmp_path: Path,
) -> None:
    """会話 schema が既にある DB では version 3 を補完して起動できること。"""
    db_path = tmp_path / "dirty-v3.db"
    _create_dirty_db_with_conversation_schema(db_path)

    manager = DatabaseManager(str(db_path), migrations_dir=MIGRATIONS_DIR)
    await manager.initialize()
    try:
        assert manager._conn is not None
        cursor = await manager._conn.execute(
            "SELECT version FROM schema_version ORDER BY version;"
        )
        rows = await cursor.fetchall()
        assert [row[0] for row in rows] == list(range(1, 36))

        cursor = await manager._conn.execute("PRAGMA table_info(chat_logs);")
        columns = await cursor.fetchall()
        column_names = {row[1] for row in columns}
        assert "conversation_session_id" in column_names
        assert "reply_to_log_id" in column_names
    finally:
        await manager.close()


@async_to_sync
async def test_initialize_logs_db_startup_boundaries(caplog: pytest.LogCaptureFixture) -> None:
    """initialize() が connect / pragmas / migrations の境界ログを出すこと。"""
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR)
    fake_conn = MagicMock()

    with (
        patch("db.db_manager.aiosqlite.connect", AsyncMock(return_value=fake_conn)),
        patch.object(manager, "_set_pragmas", AsyncMock()),
        patch.object(manager, "_apply_migrations", AsyncMock()),
        caplog.at_level(logging.INFO, logger="db.db_manager"),
    ):
        await manager.initialize()

    messages = [record.message for record in caplog.records if record.name == "db.db_manager"]
    assert any("DB startup stage: aiosqlite.connect begin" in message for message in messages)
    assert any("DB startup stage complete: connect" in message for message in messages)
    assert any("DB startup stage: set pragmas" in message for message in messages)
    assert any("DB startup stage complete: set pragmas" in message for message in messages)
    assert any("DB startup stage: apply migrations" in message for message in messages)
    assert any("DB startup stage complete: apply migrations" in message for message in messages)


@async_to_sync
async def test_initialize_falls_back_to_sync_backend_when_aiosqlite_hangs() -> None:
    manager = DatabaseManager(":memory:", migrations_dir=MIGRATIONS_DIR, connect_timeout_sec=0.01)
    fake_conn = MagicMock()
    fake_conn.row_factory = None

    async def _hung_connect(*_args: object, **_kwargs: object) -> object:
        await asyncio.sleep(60)
        return object()

    with (
        patch("db.db_manager.aiosqlite.connect", new=AsyncMock(side_effect=_hung_connect)),
        patch.object(manager, "_connect_sync_backend", AsyncMock(return_value=fake_conn)),
        patch.object(manager, "_set_pragmas", AsyncMock()),
        patch.object(manager, "_apply_migrations", AsyncMock()),
    ):
        await manager.initialize()

    assert manager._conn is fake_conn
    assert manager.backend_kind == "sync_sqlite"


@async_to_sync
async def test_story_hook_crud() -> None:
    """story_hooks を追加し、open 状態の hook を取得できること。"""
    async with _make_db() as db:
        hook_id = await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "promise",
                "status": "open",
                "owner_char_id": "char_a",
                "target_char_id": "char_b",
                "title": "放課後の約束",
                "description": "char_a が char_b に放課後の再会を約束した。",
                "priority": 0.8,
                "due_turn": 12,
            },
        )

        hooks = await db.get_open_story_hooks(_STORY_ID)

        assert len(hooks) == 1
        assert hooks[0]["id"] == hook_id
        assert hooks[0]["hook_type"] == "promise"
        assert hooks[0]["owner_char_id"] == "char_a"
        assert hooks[0]["target_char_id"] == "char_b"


@async_to_sync
async def test_resolve_story_hook_and_get_relevant_hooks() -> None:
    """関連キャラの open hook を取得でき、resolve すると一覧から外れること。"""
    async with _make_db() as db:
        open_hook_id = await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "question",
                "status": "open",
                "owner_char_id": "char_a",
                "target_char_id": "char_b",
                "title": "返答待ち",
                "description": "char_a が char_b に理由を問いかけた。",
                "priority": 0.9,
            },
        )
        await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "promise",
                "status": "open",
                "owner_char_id": "char_c",
                "target_char_id": "char_d",
                "title": "別件",
                "description": "char_c が char_d と約束した。",
                "priority": 0.4,
            },
        )

        relevant = await db.get_relevant_story_hooks(_STORY_ID, "char_b", "music_room", limit=5)

        assert [hook["id"] for hook in relevant] == [open_hook_id]

        resolution_log_id = await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T10:00",
                "turn_number": 6,
                "char_id": "char_b",
                "msg_type": "reply",
                "message": "返答した。",
                "expression": "neutral",
            },
        )

        await db.resolve_story_hook(
            open_hook_id,
            resolution_log_id=resolution_log_id,
            resolved_turn=6,
            summary="char_b が同じ scene で返答した。",
        )

        relevant_after = await db.get_relevant_story_hooks(_STORY_ID, "char_b", "music_room", limit=5)

        assert relevant_after == []
        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT status, resolution_log_id, resolved_turn, resolution_summary FROM story_hooks WHERE id = ?;",
            (open_hook_id,),
        )
        row = await cursor.fetchone()
        assert row["status"] == "resolved"
        assert row["resolution_log_id"] == resolution_log_id
        assert row["resolved_turn"] == 6
        assert row["resolution_summary"] == "char_b が同じ scene で返答した。"


@async_to_sync
async def test_update_relationship_snapshot_and_event() -> None:
    """relationships の trust/tension 更新と relationship_events 追記ができること。"""
    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO relationships (
                story_id, char_id_from, char_id_to, trust, affinity, tension, familiarity
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (_STORY_ID, "char_a", "char_b", 0.5, 0.5, 0.0, 0.5),
        )
        await db._conn.commit()

        await db.update_relationship_snapshot(
            _STORY_ID,
            "char_a",
            "char_b",
            {
                "trust": 0.57,
                "tension": 0.12,
                "last_event_turn": 4,
                "last_event_summary": "励ましに少し心を開いた。",
            },
        )
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "music_room",
                "opened_turn": 4,
            },
        )
        source_log_id = await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:30",
                "turn_number": 4,
                "char_id": "char_a",
                "msg_type": "reply",
                "message": "励ますよ。",
                "expression": "neutral",
                "scene_id": scene_id,
            },
        )
        event_id = await db.insert_relationship_event(
            _STORY_ID,
            {
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "event_type": "support",
                "delta_trust": 0.07,
                "delta_tension": -0.03,
                "summary": "励ましに少し心を開いた。",
                "source_log_id": source_log_id,
                "scene_id": scene_id,
                "turn_number": 4,
            },
        )

        cursor = await db._conn.execute(
            """
            SELECT trust, tension, last_event_turn, last_event_summary
            FROM relationships
            WHERE story_id = ? AND char_id_from = ? AND char_id_to = ?;
            """,
            (_STORY_ID, "char_a", "char_b"),
        )
        row = await cursor.fetchone()
        assert row["trust"] == pytest.approx(0.57)
        assert row["tension"] == pytest.approx(0.12)
        assert row["last_event_turn"] == 4
        assert row["last_event_summary"] == "励ましに少し心を開いた。"

        summaries = await db.get_relationship_summary(_STORY_ID, "char_a", ["char_b"], limit=5)

        assert len(summaries) == 1
        assert "char_b" in summaries[0]
        assert "0.57" in summaries[0]

        cursor = await db._conn.execute(
            """
            SELECT event_type, delta_trust, delta_tension, summary
            FROM relationship_events
            WHERE id = ?;
            """,
            (event_id,),
        )
        event_row = await cursor.fetchone()
        assert event_row["event_type"] == "support"
        assert event_row["delta_trust"] == pytest.approx(0.07)
        assert event_row["delta_tension"] == pytest.approx(-0.03)
        assert event_row["summary"] == "励ましに少し心を開いた。"


@async_to_sync
async def test_insert_generation_quality_issue_persists_json_details() -> None:
    """generation_quality_issues に JSON details を保存できること。"""
    async with _make_db() as db:
        log_id = await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T00:00",
                "turn_number": 7,
                "char_id": "_narrator",
                "msg_type": "narration_scene",
                "message": "品質 issue の参照先ログ",
                "expression": "neutral",
            },
        )
        issue_id = await db.insert_generation_quality_issue(
            _STORY_ID,
            {
                "issue_type": "abstract_repetition",
                "severity": "warning",
                "details": {"token": "誰か", "count": 5},
                "auto_action": "drop",
                "created_turn": 7,
                "log_id": log_id,
            },
        )

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT issue_type, severity, details, auto_action, log_id FROM generation_quality_issues WHERE id = ?;",
            (issue_id,),
        )
        row = await cursor.fetchone()
        assert row["issue_type"] == "abstract_repetition"
        assert row["severity"] == "warning"
        assert json.loads(row["details"]) == {"token": "誰か", "count": 5}
        assert row["auto_action"] == "drop"
        assert row["log_id"] == log_id


@async_to_sync
async def test_character_profile_overlay_upsert_and_get() -> None:
    """character_profile_overlays を upsert し、後から取得できること。"""
    async with _make_db() as db:
        await db.upsert_character_profile_overlay(
            _STORY_ID,
            "char_a",
            {
                "overlay_json": {
                    "current_goal": "誰かに本音を伝える",
                    "speech.tone": "以前より率直",
                },
                "version": 2,
                "last_committed_turn": 11,
            },
        )

        overlay = await db.get_character_profile_overlay(_STORY_ID, "char_a")

        assert overlay is not None
        assert overlay["overlay_json"]["current_goal"] == "誰かに本音を伝える"
        assert overlay["version"] == 2
        assert overlay["last_committed_turn"] == 11


@async_to_sync
async def test_character_growth_candidates_pending_only() -> None:
    """pending 状態の growth candidate だけを取得できること。"""
    async with _make_db() as db:
        await db.insert_character_growth_candidate(
            _STORY_ID,
            "char_a",
            {
                "field": "current_goal",
                "candidate_value": "歌うことから逃げない",
                "reason": "重要な約束を果たした。",
                "experience_score": 0.92,
                "identity_impact_score": 0.7,
                "confidence": 0.8,
                "detected_turn": 9,
                "status": "pending",
            },
        )
        await db.insert_character_growth_candidate(
            _STORY_ID,
            "char_a",
            {
                "field": "current_goal",
                "candidate_value": "保留",
                "reason": "既に適用済み。",
                "experience_score": 0.7,
                "identity_impact_score": 0.4,
                "confidence": 0.6,
                "detected_turn": 5,
                "status": "committed",
            },
        )

        candidates = await db.get_pending_growth_candidates(_STORY_ID, "char_a")

        assert len(candidates) == 1
        assert candidates[0]["candidate_value"] == "歌うことから逃げない"


@async_to_sync
async def test_update_character_growth_candidate_status() -> None:
    """growth candidate の status を更新できること。"""
    async with _make_db() as db:
        candidate_id = await db.insert_character_growth_candidate(
            _STORY_ID,
            "char_a",
            {
                "field": "current_goal",
                "candidate_value": "逃げない",
                "reason": "決意した。",
                "detected_turn": 10,
                "status": "pending",
            },
        )

        await db.update_character_growth_candidate_status(candidate_id, "committed")

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT status FROM character_growth_candidates WHERE id = ?;",
            (candidate_id,),
        )
        row = await cursor.fetchone()
        assert row["status"] == "committed"


@async_to_sync
async def test_apply_growth_batch_commits_candidate_evolution_and_overlay() -> None:
    """growth batch helper は candidate/evolution/overlay を1 transaction で反映する。"""
    async with _make_db() as db:
        result = await db.apply_growth_batch(
            _STORY_ID,
            "char_a",
            {
                "turn_number": 12,
                "candidate_rows": [
                    {
                        "field": "current_goal",
                        "candidate_value": "逃げずに歌う",
                        "reason": "経験が積み重なった。",
                        "experience_score": 0.9,
                        "identity_impact_score": 0.88,
                        "confidence": 0.8,
                        "detected_turn": 12,
                        "status": "pending",
                    }
                ],
                "commit_actions": [
                    {
                        "candidate_index": 0,
                        "field": "current_goal",
                        "candidate_value": "逃げずに歌う",
                        "reason": "経験が積み重なった。",
                        "previous_value": "様子を見る",
                        "source_memory_id": None,
                        "existing_candidate_ids": [],
                    }
                ],
                "overlay_payload": {
                    "overlay_json": {"current_goal": "逃げずに歌う"},
                    "version": 1,
                    "last_committed_turn": 12,
                },
            },
        )

        assert result["inserted_candidate_ids"]
        assert result["committed_candidate_ids"] == result["inserted_candidate_ids"]
        overlay = await db.get_character_profile_overlay(_STORY_ID, "char_a")
        assert overlay is not None
        assert overlay["overlay_json"]["current_goal"] == "逃げずに歌う"

        assert db._conn is not None
        cursor = await db._conn.execute(
            """
            SELECT status
            FROM character_growth_candidates
            WHERE id = ?
            """,
            (result["inserted_candidate_ids"][0],),
        )
        candidate_row = await cursor.fetchone()
        assert candidate_row["status"] == "committed"

        cursor = await db._conn.execute(
            """
            SELECT field, new_value
            FROM character_evolution
            WHERE story_id = ? AND char_id = ?
            """,
            (_STORY_ID, "char_a"),
        )
        evolution_row = await cursor.fetchone()
        assert evolution_row["field"] == "current_goal"
        assert evolution_row["new_value"] == "逃げずに歌う"


@async_to_sync
async def test_apply_growth_batch_rolls_back_partial_writes_on_error() -> None:
    """growth batch helper は途中失敗時に partial write を残さない。"""
    async with _make_db() as db:
        with pytest.raises(IndexError):
            await db.apply_growth_batch(
                _STORY_ID,
                "char_a",
                {
                    "turn_number": 12,
                    "candidate_rows": [
                        {
                            "field": "current_goal",
                            "candidate_value": "逃げずに歌う",
                            "reason": "経験が積み重なった。",
                            "experience_score": 0.9,
                            "identity_impact_score": 0.88,
                            "confidence": 0.8,
                            "detected_turn": 12,
                            "status": "pending",
                        }
                    ],
                    "commit_actions": [
                        {
                            "candidate_index": 99,
                            "field": "current_goal",
                            "candidate_value": "逃げずに歌う",
                            "reason": "経験が積み重なった。",
                            "previous_value": "様子を見る",
                            "source_memory_id": None,
                            "existing_candidate_ids": [],
                        }
                    ],
                    "overlay_payload": {
                        "overlay_json": {"current_goal": "逃げずに歌う"},
                        "version": 1,
                        "last_committed_turn": 12,
                    },
                },
            )

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT COUNT(*) FROM character_growth_candidates WHERE story_id = ?",
            (_STORY_ID,),
        )
        assert (await cursor.fetchone())[0] == 0
        cursor = await db._conn.execute(
            "SELECT COUNT(*) FROM character_evolution WHERE story_id = ?",
            (_STORY_ID,),
        )
        assert (await cursor.fetchone())[0] == 0
        cursor = await db._conn.execute(
            "SELECT COUNT(*) FROM character_profile_overlays WHERE story_id = ?",
            (_STORY_ID,),
        )
        assert (await cursor.fetchone())[0] == 0


@async_to_sync
async def test_apply_growth_batch_applies_additional_status_updates() -> None:
    """growth batch helper は commit 以外の status 更新も transaction 内で反映する。"""
    async with _make_db() as db:
        stale_id = await db.insert_character_growth_candidate(
            _STORY_ID,
            "char_a",
            {
                "field": "current_goal",
                "candidate_value": "様子を見る",
                "reason": "古い候補。",
                "detected_turn": 5,
                "status": "pending",
            },
        )
        replaced_id = await db.insert_character_growth_candidate(
            _STORY_ID,
            "char_a",
            {
                "field": "current_goal",
                "candidate_value": "保留する",
                "reason": "競合候補。",
                "detected_turn": 10,
                "status": "pending",
            },
        )

        await db.apply_growth_batch(
            _STORY_ID,
            "char_a",
            {
                "turn_number": 12,
                "candidate_rows": [
                    {
                        "field": "current_goal",
                        "candidate_value": "逃げずに歌う",
                        "reason": "経験が積み重なった。",
                        "experience_score": 0.9,
                        "identity_impact_score": 0.88,
                        "confidence": 0.8,
                        "detected_turn": 12,
                        "status": "pending",
                    }
                ],
                "commit_actions": [
                    {
                        "candidate_index": 0,
                        "field": "current_goal",
                        "candidate_value": "逃げずに歌う",
                        "reason": "経験が積み重なった。",
                        "previous_value": "様子を見る",
                        "source_memory_id": None,
                        "existing_candidate_ids": [],
                    }
                ],
                "status_updates": [
                    {"candidate_ids": [stale_id], "status": "expired"},
                    {"candidate_ids": [replaced_id], "status": "superseded"},
                ],
                "overlay_payload": {
                    "overlay_json": {"current_goal": "逃げずに歌う"},
                    "version": 1,
                    "last_committed_turn": 12,
                },
            },
        )

        assert db._conn is not None
        cursor = await db._conn.execute(
            """
            SELECT id, status
            FROM character_growth_candidates
            WHERE story_id = ? AND char_id = ?
            ORDER BY id ASC
            """,
            (_STORY_ID, "char_a"),
        )
        rows = await cursor.fetchall()
        statuses = {int(row["id"]): str(row["status"]) for row in rows}
        assert statuses[stale_id] == "expired"
        assert statuses[replaced_id] == "superseded"


@async_to_sync
async def test_relationship_modes_allow_two_active_modes_for_same_pair() -> None:
    """同一 pair でも mode_type が異なれば active row を共存できること。"""
    async with _make_db() as db:
        first_id = await db.insert_relationship_mode(
            _STORY_ID,
            "char_a",
            "char_b",
            {
                "mode_type": "irritated_respect",
                "status": "active",
                "summary": "張り合っている。",
                "confidence": 0.7,
                "intensity": 0.6,
                "first_detected_turn": 10,
                "last_reinforced_turn": 10,
            },
        )
        second_id = await db.insert_relationship_mode(
            _STORY_ID,
            "char_a",
            "char_b",
            {
                "mode_type": "cannot_ignore",
                "status": "active",
                "summary": "放っておけない。",
                "confidence": 0.68,
                "intensity": 0.52,
                "first_detected_turn": 10,
                "last_reinforced_turn": 10,
            },
        )

        assert first_id != second_id
        pair_modes = await db.get_active_relationship_modes_pair(_STORY_ID, "char_a", "char_b")
        assert {row["mode_type"] for row in pair_modes} == {"irritated_respect", "cannot_ignore"}


@async_to_sync
async def test_story_canon_reignition_fields_and_hook_lookup() -> None:
    """canon reignition state と source_canon_bit_id hook を保存・取得できること。"""
    async with _make_db() as db:
        bit_id = await db.insert_story_canon_bit(
            _STORY_ID,
            {
                "bit_type": "pair_dynamic",
                "motif_key": "status_clash",
                "canon_level": "recurring_bit",
                "status": "active",
                "title": "張り合う二人",
                "summary": "張り合いが戻りやすい。",
                "focus_char_ids": ["char_a", "char_b"],
                "dedupe_key": "pair:char_a:char_b:status_clash",
                "evidence_sources": ["pattern"],
                "first_detected_turn": 10,
                "last_reinforced_turn": 10,
                "recurrence_count": 2,
                "last_reignited_turn": 12,
                "reignition_count": 1,
            },
        )
        hook_id = await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "conflict",
                "status": "open",
                "owner_char_id": "char_a",
                "target_char_id": "char_b",
                "title": "前にも起きた張り合いが戻る",
                "description": "char_a と char_b の張り合いが戻りやすい。",
                "priority": 0.7,
                "source_canon_bit_id": bit_id,
            },
        )

        active_bits = await db.get_reignitable_story_canon_bits(_STORY_ID)
        open_hooks = await db.get_open_hooks_for_canon_bit(_STORY_ID, bit_id)

        assert active_bits[0]["id"] == bit_id
        assert active_bits[0]["last_reignited_turn"] == 12
        assert active_bits[0]["reignition_count"] == 1
        assert open_hooks[0]["id"] == hook_id
        assert open_hooks[0]["source_canon_bit_id"] == bit_id


@async_to_sync
async def test_character_canon_overlay_crud_and_writeback_fields() -> None:
    """canon overlay と writeback state を保存・取得できること。"""
    async with _make_db() as db:
        bit_id = await db.insert_story_canon_bit(
            _STORY_ID,
            {
                "bit_type": "pair_dynamic",
                "motif_key": "status_clash",
                "canon_level": "proto_canon",
                "status": "active",
                "title": "張り合う二人",
                "summary": "張り合いが固まってきた。",
                "focus_char_ids": ["char_a", "char_b"],
                "dedupe_key": "pair:char_a:char_b:status_clash",
                "evidence_sources": ["pattern"],
                "first_detected_turn": 10,
                "last_reinforced_turn": 12,
                "recurrence_count": 3,
                "last_writeback_turn": 14,
                "writeback_count": 2,
            },
        )

        eligible_bits = await db.get_writeback_eligible_canon_bits(_STORY_ID)
        assert eligible_bits[0]["id"] == bit_id
        assert eligible_bits[0]["last_writeback_turn"] == 14
        assert eligible_bits[0]["writeback_count"] == 2

        await db.upsert_character_canon_overlay(
            _STORY_ID,
            "char_a",
            {
                "overlay_json": {
                    "current_goal": "char_bとの勝ち負けをはっきりさせたい",
                    "current_worry": "char_bに押し切られるのは避けたい",
                },
                "version": 3,
                "last_written_turn": 14,
                "source_canon_bit_ids": [bit_id],
            },
        )
        overlay = await db.get_character_canon_overlay(_STORY_ID, "char_a")
        assert overlay is not None
        assert overlay["overlay_json"]["current_goal"] == "char_bとの勝ち負けをはっきりさせたい"
        assert overlay["version"] == 3
        assert overlay["source_canon_bit_ids"] == [bit_id]

        evolution_id = await db.insert_evolution(
            _STORY_ID,
            {
                "char_id": "char_a",
                "turn_number": 14,
                "field": "current_goal",
                "previous_value": "穏やかに過ごしたい",
                "new_value": "char_bとの勝ち負けをはっきりさせたい",
                "reason": "canon_writeback: current_goal updated from stable canon",
                "source_canon_bit_id": bit_id,
            },
        )
        assert evolution_id >= 1

        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT source_canon_bit_id FROM character_evolution WHERE id = ?",
            (evolution_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == bit_id

        await db.delete_character_canon_overlay(_STORY_ID, "char_a")
        assert await db.get_character_canon_overlay(_STORY_ID, "char_a") is None


@async_to_sync
async def test_get_recent_relationship_events_for_character() -> None:
    """relationship_events を char 単位・turn 範囲で取得できること。"""
    async with _make_db() as db:
        source_log_id = await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:30",
                "turn_number": 4,
                "char_id": "char_a",
                "msg_type": "reply",
                "message": "励ますよ。",
                "expression": "neutral",
            },
        )
        await db.insert_relationship_event(
            _STORY_ID,
            {
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "event_type": "support",
                "delta_trust": 0.07,
                "summary": "少し心を開いた。",
                "source_log_id": source_log_id,
                "turn_number": 4,
            },
        )
        await db.insert_relationship_event(
            _STORY_ID,
            {
                "char_id_from": "char_c",
                "char_id_to": "char_a",
                "event_type": "conflict",
                "delta_tension": 0.08,
                "summary": "険悪になった。",
                "source_log_id": source_log_id,
                "turn_number": 8,
            },
        )

        events = await db.get_recent_relationship_events_for_character(
            _STORY_ID,
            "char_a",
            since_turn=5,
            limit=5,
        )

        assert len(events) == 1
        assert events[0]["event_type"] == "conflict"


@async_to_sync
async def test_story_scene_crud() -> None:
    """story_scenes を作成し、close まで行えること。"""
    async with _make_db() as db:
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "music_room",
                "focus_char_ids": ["char_a", "char_b"],
                "objective": "放課後の会話",
                "opened_turn": 3,
            },
        )

        scenes = await db.get_active_story_scenes(_STORY_ID)
        assert len(scenes) == 1
        assert scenes[0]["id"] == scene_id
        assert scenes[0]["focus_char_ids"] == ["char_a", "char_b"]

        await db.close_story_scene(scene_id, outcome_type="resolved", outcome_summary="会話が一段落した。", closed_turn=5)
        scenes = await db.get_active_story_scenes(_STORY_ID)
        assert scenes == []


@async_to_sync
async def test_get_story_scene_and_scene_logs() -> None:
    """scene 本体と scene に紐づく chat_logs を取得できること。"""
    async with _make_db() as db:
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "music_room",
                "opened_turn": 3,
            },
        )
        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T09:00",
                "turn_number": 3,
                "char_id": "char_a",
                "msg_type": "reply",
                "message": "約束はまだ終わってない。",
                "expression": "neutral",
                "scene_id": scene_id,
            },
        )

        scene = await db.get_story_scene(scene_id)
        logs = await db.get_scene_logs(scene_id, limit=10)

        assert scene is not None
        assert scene["place_id"] == "music_room"
        assert len(logs) == 1
        assert logs[0]["scene_id"] == scene_id


@async_to_sync
async def test_get_recent_closed_story_scenes() -> None:
    """closed scene helper が scene_type と turn window で絞って返せること。"""
    async with _make_db() as db:
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "music_room",
                "opened_turn": 3,
            },
        )
        other_scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "solo",
                "status": "active",
                "place_id": "rooftop",
                "opened_turn": 3,
            },
        )
        await db.close_story_scene(
            scene_id,
            outcome_type="carry_over",
            outcome_summary="対立が未解決のまま残った。",
            closed_turn=6,
        )
        await db.close_story_scene(
            other_scene_id,
            outcome_type="pause",
            outcome_summary="一人で考え込んだ。",
            closed_turn=7,
        )

        scenes = await db.get_recent_closed_story_scenes(
            _STORY_ID,
            since_turn=5,
            limit=5,
            scene_type="conversation",
        )

        assert len(scenes) == 1
        assert scenes[0]["id"] == scene_id
        assert scenes[0]["scene_type"] == "conversation"


@async_to_sync
async def test_get_open_story_hooks_for_scene() -> None:
    """source_scene_id で scene scoped な open hook を取得できること。"""
    async with _make_db() as db:
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "music_room",
                "opened_turn": 3,
            },
        )
        hook_id = await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "promise",
                "status": "open",
                "owner_char_id": "char_a",
                "target_char_id": "char_b",
                "title": "放課後の約束",
                "description": "放課後に屋上で会う約束が残っている。",
                "priority": 0.8,
                "source_scene_id": scene_id,
            },
        )
        other_hook_id = await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "conflict",
                "status": "open",
                "owner_char_id": "char_a",
                "target_char_id": "char_c",
                "title": "別の対立",
                "description": "別シーン由来の対立。",
                "priority": 0.7,
            },
        )

        hooks = await db.get_open_story_hooks_for_scene(_STORY_ID, scene_id)

        assert [hook["id"] for hook in hooks] == [hook_id]
        assert all(hook["id"] != other_hook_id for hook in hooks)


@async_to_sync
async def test_get_relevant_story_hooks_includes_scene_scoped_open_hook_for_active_place() -> None:
    """target なしでも active scene と place が一致すれば scene-scoped hook を返す。"""
    async with _make_db() as db:
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "music_room",
                "opened_turn": 3,
            },
        )
        scene_hook_id = await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "conflict",
                "status": "open",
                "owner_char_id": "char_a",
                "target_char_id": None,
                "title": "路線対立",
                "description": "売れる音に寄せるかどうかで場が割れている。",
                "priority": 0.8,
                "source_scene_id": scene_id,
            },
        )
        direct_hook_id = await db.insert_story_hook(
            _STORY_ID,
            {
                "hook_type": "promise",
                "status": "open",
                "owner_char_id": "char_c",
                "target_char_id": "char_b",
                "title": "返答待ち",
                "description": "char_c が char_b に返事を求めている。",
                "priority": 0.7,
            },
        )

        relevant = await db.get_relevant_story_hooks(_STORY_ID, "char_b", "music_room", limit=5)

        assert [hook["id"] for hook in relevant] == [scene_hook_id, direct_hook_id]


@async_to_sync
async def test_scene_participants_replace_and_record_turn() -> None:
    """scene_participants を置き換え、発話実績を更新できること。"""
    async with _make_db() as db:
        scene_id = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "place_id": "music_room",
                "focus_char_ids": ["char_a"],
                "opened_turn": 1,
            },
        )
        await db.replace_scene_participants(
            scene_id,
            [
                {"char_id": "char_a", "role": "focus"},
                {"char_id": "char_b", "role": "support"},
                {"char_id": "char_c", "role": "observer"},
            ],
        )
        await db.record_scene_participant_turn(scene_id, "char_b", turn_number=4)

        participants = await db.get_scene_participants(scene_id)

        assert [p["char_id"] for p in participants] == ["char_a", "char_b", "char_c"]
        char_b = next(p for p in participants if p["char_id"] == "char_b")
        assert char_b["times_spoken"] == 1
        assert char_b["last_spoken_turn"] == 4


# ------------------------------------------------------------------
# テスト 3: chat_log 挿入 → 未投稿ログ取得
# ------------------------------------------------------------------

@async_to_sync
async def test_insert_and_get_chat_log() -> None:
    """insert_chat_log 後に get_unposted_logs で取得できること。"""
    async with _make_db() as db:
        log = {
            "sim_datetime": "2025-04-01T10:00:00",
            "char_id": _CHAR_ID,
            "msg_type": "monologue",
            "message": "今日もいい天気だ。",
            "conversation_session_id": 42,
            "reply_to_log_id": 24,
            "llm_provider": "ollama",
            "llm_model": "qwen2.5:14b",
        }
        new_id = await db.insert_chat_log(_STORY_ID, log)
        assert isinstance(new_id, int)
        assert new_id > 0

        logs = await db.get_unposted_logs(_STORY_ID, limit=10)
        assert len(logs) == 1
        assert logs[0]["id"] == new_id
        assert logs[0]["message"] == "今日もいい天気だ。"
        assert logs[0]["posted_to_web"] == 0
        assert logs[0]["conversation_session_id"] == 42
        assert logs[0]["reply_to_log_id"] == 24


# ------------------------------------------------------------------
# テスト 4: mark_logs_posted で posted_to_web が 1 になる
# ------------------------------------------------------------------

@async_to_sync
async def test_mark_logs_posted() -> None:
    """mark_logs_posted 後、get_unposted_logs で対象が返らなくなること。"""
    async with _make_db() as db:
        log = {
            "sim_datetime": "2025-04-01T10:05:00",
            "char_id": _CHAR_ID,
            "msg_type": "monologue",
            "message": "ひとりごと。",
        }
        new_id = await db.insert_chat_log(_STORY_ID, log)

        await db.mark_logs_posted(_STORY_ID, [new_id])

        # 未投稿一覧には出てこないはず
        unposted = await db.get_unposted_logs(_STORY_ID)
        assert all(row["id"] != new_id for row in unposted)

        # DB 直接確認
        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT posted_to_web FROM chat_logs WHERE id = ?;", (new_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1


@async_to_sync
async def test_conversation_session_lifecycle() -> None:
    """conversation_sessions を作成・更新・終了できること。"""
    async with _make_db() as db:
        session_id = await db.insert_conversation_session(
            _STORY_ID,
            {
                "place_id": "music_room",
                "participant_ids": ["char_a", "char_b"],
                "status": "active",
                "started_at_sim_datetime": "2025-04-01T10:00:00",
                "last_activity_sim_datetime": "2025-04-01T10:00:00",
                "last_turn_number": 1,
            },
        )

        active = await db.get_active_conversation_sessions(_STORY_ID)
        assert len(active) == 1
        assert active[0]["id"] == session_id
        assert active[0]["participant_ids"] == ["char_a", "char_b"]

        by_place = await db.get_active_conversation_session(_STORY_ID, "music_room")
        assert by_place is not None
        assert by_place["id"] == session_id

        await db.update_conversation_session(
            session_id,
            {
                "participant_ids": ["char_a", "char_b", "char_c"],
                "last_speaker_id": "char_b",
                "last_log_id": 7,
                "last_activity_sim_datetime": "2025-04-01T10:30:00",
                "last_turn_number": 2,
            },
        )

        updated = await db.get_active_conversation_session(_STORY_ID, "music_room")
        assert updated is not None
        assert updated["participant_ids"] == ["char_a", "char_b", "char_c"]
        assert updated["last_speaker_id"] == "char_b"
        assert updated["last_log_id"] == 7
        assert updated["last_turn_number"] == 2

        await db.close_conversation_session(
            session_id,
            last_activity_sim_datetime="2025-04-01T11:00:00",
            last_turn_number=3,
        )

        assert await db.get_active_conversation_session(_STORY_ID, "music_room") is None


@async_to_sync
async def test_get_conversation_seed_logs_and_attach_to_session() -> None:
    """同 turn/place の既存ログを seed 取得し session に紐づけられること。"""
    async with _make_db() as db:
        first_id = await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T10:00:00",
                "turn_number": 1,
                "char_id": "char_a",
                "msg_type": "monologue",
                "place_id": "music_room",
                "message": "先行発言A",
            },
        )
        second_id = await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T10:00:00",
                "turn_number": 1,
                "char_id": "char_b",
                "msg_type": "monologue",
                "place_id": "music_room",
                "message": "先行発言B",
            },
        )

        logs = await db.get_conversation_seed_logs(
            _STORY_ID,
            "music_room",
            "2025-04-01T10:00:00",
            1,
            ["char_a", "char_b"],
        )
        assert [row["id"] for row in logs] == [first_id, second_id]

        await db.attach_logs_to_conversation_session([first_id, second_id], 9)

        updated = await db.get_chat_logs(_STORY_ID)
        attached = [
            row for row in updated
            if row["id"] in {first_id, second_id}
        ]
        assert {row["conversation_session_id"] for row in attached} == {9}


# ------------------------------------------------------------------
# テスト 5: character_state 挿入 → 最新状態取得
# ------------------------------------------------------------------

@async_to_sync
async def test_insert_character_state() -> None:
    """insert_character_state 後に get_latest_character_state で取得できること。"""
    async with _make_db() as db:
        state = {
            "char_id": _CHAR_ID,
            "sim_datetime": "2025-04-01T10:00:00",
            "current_place": "school_roof",
            "stress": 0.4,
            "motivation": 0.6,
            "loneliness": 0.3,
            "excitement": 0.5,
        }
        await db.insert_character_state(_STORY_ID, state)

        result = await db.get_latest_character_state(_STORY_ID, _CHAR_ID)
        assert result is not None
        assert result["current_place"] == "school_roof"
        assert result["stress"] == pytest.approx(0.4)


# ------------------------------------------------------------------
# テスト 6: 複数挿入後も recorded_at 最新の1件のみ返る
# ------------------------------------------------------------------

@async_to_sync
async def test_get_latest_character_state_multiple() -> None:
    """複数の character_state を挿入後、最新1件だけ返ること。"""
    async with _make_db() as db:
        for i in range(3):
            state = {
                "char_id": _CHAR_ID,
                "sim_datetime": f"2025-04-01T1{i}:00:00",
                "current_place": f"place_{i}",
            }
            await db.insert_character_state(_STORY_ID, state)
            # recorded_at の順序を確実にするために少し待つ
            await asyncio.sleep(0.01)

        result = await db.get_latest_character_state(_STORY_ID, _CHAR_ID)
        assert result is not None
        assert result["current_place"] == "place_2"


# ------------------------------------------------------------------
# テスト 7: memories 挿入 → prune で古い順に削除される
# ------------------------------------------------------------------

@async_to_sync
async def test_insert_and_prune_memories() -> None:
    """limit 超過時に古い順から prune されること。"""
    async with _make_db() as db:
        for i in range(5):
            await db.insert_memory(
                _STORY_ID, _CHAR_ID, f"記憶{i}", importance=0.5
            )
            await asyncio.sleep(0.01)  # created_at の順序を確保

        # 3件に切り詰める
        await db.prune_memories(_STORY_ID, _CHAR_ID, limit=3)

        memories = await db.get_recent_memories(_STORY_ID, _CHAR_ID, limit=10)
        assert len(memories) == 3
        # 新しい順で返るので、残るのは記憶2・3・4
        contents = {m["content"] for m in memories}
        assert "記憶0" not in contents
        assert "記憶1" not in contents
        assert "記憶4" in contents


# ------------------------------------------------------------------
# テスト 8: set_story_active でフラグが 0 になる
# ------------------------------------------------------------------

@async_to_sync
async def test_story_active_flag() -> None:
    """set_story_active(False) で is_active が 0 に更新されること。"""
    async with _make_db() as db:
        story = await db.get_story(_STORY_ID)
        assert story is not None
        assert story["is_active"] == 1

        await db.set_story_active(_STORY_ID, False)

        story = await db.get_story(_STORY_ID)
        assert story is not None
        assert story["is_active"] == 0


# ------------------------------------------------------------------
# テスト 9〜12: story_memory CRUD
# ------------------------------------------------------------------

@async_to_sync
async def test_save_and_get_story_memory() -> None:
    """save_story_memory → get_relevant_story_memories で取得できること。"""
    async with _make_db() as db:
        memory_id = await db.save_story_memory(
            _STORY_ID,
            {
                "memory_type": "scene_summary",
                "summary": "アリスとボブが図書館で出会い、秘密の話をした。",
                "involved_chars": [_CHAR_ID, "char_b"],
                "trigger_turn": 5,
                "importance": 0.8,
                "emotional_tone": "mysterious",
            },
        )
        assert isinstance(memory_id, int)
        assert memory_id > 0

        results = await db.get_relevant_story_memories(_STORY_ID, _CHAR_ID, limit=5)
        assert len(results) == 1
        row = results[0]
        assert row["memory_type"] == "scene_summary"
        assert _CHAR_ID in row["involved_chars"]
        assert row["importance"] == pytest.approx(0.8)


@async_to_sync
async def test_get_relevant_story_memories_filters_by_char() -> None:
    """involved_chars に含まれないキャラは、importance < 0.7 なら取得されないこと。"""
    async with _make_db() as db:
        # char_a が involved_chars に含まれる → 取得される
        await db.save_story_memory(
            _STORY_ID,
            {
                "memory_type": "scene_summary",
                "summary": "char_a が登場するシーン。",
                "involved_chars": [_CHAR_ID],
                "trigger_turn": 1,
                "importance": 0.5,
            },
        )
        # char_a が involved 外 + importance 低め → 取得されない
        await db.save_story_memory(
            _STORY_ID,
            {
                "memory_type": "conflict",
                "summary": "char_b だけのシーン。",
                "involved_chars": ["char_b"],
                "trigger_turn": 2,
                "importance": 0.4,
            },
        )
        # importance >= 0.7 → involved_chars 問わず取得される
        await db.save_story_memory(
            _STORY_ID,
            {
                "memory_type": "resolution",
                "summary": "重要なシーン（全員対象）。",
                "involved_chars": ["char_b"],
                "trigger_turn": 3,
                "importance": 0.9,
            },
        )

        results = await db.get_relevant_story_memories(_STORY_ID, _CHAR_ID, limit=10)
        summaries = [r["summary"] for r in results]
        assert "char_a が登場するシーン。" in summaries
        assert "重要なシーン（全員対象）。" in summaries
        assert "char_b だけのシーン。" not in summaries


@async_to_sync
async def test_get_story_memories_since_turn() -> None:
    """since_turn 以降のメモリだけが返ること。"""
    async with _make_db() as db:
        for turn, summary in [(1, "ターン1"), (5, "ターン5"), (10, "ターン10")]:
            await db.save_story_memory(
                _STORY_ID,
                {
                    "memory_type": "scene_summary",
                    "summary": summary,
                    "involved_chars": [],
                    "trigger_turn": turn,
                    "importance": 0.5,
                },
            )

        results = await db.get_story_memories_since_turn(
            _STORY_ID, since_turn=5, limit=10
        )
        summaries = [r["summary"] for r in results]
        assert "ターン1" not in summaries
        assert "ターン5" in summaries
        assert "ターン10" in summaries


@async_to_sync
async def test_story_memory_involved_chars_decoded() -> None:
    """involved_chars は list として返ること（JSON から自動デコード）。"""
    async with _make_db() as db:
        await db.save_story_memory(
            _STORY_ID,
            {
                "memory_type": "scene_summary",
                "summary": "テスト",
                "involved_chars": [_CHAR_ID, "char_b", "char_c"],
                "trigger_turn": 1,
                "importance": 0.6,
            },
        )
        results = await db.get_story_memories_since_turn(
            _STORY_ID, since_turn=0, limit=5
        )
        assert len(results) == 1
        assert isinstance(results[0]["involved_chars"], list)
        assert results[0]["involved_chars"] == [_CHAR_ID, "char_b", "char_c"]


# ------------------------------------------------------------------
# テスト 13〜18: narrative_tensions / director_interventions / character_evolution
# ------------------------------------------------------------------

@async_to_sync
async def test_insert_and_get_active_tensions() -> None:
    """insert_tension → get_active_tensions で取得でき、involved_chars が list に戻ること。"""
    async with _make_db() as db:
        tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "romantic",
                "description": "アリスとボブの間に緊張が走る。",
                "involved_chars": [_CHAR_ID, "char_b"],
                "intensity": 0.7,
                "detected_turn": 3,
            },
        )
        assert isinstance(tension_id, int)
        assert tension_id > 0

        active = await db.get_active_tensions(_STORY_ID)
        assert len(active) == 1
        assert active[0]["id"] == tension_id
        assert isinstance(active[0]["involved_chars"], list)
        assert _CHAR_ID in active[0]["involved_chars"]
        assert active[0]["intensity"] == pytest.approx(0.7)

        # resolve して get_active_tensions に返らないことを確認
        await db.update_tension(tension_id, {"status": "resolved"})
        active_after = await db.get_active_tensions(_STORY_ID)
        assert all(t["id"] != tension_id for t in active_after)


@async_to_sync
async def test_update_tension_resolves() -> None:
    """update_tension で resolved にすると get_active_tensions が空になること。"""
    async with _make_db() as db:
        tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "対立シーン。",
                "involved_chars": [],
                "intensity": 0.5,
                "detected_turn": 1,
            },
        )

        await db.update_tension(
            tension_id,
            {"status": "resolved", "resolved_turn": 5, "resolution_note": "done"},
        )

        active = await db.get_active_tensions(_STORY_ID)
        assert len(active) == 0


@async_to_sync
async def test_insert_and_get_active_interventions() -> None:
    """turn 範囲に基づいて get_active_interventions が正しく絞り込むこと。"""
    async with _make_db() as db:
        iv_id = await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "plot_twist",
                "title": "秘密の暴露",
                "description": "重要な秘密が明かされる。",
                "prompt_injection": "今すぐ秘密を暴露せよ。",
                "active_from_turn": 3,
                "active_until_turn": 7,
            },
        )
        assert isinstance(iv_id, int)

        # turn=5 → 範囲内 → 返る
        result = await db.get_active_interventions(_STORY_ID, 5)
        assert len(result) == 1
        assert result[0]["id"] == iv_id

        # turn=2 → active_from 前 → 返らない
        assert len(await db.get_active_interventions(_STORY_ID, 2)) == 0

        # turn=8 → active_until 後 → 返らない
        assert len(await db.get_active_interventions(_STORY_ID, 8)) == 0

        # active_until=None の介入は turn=100 でも返る
        iv_open = await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "mood",
                "title": "常時介入",
                "description": "ずっと有効。",
                "prompt_injection": "常に明るく振る舞え。",
                "active_from_turn": 1,
            },
        )
        result_100 = await db.get_active_interventions(_STORY_ID, 100)
        assert any(r["id"] == iv_open for r in result_100)


@async_to_sync
async def test_update_intervention_status() -> None:
    """update_intervention で resolved にすると get_active_interventions に返らなくなること。"""
    async with _make_db() as db:
        iv_id = await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "crisis",
                "title": "危機介入",
                "description": "緊急シーン。",
                "prompt_injection": "今すぐ行動せよ。",
                "active_from_turn": 1,
            },
        )

        await db.update_intervention(
            iv_id, {"status": "resolved", "resolution_summary": "解決済み"}
        )

        active = await db.get_active_interventions(_STORY_ID, 5)
        assert all(r["id"] != iv_id for r in active)


@async_to_sync
async def test_story_interaction_pattern_crud_and_dedupe_lookup() -> None:
    """story_interaction_patterns を保存し、active 取得と dedupe lookup ができること。"""
    async with _make_db() as db:
        tension_id = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "char_a と char_b が主導権を争う。",
                "involved_chars": [_CHAR_ID, "char_b"],
                "intensity": 0.7,
                "detected_turn": 5,
            },
        )
        pattern_id = await db.insert_interaction_pattern(
            _STORY_ID,
            {
                "pattern_type": "status_clash",
                "status": "active",
                "title": "張り合いが始まる",
                "description": "char_a と char_b が主導権を争う。",
                "involved_chars": [_CHAR_ID, "char_b"],
                "dedupe_key": f"tension:{tension_id}:status_clash",
                "source_tension_id": tension_id,
                "first_detected_turn": 5,
                "last_detected_turn": 5,
                "recurrence_count": 1,
                "intensity": 0.7,
                "confidence": 0.8,
            },
        )
        assert isinstance(pattern_id, int)

        active = await db.get_active_interaction_patterns(_STORY_ID)
        assert len(active) == 1
        assert active[0]["id"] == pattern_id
        assert active[0]["pattern_type"] == "status_clash"
        assert active[0]["involved_chars"] == [_CHAR_ID, "char_b"]

        matched = await db.find_active_interaction_pattern_by_dedupe_key(
            _STORY_ID,
            f"tension:{tension_id}:status_clash",
        )
        assert matched is not None
        assert matched["id"] == pattern_id

        await db.update_interaction_pattern(
            pattern_id,
            {
                "last_detected_turn": 8,
                "recurrence_count": 2,
            },
        )
        updated = await db.find_active_interaction_pattern_by_dedupe_key(
            _STORY_ID,
            f"tension:{tension_id}:status_clash",
        )
        assert updated is not None
        assert updated["last_detected_turn"] == 8
        assert updated["recurrence_count"] == 2

        await db.resolve_interaction_pattern(
            pattern_id,
            resolved_turn=9,
            resolution_note="対立が一旦収まった。",
        )
        assert await db.get_active_interaction_patterns(_STORY_ID) == []


@async_to_sync
async def test_story_canon_bit_crud_and_dedupe_lookup() -> None:
    """story_canon_bits を保存し、active 取得と dedupe lookup ができること。"""
    async with _make_db() as db:
        bit_id = await db.insert_story_canon_bit(
            _STORY_ID,
            {
                "bit_type": "pair_dynamic",
                "motif_key": "irritated_respect",
                "canon_level": "momentary_bit",
                "status": "active",
                "title": "ぶつかり合う二人",
                "summary": "char_a と char_b は張り合いに戻りやすい。",
                "focus_char_ids": [_CHAR_ID, "char_b"],
                "focus_place_id": "music_room",
                "dedupe_key": "pair:char_a:char_b:irritated_respect",
                "evidence_sources": ["relationship_mode", "pattern"],
                "first_detected_turn": 5,
                "last_reinforced_turn": 5,
                "recurrence_count": 1,
                "confidence": 0.8,
                "novelty": 0.6,
                "intent_alignment": 0.8,
            },
        )
        assert isinstance(bit_id, int)

        active = await db.get_active_story_canon_bits(_STORY_ID)
        assert len(active) == 1
        assert active[0]["id"] == bit_id
        assert active[0]["focus_char_ids"] == [_CHAR_ID, "char_b"]
        assert active[0]["evidence_sources"] == ["relationship_mode", "pattern"]

        matched = await db.find_active_story_canon_bit_by_dedupe_key(
            _STORY_ID,
            "pair:char_a:char_b:irritated_respect",
        )
        assert matched is not None
        assert matched["id"] == bit_id

        await db.update_story_canon_bit(
            bit_id,
            {
                "canon_level": "recurring_bit",
                "recurrence_count": 2,
            },
        )
        updated = await db.find_active_story_canon_bit_by_dedupe_key(
            _STORY_ID,
            "pair:char_a:char_b:irritated_respect",
        )
        assert updated is not None
        assert updated["canon_level"] == "recurring_bit"
        assert updated["recurrence_count"] == 2

        await db.archive_story_canon_bit(bit_id)
        assert await db.get_active_story_canon_bits(_STORY_ID) == []


@async_to_sync
async def test_story_dramatic_pressure_crud_and_dedupe_lookup() -> None:
    """story_dramatic_pressures を保存し、active 取得と dedupe lookup ができること。"""
    async with _make_db() as db:
        pressure_id = await db.insert_story_dramatic_pressure(
            _STORY_ID,
            {
                "pressure_type": "status_flashpoint",
                "status": "active",
                "title": "張り合いの火花",
                "summary": "char_a と char_b の張り合いを押すべき局面。",
                "focus_char_ids": [_CHAR_ID, "char_b"],
                "focus_place_id": "music_room",
                "dedupe_key": "tension:1:status_flashpoint",
                "first_detected_turn": 8,
                "last_detected_turn": 8,
                "recurrence_count": 1,
                "score": 0.82,
                "urgency": 0.8,
                "payoff_ready": 0.7,
                "intent_alignment": 0.8,
            },
        )
        assert isinstance(pressure_id, int)

        active = await db.get_active_story_dramatic_pressures(_STORY_ID)
        assert len(active) == 1
        assert active[0]["id"] == pressure_id
        assert active[0]["focus_char_ids"] == [_CHAR_ID, "char_b"]

        matched = await db.find_active_story_dramatic_pressure_by_dedupe_key(
            _STORY_ID,
            "tension:1:status_flashpoint",
        )
        assert matched is not None
        assert matched["id"] == pressure_id

        await db.update_story_dramatic_pressure(
            pressure_id,
            {
                "recurrence_count": 2,
                "score": 0.9,
            },
        )
        updated = await db.find_active_story_dramatic_pressure_by_dedupe_key(
            _STORY_ID,
            "tension:1:status_flashpoint",
        )
        assert updated is not None
        assert updated["recurrence_count"] == 2
        assert updated["score"] == pytest.approx(0.9)

        await db.resolve_story_dramatic_pressure(
            pressure_id,
            resolved_turn=10,
            resolution_note="一旦押し切った。",
        )
        assert await db.get_active_story_dramatic_pressures(_STORY_ID) == []


@async_to_sync
async def test_get_active_interventions_includes_acknowledged() -> None:
    """acknowledged は live intervention として返ること。"""
    async with _make_db() as db:
        iv_id = await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "mood",
                "title": "気配",
                "description": "誰かが息をひそめている。",
                "prompt_injection": "誰かが息をひそめている気配がある。",
                "active_from_turn": 1,
                "status": "acknowledged",
            },
        )

        active = await db.get_active_interventions(_STORY_ID, 5)

    assert len(active) == 1
    assert active[0]["id"] == iv_id
    assert active[0]["status"] == "acknowledged"


@async_to_sync
async def test_expire_interventions_before_turn_updates_status() -> None:
    """期限切れの live intervention を expired に更新できること。"""
    async with _make_db() as db:
        iv_id = await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "crisis",
                "title": "短命介入",
                "description": "一瞬の危機。",
                "prompt_injection": "一瞬だけ危機が走る。",
                "active_from_turn": 1,
                "active_until_turn": 2,
            },
        )

        expired_ids = await db.expire_interventions_before_turn(_STORY_ID, turn_number=3)

        assert expired_ids == [iv_id]
        assert db._conn is not None
        cursor = await db._conn.execute(
            "SELECT status FROM director_interventions WHERE id = ?;",
            (iv_id,),
        )
        row = await cursor.fetchone()

    assert row is not None
    assert row["status"] == "expired"


@async_to_sync
async def test_get_live_interventions_for_tension_ids() -> None:
    """指定 tension_id 群に紐づく live intervention を取得できること。"""
    async with _make_db() as db:
        tension_keep = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "conflict",
                "description": "残る tension",
                "involved_chars": [],
                "intensity": 0.6,
                "detected_turn": 1,
            },
        )
        tension_other = await db.insert_tension(
            _STORY_ID,
            {
                "tension_type": "mystery",
                "description": "別 tension",
                "involved_chars": [],
                "intensity": 0.4,
                "detected_turn": 1,
            },
        )
        iv_keep = await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "mood",
                "title": "残る介入",
                "description": "対象 tension に紐づく。",
                "prompt_injection": "空気が張りつめる。",
                "tension_id": tension_keep,
                "active_from_turn": 1,
                "status": "acknowledged",
            },
        )
        await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "mood",
                "title": "別 tension",
                "description": "別 tension に紐づく。",
                "prompt_injection": "別の空気が流れる。",
                "tension_id": tension_other,
                "active_from_turn": 1,
            },
        )
        await db.insert_intervention(
            _STORY_ID,
            {
                "intervention_type": "mood",
                "title": "resolved",
                "description": "解決済み。",
                "prompt_injection": "もう終わった。",
                "tension_id": tension_keep,
                "active_from_turn": 1,
                "status": "resolved",
            },
        )

        rows = await db.get_live_interventions_for_tension_ids(_STORY_ID, [tension_keep])

    assert [row["id"] for row in rows] == [iv_keep]


@async_to_sync
async def test_get_closed_story_scene_summaries_since_turn() -> None:
    """closed_turn 以降の outcome_summary だけが新しい順で返ること。"""
    async with _make_db() as db:
        old_scene = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "music_room",
                "opened_turn": 1,
            },
        )
        new_scene = await db.insert_story_scene(
            _STORY_ID,
            {
                "scene_type": "conversation",
                "status": "active",
                "place_id": "rooftop",
                "opened_turn": 3,
            },
        )
        await db.close_story_scene(
            old_scene,
            outcome_type="dispersed",
            outcome_summary="古い場面の要約。",
            closed_turn=2,
        )
        await db.close_story_scene(
            new_scene,
            outcome_type="decision",
            outcome_summary="新しい場面の要約。",
            closed_turn=5,
        )

        summaries = await db.get_closed_story_scene_summaries(
            _STORY_ID,
            since_turn=3,
            limit=10,
        )

    assert summaries == ["新しい場面の要約。"]


@async_to_sync
async def test_get_recent_relationship_events_story_wide() -> None:
    """story 単位の relationship_events を turn 降順で取得できること。"""
    async with _make_db() as db:
        await db.insert_relationship_event(
            _STORY_ID,
            {
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "event_type": "support",
                "delta_trust": 0.05,
                "delta_tension": -0.02,
                "summary": "char_a が char_b を支えた。",
                "turn_number": 2,
            },
        )
        await db.insert_relationship_event(
            _STORY_ID,
            {
                "char_id_from": "char_b",
                "char_id_to": "char_a",
                "event_type": "conflict",
                "delta_trust": -0.04,
                "delta_tension": 0.08,
                "summary": "char_b が char_a に反発した。",
                "turn_number": 5,
            },
        )

        events = await db.get_recent_relationship_events(
            _STORY_ID,
            since_turn=3,
            limit=10,
        )

    assert len(events) == 1
    assert events[0]["summary"] == "char_b が char_a に反発した。"
    assert events[0]["turn_number"] == 5


@async_to_sync
async def test_insert_evolution_and_get_overlay() -> None:
    """同フィールドの複数 evolution で overlay が最大 turn の値を返すこと。"""
    async with _make_db() as db:
        for turn, value in [(1, "shy"), (3, "cautious"), (5, "bold")]:
            await db.insert_evolution(
                _STORY_ID,
                {
                    "char_id": _CHAR_ID,
                    "turn_number": turn,
                    "field": "personality",
                    "previous_value": None,
                    "new_value": value,
                    "reason": f"turn {turn} の変化",
                },
            )

        # 別フィールドも追加
        await db.insert_evolution(
            _STORY_ID,
            {
                "char_id": _CHAR_ID,
                "turn_number": 2,
                "field": "speech_style",
                "new_value": "polite",
                "reason": "礼儀正しくなった",
            },
        )

        overlay = await db.get_latest_evolution_overlay(_STORY_ID, _CHAR_ID)
        assert overlay["personality"] == "bold"   # turn=5 の値
        assert overlay["speech_style"] == "polite"


@async_to_sync
async def test_get_evolution_overlay_multiple_chars_isolated() -> None:
    """char_a と char_b の evolution が独立して返ること。"""
    async with _make_db() as db:
        await db.insert_evolution(
            _STORY_ID,
            {
                "char_id": "char_a",
                "turn_number": 1,
                "field": "mood",
                "new_value": "happy",
                "reason": "良いことがあった",
            },
        )
        await db.insert_evolution(
            _STORY_ID,
            {
                "char_id": "char_b",
                "turn_number": 1,
                "field": "mood",
                "new_value": "sad",
                "reason": "悪いことがあった",
            },
        )

        overlay_a = await db.get_latest_evolution_overlay(_STORY_ID, "char_a")
        overlay_b = await db.get_latest_evolution_overlay(_STORY_ID, "char_b")

        assert overlay_a["mood"] == "happy"
        assert overlay_b["mood"] == "sad"


# ------------------------------------------------------------------
# テスト 19〜22: story_arc / novel_output (Phase C)
# ------------------------------------------------------------------

@async_to_sync
async def test_insert_and_get_open_arcs() -> None:
    """insert_arc → get_open_arcs で取得でき、close_arc で消えること。"""
    async with _make_db() as db:
        arc_id = await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "chapter",
                "title": "第一章",
                "summary": "物語の始まり。",
                "turn_from": 1,
            },
        )
        assert isinstance(arc_id, int)
        open_arcs = await db.get_open_arcs(_STORY_ID)
        assert len(open_arcs) == 1
        assert open_arcs[0]["id"] == arc_id
        assert open_arcs[0]["arc_type"] == "chapter"

        await db.close_arc(arc_id, turn_to=10)

        open_after = await db.get_open_arcs(_STORY_ID)
        assert all(a["id"] != arc_id for a in open_after)


@async_to_sync
async def test_get_open_arcs_filters_by_type() -> None:
    """get_open_arcs(arc_type='scene') は他 arc_type を含まないこと。"""
    async with _make_db() as db:
        await db.insert_arc(
            _STORY_ID,
            {"arc_type": "chapter", "title": "章", "summary": "ch", "turn_from": 1},
        )
        scene_id = await db.insert_arc(
            _STORY_ID,
            {"arc_type": "scene", "title": "場面", "summary": "sc", "turn_from": 2},
        )
        result = await db.get_open_arcs(_STORY_ID, arc_type="scene")
        assert len(result) == 1
        assert result[0]["id"] == scene_id


@async_to_sync
async def test_get_arcs_includes_closed() -> None:
    """get_arcs は turn_to が設定された閉じたアークも返すこと。"""
    async with _make_db() as db:
        arc_id = await db.insert_arc(
            _STORY_ID,
            {"arc_type": "chapter", "title": "章", "summary": "summary", "turn_from": 1},
        )
        await db.close_arc(arc_id, turn_to=5)

        all_arcs = await db.get_arcs(_STORY_ID)
        assert any(a["id"] == arc_id for a in all_arcs)
        closed = next(a for a in all_arcs if a["id"] == arc_id)
        assert closed["turn_to"] == 5


@async_to_sync
async def test_get_arc_by_source_scene_id() -> None:
    """source_scene_id で scene arc を一意に取得できること。"""
    async with _make_db() as db:
        arc_id = await db.insert_arc(
            _STORY_ID,
            {
                "arc_type": "scene",
                "title": "閉じた場面",
                "summary": "scene close prose。",
                "turn_from": 3,
                "source_scene_id": 91,
            },
        )

        row = await db.get_arc_by_source_scene_id(_STORY_ID, 91)

    assert row is not None
    assert row["id"] == arc_id
    assert row["source_scene_id"] == 91


@async_to_sync
async def test_insert_and_get_novel_outputs() -> None:
    """insert_novel_output → get_novel_outputs で ordering 順に返り、
    source_log_ids が list としてデコードされること。"""
    async with _make_db() as db:
        arc_id = await db.insert_arc(
            _STORY_ID,
            {"arc_type": "scene", "title": "場面1", "summary": "sc", "turn_from": 1},
        )
        await db.insert_novel_output(
            _STORY_ID,
            arc_id,
            {
                "content_type": "prose",
                "content": "夜が明けた。",
                "source_log_ids": [1, 2, 3],
                "ordering": 1,
            },
        )
        await db.insert_novel_output(
            _STORY_ID,
            arc_id,
            {
                "content_type": "dialogue",
                "content": "「おはよう」と彼女は言った。",
                "ordering": 2,
            },
        )
        outputs = await db.get_novel_outputs(_STORY_ID, arc_id)
        assert len(outputs) == 2
        assert outputs[0]["ordering"] == 1
        assert outputs[0]["content_type"] == "prose"
        assert isinstance(outputs[0]["source_log_ids"], list)
        assert outputs[0]["source_log_ids"] == [1, 2, 3]
        assert outputs[1]["source_log_ids"] == []  # None → 空リスト


# ------------------------------------------------------------------
# get_all_novel_outputs_by_story
# ------------------------------------------------------------------


@async_to_sync
async def test_get_all_novel_outputs_by_story() -> None:
    """1クエリで全 novel_output を取得し arc_id でグルーピングされる。"""
    async with _make_db() as db:
        # 2つのアークを作成
        arc_id_1 = await db.insert_arc(
            _STORY_ID,
            {"arc_type": "scene", "title": "夜明け", "summary": "テスト。", "turn_from": 1},
        )
        arc_id_2 = await db.insert_arc(
            _STORY_ID,
            {"arc_type": "scene", "title": "夕暮れ", "summary": "テスト2。", "turn_from": 5},
        )
        await db.insert_novel_output(
            _STORY_ID,
            arc_id_1,
            {"content_type": "prose", "content": "夜明けの場面。", "ordering": 1},
        )
        await db.insert_novel_output(
            _STORY_ID,
            arc_id_2,
            {"content_type": "prose", "content": "夕暮れの場面。", "ordering": 1},
        )
        await db.insert_novel_output(
            _STORY_ID,
            arc_id_2,
            {"content_type": "prose", "content": "夕暮れの場面2。", "ordering": 2},
        )

        result = await db.get_all_novel_outputs_by_story(_STORY_ID)

    assert len(result) == 2
    assert len(result[arc_id_1]) == 1
    assert len(result[arc_id_2]) == 2
    assert result[arc_id_1][0]["content"] == "夜明けの場面。"
    assert result[arc_id_2][0]["ordering"] == 1
    assert result[arc_id_2][1]["ordering"] == 2


# ------------------------------------------------------------------
# get_recent_chat_logs に id カラムが含まれること
# ------------------------------------------------------------------

@async_to_sync
async def test_get_recent_chat_logs_includes_id() -> None:
    """get_recent_chat_logs の各行に id カラムが含まれること。"""
    async with _make_db() as db:
        log_id = await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T10:00:00",
                "char_id": _CHAR_ID,
                "msg_type": "monologue",
                "message": "テスト発言。",
            },
        )

        recent = await db.get_recent_chat_logs(_STORY_ID, limit=5)
        assert len(recent) == 1
        assert "id" in recent[0]
        assert recent[0]["id"] == log_id


@async_to_sync
async def test_get_story_viewer_snapshot_returns_enriched_logs() -> None:
    async with _make_db() as db:
        assert db._conn is not None
        await db._conn.execute(
            """
            INSERT INTO characters (id, story_id, name_ja, expressions_available, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (_CHAR_ID, _STORY_ID, "テスト太郎", '["neutral", "happy"]'),
        )
        await db._conn.execute(
            """
            INSERT INTO places (id, story_id, label, zone, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            ("classroom", _STORY_ID, "教室", "school"),
        )
        await db._conn.commit()

        await db.insert_chat_log(
            _STORY_ID,
            {
                "sim_datetime": "2025-04-01T10:00:00",
                "turn_number": 7,
                "char_id": _CHAR_ID,
                "msg_type": "talk",
                "place_id": "classroom",
                "expression": "happy",
                "message": "テスト発言。",
                "llm_provider": "openai",
                "llm_model": "gpt-5.4-nano",
            },
        )

        snapshot = await db.get_story_viewer_snapshot(_STORY_ID, limit=10)

    assert snapshot["story"]["story_id"] == _STORY_ID
    assert snapshot["live"]["latest_turn"] == 7
    assert snapshot["llm"]["provider"] == "openai"
    assert snapshot["llm"]["model"] == "gpt-5.4-nano"
    assert snapshot["characters"][0]["char_id"] == _CHAR_ID
    assert snapshot["characters"][0]["name"] == "テスト太郎"
    assert snapshot["characters"][0]["expressions_available"] == ["neutral", "happy"]
    assert snapshot["places"][0]["label"] == "教室"
    assert snapshot["logs"][0]["speaker_name"] == "テスト太郎"
    assert snapshot["logs"][0]["place_label"] == "教室"
    assert snapshot["logs"][0]["expression"] == "happy"


# ------------------------------------------------------------------
# テスト: get_character_entry_turn
# ------------------------------------------------------------------


@async_to_sync
async def test_get_character_entry_turn_returns_latest_entry() -> None:
    """移動シナリオで最後に place A に入った turn を返すこと。"""
    async with _make_db() as db:
        # turn 1: place_a に初入場（previous_place IS NULL）
        await db.insert_character_state(_STORY_ID, {
            "char_id": _CHAR_ID,
            "sim_datetime": "2025-04-01T10:00:00",
            "turn_number": 1,
            "current_place": "place_a",
            "previous_place": None,
            "current_expression": "neutral",
        })
        # turn 2: place_b へ移動
        await db.insert_character_state(_STORY_ID, {
            "char_id": _CHAR_ID,
            "sim_datetime": "2025-04-01T10:01:00",
            "turn_number": 2,
            "current_place": "place_b",
            "previous_place": "place_a",
            "current_expression": "neutral",
        })
        # turn 3: place_a へ再入場
        await db.insert_character_state(_STORY_ID, {
            "char_id": _CHAR_ID,
            "sim_datetime": "2025-04-01T10:02:00",
            "turn_number": 3,
            "current_place": "place_a",
            "previous_place": "place_b",
            "current_expression": "neutral",
        })

        result = await db.get_character_entry_turn(_STORY_ID, _CHAR_ID, "place_a")

    # 最後に place_a に入った turn は 3
    assert result == 3


@async_to_sync
async def test_get_character_entry_turn_returns_none_when_no_entry_recorded() -> None:
    """place_a の入場記録が無ければ None を返すこと。"""
    async with _make_db() as db:
        result = await db.get_character_entry_turn(_STORY_ID, _CHAR_ID, "place_a")

    assert result is None


@async_to_sync
async def test_get_character_entry_turn_first_entry_is_null_previous() -> None:
    """previous_place IS NULL の初回入場 turn も検出できること。"""
    async with _make_db() as db:
        await db.insert_character_state(_STORY_ID, {
            "char_id": _CHAR_ID,
            "sim_datetime": "2025-04-01T10:00:00",
            "turn_number": 5,
            "current_place": "place_a",
            "previous_place": None,
            "current_expression": "neutral",
        })

        result = await db.get_character_entry_turn(_STORY_ID, _CHAR_ID, "place_a")

    assert result == 5


# ------------------------------------------------------------------
# テスト: get_place_dialogue_since_turn
# ------------------------------------------------------------------


@async_to_sync
async def test_get_place_dialogue_since_turn_returns_scoped_logs() -> None:
    """指定 place かつ since_turn 以降のログのみ古い順で返すこと。"""
    async with _make_db() as db:
        logs_data = [
            # turn 1: place_a (since_turn より前 → 除外)
            {"turn_number": 1, "char_id": _CHAR_ID, "place_id": "place_a", "msg_type": "talk",
             "message": "turn1-a"},
            # turn 2: place_a (since_turn 以降 → 含む)
            {"turn_number": 2, "char_id": _CHAR_ID, "place_id": "place_a", "msg_type": "talk",
             "message": "turn2-a"},
            # turn 3: place_b (place が違う → 除外)
            {"turn_number": 3, "char_id": _CHAR_ID, "place_id": "place_b", "msg_type": "talk",
             "message": "turn3-b"},
            # turn 4: place_a (since_turn 以降 → 含む)
            {"turn_number": 4, "char_id": _CHAR_ID, "place_id": "place_a", "msg_type": "talk",
             "message": "turn4-a"},
        ]
        for log in logs_data:
            await db.insert_chat_log(_STORY_ID, {
                **log,
                "sim_datetime": "2025-04-01T10:00:00",
                "expression": "neutral",
                "message": log["message"],
            })

        result = await db.get_place_dialogue_since_turn(
            _STORY_ID, "place_a", since_turn=2
        )

    messages = [r["message"] for r in result]
    assert messages == ["turn2-a", "turn4-a"]


@async_to_sync
async def test_get_place_dialogue_since_turn_excludes_narrator() -> None:
    """_narrator の行は除外されること。"""
    async with _make_db() as db:
        await db.insert_chat_log(_STORY_ID, {
            "turn_number": 1, "char_id": "_narrator", "place_id": "place_a",
            "msg_type": "narration", "message": "ナレーション文",
            "sim_datetime": "2025-04-01T10:00:00", "expression": "neutral",
        })
        await db.insert_chat_log(_STORY_ID, {
            "turn_number": 2, "char_id": _CHAR_ID, "place_id": "place_a",
            "msg_type": "talk", "message": "キャラ発言",
            "sim_datetime": "2025-04-01T10:01:00", "expression": "neutral",
        })

        result = await db.get_place_dialogue_since_turn(
            _STORY_ID, "place_a", since_turn=1
        )

    assert len(result) == 1
    assert result[0]["message"] == "キャラ発言"


@async_to_sync
async def test_get_place_dialogue_since_turn_respects_limit() -> None:
    """limit が適用されて最新側 limit 件が返ること（古い順で）。"""
    async with _make_db() as db:
        for i in range(5):
            await db.insert_chat_log(_STORY_ID, {
                "turn_number": i + 1, "char_id": _CHAR_ID, "place_id": "place_a",
                "msg_type": "talk", "message": f"msg{i + 1}",
                "sim_datetime": f"2025-04-01T10:0{i}:00", "expression": "neutral",
            })

        result = await db.get_place_dialogue_since_turn(
            _STORY_ID, "place_a", since_turn=1, limit=3
        )

    # DESC LIMIT 3 → 最新3件を古→新順に返す
    messages = [r["message"] for r in result]
    assert messages == ["msg3", "msg4", "msg5"]


# ------------------------------------------------------------------
# delete_story_completely のテスト
# ------------------------------------------------------------------

@async_to_sync
async def test_delete_story_completely_removes_story_and_cascades() -> None:
    """delete_story_completely が stories 行を削除し、CASCADE でキャラも消えること。"""
    async with _make_db() as db:
        assert await db.get_story(_STORY_ID) is not None

        # 直接 SQL でキャラを挿入
        assert db._conn is not None
        await db._conn.execute(
            "INSERT INTO characters (story_id, id, name_ja, is_active)"
            " VALUES (?, ?, ?, 1);",
            (_STORY_ID, "del_char", "削除テストキャラ"),
        )
        await db._conn.commit()

        # is_active=False にしてから削除
        await db.set_story_active(_STORY_ID, False)
        result = await db.delete_story_completely(_STORY_ID)
        assert result is True

        assert await db.get_story(_STORY_ID) is None
        chars = await db.get_characters(_STORY_ID)
        assert chars == []


@async_to_sync
async def test_delete_story_completely_active_story_raises() -> None:
    """is_active=True（デフォルト）の story は ValueError を raise する。"""
    async with _make_db() as db:
        # _make_db はデフォルト is_active=1 で挿入する
        with pytest.raises(ValueError, match="is currently active"):
            await db.delete_story_completely(_STORY_ID)


@async_to_sync
async def test_delete_story_completely_nonexistent_returns_false() -> None:
    """存在しない story_id は False を返す（例外なし）。"""
    async with _make_db() as db:
        result = await db.delete_story_completely("nonexistent_story_xyz")
        assert result is False


@async_to_sync
async def test_delete_story_completely_inactive_story_succeeds() -> None:
    """is_active=False に設定すれば削除が通ること。"""
    async with _make_db() as db:
        await db.set_story_active(_STORY_ID, False)
        result = await db.delete_story_completely(_STORY_ID)
        assert result is True
        assert await db.get_story(_STORY_ID) is None
