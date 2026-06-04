"""
db/db_manager.py — 非同期DB操作クラス

aiosqlite を使い single connection を保持する。
接続ごとに WAL / foreign_keys / busy_timeout PRAGMA を設定し、
migrations/ ディレクトリの SQL ファイルを昇順に自動適用する。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any

import sqlite3
import aiosqlite

from db.backends import SupportsAsyncConnection, SyncSqliteConnection

logger = logging.getLogger(__name__)

# _apply_migrations の前に schema_version テーブルだけを先に作るための DDL
_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    description TEXT
);
"""

_MIGRATION_DESCRIPTIONS = {
    2: "places.is_active added",
    3: "conversation sessions added",
    4: "story_memory table added",
    5: "character_evolution table added",
    6: "narrative_tensions table added",
    7: "director_interventions table added",
    8: "story_arc and novel_output tables added",
    9: "admin auth and publication metadata added",
    10: "ambient states added",
    11: "story emergence foundations added",
    12: "story hook resolution fields added",
    13: "story_arc source_scene_id added",
    14: "story_interaction_patterns table added",
    15: "story_episodes table added",
    16: "relationship_modes table added",
    17: "story_canon_bits table added",
    18: "story_dramatic_pressures table added",
    19: "relationship_modes multi-mode active index",
    20: "canon reignition hook tracking added",
    21: "canon profile writeback overlays added",
    22: "chapters table added",
    23: "director_persona table added",
    24: "director_swap_log table added",
    25: "chapter_proposals table added",
    26: "story_mode column added",
    27: "conversation motif settings and runs added",
    28: "scene_scripts table for pre-turn director scene descriptions",
    29: "scene_scripts compatibility marker for existing v28 deployments",
    30: "news mode settings and commentary history added",
    31: "story-level utterance character limit settings",
    32: "story-level max sentence count for utterances",
    33: "per-story news tag filter",
    34: "per-story web post target url and auth token",
    35: "backfill NULL turn_number on character_states initial rows",
}

DEFAULT_CONVERSATION_MOTIF_SETTINGS: tuple[dict[str, Any], ...] = (
    {
        "motif_id": "solo_seed_rondo",
        "display_name": "会話輪舞",
        "description": "誰かの独り言を別キャラが拾い、元のキャラへ戻して会話の種にする。",
        "enabled": False,
        "strength": "moderate",
        "cooldown_turns": 4,
    },
)


class DatabaseConnectionTimeoutError(RuntimeError):
    """aiosqlite 接続がタイムアウトしたことを示す例外。"""


class DatabaseManager:
    """非同期 SQLite DB 管理クラス。

    使い方::

        async with DatabaseManager(":memory:", migrations_dir) as db:
            await db.insert_chat_log(story_id, log)
    """

    def __init__(
        self,
        db_path: str | Path,
        migrations_dir: str | Path = "db/migrations",
        connect_timeout_sec: float = 10.0,
    ) -> None:
        self._db_path = str(db_path)
        self._migrations_dir = Path(migrations_dir)
        self._connect_timeout_sec = float(connect_timeout_sec)
        self._conn: SupportsAsyncConnection | None = None
        self.backend_kind: str = "uninitialized"

    @property
    def db_path(self) -> str:
        return self._db_path

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """DB 接続・PRAGMA 設定・マイグレーション自動適用。"""
        logger.info("DB startup stage: aiosqlite.connect begin path=%s", self._db_path)
        try:
            self._conn = await self._connect_aiosqlite_backend()
            self.backend_kind = "aiosqlite"
        except DatabaseConnectionTimeoutError:
            logger.warning(
                "DB startup fallback: switching to sync sqlite backend path=%s",
                self._db_path,
            )
            self._conn = await self._connect_sync_backend()
            self.backend_kind = "sync_sqlite"
        logger.info(
            "DB startup stage complete: connect path=%s backend=%s",
            self._db_path,
            self.backend_kind,
        )
        self._conn.row_factory = sqlite3.Row
        logger.info("DB startup stage: set pragmas path=%s", self._db_path)
        await self._set_pragmas()
        logger.info("DB startup stage complete: set pragmas path=%s", self._db_path)
        logger.info("DB startup stage: apply migrations path=%s", self._db_path)
        await self._apply_migrations()
        logger.info("DB startup stage complete: apply migrations path=%s", self._db_path)
        logger.info("DB初期化完了: path=%s", self._db_path)

    async def _connect_aiosqlite_backend(self) -> aiosqlite.Connection:
        try:
            return await asyncio.wait_for(
                aiosqlite.connect(self._db_path),
                timeout=self._connect_timeout_sec,
            )
        except TimeoutError as exc:
            raise DatabaseConnectionTimeoutError(
                "Timed out while connecting to sqlite database "
                f"(path={self._db_path}, timeout_sec={self._connect_timeout_sec})."
            ) from exc

    async def _connect_sync_backend(self) -> SyncSqliteConnection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        return SyncSqliteConnection(conn)

    async def close(self) -> None:
        """接続を閉じる。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "DatabaseManager":
        await self.initialize()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------

    async def _set_pragmas(self) -> None:
        assert self._conn is not None
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        await self._conn.execute("PRAGMA busy_timeout = 5000;")

    async def _apply_migrations(self) -> None:
        """migrations_dir 内の *.sql ファイルを昇順に未適用分だけ実行する。"""
        assert self._conn is not None

        # schema_version テーブルを先に作成（マイグレーション管理用）
        await self._conn.executescript(_SCHEMA_VERSION_DDL)
        await self._conn.commit()

        # 適用済みバージョンを取得
        cursor = await self._conn.execute(
            "SELECT version FROM schema_version ORDER BY version;"
        )
        rows = await cursor.fetchall()
        applied: set[int] = {row[0] for row in rows}

        # .sql ファイルをソートしてスキャン
        sql_files = sorted(self._migrations_dir.glob("*.sql"))
        for sql_file in sql_files:
            # ファイル名先頭の数字を version として取得（例: 001_initial.sql → 1）
            match = re.match(r"^(\d+)", sql_file.stem)
            if match is None:
                logger.warning("スキップ: バージョン番号が読み取れません: %s", sql_file.name)
                continue
            version = int(match.group(1))

            if version in applied:
                continue

            if await self._repair_known_migration(version):
                applied.add(version)
                logger.info(
                    "マイグレーション補完記録: version=%d, %s",
                    version,
                    sql_file.name,
                )
                continue

            # 未適用マイグレーションを実行
            sql = sql_file.read_text(encoding="utf-8")
            await self._conn.executescript(sql)
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO schema_version (version, description)
                VALUES (?, ?)
                """,
                (version, self._migration_description(version, sql_file.name)),
            )
            await self._conn.commit()
            applied.add(version)
            logger.info(
                "マイグレーション適用: version=%d, %s", version, sql_file.name
            )

    async def _repair_known_migration(self, version: int) -> bool:
        """既知の migration 記録漏れだけを補完する。"""
        assert self._conn is not None

        if version == 2:
            if not await self._table_has_column("places", "is_active"):
                return False
        elif version == 3:
            if not await self._table_exists("conversation_sessions"):
                return False
            if not await self._table_has_column("chat_logs", "conversation_session_id"):
                return False
            if not await self._table_has_column("chat_logs", "reply_to_log_id"):
                return False
        else:
            return False

        await self._conn.execute(
            """
            INSERT OR IGNORE INTO schema_version (version, description)
            VALUES (?, ?)
            """,
            (version, self._migration_description(version, "repair")),
        )
        await self._conn.commit()
        return True

    async def _table_has_column(self, table_name: str, column_name: str) -> bool:
        """テーブルに指定カラムが存在するかを返す。"""
        assert self._conn is not None

        cursor = await self._conn.execute(f"PRAGMA table_info({table_name});")
        rows = await cursor.fetchall()
        return any(row[1] == column_name for row in rows)

    async def _table_exists(self, table_name: str) -> bool:
        """指定テーブルが存在するかを返す。"""
        assert self._conn is not None

        cursor = await self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?;",
            (table_name,),
        )
        row = await cursor.fetchone()
        return row is not None

    def _migration_description(self, version: int, fallback: str) -> str:
        """schema_version に保存する説明文字列を返す。"""
        return _MIGRATION_DESCRIPTIONS.get(version, fallback)

    def _decode_conversation_session(
        self, row: aiosqlite.Row | dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """conversation_sessions の JSON カラムを Python 型へ戻す。"""
        if row is None:
            return None
        data = dict(row)
        raw_participants = data.get("participant_ids")
        if isinstance(raw_participants, str) and raw_participants:
            data["participant_ids"] = json.loads(raw_participants)
        elif raw_participants in (None, ""):
            data["participant_ids"] = []
        return data

    def _decode_story_memory(self, row: dict[str, Any]) -> dict[str, Any]:
        """story_memory の JSON カラムを Python 型へ戻す。"""
        raw = row.get("involved_chars")
        if isinstance(raw, str) and raw:
            row["involved_chars"] = json.loads(raw)
        elif raw in (None, ""):
            row["involved_chars"] = []
        return row

    def _decode_tension(self, row: dict[str, Any]) -> dict[str, Any]:
        """narrative_tensions の JSON カラムを Python 型へ戻す。"""
        raw = row.get("involved_chars")
        if isinstance(raw, str) and raw:
            row["involved_chars"] = json.loads(raw)
        elif raw in (None, ""):
            row["involved_chars"] = []
        return row

    def _decode_ambient_state(self, row: dict[str, Any]) -> dict[str, Any]:
        """ambient_states の JSON カラムを Python 型へ戻す。"""
        raw = row.get("emotion_delta")
        if isinstance(raw, str) and raw:
            row["emotion_delta"] = json.loads(raw)
        elif raw in (None, ""):
            row["emotion_delta"] = {}
        return row

    def _decode_json_field(
        self, row: dict[str, Any], key: str, default: Any
    ) -> dict[str, Any]:
        raw = row.get(key)
        if isinstance(raw, str) and raw:
            row[key] = json.loads(raw)
        elif raw in (None, ""):
            row[key] = default
        return row

    # ------------------------------------------------------------------
    # Stories
    # ------------------------------------------------------------------

    async def get_story(self, story_id: str) -> dict[str, Any] | None:
        """story_id に対応するストーリー行を返す。存在しない場合は None。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM stories WHERE id = ?;", (story_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def update_last_sim_time(self, story_id: str, sim_time: str) -> None:
        """last_sim_time を更新する（再開用）。"""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE stories SET last_sim_time = ?, updated_at = datetime('now') WHERE id = ?;",
            (sim_time, story_id),
        )
        await self._conn.commit()

    async def get_stories(self) -> list[dict[str, Any]]:
        """stories を ID 順に返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM stories ORDER BY id ASC;"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def set_story_active(self, story_id: str, is_active: bool) -> None:
        """is_active フラグを更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE stories SET is_active = ?, updated_at = datetime('now') WHERE id = ?;",
            (1 if is_active else 0, story_id),
        )
        await self._conn.commit()

    async def delete_story(self, story_id: str) -> None:
        """story とその story-scoped row を削除する。"""
        assert self._conn is not None
        await self._conn.execute("DELETE FROM stories WHERE id = ?;", (story_id,))
        await self._conn.commit()

    async def delete_story_completely(self, story_id: str) -> bool:
        """ストーリーとすべての関連 DB データを完全削除する。

        安全保証:
        - story_id が存在しない場合は False を返す（例外なし）
        - is_active=True の story は ValueError を raise（実行中の削除防止）
        - ambient_states は CASCADE 対象外のため明示削除
        - story_arc.parent_arc_id は自己参照 FK のため先に NULL 化
        - admin_audit_log は ON DELETE SET NULL のため行は残る（意図的）
        - 残りの全テーブルは FOREIGN KEY ON DELETE CASCADE が処理する
        """
        assert self._conn is not None
        story = await self.get_story(story_id)
        if story is None:
            return False
        if story.get("is_active"):
            raise ValueError(
                f"Story '{story_id}' is currently active. Stop the engine first."
            )

        await self._conn.execute("BEGIN;")
        try:
            # story_arc.parent_arc_id 自己参照 FK を先に NULL 化
            await self._conn.execute(
                "UPDATE story_arc SET parent_arc_id = NULL WHERE story_id = ?;",
                (story_id,),
            )
            # ambient_states は CASCADE 宣言なしのため明示削除
            await self._delete_story_rows("ambient_states", story_id)
            # stories を DELETE → CASCADE FK テーブルが連鎖削除
            await self._conn.execute(
                "DELETE FROM stories WHERE id = ?;", (story_id,)
            )
            await self._conn.execute("COMMIT;")
        except Exception:
            await self._conn.execute("ROLLBACK;")
            raise
        return True

    # ------------------------------------------------------------------
    # Characters
    # ------------------------------------------------------------------

    async def get_characters(self, story_id: str) -> list[dict[str, Any]]:
        """story_id に属するアクティブなキャラクター一覧を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM characters WHERE story_id = ? AND is_active = 1;",
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_character(
        self, story_id: str, char_id: str
    ) -> dict[str, Any] | None:
        """特定キャラクターを返す。存在しない場合は None。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM characters WHERE story_id = ? AND id = ?;",
            (story_id, char_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Character States
    # ------------------------------------------------------------------

    async def insert_character_state(
        self, story_id: str, state: dict[str, Any]
    ) -> None:
        """character_states に1行挿入する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO character_states (
                char_id, story_id, sim_datetime, turn_number,
                current_place, previous_place, move_reason,
                current_action, current_expression,
                stress, motivation, loneliness, excitement
            ) VALUES (
                :char_id, :story_id, :sim_datetime, :turn_number,
                :current_place, :previous_place, :move_reason,
                :current_action, :current_expression,
                :stress, :motivation, :loneliness, :excitement
            );
            """,
            {
                "char_id": state["char_id"],
                "story_id": story_id,
                "sim_datetime": state["sim_datetime"],
                "turn_number": state.get("turn_number"),
                "current_place": state["current_place"],
                "previous_place": state.get("previous_place"),
                "move_reason": state.get("move_reason"),
                "current_action": state.get("current_action"),
                "current_expression": state.get("current_expression", "neutral"),
                "stress": state.get("stress", 0.3),
                "motivation": state.get("motivation", 0.7),
                "loneliness": state.get("loneliness", 0.2),
                "excitement": state.get("excitement", 0.5),
            },
        )
        await self._conn.commit()

    async def get_latest_character_state(
        self, story_id: str, char_id: str
    ) -> dict[str, Any] | None:
        """指定キャラクターの最新状態を1件返す。存在しない場合は None。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM character_states
            WHERE char_id = ? AND story_id = ?
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1;
            """,
            (char_id, story_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def reset_story_progress(self, story_id: str) -> dict[str, int]:
        """story 定義を残したまま、ランタイム進行データを初期状態へ戻す。"""
        assert self._conn is not None
        story = await self.get_story(story_id)
        if story is None:
            raise ValueError(f"Story not found: {story_id}")

        counts: dict[str, int] = {}
        season_start = story.get("season_start") or "2000-01-01"
        initial_datetime = f"{season_start}T00:00:00"
        active_characters = await self.get_characters(story_id)
        active_places = await self._get_active_place_ids(story_id)

        await self._conn.execute("BEGIN;")
        try:
            for table in (
                "chapter_proposals",
                "director_swap_log",
                "director_satisfaction",
                "novel_output",
                "story_dramatic_pressures",
                "story_canon_bits",
                "relationship_modes",
                "story_episodes",
                "story_interaction_patterns",
                "generation_quality_issues",
                "relationship_events",
                "character_growth_candidates",
                "character_profile_overlays",
                "character_canon_overlays",
                "character_drives",
                "conversation_motif_runs",
                "story_hooks",
            ):
                counts[table] = await self._delete_story_rows(table, story_id)

            counts["scene_participants"] = await self._delete_scene_participants(story_id)
            counts["story_scenes"] = await self._delete_story_rows("story_scenes", story_id)
            counts["director_interventions"] = await self._delete_story_rows(
                "director_interventions", story_id
            )
            counts["narrative_tensions"] = await self._delete_story_rows(
                "narrative_tensions", story_id
            )
            counts["character_evolution"] = await self._delete_story_rows(
                "character_evolution", story_id
            )
            counts["story_memory"] = await self._delete_story_rows("story_memory", story_id)
            counts["memories"] = await self._delete_story_rows("memories", story_id)
            counts["chat_logs"] = await self._delete_story_rows("chat_logs", story_id)
            counts["scene_scripts"] = await self._delete_story_rows("scene_scripts", story_id)
            counts["conversation_sessions"] = await self._delete_story_rows(
                "conversation_sessions", story_id
            )
            counts["ambient_states"] = await self._delete_story_rows("ambient_states", story_id)

            await self._conn.execute(
                "UPDATE story_arc SET parent_arc_id = NULL WHERE story_id = ?;",
                (story_id,),
            )
            counts["story_arc"] = await self._delete_story_rows("story_arc", story_id)

            counts["character_states"] = await self._delete_story_rows(
                "character_states", story_id
            )
            counts["relationships"] = await self._delete_story_rows("relationships", story_id)

            inserted_states = 0
            for character in active_characters:
                await self._insert_initial_character_state_for_reset(
                    story_id,
                    character,
                    initial_datetime,
                    active_places,
                )
                inserted_states += 1
            counts["character_states_inserted"] = inserted_states

            inserted_relationships = 0
            active_char_ids = [str(character["id"]) for character in active_characters]
            for char_id_from in active_char_ids:
                for char_id_to in active_char_ids:
                    if char_id_from == char_id_to:
                        continue
                    await self._conn.execute(
                        """
                        INSERT INTO relationships (
                            story_id, char_id_from, char_id_to,
                            trust, affinity, tension, familiarity,
                            last_event_turn, last_event_summary
                        )
                        VALUES (?, ?, ?, 0.5, 0.5, 0.0, 0.5, NULL, NULL);
                        """,
                        (story_id, char_id_from, char_id_to),
                    )
                    inserted_relationships += 1
            counts["relationships_inserted"] = inserted_relationships

            cursor = await self._conn.execute(
                """
                UPDATE story_chapters
                SET status = 'pending',
                    current_beat = 'setup',
                    opened_turn = NULL,
                    closed_turn = NULL,
                    close_reason = NULL,
                    carry_over_json = '{}'
                WHERE story_id = ?;
                """,
                (story_id,),
            )
            counts["story_chapters_reset"] = int(cursor.rowcount or 0)

            cursor = await self._conn.execute(
                """
                UPDATE story_chapter_beats
                SET status = 'pending', reached_turn = NULL
                WHERE chapter_db_id IN (
                    SELECT id FROM story_chapters WHERE story_id = ?
                );
                """,
                (story_id,),
            )
            counts["story_chapter_beats_reset"] = int(cursor.rowcount or 0)

            cursor = await self._conn.execute(
                """
                UPDATE stories
                SET last_sim_time = NULL, updated_at = datetime('now')
                WHERE id = ?;
                """,
                (story_id,),
            )
            counts["stories_reset"] = int(cursor.rowcount or 0)

            await self._conn.commit()
            return counts
        except Exception:
            await self._conn.rollback()
            raise

    async def _get_active_place_ids(self, story_id: str) -> list[str]:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id
            FROM places
            WHERE story_id = ? AND is_active = 1
            ORDER BY id ASC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [str(row["id"]) for row in rows]

    async def _delete_story_rows(self, table_name: str, story_id: str) -> int:
        assert self._conn is not None
        cursor = await self._conn.execute(
            f"DELETE FROM {table_name} WHERE story_id = ?;",
            (story_id,),
        )
        return int(cursor.rowcount or 0)

    async def _delete_scene_participants(self, story_id: str) -> int:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            DELETE FROM scene_participants
            WHERE scene_id IN (
                SELECT id FROM story_scenes WHERE story_id = ?
            );
            """,
            (story_id,),
        )
        return int(cursor.rowcount or 0)

    async def _insert_initial_character_state_for_reset(
        self,
        story_id: str,
        character: dict[str, Any],
        sim_datetime: str,
        active_places: list[str],
    ) -> None:
        assert self._conn is not None
        emotion = self._json_object(character.get("emotion_default"))
        favorite_places = self._json_list(character.get("favorite_places"))
        active_place_set = set(active_places)
        current_place = next(
            (place_id for place_id in favorite_places if place_id in active_place_set),
            active_places[0] if active_places else "school_gate",
        )
        await self._conn.execute(
            """
            INSERT INTO character_states (
                char_id, story_id, sim_datetime, turn_number,
                current_place, current_expression,
                stress, motivation, loneliness, excitement
            ) VALUES (?, ?, ?, 0, ?, 'neutral', ?, ?, ?, ?);
            """,
            (
                character["id"],
                story_id,
                sim_datetime,
                current_place,
                float(emotion.get("stress", 0.3)),
                float(emotion.get("motivation", 0.7)),
                float(emotion.get("loneliness", 0.2)),
                float(emotion.get("excitement", 0.5)),
            ),
        )

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return decoded if isinstance(decoded, dict) else {}
        return {}

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str) and value:
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return []
            if isinstance(decoded, list):
                return [str(item) for item in decoded]
        return []

    # ------------------------------------------------------------------
    # Chat Logs
    # ------------------------------------------------------------------

    async def insert_chat_log(self, story_id: str, log: dict[str, Any]) -> int:
        """chat_logs に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO chat_logs (
                story_id, sim_datetime, turn_number,
                char_id, msg_type, target_char_id,
                place_id, expression, message,
                conversation_session_id, reply_to_log_id,
                emotion_snapshot, llm_provider, llm_model,
                posted_to_web, scene_id, speaker_intent,
                quality_score, quality_flags, state_effect_summary,
                source_intervention_id, generation_attempt
            ) VALUES (
                :story_id, :sim_datetime, :turn_number,
                :char_id, :msg_type, :target_char_id,
                :place_id, :expression, :message,
                :conversation_session_id, :reply_to_log_id,
                :emotion_snapshot, :llm_provider, :llm_model,
                :posted_to_web, :scene_id, :speaker_intent,
                :quality_score, :quality_flags, :state_effect_summary,
                :source_intervention_id, :generation_attempt
            );
            """,
            {
                "story_id": story_id,
                "sim_datetime": log["sim_datetime"],
                "turn_number": log.get("turn_number"),
                "char_id": log["char_id"],
                "msg_type": log["msg_type"],
                "target_char_id": log.get("target_char_id"),
                "place_id": log.get("place_id"),
                "expression": log.get("expression", "neutral"),
                "message": log["message"],
                "conversation_session_id": log.get("conversation_session_id"),
                "reply_to_log_id": log.get("reply_to_log_id"),
                "emotion_snapshot": log.get("emotion_snapshot"),
                "llm_provider": log.get("llm_provider"),
                "llm_model": log.get("llm_model"),
                "posted_to_web": log.get("posted_to_web", 0),
                "scene_id": log.get("scene_id"),
                "speaker_intent": log.get("speaker_intent"),
                "quality_score": log.get("quality_score"),
                "quality_flags": json.dumps(log.get("quality_flags", []), ensure_ascii=False),
                "state_effect_summary": log.get("state_effect_summary"),
                "source_intervention_id": log.get("source_intervention_id"),
                "generation_attempt": log.get("generation_attempt", 1),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_recent_chat_logs(
        self, story_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """直近 limit 件の chat_logs を古い順（発言順）で返す。

        _narrator 行も含む。呼び出し元でフィルタすること。
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id, char_id, message, msg_type, place_id, sim_datetime
            FROM chat_logs
            WHERE story_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (story_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]  # 古い順に返す

    async def get_story_viewer_snapshot(
        self, story_id: str, limit: int = 50
    ) -> dict[str, Any] | None:
        """local viewer 用の story snapshot を返す。"""
        story = await self.get_story(story_id)
        if story is None:
            return None

        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT
                chat.id,
                chat.char_id,
                chat.message,
                chat.msg_type,
                chat.place_id,
                chat.sim_datetime,
                chat.turn_number,
                chat.expression,
                chat.llm_provider,
                chat.llm_model,
                chars.name_ja AS speaker_name,
                places.label AS place_label
            FROM chat_logs AS chat
            LEFT JOIN characters AS chars
                ON chars.story_id = chat.story_id
               AND chars.id = chat.char_id
            LEFT JOIN places AS places
                ON places.story_id = chat.story_id
               AND places.id = chat.place_id
            WHERE chat.story_id = ?
            ORDER BY chat.id DESC
            LIMIT ?
            """,
            (story_id, limit),
        )
        rows = await cursor.fetchall()
        logs: list[dict[str, Any]] = []
        latest_turn: int | None = None
        latest_sim_datetime: str | None = None
        latest_provider: str | None = None
        latest_model: str | None = None
        for row in reversed(rows):
            data = dict(row)
            data["speaker_name"] = data.get("speaker_name") or data["char_id"]
            data["place_label"] = data.get("place_label") or data.get("place_id")
            logs.append(data)
            latest_turn = data.get("turn_number") if data.get("turn_number") is not None else latest_turn
            latest_sim_datetime = data.get("sim_datetime") or latest_sim_datetime
            latest_provider = data.get("llm_provider") or latest_provider
            latest_model = data.get("llm_model") or latest_model

        characters_raw = await self.get_characters(story_id)
        characters: list[dict[str, Any]] = []
        for character in characters_raw:
            decoded = self._decode_json_field(dict(character), "expressions_available", [])
            characters.append(
                {
                    "char_id": decoded["id"],
                    "name": decoded.get("name_ja") or decoded["id"],
                    "image_path": decoded.get("image_path"),
                    "expressions_available": decoded.get("expressions_available", []),
                }
            )

        cursor = await self._conn.execute(
            """
            SELECT id AS place_id, label
            FROM places
            WHERE story_id = ? AND is_active = 1
            ORDER BY id ASC
            """,
            (story_id,),
        )
        place_rows = await cursor.fetchall()
        places = [dict(row) for row in place_rows]

        return {
            "story": {
                "story_id": story["id"],
                "title": story["title"],
            },
            "llm": {
                "provider": latest_provider or story.get("llm_provider"),
                "model": latest_model or story.get("llm_model"),
            },
            "live": {
                "latest_turn": latest_turn,
                "latest_sim_datetime": latest_sim_datetime,
                "log_count": len(logs),
            },
            "characters": characters,
            "places": places,
            "logs": logs,
        }

    async def get_unposted_logs(
        self, story_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Web 未送信（posted_to_web=0）のログを古い順に最大 limit 件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM chat_logs
            WHERE story_id = ? AND posted_to_web = 0
            ORDER BY id ASC
            LIMIT ?;
            """,
            (story_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_chat_logs(
        self,
        story_id: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """chat_logs を全件取得（オプションで provider / model フィルタ）。"""
        assert self._conn is not None
        clauses = ["story_id = ?"]
        params: list[Any] = [story_id]
        if provider is not None:
            clauses.append("llm_provider = ?")
            params.append(provider)
        if model is not None:
            clauses.append("llm_model = ?")
            params.append(model)
        where = " AND ".join(clauses)
        cursor = await self._conn.execute(
            f"SELECT * FROM chat_logs WHERE {where} ORDER BY id ASC;",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_logs_posted(self, story_id: str, log_ids: list[int]) -> None:
        """指定した id リストの posted_to_web を 1 に更新する。"""
        if not log_ids:
            return
        assert self._conn is not None
        placeholders = ",".join("?" * len(log_ids))
        await self._conn.execute(
            f"UPDATE chat_logs SET posted_to_web = 1 WHERE story_id = ? AND id IN ({placeholders});",
            (story_id, *log_ids),
        )
        await self._conn.commit()

    async def get_recent_conversation_logs(
        self,
        story_id: str,
        conversation_session_id: int,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """指定 session の直近ログを古い順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM chat_logs
            WHERE story_id = ? AND conversation_session_id = ?
            ORDER BY id DESC
            LIMIT ?;
            """,
            (story_id, conversation_session_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]

    async def get_conversation_seed_logs(
        self,
        story_id: str,
        place_id: str,
        sim_datetime: str,
        turn_number: int,
        participant_ids: list[str],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """同 turn/place にある未割当ログを seed 候補として返す。"""
        assert self._conn is not None
        if not participant_ids:
            return []

        placeholders = ",".join("?" * len(participant_ids))
        cursor = await self._conn.execute(
            f"""
            SELECT * FROM chat_logs
            WHERE story_id = ?
              AND place_id = ?
              AND sim_datetime = ?
              AND turn_number = ?
              AND conversation_session_id IS NULL
              AND char_id IN ({placeholders})
            ORDER BY id ASC
            LIMIT ?;
            """,
            (
                story_id,
                place_id,
                sim_datetime,
                turn_number,
                *participant_ids,
                limit,
            ),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_character_entry_turn(
        self,
        story_id: str,
        char_id: str,
        current_place: str,
    ) -> int | None:
        """キャラが current_place に最後に入った turn_number を返す。

        character_states で previous_place != current_place の最新行の turn_number を返す。
        同じ場所に居続けている（移動記録が無い）場合は None を返す。
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT turn_number
              FROM character_states
             WHERE story_id = ? AND char_id = ? AND current_place = ?
               AND (previous_place IS NULL OR previous_place <> current_place)
             ORDER BY turn_number DESC
             LIMIT 1;
            """,
            (story_id, char_id, current_place),
        )
        row = await cursor.fetchone()
        return int(row["turn_number"]) if row else None

    async def get_place_dialogue_since_turn(
        self,
        story_id: str,
        place_id: str,
        since_turn: int,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """place_id で since_turn 以降の chat_logs を古い順で返す（_narrator 除外）。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT turn_number, char_id, msg_type, message
              FROM chat_logs
             WHERE story_id = ?
               AND place_id = ?
               AND turn_number >= ?
               AND char_id <> '_narrator'
               AND message IS NOT NULL AND message <> ''
             ORDER BY id DESC
             LIMIT ?;
            """,
            (story_id, place_id, since_turn, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]

    async def attach_logs_to_conversation_session(
        self, log_ids: list[int], conversation_session_id: int
    ) -> None:
        """既存 chat_logs を conversation session へ紐づける。"""
        if not log_ids:
            return
        assert self._conn is not None

        placeholders = ",".join("?" * len(log_ids))
        await self._conn.execute(
            f"""
            UPDATE chat_logs
            SET conversation_session_id = ?
            WHERE id IN ({placeholders});
            """,
            (conversation_session_id, *log_ids),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Conversation Sessions
    # ------------------------------------------------------------------

    async def insert_conversation_session(
        self, story_id: str, session: dict[str, Any]
    ) -> int:
        """conversation_sessions に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO conversation_sessions (
                story_id, place_id, participant_ids, status,
                last_speaker_id, last_log_id,
                started_at_sim_datetime, last_activity_sim_datetime,
                last_turn_number, scene_id, session_kind, closure_reason
            ) VALUES (
                :story_id, :place_id, :participant_ids, :status,
                :last_speaker_id, :last_log_id,
                :started_at_sim_datetime, :last_activity_sim_datetime,
                :last_turn_number, :scene_id, :session_kind, :closure_reason
            );
            """,
            {
                "story_id": story_id,
                "place_id": session["place_id"],
                "participant_ids": json.dumps(
                    session.get("participant_ids", []), ensure_ascii=False
                ),
                "status": session.get("status", "active"),
                "last_speaker_id": session.get("last_speaker_id"),
                "last_log_id": session.get("last_log_id"),
                "started_at_sim_datetime": session["started_at_sim_datetime"],
                "last_activity_sim_datetime": session["last_activity_sim_datetime"],
                "last_turn_number": session.get("last_turn_number"),
                "scene_id": session.get("scene_id"),
                "session_kind": session.get("session_kind", "conversation"),
                "closure_reason": session.get("closure_reason"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_conversation_sessions(
        self, story_id: str
    ) -> list[dict[str, Any]]:
        """story_id に属する active な会話 session 一覧を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM conversation_sessions
            WHERE story_id = ? AND status = 'active'
            ORDER BY id ASC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [
            self._decode_conversation_session(row)
            for row in rows
            if self._decode_conversation_session(row) is not None
        ]

    async def get_active_conversation_session(
        self, story_id: str, place_id: str
    ) -> dict[str, Any] | None:
        """指定場所の active な会話 session を1件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM conversation_sessions
            WHERE story_id = ? AND place_id = ? AND status = 'active'
            ORDER BY id DESC
            LIMIT 1;
            """,
            (story_id, place_id),
        )
        row = await cursor.fetchone()
        return self._decode_conversation_session(row)

    async def update_conversation_session(
        self, session_id: int, patch: dict[str, Any]
    ) -> None:
        """conversation_session の一部カラムを更新する。"""
        assert self._conn is not None
        allowed_keys = {
            "place_id",
            "participant_ids",
            "status",
            "last_speaker_id",
            "last_log_id",
            "last_activity_sim_datetime",
            "last_turn_number",
            "scene_id",
            "session_kind",
            "closure_reason",
        }
        assignments: list[str] = []
        params: list[Any] = []

        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            if key == "participant_ids":
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            params.append(value)

        if not assignments:
            return

        assignments.append("updated_at = datetime('now')")
        params.append(session_id)
        await self._conn.execute(
            f"""
            UPDATE conversation_sessions
            SET {", ".join(assignments)}
            WHERE id = ?;
            """,
            params,
        )
        await self._conn.commit()

    async def close_conversation_session(
        self,
        session_id: int,
        *,
        last_activity_sim_datetime: str | None = None,
        last_turn_number: int | None = None,
    ) -> None:
        """conversation_session を closed にする。"""
        patch: dict[str, Any] = {"status": "closed"}
        if last_activity_sim_datetime is not None:
            patch["last_activity_sim_datetime"] = last_activity_sim_datetime
        if last_turn_number is not None:
            patch["last_turn_number"] = last_turn_number
        await self.update_conversation_session(session_id, patch)

    # ------------------------------------------------------------------
    # Memories
    # ------------------------------------------------------------------

    async def get_recent_memories(
        self, story_id: str, char_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """指定キャラクターの最近の記憶を新しい順に最大 limit 件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM memories
            WHERE char_id = ? AND story_id = ?
            ORDER BY created_at DESC
            LIMIT ?;
            """,
            (char_id, story_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def insert_memory(
        self,
        story_id: str,
        char_id: str,
        content: str,
        importance: float = 0.5,
        memory_type: str = "short",
    ) -> None:
        """memories に1行挿入する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO memories (char_id, story_id, memory_type, content, importance)
            VALUES (?, ?, ?, ?, ?);
            """,
            (char_id, story_id, memory_type, content, importance),
        )
        await self._conn.commit()

    async def prune_memories(
        self, story_id: str, char_id: str, limit: int
    ) -> None:
        """memories が limit 件を超えている場合、古い順に超過分を削除する。"""
        assert self._conn is not None
        # 古い順で削除対象の id を取得
        cursor = await self._conn.execute(
            """
            SELECT id FROM memories
            WHERE char_id = ? AND story_id = ?
            ORDER BY created_at ASC
            LIMIT MAX(0, (
                SELECT COUNT(*) FROM memories
                WHERE char_id = ? AND story_id = ?
            ) - ?);
            """,
            (char_id, story_id, char_id, story_id, limit),
        )
        rows = await cursor.fetchall()
        ids_to_delete = [row[0] for row in rows]
        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            await self._conn.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders});",
                ids_to_delete,
            )
            await self._conn.commit()

    # ------------------------------------------------------------------
    # Story Memory
    # ------------------------------------------------------------------

    async def save_story_memory(
        self, story_id: str, memory: dict[str, Any]
    ) -> int:
        """story_memory に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_memory (
                story_id, memory_type, summary, involved_chars,
                trigger_turn, importance, emotional_tone, is_active
            ) VALUES (
                :story_id, :memory_type, :summary, :involved_chars,
                :trigger_turn, :importance, :emotional_tone, :is_active
            );
            """,
            {
                "story_id": story_id,
                "memory_type": memory["memory_type"],
                "summary": memory["summary"],
                "involved_chars": json.dumps(
                    memory.get("involved_chars", []), ensure_ascii=False
                ),
                "trigger_turn": memory["trigger_turn"],
                "importance": memory.get("importance", 0.5),
                "emotional_tone": memory.get("emotional_tone"),
                "is_active": memory.get("is_active", 1),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_relevant_story_memories(
        self, story_id: str, char_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """キャラに関連するストーリーメモリを importance 降順で返す。

        char_id が involved_chars に含まれる、または importance >= 0.7 の
        アクティブな記憶を返す。
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_memory
            WHERE story_id = ?
              AND is_active = 1
              AND (
                EXISTS (
                  SELECT 1 FROM json_each(involved_chars) WHERE value = ?
                )
                OR importance >= 0.7
              )
            ORDER BY importance DESC
            LIMIT ?;
            """,
            (story_id, char_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._decode_story_memory(dict(row)) for row in rows]

    async def get_story_memories_since_turn(
        self, story_id: str, since_turn: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        """since_turn 以降に生成されたアクティブなストーリーメモリを返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_memory
            WHERE story_id = ?
              AND is_active = 1
              AND trigger_turn >= ?
            ORDER BY trigger_turn ASC
            LIMIT ?;
            """,
            (story_id, since_turn, limit),
        )
        rows = await cursor.fetchall()
        return [self._decode_story_memory(dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # Narrative Tensions
    # ------------------------------------------------------------------

    async def insert_tension(self, story_id: str, tension: dict[str, Any]) -> int:
        """narrative_tensions に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO narrative_tensions (
                story_id, tension_type, description, involved_chars,
                intensity, detected_turn, status
            ) VALUES (
                :story_id, :tension_type, :description, :involved_chars,
                :intensity, :detected_turn, :status
            );
            """,
            {
                "story_id": story_id,
                "tension_type": tension["tension_type"],
                "description": tension["description"],
                "involved_chars": json.dumps(tension.get("involved_chars", []), ensure_ascii=False),
                "intensity": tension.get("intensity", 0.3),
                "detected_turn": tension["detected_turn"],
                "status": tension.get("status", "simmering"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_tensions(self, story_id: str) -> list[dict[str, Any]]:
        """status が 'resolved' 以外の緊張を intensity 降順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM narrative_tensions
            WHERE story_id = ? AND status != 'resolved'
            ORDER BY intensity DESC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [self._decode_tension(dict(row)) for row in rows]

    async def update_tension(self, tension_id: int, patch: dict[str, Any]) -> None:
        """narrative_tension の status / intensity / resolved_turn / resolution_note を更新する。"""
        assert self._conn is not None
        allowed_keys = {"status", "intensity", "resolved_turn", "resolution_note"}
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            assignments.append(f"{key} = ?")
            params.append(value)
        if not assignments:
            return
        params.append(tension_id)
        await self._conn.execute(
            f"UPDATE narrative_tensions SET {', '.join(assignments)} WHERE id = ?;",
            params,
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Director Interventions
    # ------------------------------------------------------------------

    async def insert_intervention(self, story_id: str, intervention: dict[str, Any]) -> int:
        """director_interventions に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO director_interventions (
                story_id, intervention_type, title, description, prompt_injection,
                scope, tension_id, active_from_turn, active_until_turn, status
            ) VALUES (
                :story_id, :intervention_type, :title, :description, :prompt_injection,
                :scope, :tension_id, :active_from_turn, :active_until_turn, :status
            );
            """,
            {
                "story_id": story_id,
                "intervention_type": intervention.get("intervention_type") or "",
                "title": intervention.get("title") or "",
                "description": intervention.get("description") or "",
                "prompt_injection": intervention.get("prompt_injection") or "",
                "scope": intervention.get("scope", "all"),
                "tension_id": intervention.get("tension_id"),
                "active_from_turn": intervention["active_from_turn"],
                "active_until_turn": intervention.get("active_until_turn"),
                "status": intervention.get("status", "active"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_interventions(
        self, story_id: str, turn_number: int
    ) -> list[dict[str, Any]]:
        """turn_number に有効な介入を返す。

        status が active / acknowledged かつ active_from_turn <= turn_number
        かつ (active_until_turn IS NULL OR active_until_turn >= turn_number)
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM director_interventions
            WHERE story_id = ?
              AND status IN ('active', 'acknowledged')
              AND active_from_turn <= ?
              AND (active_until_turn IS NULL OR active_until_turn >= ?)
            ORDER BY id ASC;
            """,
            (story_id, turn_number, turn_number),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_interventions_by_ids(
        self,
        intervention_ids: list[int],
    ) -> list[dict[str, Any]]:
        """指定 ID 群の director_interventions を返す。"""
        if not intervention_ids:
            return []
        assert self._conn is not None
        placeholders = ", ".join("?" for _ in intervention_ids)
        cursor = await self._conn.execute(
            f"""
            SELECT *
            FROM director_interventions
            WHERE id IN ({placeholders})
            ORDER BY id ASC;
            """,
            intervention_ids,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_interventions_started_on_turn(
        self,
        story_id: str,
        *,
        active_from_turn: int,
    ) -> list[dict[str, Any]]:
        """指定 turn に開始した director_interventions を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM director_interventions
            WHERE story_id = ?
              AND active_from_turn = ?
            ORDER BY id ASC;
            """,
            (story_id, active_from_turn),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def expire_interventions_before_turn(
        self,
        story_id: str,
        *,
        turn_number: int,
    ) -> list[int]:
        """turn_number より前に期限切れした live intervention を expired に更新する。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id
            FROM director_interventions
            WHERE story_id = ?
              AND status IN ('active', 'acknowledged')
              AND active_until_turn IS NOT NULL
              AND active_until_turn < ?
            ORDER BY id ASC;
            """,
            (story_id, turn_number),
        )
        rows = await cursor.fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        await self._conn.execute(
            f"""
            UPDATE director_interventions
            SET status = 'expired'
            WHERE story_id = ?
              AND id IN ({placeholders});
            """,
            (story_id, *ids),
        )
        await self._conn.commit()
        return ids

    async def get_live_interventions_for_tension_ids(
        self,
        story_id: str,
        tension_ids: list[int],
    ) -> list[dict[str, Any]]:
        """指定 tension_id 群に紐づく live intervention を返す。"""
        if not tension_ids:
            return []
        assert self._conn is not None
        placeholders = ", ".join("?" for _ in tension_ids)
        cursor = await self._conn.execute(
            f"""
            SELECT *
            FROM director_interventions
            WHERE story_id = ?
              AND status IN ('active', 'acknowledged')
              AND tension_id IN ({placeholders})
            ORDER BY id ASC;
            """,
            (story_id, *tension_ids),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_intervention(self, intervention_id: int, patch: dict[str, Any]) -> None:
        """director_intervention の status / resolution_summary を更新する。"""
        assert self._conn is not None
        allowed_keys = {"status", "resolution_summary", "active_until_turn"}
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            assignments.append(f"{key} = ?")
            params.append(value)
        if not assignments:
            return
        params.append(intervention_id)
        await self._conn.execute(
            f"UPDATE director_interventions SET {', '.join(assignments)} WHERE id = ?;",
            params,
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Story Interaction Patterns
    # ------------------------------------------------------------------

    async def insert_interaction_pattern(self, story_id: str, pattern: dict[str, Any]) -> int:
        """story_interaction_patterns に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_interaction_patterns (
                story_id, pattern_type, status, title, description, involved_chars,
                dedupe_key, source_hook_id, source_tension_id, source_scene_id, source_event_id,
                first_detected_turn, last_detected_turn, recurrence_count, intensity, confidence,
                resolved_turn, resolution_note
            ) VALUES (
                :story_id, :pattern_type, :status, :title, :description, :involved_chars,
                :dedupe_key, :source_hook_id, :source_tension_id, :source_scene_id, :source_event_id,
                :first_detected_turn, :last_detected_turn, :recurrence_count, :intensity, :confidence,
                :resolved_turn, :resolution_note
            );
            """,
            {
                "story_id": story_id,
                "pattern_type": pattern["pattern_type"],
                "status": pattern.get("status", "active"),
                "title": pattern["title"],
                "description": pattern["description"],
                "involved_chars": json.dumps(pattern.get("involved_chars", []), ensure_ascii=False),
                "dedupe_key": pattern["dedupe_key"],
                "source_hook_id": pattern.get("source_hook_id"),
                "source_tension_id": pattern.get("source_tension_id"),
                "source_scene_id": pattern.get("source_scene_id"),
                "source_event_id": pattern.get("source_event_id"),
                "first_detected_turn": pattern["first_detected_turn"],
                "last_detected_turn": pattern["last_detected_turn"],
                "recurrence_count": pattern.get("recurrence_count", 1),
                "intensity": pattern.get("intensity", 0.5),
                "confidence": pattern.get("confidence", 0.5),
                "resolved_turn": pattern.get("resolved_turn"),
                "resolution_note": pattern.get("resolution_note"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_interaction_patterns(self, story_id: str) -> list[dict[str, Any]]:
        """active な interaction pattern を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_interaction_patterns
            WHERE story_id = ?
              AND status = 'active'
            ORDER BY last_detected_turn DESC, intensity DESC, id DESC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [self._decode_interaction_pattern(dict(row)) for row in rows]

    async def get_recent_interaction_patterns(
        self,
        story_id: str,
        *,
        since_turn: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """since_turn 以降に更新された pattern を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_interaction_patterns
            WHERE story_id = ?
              AND last_detected_turn >= ?
            ORDER BY last_detected_turn DESC, id DESC
            LIMIT ?;
            """,
            (story_id, since_turn, limit),
        )
        rows = await cursor.fetchall()
        return [self._decode_interaction_pattern(dict(row)) for row in rows]

    async def find_active_interaction_pattern_by_dedupe_key(
        self,
        story_id: str,
        dedupe_key: str,
    ) -> dict[str, Any] | None:
        """story_id + dedupe_key に一致する active pattern を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_interaction_patterns
            WHERE story_id = ?
              AND dedupe_key = ?
              AND status = 'active'
            ORDER BY id DESC
            LIMIT 1;
            """,
            (story_id, dedupe_key),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._decode_interaction_pattern(dict(row))

    async def update_interaction_pattern(self, pattern_id: int, patch: dict[str, Any]) -> None:
        """interaction pattern の一部カラムを更新する。"""
        assert self._conn is not None
        allowed_keys = {
            "status",
            "title",
            "description",
            "last_detected_turn",
            "recurrence_count",
            "intensity",
            "confidence",
            "resolved_turn",
            "resolution_note",
        }
        assignments: list[str] = ["updated_at = datetime('now')"]
        params: list[Any] = []
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            assignments.append(f"{key} = ?")
            params.append(value)
        if len(assignments) == 1:
            return
        params.append(pattern_id)
        await self._conn.execute(
            f"UPDATE story_interaction_patterns SET {', '.join(assignments)} WHERE id = ?;",
            params,
        )
        await self._conn.commit()

    async def resolve_interaction_pattern(
        self,
        pattern_id: int,
        *,
        resolved_turn: int,
        resolution_note: str | None,
    ) -> None:
        """interaction pattern を resolved に更新する。"""
        await self.update_interaction_pattern(
            pattern_id,
            {
                "status": "resolved",
                "resolved_turn": resolved_turn,
                "resolution_note": resolution_note,
            },
        )

    # ------------------------------------------------------------------
    # Conversation Motifs
    # ------------------------------------------------------------------

    def _conversation_motif_defaults_by_id(self) -> dict[str, dict[str, Any]]:
        return {item["motif_id"]: dict(item) for item in DEFAULT_CONVERSATION_MOTIF_SETTINGS}

    def _decode_conversation_motif_setting(self, row: dict[str, Any]) -> dict[str, Any]:
        row["enabled"] = bool(int(row.get("enabled") or 0))
        row["cooldown_turns"] = int(row.get("cooldown_turns") or 0)
        return row

    def _decode_conversation_motif_run(self, row: dict[str, Any]) -> dict[str, Any]:
        for key in (
            "id",
            "seed_hook_id",
            "seed_log_id",
            "started_turn",
            "last_advanced_turn",
            "cooldown_until_turn",
        ):
            if row.get(key) is not None:
                row[key] = int(row[key])
        return row

    async def get_conversation_motif_settings(self, story_id: str) -> list[dict[str, Any]]:
        """story ごとの会話モチーフ設定を組み込み default と merge して返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM conversation_motif_settings
            WHERE story_id = ?
            ORDER BY motif_id;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        merged = self._conversation_motif_defaults_by_id()
        for row in rows:
            decoded = self._decode_conversation_motif_setting(dict(row))
            motif_id = str(decoded.get("motif_id") or "")
            if motif_id in merged:
                merged[motif_id].update(decoded)
            else:
                merged[motif_id] = decoded
        return [self._decode_conversation_motif_setting(item) for item in merged.values()]

    async def upsert_conversation_motif_settings(
        self,
        story_id: str,
        settings: list[dict[str, Any]],
    ) -> None:
        """会話モチーフ設定を story 単位で upsert する。"""
        assert self._conn is not None
        allowed = self._conversation_motif_defaults_by_id()
        for item in settings:
            motif_id = str(item.get("motif_id") or "").strip()
            if motif_id not in allowed:
                continue
            strength = str(item.get("strength") or allowed[motif_id]["strength"]).strip()
            if strength not in {"subtle", "moderate", "strong"}:
                strength = allowed[motif_id]["strength"]
            cooldown_turns = int(item.get("cooldown_turns", allowed[motif_id]["cooldown_turns"]))
            cooldown_turns = max(0, min(50, cooldown_turns))
            await self._conn.execute(
                """
                INSERT INTO conversation_motif_settings (
                    story_id, motif_id, enabled, strength, cooldown_turns, updated_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(story_id, motif_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    strength = excluded.strength,
                    cooldown_turns = excluded.cooldown_turns,
                    updated_at = datetime('now');
                """,
                (
                    story_id,
                    motif_id,
                    1 if bool(item.get("enabled")) else 0,
                    strength,
                    cooldown_turns,
                ),
            )
        await self._conn.commit()

    async def insert_conversation_motif_run(self, story_id: str, run: dict[str, Any]) -> int:
        """conversation_motif_runs に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO conversation_motif_runs (
                story_id, motif_id, status, stage, seed_hook_id, seed_log_id,
                owner_char_id, pickup_char_id, place_id, title, description,
                started_turn, last_advanced_turn, cooldown_until_turn
            ) VALUES (
                :story_id, :motif_id, :status, :stage, :seed_hook_id, :seed_log_id,
                :owner_char_id, :pickup_char_id, :place_id, :title, :description,
                :started_turn, :last_advanced_turn, :cooldown_until_turn
            );
            """,
            {
                "story_id": story_id,
                "motif_id": run["motif_id"],
                "status": run.get("status", "active"),
                "stage": run.get("stage", "seeded"),
                "seed_hook_id": run.get("seed_hook_id"),
                "seed_log_id": run.get("seed_log_id"),
                "owner_char_id": run.get("owner_char_id"),
                "pickup_char_id": run.get("pickup_char_id"),
                "place_id": run.get("place_id"),
                "title": run.get("title") or run["motif_id"],
                "description": run.get("description") or "",
                "started_turn": int(run.get("started_turn") or 0),
                "last_advanced_turn": int(run.get("last_advanced_turn") or 0),
                "cooldown_until_turn": run.get("cooldown_until_turn"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    async def get_active_conversation_motif_runs(self, story_id: str) -> list[dict[str, Any]]:
        """active な conversation motif run を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM conversation_motif_runs
            WHERE story_id = ?
              AND status = 'active'
            ORDER BY last_advanced_turn ASC, id ASC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [self._decode_conversation_motif_run(dict(row)) for row in rows]

    async def find_active_conversation_motif_run_by_seed_hook(
        self,
        story_id: str,
        seed_hook_id: int,
    ) -> dict[str, Any] | None:
        """seed hook に紐づく active motif run を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM conversation_motif_runs
            WHERE story_id = ?
              AND seed_hook_id = ?
              AND status = 'active'
            ORDER BY id DESC
            LIMIT 1;
            """,
            (story_id, seed_hook_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._decode_conversation_motif_run(dict(row))

    async def update_conversation_motif_run(
        self,
        run_id: int,
        patch: dict[str, Any],
    ) -> None:
        """conversation motif run の一部カラムを更新する。"""
        assert self._conn is not None
        allowed_keys = {
            "status",
            "stage",
            "pickup_char_id",
            "place_id",
            "last_advanced_turn",
            "cooldown_until_turn",
        }
        assignments: list[str] = ["updated_at = datetime('now')"]
        params: list[Any] = []
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            assignments.append(f"{key} = ?")
            params.append(value)
        if len(assignments) == 1:
            return
        params.append(run_id)
        await self._conn.execute(
            f"UPDATE conversation_motif_runs SET {', '.join(assignments)} WHERE id = ?;",
            params,
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Story Episodes
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_story_episode(row: dict[str, Any]) -> dict[str, Any]:
        raw_focus = row.get("focus_char_ids")
        if isinstance(raw_focus, str) and raw_focus:
            row["focus_char_ids"] = json.loads(raw_focus)
        elif raw_focus in (None, ""):
            row["focus_char_ids"] = []
        raw_hooks = row.get("carry_over_hook_ids")
        if isinstance(raw_hooks, str) and raw_hooks:
            row["carry_over_hook_ids"] = json.loads(raw_hooks)
        elif raw_hooks in (None, ""):
            row["carry_over_hook_ids"] = []
        return row

    @staticmethod
    def _decode_relationship_mode(row: dict[str, Any]) -> dict[str, Any]:
        return row

    def _decode_story_canon_bit(self, row: dict[str, Any]) -> dict[str, Any]:
        row = self._decode_json_field(row, "focus_char_ids", [])
        row = self._decode_json_field(row, "evidence_sources", [])
        return row

    def _decode_story_dramatic_pressure(self, row: dict[str, Any]) -> dict[str, Any]:
        return self._decode_json_field(row, "focus_char_ids", [])

    async def insert_story_episode(self, story_id: str, episode: dict[str, Any]) -> int:
        """story_episodes に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_episodes (
                story_id, status, episode_type, goal, stakes, active_pattern_id, active_pattern_type,
                focus_char_ids, carry_over_hook_ids, focus_place_id, opened_turn, last_progress_turn,
                closed_turn, exit_condition, summary
            ) VALUES (
                :story_id, :status, :episode_type, :goal, :stakes, :active_pattern_id, :active_pattern_type,
                :focus_char_ids, :carry_over_hook_ids, :focus_place_id, :opened_turn, :last_progress_turn,
                :closed_turn, :exit_condition, :summary
            );
            """,
            {
                "story_id": story_id,
                "status": episode.get("status", "active"),
                "episode_type": episode["episode_type"],
                "goal": episode["goal"],
                "stakes": episode.get("stakes"),
                "active_pattern_id": episode.get("active_pattern_id"),
                "active_pattern_type": episode.get("active_pattern_type"),
                "focus_char_ids": json.dumps(episode.get("focus_char_ids", []), ensure_ascii=False),
                "carry_over_hook_ids": json.dumps(
                    episode.get("carry_over_hook_ids", []),
                    ensure_ascii=False,
                ),
                "focus_place_id": episode.get("focus_place_id"),
                "opened_turn": episode["opened_turn"],
                "last_progress_turn": episode.get("last_progress_turn"),
                "closed_turn": episode.get("closed_turn"),
                "exit_condition": episode.get("exit_condition"),
                "summary": episode.get("summary"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_story_episode(self, story_id: str) -> dict[str, Any] | None:
        """story の active episode を1件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_episodes
            WHERE story_id = ? AND status = 'active'
            ORDER BY opened_turn DESC, id DESC
            LIMIT 1;
            """,
            (story_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._decode_story_episode(dict(row))

    async def get_recent_story_episodes(
        self,
        story_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """story の recent episodes を新しい順に返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_episodes
            WHERE story_id = ?
            ORDER BY COALESCE(closed_turn, opened_turn) DESC, id DESC
            LIMIT ?;
            """,
            (story_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._decode_story_episode(dict(row)) for row in rows]

    async def update_story_episode(self, episode_id: int, patch: dict[str, Any]) -> None:
        """story episode の一部カラムを更新する。"""
        assert self._conn is not None
        allowed_keys = {
            "status",
            "goal",
            "stakes",
            "active_pattern_id",
            "active_pattern_type",
            "focus_char_ids",
            "carry_over_hook_ids",
            "focus_place_id",
            "last_progress_turn",
            "closed_turn",
            "exit_condition",
            "summary",
        }
        assignments: list[str] = ["updated_at = datetime('now')"]
        params: list[Any] = []
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            if key in {"focus_char_ids", "carry_over_hook_ids"}:
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            params.append(value)
        if len(assignments) == 1:
            return
        params.append(episode_id)
        await self._conn.execute(
            f"UPDATE story_episodes SET {', '.join(assignments)} WHERE id = ?;",
            params,
        )
        await self._conn.commit()

    async def close_story_episode(
        self,
        episode_id: int,
        *,
        closed_turn: int,
        exit_condition: str,
        summary: str | None,
    ) -> None:
        """story episode を closed に更新する。"""
        await self.update_story_episode(
            episode_id,
            {
                "status": "closed",
                "closed_turn": closed_turn,
                "exit_condition": exit_condition,
                "summary": summary,
            },
        )

    # ------------------------------------------------------------------
    # Character Evolution
    # ------------------------------------------------------------------

    async def insert_evolution(self, story_id: str, evolution: dict[str, Any]) -> int:
        """character_evolution に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO character_evolution (
                char_id, story_id, turn_number, field,
                previous_value, new_value, reason, source_memory_id, source_canon_bit_id
            ) VALUES (
                :char_id, :story_id, :turn_number, :field,
                :previous_value, :new_value, :reason, :source_memory_id, :source_canon_bit_id
            );
            """,
            {
                "char_id": evolution["char_id"],
                "story_id": story_id,
                "turn_number": evolution["turn_number"],
                "field": evolution["field"],
                "previous_value": evolution.get("previous_value"),
                "new_value": evolution["new_value"],
                "reason": evolution["reason"],
                "source_memory_id": evolution.get("source_memory_id"),
                "source_canon_bit_id": evolution.get("source_canon_bit_id"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_latest_evolution_overlay(
        self, story_id: str, char_id: str
    ) -> dict[str, str]:
        """キャラの現在の evolution overlay を {field: new_value} の dict で返す。

        field ごとに最大 turn_number の new_value を返す。
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT field, new_value FROM character_evolution e1
            WHERE char_id = ? AND story_id = ?
              AND turn_number = (
                  SELECT MAX(turn_number) FROM character_evolution e2
                  WHERE e2.char_id = e1.char_id
                    AND e2.story_id = e1.story_id
                    AND e2.field = e1.field
              )
            ORDER BY field;
            """,
            (char_id, story_id),
        )
        rows = await cursor.fetchall()
        return {row["field"]: row["new_value"] for row in rows}

    # ------------------------------------------------------------------
    # Story Emergence Foundations
    # ------------------------------------------------------------------

    async def insert_story_scene(self, story_id: str, scene: dict[str, Any]) -> int:
        """story_scenes に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_scenes (
                story_id, scene_type, status, place_id, focus_char_ids,
                objective, dominant_tension_id, opened_turn, outcome_type, outcome_summary
            ) VALUES (
                :story_id, :scene_type, :status, :place_id, :focus_char_ids,
                :objective, :dominant_tension_id, :opened_turn, :outcome_type, :outcome_summary
            );
            """,
            {
                "story_id": story_id,
                "scene_type": scene.get("scene_type", "conversation"),
                "status": scene.get("status", "active"),
                "place_id": scene.get("place_id"),
                "focus_char_ids": json.dumps(scene.get("focus_char_ids", []), ensure_ascii=False),
                "objective": scene.get("objective"),
                "dominant_tension_id": scene.get("dominant_tension_id"),
                "opened_turn": scene.get("opened_turn"),
                "outcome_type": scene.get("outcome_type"),
                "outcome_summary": scene.get("outcome_summary"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_story_scenes(self, story_id: str) -> list[dict[str, Any]]:
        """active な story_scenes を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_scenes
            WHERE story_id = ? AND status = 'active'
            ORDER BY id ASC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        decoded: list[dict[str, Any]] = []
        for row in rows:
            decoded.append(self._decode_json_field(dict(row), "focus_char_ids", []))
        return decoded

    async def get_story_scene(self, scene_id: int) -> dict[str, Any] | None:
        """scene_id に一致する story_scene を1件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_scenes
            WHERE id = ?
            LIMIT 1;
            """,
            (scene_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._decode_json_field(dict(row), "focus_char_ids", [])

    async def get_recent_closed_story_scenes(
        self,
        story_id: str,
        *,
        since_turn: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """since_turn 以降の closed story_scene を古い順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_scenes
            WHERE story_id = ?
              AND status = 'closed'
              AND COALESCE(closed_turn, opened_turn, 0) >= ?
            ORDER BY COALESCE(closed_turn, opened_turn, 0) ASC, id ASC
            LIMIT ?;
            """,
            (story_id, since_turn, limit),
        )
        rows = await cursor.fetchall()
        return [self._decode_json_field(dict(row), "focus_char_ids", []) for row in rows]

    async def get_closed_story_scene_summaries(
        self,
        story_id: str,
        *,
        since_turn: int,
        limit: int,
    ) -> list[str]:
        """closed_turn 以降の closed scene の outcome_summary を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT outcome_summary
            FROM story_scenes
            WHERE story_id = ?
              AND status = 'closed'
              AND closed_turn IS NOT NULL
              AND closed_turn >= ?
              AND outcome_summary IS NOT NULL
              AND trim(outcome_summary) != ''
            ORDER BY closed_turn DESC, id DESC
            LIMIT ?;
            """,
            (story_id, since_turn, limit),
        )
        rows = await cursor.fetchall()
        return [str(row["outcome_summary"]) for row in rows]

    async def get_recent_closed_story_scenes(
        self,
        story_id: str,
        *,
        since_turn: int,
        limit: int,
        scene_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """closed story_scenes を新しい順で返す。"""
        assert self._conn is not None
        params: list[Any] = [story_id, since_turn]
        query = """
            SELECT *
            FROM story_scenes
            WHERE story_id = ?
              AND status = 'closed'
              AND closed_turn IS NOT NULL
              AND closed_turn >= ?
        """
        if scene_type is not None:
            query += " AND scene_type = ?"
            params.append(scene_type)
        query += """
            ORDER BY closed_turn DESC, id DESC
            LIMIT ?;
        """
        params.append(limit)
        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        decoded: list[dict[str, Any]] = []
        for row in rows:
            decoded.append(self._decode_json_field(dict(row), "focus_char_ids", []))
        return decoded

    async def update_story_scene(self, scene_id: int, patch: dict[str, Any]) -> None:
        """story_scene の一部カラムを更新する。"""
        assert self._conn is not None
        allowed_keys = {
            "status",
            "place_id",
            "focus_char_ids",
            "objective",
            "dominant_tension_id",
            "closed_turn",
            "outcome_type",
            "outcome_summary",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            if key == "focus_char_ids":
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            params.append(value)
        if not assignments:
            return
        assignments.append("updated_at = datetime('now')")
        params.append(scene_id)
        await self._conn.execute(
            f"UPDATE story_scenes SET {', '.join(assignments)} WHERE id = ?;",
            params,
        )
        await self._conn.commit()

    async def close_story_scene(
        self,
        scene_id: int,
        *,
        outcome_type: str,
        outcome_summary: str,
        closed_turn: int,
    ) -> None:
        await self.update_story_scene(
            scene_id,
            {
                "status": "closed",
                "outcome_type": outcome_type,
                "outcome_summary": outcome_summary,
                "closed_turn": closed_turn,
            },
        )

    async def replace_scene_participants(
        self, scene_id: int, participants: list[dict[str, Any]]
    ) -> None:
        """scene_participants を scene 単位で置き換える。"""
        assert self._conn is not None
        existing_rows = await self.get_scene_participants(scene_id)
        existing_by_char = {row["char_id"]: row for row in existing_rows}
        await self._conn.execute("DELETE FROM scene_participants WHERE scene_id = ?;", (scene_id,))
        for participant in participants:
            existing = existing_by_char.get(participant["char_id"], {})
            await self._conn.execute(
                """
                INSERT INTO scene_participants (
                    scene_id, char_id, role, join_turn, leave_turn,
                    speak_budget, times_spoken, last_spoken_turn, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    scene_id,
                    participant["char_id"],
                    participant.get("role", "observer"),
                    participant.get("join_turn"),
                    participant.get("leave_turn"),
                    participant.get("speak_budget", 0),
                    participant.get("times_spoken", existing.get("times_spoken", 0)),
                    participant.get("last_spoken_turn", existing.get("last_spoken_turn")),
                    participant.get("state", "active"),
                ),
            )
        await self._conn.commit()

    async def get_scene_participants(self, scene_id: int) -> list[dict[str, Any]]:
        """scene_id に紐づく participants を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM scene_participants
            WHERE scene_id = ?
            ORDER BY id ASC;
            """,
            (scene_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_scene_logs(self, scene_id: int, *, limit: int) -> list[dict[str, Any]]:
        """scene_id に紐づく chat_logs を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM chat_logs
            WHERE scene_id = ?
            ORDER BY id DESC
            LIMIT ?;
            """,
            (scene_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def record_scene_participant_turn(
        self, scene_id: int, char_id: str, *, turn_number: int
    ) -> None:
        """scene participant の発話実績を更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE scene_participants
            SET times_spoken = times_spoken + 1,
                last_spoken_turn = ?,
                updated_at = datetime('now')
            WHERE scene_id = ? AND char_id = ?;
            """,
            (turn_number, scene_id, char_id),
        )
        await self._conn.commit()

    async def insert_story_hook(self, story_id: str, hook: dict[str, Any]) -> int:
        """story_hooks に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_hooks (
                story_id, hook_type, status, owner_char_id, target_char_id,
                title, description, priority, due_turn, source_scene_id,
                source_log_id, resolution_log_id, source_canon_bit_id
            ) VALUES (
                :story_id, :hook_type, :status, :owner_char_id, :target_char_id,
                :title, :description, :priority, :due_turn, :source_scene_id,
                :source_log_id, :resolution_log_id, :source_canon_bit_id
            );
            """,
            {
                "story_id": story_id,
                "hook_type": hook["hook_type"],
                "status": hook.get("status", "open"),
                "owner_char_id": hook.get("owner_char_id"),
                "target_char_id": hook.get("target_char_id"),
                "title": hook["title"],
                "description": hook["description"],
                "priority": hook.get("priority", 0.5),
                "due_turn": hook.get("due_turn"),
                "source_scene_id": hook.get("source_scene_id"),
                "source_log_id": hook.get("source_log_id"),
                "resolution_log_id": hook.get("resolution_log_id"),
                "source_canon_bit_id": hook.get("source_canon_bit_id"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_open_story_hooks(self, story_id: str) -> list[dict[str, Any]]:
        """open 状態の story_hooks を priority 降順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_hooks
            WHERE story_id = ? AND status = 'open'
            ORDER BY priority DESC, id ASC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_resolved_story_hooks(
        self,
        story_id: str,
        *,
        since_turn: int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """resolved 状態の story_hooks を resolved_turn 降順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_hooks
            WHERE story_id = ?
              AND status = 'resolved'
              AND resolved_turn IS NOT NULL
              AND resolved_turn >= ?
            ORDER BY resolved_turn DESC, id DESC
            LIMIT ?;
            """,
            (story_id, since_turn, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    def _decode_interaction_pattern(self, pattern: dict[str, Any]) -> dict[str, Any]:
        """story_interaction_patterns row の JSON field を decode する。"""
        return self._decode_json_field(pattern, "involved_chars", [])

    async def get_open_story_hooks_for_scene(
        self,
        story_id: str,
        scene_id: int,
    ) -> list[dict[str, Any]]:
        """scene に紐づく open hook を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_hooks
            WHERE story_id = ?
              AND status = 'open'
              AND source_scene_id = ?
            ORDER BY priority DESC, id ASC;
            """,
            (story_id, scene_id),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_open_hooks_for_canon_bit(
        self,
        story_id: str,
        canon_bit_id: int,
    ) -> list[dict[str, Any]]:
        """source_canon_bit_id に紐づく open hook を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_hooks
            WHERE story_id = ?
              AND status = 'open'
              AND source_canon_bit_id = ?
            ORDER BY priority DESC, id ASC;
            """,
            (story_id, canon_bit_id),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def resolve_story_hook(
        self,
        hook_id: int,
        *,
        resolution_log_id: int | None,
        resolved_turn: int | None,
        summary: str | None,
    ) -> None:
        """story_hook を resolved に更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE story_hooks
            SET status = 'resolved',
                resolution_log_id = ?,
                resolved_turn = ?,
                resolution_summary = ?,
                updated_at = datetime('now')
            WHERE id = ?;
            """,
            (resolution_log_id, resolved_turn, summary, hook_id),
        )
        await self._conn.commit()

    async def get_relevant_story_hooks(
        self,
        story_id: str,
        char_id: str,
        place_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """キャラに関連する open hook を priority 順で返す。"""
        assert self._conn is not None
        if place_id is None:
            cursor = await self._conn.execute(
                """
                SELECT * FROM story_hooks
                WHERE story_id = ?
                  AND status = 'open'
                  AND (owner_char_id = ? OR target_char_id = ?)
                ORDER BY priority DESC, id ASC
                LIMIT ?;
                """,
                (story_id, char_id, char_id, limit),
            )
        else:
            cursor = await self._conn.execute(
                """
                SELECT * FROM story_hooks
                WHERE story_id = ?
                  AND status = 'open'
                  AND (
                    owner_char_id = ?
                    OR target_char_id = ?
                    OR (
                      target_char_id IS NULL
                      AND source_scene_id IN (
                        SELECT id FROM story_scenes
                        WHERE story_id = ?
                          AND status = 'active'
                          AND place_id = ?
                      )
                    )
                  )
                ORDER BY priority DESC, id ASC
                LIMIT ?;
                """,
                (story_id, char_id, char_id, story_id, place_id, limit),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_relationship_snapshot(
        self,
        story_id: str,
        char_id_from: str,
        char_id_to: str,
        patch: dict[str, Any],
    ) -> None:
        """relationships を UPSERT で更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO relationships (
                story_id, char_id_from, char_id_to, trust, affinity, tension,
                familiarity, last_event_turn, last_event_summary
            ) VALUES (
                :story_id, :char_id_from, :char_id_to, :trust, :affinity, :tension,
                :familiarity, :last_event_turn, :last_event_summary
            )
            ON CONFLICT(story_id, char_id_from, char_id_to) DO UPDATE SET
                trust = excluded.trust,
                affinity = excluded.affinity,
                tension = excluded.tension,
                familiarity = excluded.familiarity,
                last_event_turn = excluded.last_event_turn,
                last_event_summary = excluded.last_event_summary,
                updated_at = datetime('now');
            """,
            {
                "story_id": story_id,
                "char_id_from": char_id_from,
                "char_id_to": char_id_to,
                "trust": patch.get("trust", 0.5),
                "affinity": patch.get("affinity", 0.5),
                "tension": patch.get("tension", 0.0),
                "familiarity": patch.get("familiarity", 0.5),
                "last_event_turn": patch.get("last_event_turn"),
                "last_event_summary": patch.get("last_event_summary"),
            },
        )
        await self._conn.commit()

    async def insert_relationship_event(
        self,
        story_id: str,
        event: dict[str, Any],
    ) -> int:
        """relationship_events に1行挿入する。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO relationship_events (
                story_id, char_id_from, char_id_to, event_type,
                delta_trust, delta_affinity, delta_tension, summary,
                source_log_id, scene_id, turn_number
            ) VALUES (
                :story_id, :char_id_from, :char_id_to, :event_type,
                :delta_trust, :delta_affinity, :delta_tension, :summary,
                :source_log_id, :scene_id, :turn_number
            );
            """,
            {
                "story_id": story_id,
                "char_id_from": event["char_id_from"],
                "char_id_to": event["char_id_to"],
                "event_type": event["event_type"],
                "delta_trust": event.get("delta_trust", 0.0),
                "delta_affinity": event.get("delta_affinity", 0.0),
                "delta_tension": event.get("delta_tension", 0.0),
                "summary": event.get("summary"),
                "source_log_id": event.get("source_log_id"),
                "scene_id": event.get("scene_id"),
                "turn_number": event.get("turn_number"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_relationship_summary(
        self,
        story_id: str,
        char_id: str,
        counterpart_ids: list[str],
        limit: int,
    ) -> list[str]:
        """キャラから見た相手との trust/tension 要約を返す。"""
        if not counterpart_ids:
            return []
        assert self._conn is not None
        placeholders = ", ".join("?" for _ in counterpart_ids)
        cursor = await self._conn.execute(
            f"""
            SELECT char_id_to, trust, tension
            FROM relationships
            WHERE story_id = ?
              AND char_id_from = ?
              AND char_id_to IN ({placeholders})
            ORDER BY updated_at DESC, char_id_to ASC
            LIMIT ?;
            """,
            (story_id, char_id, *counterpart_ids, limit),
        )
        rows = await cursor.fetchall()
        return [
            f"{row['char_id_to']} への信頼は {row['trust']:.2f}、緊張は {row['tension']:.2f}。"
            for row in rows
        ]

    async def get_relationship_snapshots(self, story_id: str) -> list[dict[str, Any]]:
        """story の relationships snapshot を全件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM relationships
            WHERE story_id = ?
            ORDER BY updated_at DESC, char_id_from ASC, char_id_to ASC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def insert_relationship_mode(
        self,
        story_id: str,
        char_id_from: str,
        char_id_to: str,
        mode: dict[str, Any],
    ) -> int:
        """relationship_modes に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO relationship_modes (
                story_id, char_id_from, char_id_to, mode_type, status, summary,
                confidence, intensity, source_pattern_id, source_episode_id,
                first_detected_turn, last_reinforced_turn, last_trigger_event_id
            ) VALUES (
                :story_id, :char_id_from, :char_id_to, :mode_type, :status, :summary,
                :confidence, :intensity, :source_pattern_id, :source_episode_id,
                :first_detected_turn, :last_reinforced_turn, :last_trigger_event_id
            );
            """,
            {
                "story_id": story_id,
                "char_id_from": char_id_from,
                "char_id_to": char_id_to,
                "mode_type": mode["mode_type"],
                "status": mode.get("status", "active"),
                "summary": mode.get("summary"),
                "confidence": mode.get("confidence", 0.5),
                "intensity": mode.get("intensity", 0.5),
                "source_pattern_id": mode.get("source_pattern_id"),
                "source_episode_id": mode.get("source_episode_id"),
                "first_detected_turn": mode["first_detected_turn"],
                "last_reinforced_turn": mode["last_reinforced_turn"],
                "last_trigger_event_id": mode.get("last_trigger_event_id"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_relationship_modes(self, story_id: str) -> list[dict[str, Any]]:
        """active な relationship mode を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM relationship_modes
            WHERE story_id = ? AND status = 'active'
            ORDER BY last_reinforced_turn DESC, intensity DESC, id DESC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [self._decode_relationship_mode(dict(row)) for row in rows]

    async def get_active_relationship_mode_pair(
        self,
        story_id: str,
        char_id_from: str,
        char_id_to: str,
    ) -> dict[str, Any] | None:
        """directed pair の active relationship mode を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM relationship_modes
            WHERE story_id = ?
              AND char_id_from = ?
              AND char_id_to = ?
              AND status = 'active'
            ORDER BY last_reinforced_turn DESC, intensity DESC, id DESC
            LIMIT 1;
            """,
            (story_id, char_id_from, char_id_to),
        )
        row = await cursor.fetchone()
        return self._decode_relationship_mode(dict(row)) if row is not None else None

    async def get_active_relationship_modes_pair(
        self,
        story_id: str,
        char_id_from: str,
        char_id_to: str,
    ) -> list[dict[str, Any]]:
        """directed pair の active relationship mode 一覧を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM relationship_modes
            WHERE story_id = ?
              AND char_id_from = ?
              AND char_id_to = ?
              AND status = 'active'
            ORDER BY intensity DESC, confidence DESC, last_reinforced_turn DESC, id DESC;
            """,
            (story_id, char_id_from, char_id_to),
        )
        rows = await cursor.fetchall()
        return [self._decode_relationship_mode(dict(row)) for row in rows]

    async def find_active_relationship_mode(
        self,
        story_id: str,
        char_id_from: str,
        char_id_to: str,
        mode_type: str,
    ) -> dict[str, Any] | None:
        """directed pair + mode_type の active relationship mode を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM relationship_modes
            WHERE story_id = ?
              AND char_id_from = ?
              AND char_id_to = ?
              AND mode_type = ?
              AND status = 'active'
            ORDER BY last_reinforced_turn DESC, intensity DESC, id DESC
            LIMIT 1;
            """,
            (story_id, char_id_from, char_id_to, mode_type),
        )
        row = await cursor.fetchone()
        return self._decode_relationship_mode(dict(row)) if row is not None else None

    async def get_recent_relationship_modes(
        self,
        story_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """recent relationship modes を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM relationship_modes
            WHERE story_id = ?
            ORDER BY last_reinforced_turn DESC, id DESC
            LIMIT ?;
            """,
            (story_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._decode_relationship_mode(dict(row)) for row in rows]

    async def update_relationship_mode(self, mode_id: int, patch: dict[str, Any]) -> None:
        """relationship mode の一部カラムを更新する。"""
        assert self._conn is not None
        allowed_keys = {
            "mode_type",
            "status",
            "summary",
            "confidence",
            "intensity",
            "source_pattern_id",
            "source_episode_id",
            "last_reinforced_turn",
            "last_trigger_event_id",
        }
        assignments: list[str] = ["updated_at = datetime('now')"]
        params: list[Any] = []
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            assignments.append(f"{key} = ?")
            params.append(value)
        if len(assignments) == 1:
            return
        params.append(mode_id)
        await self._conn.execute(
            f"UPDATE relationship_modes SET {', '.join(assignments)} WHERE id = ?;",
            params,
        )
        await self._conn.commit()

    async def deactivate_relationship_mode(
        self,
        mode_id: int,
        *,
        summary: str | None = None,
    ) -> None:
        """relationship mode を inactive にする。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE relationship_modes
            SET status = 'inactive',
                summary = COALESCE(?, summary),
                updated_at = datetime('now')
            WHERE id = ?;
            """,
            (summary, mode_id),
        )
        await self._conn.commit()

    async def insert_story_canon_bit(self, story_id: str, bit: dict[str, Any]) -> int:
        """story_canon_bits に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_canon_bits (
                story_id, bit_type, motif_key, canon_level, status, title, summary,
                focus_char_ids, focus_place_id, dedupe_key, evidence_sources,
                anchor_pattern_id, anchor_episode_id, anchor_relationship_mode_id, anchor_scene_id,
                first_detected_turn, last_reinforced_turn, recurrence_count,
                confidence, novelty, intent_alignment, last_reignited_turn, reignition_count,
                last_writeback_turn, writeback_count
            ) VALUES (
                :story_id, :bit_type, :motif_key, :canon_level, :status, :title, :summary,
                :focus_char_ids, :focus_place_id, :dedupe_key, :evidence_sources,
                :anchor_pattern_id, :anchor_episode_id, :anchor_relationship_mode_id, :anchor_scene_id,
                :first_detected_turn, :last_reinforced_turn, :recurrence_count,
                :confidence, :novelty, :intent_alignment, :last_reignited_turn, :reignition_count,
                :last_writeback_turn, :writeback_count
            );
            """,
            {
                "story_id": story_id,
                "bit_type": bit["bit_type"],
                "motif_key": bit["motif_key"],
                "canon_level": bit.get("canon_level", "momentary_bit"),
                "status": bit.get("status", "active"),
                "title": bit.get("title"),
                "summary": bit.get("summary"),
                "focus_char_ids": json.dumps(bit.get("focus_char_ids", []), ensure_ascii=False),
                "focus_place_id": bit.get("focus_place_id"),
                "dedupe_key": bit["dedupe_key"],
                "evidence_sources": json.dumps(bit.get("evidence_sources", []), ensure_ascii=False),
                "anchor_pattern_id": bit.get("anchor_pattern_id"),
                "anchor_episode_id": bit.get("anchor_episode_id"),
                "anchor_relationship_mode_id": bit.get("anchor_relationship_mode_id"),
                "anchor_scene_id": bit.get("anchor_scene_id"),
                "first_detected_turn": bit["first_detected_turn"],
                "last_reinforced_turn": bit["last_reinforced_turn"],
                "recurrence_count": bit.get("recurrence_count", 1),
                "confidence": bit.get("confidence", 0.5),
                "novelty": bit.get("novelty", 0.5),
                "intent_alignment": bit.get("intent_alignment", 0.55),
                "last_reignited_turn": bit.get("last_reignited_turn"),
                "reignition_count": bit.get("reignition_count", 0),
                "last_writeback_turn": bit.get("last_writeback_turn"),
                "writeback_count": bit.get("writeback_count", 0),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_story_canon_bits(self, story_id: str) -> list[dict[str, Any]]:
        """active な canon bit を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_canon_bits
            WHERE story_id = ? AND status = 'active'
            ORDER BY last_reinforced_turn DESC, recurrence_count DESC, id DESC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [self._decode_story_canon_bit(dict(row)) for row in rows]

    async def get_reignitable_story_canon_bits(self, story_id: str) -> list[dict[str, Any]]:
        """reignition 対象となる active canon bits を優先順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_canon_bits
            WHERE story_id = ?
              AND status = 'active'
              AND canon_level IN ('recurring_bit', 'proto_canon', 'canon')
            ORDER BY
                CASE canon_level
                    WHEN 'canon' THEN 3
                    WHEN 'proto_canon' THEN 2
                    WHEN 'recurring_bit' THEN 1
                    ELSE 0
                END DESC,
                last_reinforced_turn DESC,
                recurrence_count DESC,
                id DESC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [self._decode_story_canon_bit(dict(row)) for row in rows]

    async def get_writeback_eligible_canon_bits(self, story_id: str) -> list[dict[str, Any]]:
        """profile writeback 対象となる active canon bits を優先順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_canon_bits
            WHERE story_id = ?
              AND status = 'active'
              AND canon_level IN ('proto_canon', 'canon')
            ORDER BY
                CASE canon_level
                    WHEN 'canon' THEN 3
                    WHEN 'proto_canon' THEN 2
                    ELSE 0
                END DESC,
                recurrence_count DESC,
                intent_alignment DESC,
                last_reinforced_turn DESC,
                id DESC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [self._decode_story_canon_bit(dict(row)) for row in rows]

    async def get_recent_story_canon_bits(
        self,
        story_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """recent canon bits を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_canon_bits
            WHERE story_id = ?
            ORDER BY last_reinforced_turn DESC, id DESC
            LIMIT ?;
            """,
            (story_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._decode_story_canon_bit(dict(row)) for row in rows]

    async def find_active_story_canon_bit_by_dedupe_key(
        self,
        story_id: str,
        dedupe_key: str,
    ) -> dict[str, Any] | None:
        """story_id + dedupe_key に一致する active canon bit を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_canon_bits
            WHERE story_id = ?
              AND dedupe_key = ?
              AND status = 'active'
            ORDER BY id DESC
            LIMIT 1;
            """,
            (story_id, dedupe_key),
        )
        row = await cursor.fetchone()
        return self._decode_story_canon_bit(dict(row)) if row is not None else None

    async def update_story_canon_bit(self, bit_id: int, patch: dict[str, Any]) -> None:
        """canon bit の一部カラムを更新する。"""
        assert self._conn is not None
        allowed_keys = {
            "bit_type",
            "motif_key",
            "canon_level",
            "status",
            "title",
            "summary",
            "focus_char_ids",
            "focus_place_id",
            "evidence_sources",
            "anchor_pattern_id",
            "anchor_episode_id",
            "anchor_relationship_mode_id",
            "anchor_scene_id",
            "last_reinforced_turn",
            "recurrence_count",
            "confidence",
            "novelty",
            "intent_alignment",
            "last_reignited_turn",
            "reignition_count",
            "last_writeback_turn",
            "writeback_count",
        }
        assignments: list[str] = ["updated_at = datetime('now')"]
        params: list[Any] = []
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            if key in {"focus_char_ids", "evidence_sources"}:
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            params.append(value)
        if len(assignments) == 1:
            return
        params.append(bit_id)
        await self._conn.execute(
            f"UPDATE story_canon_bits SET {', '.join(assignments)} WHERE id = ?;",
            params,
        )
        await self._conn.commit()

    async def archive_story_canon_bit(self, bit_id: int) -> None:
        """canon bit を archived に更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE story_canon_bits
            SET status = 'archived',
                updated_at = datetime('now')
            WHERE id = ?;
            """,
            (bit_id,),
        )
        await self._conn.commit()

    async def insert_story_dramatic_pressure(self, story_id: str, pressure: dict[str, Any]) -> int:
        """story_dramatic_pressures に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_dramatic_pressures (
                story_id, pressure_type, status, title, summary, focus_char_ids, focus_place_id,
                dedupe_key, source_hook_id, source_tension_id, source_pattern_id, source_episode_id,
                source_relationship_mode_id, source_canon_bit_id, source_scene_id,
                first_detected_turn, last_detected_turn, recurrence_count, score, urgency,
                payoff_ready, intent_alignment
            ) VALUES (
                :story_id, :pressure_type, :status, :title, :summary, :focus_char_ids, :focus_place_id,
                :dedupe_key, :source_hook_id, :source_tension_id, :source_pattern_id, :source_episode_id,
                :source_relationship_mode_id, :source_canon_bit_id, :source_scene_id,
                :first_detected_turn, :last_detected_turn, :recurrence_count, :score, :urgency,
                :payoff_ready, :intent_alignment
            );
            """,
            {
                "story_id": story_id,
                "pressure_type": pressure["pressure_type"],
                "status": pressure.get("status", "active"),
                "title": pressure.get("title"),
                "summary": pressure.get("summary"),
                "focus_char_ids": json.dumps(pressure.get("focus_char_ids", []), ensure_ascii=False),
                "focus_place_id": pressure.get("focus_place_id"),
                "dedupe_key": pressure["dedupe_key"],
                "source_hook_id": pressure.get("source_hook_id"),
                "source_tension_id": pressure.get("source_tension_id"),
                "source_pattern_id": pressure.get("source_pattern_id"),
                "source_episode_id": pressure.get("source_episode_id"),
                "source_relationship_mode_id": pressure.get("source_relationship_mode_id"),
                "source_canon_bit_id": pressure.get("source_canon_bit_id"),
                "source_scene_id": pressure.get("source_scene_id"),
                "first_detected_turn": pressure["first_detected_turn"],
                "last_detected_turn": pressure["last_detected_turn"],
                "recurrence_count": pressure.get("recurrence_count", 1),
                "score": pressure.get("score", 0.5),
                "urgency": pressure.get("urgency", 0.5),
                "payoff_ready": pressure.get("payoff_ready", 0.5),
                "intent_alignment": pressure.get("intent_alignment", 0.55),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_story_dramatic_pressures(self, story_id: str) -> list[dict[str, Any]]:
        """active な dramatic pressure を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_dramatic_pressures
            WHERE story_id = ? AND status = 'active'
            ORDER BY score DESC, last_detected_turn DESC, id DESC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [self._decode_story_dramatic_pressure(dict(row)) for row in rows]

    async def get_recent_story_dramatic_pressures(
        self,
        story_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """recent dramatic pressures を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_dramatic_pressures
            WHERE story_id = ?
            ORDER BY last_detected_turn DESC, id DESC
            LIMIT ?;
            """,
            (story_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._decode_story_dramatic_pressure(dict(row)) for row in rows]

    async def find_active_story_dramatic_pressure_by_dedupe_key(
        self,
        story_id: str,
        dedupe_key: str,
    ) -> dict[str, Any] | None:
        """story_id + dedupe_key に一致する active dramatic pressure を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_dramatic_pressures
            WHERE story_id = ?
              AND dedupe_key = ?
              AND status = 'active'
            ORDER BY id DESC
            LIMIT 1;
            """,
            (story_id, dedupe_key),
        )
        row = await cursor.fetchone()
        return self._decode_story_dramatic_pressure(dict(row)) if row is not None else None

    async def update_story_dramatic_pressure(self, pressure_id: int, patch: dict[str, Any]) -> None:
        """dramatic pressure の一部カラムを更新する。"""
        assert self._conn is not None
        allowed_keys = {
            "pressure_type",
            "status",
            "title",
            "summary",
            "focus_char_ids",
            "focus_place_id",
            "source_hook_id",
            "source_tension_id",
            "source_pattern_id",
            "source_episode_id",
            "source_relationship_mode_id",
            "source_canon_bit_id",
            "source_scene_id",
            "last_detected_turn",
            "recurrence_count",
            "score",
            "urgency",
            "payoff_ready",
            "intent_alignment",
            "resolved_turn",
            "resolution_note",
        }
        assignments: list[str] = ["updated_at = datetime('now')"]
        params: list[Any] = []
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            if key == "focus_char_ids":
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            params.append(value)
        if len(assignments) == 1:
            return
        params.append(pressure_id)
        await self._conn.execute(
            f"UPDATE story_dramatic_pressures SET {', '.join(assignments)} WHERE id = ?;",
            params,
        )
        await self._conn.commit()

    async def resolve_story_dramatic_pressure(
        self,
        pressure_id: int,
        *,
        resolved_turn: int,
        resolution_note: str | None,
    ) -> None:
        """dramatic pressure を resolved に更新する。"""
        await self.update_story_dramatic_pressure(
            pressure_id,
            {
                "status": "resolved",
                "resolved_turn": resolved_turn,
                "resolution_note": resolution_note,
            },
        )

    async def insert_generation_quality_issue(
        self, story_id: str, issue: dict[str, Any]
    ) -> int:
        """generation_quality_issues に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO generation_quality_issues (
                story_id, log_id, scene_id, issue_type, severity,
                details, auto_action, created_turn
            ) VALUES (
                :story_id, :log_id, :scene_id, :issue_type, :severity,
                :details, :auto_action, :created_turn
            );
            """,
            {
                "story_id": story_id,
                "log_id": issue.get("log_id"),
                "scene_id": issue.get("scene_id"),
                "issue_type": issue["issue_type"],
                "severity": issue.get("severity", "warning"),
                "details": json.dumps(issue.get("details", {}), ensure_ascii=False),
                "auto_action": issue.get("auto_action", "accept"),
                "created_turn": issue.get("created_turn"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def count_generation_quality_issues(
        self,
        story_id: str,
        *,
        since_turn: int,
        auto_action: str | None = None,
    ) -> int:
        """created_turn 以降の generation_quality_issues 件数を返す。"""
        assert self._conn is not None
        if auto_action is None:
            cursor = await self._conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM generation_quality_issues
                WHERE story_id = ?
                  AND created_turn IS NOT NULL
                  AND created_turn >= ?;
                """,
                (story_id, since_turn),
            )
        else:
            cursor = await self._conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM generation_quality_issues
                WHERE story_id = ?
                  AND created_turn IS NOT NULL
                  AND created_turn >= ?
                  AND auto_action = ?;
                """,
                (story_id, since_turn, auto_action),
            )
        row = await cursor.fetchone()
        return int(row["cnt"] if row is not None else 0)

    async def upsert_character_profile_overlay(
        self, story_id: str, char_id: str, overlay: dict[str, Any]
    ) -> None:
        """character_profile_overlays を UPSERT する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO character_profile_overlays (
                story_id, char_id, overlay_json, version,
                last_committed_turn, source_evolution_id
            ) VALUES (
                :story_id, :char_id, :overlay_json, :version,
                :last_committed_turn, :source_evolution_id
            )
            ON CONFLICT(story_id, char_id) DO UPDATE SET
                overlay_json = excluded.overlay_json,
                version = excluded.version,
                last_committed_turn = excluded.last_committed_turn,
                source_evolution_id = excluded.source_evolution_id,
                updated_at = datetime('now');
            """,
            {
                "story_id": story_id,
                "char_id": char_id,
                "overlay_json": json.dumps(overlay.get("overlay_json", {}), ensure_ascii=False),
                "version": overlay.get("version", 1),
                "last_committed_turn": overlay.get("last_committed_turn"),
                "source_evolution_id": overlay.get("source_evolution_id"),
            },
        )
        await self._conn.commit()

    async def get_character_profile_overlay(
        self, story_id: str, char_id: str
    ) -> dict[str, Any] | None:
        """character_profile_overlays を1件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM character_profile_overlays
            WHERE story_id = ? AND char_id = ?
            LIMIT 1;
            """,
            (story_id, char_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = self._decode_json_field(dict(row), "overlay_json", {})
        return data

    async def upsert_character_canon_overlay(
        self, story_id: str, char_id: str, overlay: dict[str, Any]
    ) -> None:
        """character_canon_overlays を UPSERT する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO character_canon_overlays (
                story_id, char_id, overlay_json, version,
                last_written_turn, source_canon_bit_ids
            ) VALUES (
                :story_id, :char_id, :overlay_json, :version,
                :last_written_turn, :source_canon_bit_ids
            )
            ON CONFLICT(story_id, char_id) DO UPDATE SET
                overlay_json = excluded.overlay_json,
                version = excluded.version,
                last_written_turn = excluded.last_written_turn,
                source_canon_bit_ids = excluded.source_canon_bit_ids,
                updated_at = datetime('now');
            """,
            {
                "story_id": story_id,
                "char_id": char_id,
                "overlay_json": json.dumps(overlay.get("overlay_json", {}), ensure_ascii=False),
                "version": overlay.get("version", 1),
                "last_written_turn": overlay.get("last_written_turn"),
                "source_canon_bit_ids": json.dumps(
                    list(overlay.get("source_canon_bit_ids", [])),
                    ensure_ascii=False,
                ),
            },
        )
        await self._conn.commit()

    async def get_character_canon_overlay(
        self, story_id: str, char_id: str
    ) -> dict[str, Any] | None:
        """character_canon_overlays を1件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM character_canon_overlays
            WHERE story_id = ? AND char_id = ?
            LIMIT 1;
            """,
            (story_id, char_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = self._decode_json_field(dict(row), "overlay_json", {})
        data = self._decode_json_field(data, "source_canon_bit_ids", [])
        return data

    async def delete_character_canon_overlay(self, story_id: str, char_id: str) -> None:
        """character_canon_overlays を削除する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            DELETE FROM character_canon_overlays
            WHERE story_id = ? AND char_id = ?;
            """,
            (story_id, char_id),
        )
        await self._conn.commit()

    async def insert_character_growth_candidate(
        self, story_id: str, char_id: str, candidate: dict[str, Any]
    ) -> int:
        """character_growth_candidates に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO character_growth_candidates (
                story_id, char_id, field, candidate_value, reason,
                experience_score, identity_impact_score, confidence,
                source_memory_id, source_hook_id, source_log_id,
                source_scene_id, detected_turn, status
            ) VALUES (
                :story_id, :char_id, :field, :candidate_value, :reason,
                :experience_score, :identity_impact_score, :confidence,
                :source_memory_id, :source_hook_id, :source_log_id,
                :source_scene_id, :detected_turn, :status
            );
            """,
            {
                "story_id": story_id,
                "char_id": char_id,
                "field": candidate["field"],
                "candidate_value": candidate["candidate_value"],
                "reason": candidate["reason"],
                "experience_score": candidate.get("experience_score", 0.5),
                "identity_impact_score": candidate.get("identity_impact_score", 0.5),
                "confidence": candidate.get("confidence", 0.5),
                "source_memory_id": candidate.get("source_memory_id"),
                "source_hook_id": candidate.get("source_hook_id"),
                "source_log_id": candidate.get("source_log_id"),
                "source_scene_id": candidate.get("source_scene_id"),
                "detected_turn": candidate["detected_turn"],
                "status": candidate.get("status", "pending"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_pending_growth_candidates(
        self, story_id: str, char_id: str
    ) -> list[dict[str, Any]]:
        """pending 状態の character_growth_candidates を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM character_growth_candidates
            WHERE story_id = ? AND char_id = ? AND status = 'pending'
            ORDER BY detected_turn DESC, id DESC;
            """,
            (story_id, char_id),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_character_growth_candidate_status(
        self,
        candidate_id: int,
        status: str,
    ) -> None:
        """character_growth_candidates の status を更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE character_growth_candidates
            SET status = ?, created_at = created_at
            WHERE id = ?;
            """,
            (status, candidate_id),
        )
        await self._conn.commit()

    async def apply_growth_batch(
        self,
        story_id: str,
        char_id: str,
        batch: dict[str, Any],
    ) -> dict[str, Any]:
        """GrowthEngine の candidate/evolution/overlay 更新を1 transaction で適用する。"""
        assert self._conn is not None
        inserted_candidate_ids: list[int] = []
        committed_candidate_ids: list[int] = []
        last_evolution_id: int | None = None
        try:
            await self._conn.execute("BEGIN;")
            for candidate in list(batch.get("candidate_rows") or []):
                cursor = await self._conn.execute(
                    """
                    INSERT INTO character_growth_candidates (
                        story_id, char_id, field, candidate_value, reason,
                        experience_score, identity_impact_score, confidence,
                        source_memory_id, source_hook_id, source_log_id,
                        source_scene_id, detected_turn, status
                    ) VALUES (
                        :story_id, :char_id, :field, :candidate_value, :reason,
                        :experience_score, :identity_impact_score, :confidence,
                        :source_memory_id, :source_hook_id, :source_log_id,
                        :source_scene_id, :detected_turn, :status
                    );
                    """,
                    {
                        "story_id": story_id,
                        "char_id": char_id,
                        "field": candidate["field"],
                        "candidate_value": candidate["candidate_value"],
                        "reason": candidate["reason"],
                        "experience_score": candidate.get("experience_score", 0.5),
                        "identity_impact_score": candidate.get("identity_impact_score", 0.5),
                        "confidence": candidate.get("confidence", 0.5),
                        "source_memory_id": candidate.get("source_memory_id"),
                        "source_hook_id": candidate.get("source_hook_id"),
                        "source_log_id": candidate.get("source_log_id"),
                        "source_scene_id": candidate.get("source_scene_id"),
                        "detected_turn": candidate["detected_turn"],
                        "status": candidate.get("status", "pending"),
                    },
                )
                assert cursor.lastrowid is not None
                inserted_candidate_ids.append(int(cursor.lastrowid))

            for status_update in list(batch.get("status_updates") or []):
                for candidate_id in list(status_update.get("candidate_ids") or []):
                    await self._conn.execute(
                        """
                        UPDATE character_growth_candidates
                        SET status = ?, created_at = created_at
                        WHERE id = ?;
                        """,
                        (str(status_update["status"]), int(candidate_id)),
                    )

            for commit_action in list(batch.get("commit_actions") or []):
                cursor = await self._conn.execute(
                    """
                    INSERT INTO character_evolution (
                        char_id, story_id, turn_number, field,
                        previous_value, new_value, reason, source_memory_id
                    ) VALUES (
                        :char_id, :story_id, :turn_number, :field,
                        :previous_value, :new_value, :reason, :source_memory_id
                    );
                    """,
                    {
                        "char_id": char_id,
                        "story_id": story_id,
                        "turn_number": batch["turn_number"],
                        "field": commit_action["field"],
                        "previous_value": commit_action.get("previous_value"),
                        "new_value": commit_action["candidate_value"],
                        "reason": commit_action["reason"],
                        "source_memory_id": commit_action.get("source_memory_id"),
                    },
                )
                assert cursor.lastrowid is not None
                last_evolution_id = int(cursor.lastrowid)
                candidate_index = int(commit_action["candidate_index"])
                new_candidate_id = inserted_candidate_ids[candidate_index]
                await self._conn.execute(
                    """
                    UPDATE character_growth_candidates
                    SET status = ?, created_at = created_at
                    WHERE id = ?;
                    """,
                    ("committed", new_candidate_id),
                )
                committed_candidate_ids.append(new_candidate_id)
                for existing_candidate_id in list(commit_action.get("existing_candidate_ids") or []):
                    await self._conn.execute(
                        """
                        UPDATE character_growth_candidates
                        SET status = ?, created_at = created_at
                        WHERE id = ?;
                        """,
                        ("committed", int(existing_candidate_id)),
                    )
                    committed_candidate_ids.append(int(existing_candidate_id))

            overlay_payload = batch.get("overlay_payload")
            if overlay_payload is not None:
                await self._conn.execute(
                    """
                    INSERT INTO character_profile_overlays (
                        story_id, char_id, overlay_json, version,
                        last_committed_turn, source_evolution_id
                    ) VALUES (
                        :story_id, :char_id, :overlay_json, :version,
                        :last_committed_turn, :source_evolution_id
                    )
                    ON CONFLICT(story_id, char_id) DO UPDATE SET
                        overlay_json = excluded.overlay_json,
                        version = excluded.version,
                        last_committed_turn = excluded.last_committed_turn,
                        source_evolution_id = excluded.source_evolution_id,
                        updated_at = datetime('now');
                    """,
                    {
                        "story_id": story_id,
                        "char_id": char_id,
                        "overlay_json": json.dumps(
                            overlay_payload.get("overlay_json", {}),
                            ensure_ascii=False,
                        ),
                        "version": overlay_payload.get("version", 1),
                        "last_committed_turn": overlay_payload.get("last_committed_turn"),
                        "source_evolution_id": last_evolution_id,
                    },
                )
            await self._conn.commit()
            return {
                "inserted_candidate_ids": inserted_candidate_ids,
                "committed_candidate_ids": committed_candidate_ids,
                "last_evolution_id": last_evolution_id,
            }
        except Exception:
            await self._conn.rollback()
            raise

    async def get_recent_relationship_events_for_character(
        self,
        story_id: str,
        char_id: str,
        *,
        since_turn: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """char が関与した relationship_events を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM relationship_events
            WHERE story_id = ?
              AND turn_number >= ?
              AND (char_id_from = ? OR char_id_to = ?)
            ORDER BY turn_number DESC, id DESC
            LIMIT ?;
            """,
            (story_id, since_turn, char_id, char_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_recent_relationship_events(
        self,
        story_id: str,
        *,
        since_turn: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """story 単位の relationship_events を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM relationship_events
            WHERE story_id = ?
              AND turn_number >= ?
            ORDER BY turn_number DESC, id DESC
            LIMIT ?;
            """,
            (story_id, since_turn, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def count_recent_character_evolutions(
        self,
        story_id: str,
        *,
        since_turn: int,
    ) -> int:
        """turn_number 以降の character_evolution 件数を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM character_evolution
            WHERE story_id = ?
              AND turn_number >= ?;
            """,
            (story_id, since_turn),
        )
        row = await cursor.fetchone()
        return int(row["cnt"] if row is not None else 0)

    # ------------------------------------------------------------------
    # Story Arc
    # ------------------------------------------------------------------

    async def insert_arc(self, story_id: str, arc: dict[str, Any]) -> int:
        """story_arc に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_arc (
                story_id, arc_type, title, summary, turn_from,
                turn_to, theme, tension_level, parent_arc_id, source_scene_id
            ) VALUES (
                :story_id, :arc_type, :title, :summary, :turn_from,
                :turn_to, :theme, :tension_level, :parent_arc_id, :source_scene_id
            );
            """,
            {
                "story_id": story_id,
                "arc_type": arc["arc_type"],
                "title": arc["title"],
                "summary": arc["summary"],
                "turn_from": arc["turn_from"],
                "turn_to": arc.get("turn_to"),
                "theme": arc.get("theme"),
                "tension_level": arc.get("tension_level", 0.5),
                "parent_arc_id": arc.get("parent_arc_id"),
                "source_scene_id": arc.get("source_scene_id"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def close_arc(self, arc_id: int, turn_to: int) -> None:
        """story_arc の turn_to を設定してアークを閉じる。"""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE story_arc SET turn_to = ? WHERE id = ?;",
            (turn_to, arc_id),
        )
        await self._conn.commit()

    async def get_open_arcs(
        self, story_id: str, arc_type: str | None = None
    ) -> list[dict[str, Any]]:
        """turn_to IS NULL の進行中アークを返す。arc_type 指定時はさらに絞り込む。"""
        assert self._conn is not None
        if arc_type is not None:
            cursor = await self._conn.execute(
                """
                SELECT * FROM story_arc
                WHERE story_id = ? AND turn_to IS NULL AND arc_type = ?
                ORDER BY turn_from ASC;
                """,
                (story_id, arc_type),
            )
        else:
            cursor = await self._conn.execute(
                """
                SELECT * FROM story_arc
                WHERE story_id = ? AND turn_to IS NULL
                ORDER BY turn_from ASC;
                """,
                (story_id,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_arcs(
        self, story_id: str, arc_type: str | None = None
    ) -> list[dict[str, Any]]:
        """story_id の全アーク（閉じたものも含む）を返す。"""
        assert self._conn is not None
        if arc_type is not None:
            cursor = await self._conn.execute(
                """
                SELECT * FROM story_arc
                WHERE story_id = ? AND arc_type = ?
                ORDER BY turn_from ASC;
                """,
                (story_id, arc_type),
            )
        else:
            cursor = await self._conn.execute(
                """
                SELECT * FROM story_arc
                WHERE story_id = ?
                ORDER BY turn_from ASC;
                """,
                (story_id,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_arc_by_source_scene_id(
        self, story_id: str, scene_id: int
    ) -> dict[str, Any] | None:
        """source_scene_id に紐づく scene arc を1件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_arc
            WHERE story_id = ? AND source_scene_id = ?
            ORDER BY id DESC
            LIMIT 1;
            """,
            (story_id, scene_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _decode_novel_output(row: dict[str, Any]) -> dict[str, Any]:
        """source_log_ids JSON フィールドを list にデコードする。"""
        raw = row.get("source_log_ids")
        if isinstance(raw, str) and raw:
            row["source_log_ids"] = json.loads(raw)
        else:
            row["source_log_ids"] = []
        return row

    async def insert_novel_output(
        self, story_id: str, arc_id: int, output: dict[str, Any]
    ) -> int:
        """novel_output に1行挿入し、新規 id を返す。"""
        assert self._conn is not None
        source_log_ids = output.get("source_log_ids")
        source_log_ids_json = json.dumps(source_log_ids) if source_log_ids is not None else None
        cursor = await self._conn.execute(
            """
            INSERT INTO novel_output (
                story_id, arc_id, content_type, content, source_log_ids, ordering
            ) VALUES (
                :story_id, :arc_id, :content_type, :content, :source_log_ids, :ordering
            );
            """,
            {
                "story_id": story_id,
                "arc_id": arc_id,
                "content_type": output["content_type"],
                "content": output["content"],
                "source_log_ids": source_log_ids_json,
                "ordering": output["ordering"],
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_novel_outputs(
        self, story_id: str, arc_id: int
    ) -> list[dict[str, Any]]:
        """arc_id に属する novel_output を ordering 順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM novel_output
            WHERE story_id = ? AND arc_id = ?
            ORDER BY ordering ASC;
            """,
            (story_id, arc_id),
        )
        rows = await cursor.fetchall()
        return [self._decode_novel_output(dict(row)) for row in rows]

    async def get_all_novel_outputs_by_story(
        self, story_id: str
    ) -> dict[int, list[dict[str, Any]]]:
        """story_id の全 novel_output を1クエリで取得し arc_id でグルーピングして返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM novel_output
            WHERE story_id = ?
            ORDER BY arc_id ASC, ordering ASC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            d = self._decode_novel_output(dict(row))
            arc_id = d["arc_id"]
            result.setdefault(arc_id, []).append(d)
        return result

    # ------------------------------------------------------------------
    # Hosted admin/auth/publication
    # ------------------------------------------------------------------

    async def ensure_admin_user(self, username: str, password: str) -> dict[str, Any]:
        """単一管理者の作成または更新を行う。"""
        assert self._conn is not None
        existing = await self._conn.execute(
            "SELECT * FROM admin_users WHERE username = ?;",
            (username,),
        )
        row = await existing.fetchone()
        salt = secrets.token_hex(16)
        password_hash = self._hash_secret(password, salt)
        if row is None:
            cursor = await self._conn.execute(
                """
                INSERT INTO admin_users (username, password_hash, password_salt)
                VALUES (?, ?, ?);
                """,
                (username, password_hash, salt),
            )
            await self._conn.commit()
            user_id = cursor.lastrowid
        else:
            user_id = row["id"]
            await self._conn.execute(
                """
                UPDATE admin_users
                SET password_hash = ?, password_salt = ?, updated_at = datetime('now')
                WHERE id = ?;
                """,
                (password_hash, salt, user_id),
            )
            await self._conn.commit()
        cursor = await self._conn.execute(
            "SELECT id, username, created_at, updated_at FROM admin_users WHERE id = ?;",
            (user_id,),
        )
        user = await cursor.fetchone()
        assert user is not None
        return dict(user)

    async def verify_admin_credentials(
        self, username: str, password: str
    ) -> dict[str, Any] | None:
        """管理者認証。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM admin_users WHERE username = ?;",
            (username,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        if self._hash_secret(password, data["password_salt"]) != data["password_hash"]:
            return None
        return data

    async def create_admin_session(self, admin_user_id: int, ttl_hours: int = 24) -> str:
        """管理セッションを作成する。"""
        assert self._conn is not None
        token = secrets.token_urlsafe(32)
        await self._conn.execute(
            """
            INSERT INTO admin_sessions (admin_user_id, session_token, expires_at)
            VALUES (?, ?, datetime('now', ?));
            """,
            (admin_user_id, token, f"+{ttl_hours} hours"),
        )
        await self._conn.commit()
        return token

    async def get_admin_session(self, session_token: str) -> dict[str, Any] | None:
        """有効な管理セッションを返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT s.*, u.username
            FROM admin_sessions AS s
            JOIN admin_users AS u ON u.id = s.admin_user_id
            WHERE s.session_token = ? AND s.expires_at > datetime('now');
            """,
            (session_token,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        await self._conn.execute(
            "UPDATE admin_sessions SET last_used_at = datetime('now') WHERE session_token = ?;",
            (session_token,),
        )
        await self._conn.commit()
        return dict(row)

    async def delete_admin_session(self, session_token: str) -> None:
        """管理セッションを削除する。"""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM admin_sessions WHERE session_token = ?;",
            (session_token,),
        )
        await self._conn.commit()

    async def create_admin_api_token(
        self,
        label: str,
        admin_user_id: int | None = None,
    ) -> str:
        """新しい bearer token を返し、ハッシュだけ保存する。"""
        assert self._conn is not None
        token = f"ptr_{secrets.token_urlsafe(24)}"
        token_hash = self._hash_secret(token, "")
        await self._conn.execute(
            """
            INSERT INTO admin_api_tokens (admin_user_id, label, token_hash, token_prefix)
            VALUES (?, ?, ?, ?);
            """,
            (admin_user_id, label, token_hash, token[:12]),
        )
        await self._conn.commit()
        return token

    async def list_admin_api_tokens(self) -> list[dict[str, Any]]:
        """管理 API token 一覧を返す。平文 token は含めない。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id, admin_user_id, label, token_prefix, created_at, last_used_at, revoked_at
            FROM admin_api_tokens
            ORDER BY id ASC;
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def authenticate_admin_api_token(self, token: str) -> dict[str, Any] | None:
        """bearer token を検証する。"""
        assert self._conn is not None
        token_hash = self._hash_secret(token, "")
        cursor = await self._conn.execute(
            """
            SELECT * FROM admin_api_tokens
            WHERE token_hash = ? AND revoked_at IS NULL;
            """,
            (token_hash,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        await self._conn.execute(
            "UPDATE admin_api_tokens SET last_used_at = datetime('now') WHERE id = ?;",
            (row["id"],),
        )
        await self._conn.commit()
        return dict(row)

    async def revoke_admin_api_token(self, token_id: int) -> bool:
        """bearer token を失効させる。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            UPDATE admin_api_tokens
            SET revoked_at = datetime('now')
            WHERE id = ? AND revoked_at IS NULL;
            """,
            (token_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def upsert_story_publication(
        self,
        story_id: str,
        visibility: str,
        published_at: str | None = None,
        page_size_scenes: int = 10,
    ) -> dict[str, Any]:
        """story_publications を upsert する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO story_publications (
                story_id, visibility, page_size_scenes, published_at, updated_at
            ) VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(story_id) DO UPDATE SET
                visibility = excluded.visibility,
                page_size_scenes = excluded.page_size_scenes,
                published_at = excluded.published_at,
                updated_at = datetime('now');
            """,
            (story_id, visibility, page_size_scenes, published_at),
        )
        await self._conn.commit()
        cursor = await self._conn.execute(
            "SELECT * FROM story_publications WHERE story_id = ?;",
            (story_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    async def get_story_publication(self, story_id: str) -> dict[str, Any] | None:
        """story_publications を1件返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM story_publications WHERE story_id = ?;",
            (story_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def list_story_publications(self) -> list[dict[str, Any]]:
        """story_publications 一覧を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM story_publications ORDER BY story_id ASC;"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def record_story_archive_build(
        self,
        story_id: str,
        published_at: str,
        last_error: str | None,
        latest_turn: int | None,
    ) -> None:
        """archive build の結果を記録する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO story_publications (
                story_id, visibility, published_at, last_build_at, last_success_at,
                last_error, latest_published_turn, updated_at
            ) VALUES (
                ?, COALESCE((SELECT visibility FROM story_publications WHERE story_id = ?), 'draft'),
                ?, datetime('now'), datetime('now'), ?, ?, datetime('now')
            )
            ON CONFLICT(story_id) DO UPDATE SET
                published_at = excluded.published_at,
                last_build_at = datetime('now'),
                last_success_at = datetime('now'),
                last_error = excluded.last_error,
                latest_published_turn = excluded.latest_published_turn,
                updated_at = datetime('now');
            """,
            (story_id, story_id, published_at, last_error, latest_turn),
        )
        await self._conn.commit()

    async def get_active_ambient_states(
        self,
        story_id: str,
        turn_number: int,
        scope_filters: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """有効な ambient state を scope filter ごとに返す。"""
        assert self._conn is not None
        if not scope_filters:
            return []
        clauses = " OR ".join("(scope_type = ? AND scope_id = ?)" for _ in scope_filters)
        params: list[Any] = [story_id, turn_number]
        for scope_type, scope_id in scope_filters:
            params.extend([scope_type, scope_id])
        cursor = await self._conn.execute(
            f"""
            SELECT * FROM ambient_states
            WHERE story_id = ?
              AND expires_turn >= ?
              AND ({clauses})
            ORDER BY created_turn ASC, id ASC;
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [self._decode_ambient_state(dict(row)) for row in rows]

    async def insert_ambient_state(self, story_id: str, state: dict[str, Any]) -> int:
        """ambient state を1件保存する。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO ambient_states (
                story_id, scope_type, scope_id, factor_kind, factor_key, summary,
                intensity, emotion_delta, created_turn, expires_turn,
                last_applied_turn, source
            ) VALUES (
                :story_id, :scope_type, :scope_id, :factor_kind, :factor_key, :summary,
                :intensity, :emotion_delta, :created_turn, :expires_turn,
                :last_applied_turn, :source
            );
            """,
            {
                "story_id": story_id,
                "scope_type": state["scope_type"],
                "scope_id": state["scope_id"],
                "factor_kind": state["factor_kind"],
                "factor_key": state["factor_key"],
                "summary": state["summary"],
                "intensity": float(state.get("intensity", 0.3)),
                "emotion_delta": json.dumps(state.get("emotion_delta", {}), ensure_ascii=False),
                "created_turn": int(state["created_turn"]),
                "expires_turn": int(state["expires_turn"]),
                "last_applied_turn": state.get("last_applied_turn"),
                "source": state.get("source", "template"),
            },
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    async def get_system_setting(self, key: str, default: str | None = None) -> str | None:
        """system_settings から設定値を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT value FROM system_settings WHERE key = ?;",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return default
        return str(row["value"])

    async def set_system_setting(self, key: str, value: str) -> None:
        """system_settings を upsert する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now');
            """,
            (key, value),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Web 投稿先: per-story 設定
    # ------------------------------------------------------------------

    async def get_story_web_post_settings(self, story_id: str) -> dict[str, str]:
        """per-story の Web 投稿先設定を返す。未設定の場合は空文字で返す。"""
        story = await self.get_story(story_id)
        if story is None:
            return {"receiver_url": "", "auth_token": ""}
        return {
            "receiver_url": story.get("web_receiver_url") or "",
            "auth_token":   story.get("web_auth_token")   or "",
        }

    async def set_story_web_post_settings(
        self, story_id: str, receiver_url: str, auth_token: str
    ) -> bool:
        """per-story の Web 投稿先 URL / auth_token を更新する。空文字は NULL として保存。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE stories SET web_receiver_url = ?, web_auth_token = ? WHERE id = ?;",
            (receiver_url or None, auth_token or None, story_id),
        )
        await self._conn.commit()
        return bool(cursor.rowcount)

    async def insert_admin_audit_log(
        self,
        actor_type: str,
        actor_label: str,
        action: str,
        story_id: str | None,
        result: str,
        payload_summary: str | None,
    ) -> None:
        """admin_audit_logs に1件追加する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO admin_audit_logs (
                actor_type, actor_label, action, story_id, result, payload_summary
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            (actor_type, actor_label, action, story_id, result, payload_summary),
        )
        await self._conn.commit()

    async def list_admin_audit_logs(
        self,
        limit: int = 50,
        offset: int = 0,
        story_id: str | None = None,
        action: str | None = None,
    ) -> list[dict[str, Any]]:
        """admin_audit_logs を新しい順で返す。story_id / action でフィルタ可。"""
        assert self._conn is not None
        conditions = []
        params: list[object] = []
        if story_id:
            conditions.append("story_id = ?")
            params.append(story_id)
        if action:
            conditions.append("action LIKE ?")
            params.append(f"%{action}%")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        cursor = await self._conn.execute(
            f"""
            SELECT * FROM admin_audit_logs
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?;
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def count_chat_logs_by_day(self, days: int = 7) -> list[dict[str, Any]]:
        """直近 days 日分のチャットログ件数を日別 (UTC) で返す。

        ログが 0 件の日も {"day": "YYYY-MM-DD", "count": 0} で埋める。
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT date(created_at) AS day, COUNT(*) AS count
            FROM chat_logs
            WHERE created_at >= date('now', ?)
            GROUP BY day;
            """,
            (f"-{max(0, days - 1)} days",),
        )
        rows = await cursor.fetchall()
        counts = {row["day"]: int(row["count"]) for row in rows}

        from datetime import UTC, datetime, timedelta

        today = datetime.now(UTC).date()
        out: list[dict[str, Any]] = []
        for offset in range(days - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            out.append({"day": day, "count": counts.get(day, 0)})
        return out

    async def get_recent_dashboard_chats(self, limit: int = 6) -> list[dict[str, Any]]:
        """ストーリー横断で直近 limit 件のチャットログをキャラ名込みで返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT
                chat.id,
                chat.story_id,
                chat.char_id,
                chat.message,
                chat.msg_type,
                chat.expression,
                chat.created_at,
                chat.sim_datetime,
                stories.title AS story_title,
                chars.name_ja AS speaker_name
            FROM chat_logs AS chat
            LEFT JOIN stories
                ON stories.id = chat.story_id
            LEFT JOIN characters AS chars
                ON chars.story_id = chat.story_id
               AND chars.id = chat.char_id
            ORDER BY chat.id DESC
            LIMIT ?;
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_latest_chat_per_story(self) -> dict[str, dict[str, Any]]:
        """各ストーリーの最新チャットログを 1 件ずつ返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT chat.story_id, chat.created_at, chat.sim_datetime, chat.id
            FROM chat_logs AS chat
            INNER JOIN (
                SELECT story_id, MAX(id) AS max_id
                FROM chat_logs
                GROUP BY story_id
            ) AS latest
                ON latest.story_id = chat.story_id
               AND latest.max_id = chat.id;
            """
        )
        rows = await cursor.fetchall()
        return {row["story_id"]: dict(row) for row in rows}

    # ------------------------------------------------------------------
    # Story Chapters（v2 upgrade）
    # ------------------------------------------------------------------

    async def insert_chapter(self, story_id: str, chapter: dict[str, Any]) -> int:
        """story_chapters に 1 件追加する。挿入された行の id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_chapters (
                story_id, chapter_id, title, theme, world_injection,
                status, start_condition, current_beat, carry_over_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                story_id,
                chapter["chapter_id"],
                chapter["title"],
                chapter.get("theme"),
                chapter.get("world_injection"),
                chapter.get("status", "pending"),
                chapter.get("start_condition"),
                chapter.get("current_beat", "setup"),
                json.dumps(chapter.get("carry_over_json", {}), ensure_ascii=False),
            ),
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_chapter(self, story_id: str) -> dict[str, Any] | None:
        """現在 active な chapter を返す。存在しない場合は None。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_chapters
            WHERE story_id = ? AND status = 'active'
            LIMIT 1;
            """,
            (story_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        self._decode_json_field(data, "carry_over_json", {})
        return data

    async def get_story_chapter(self, chapter_id: int) -> dict[str, Any] | None:
        """ID 指定で story_chapters を 1 件取得する。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_chapters
            WHERE id = ?
            LIMIT 1;
            """,
            (chapter_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        self._decode_json_field(data, "carry_over_json", {})
        return data

    async def get_story_chapter_by_chapter_id(
        self,
        story_id: str,
        chapter_id: str,
    ) -> dict[str, Any] | None:
        """story_id + chapter_id で story_chapters を 1 件取得する。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT *
            FROM story_chapters
            WHERE story_id = ? AND chapter_id = ?
            LIMIT 1;
            """,
            (story_id, chapter_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        self._decode_json_field(data, "carry_over_json", {})
        return data

    async def get_pending_chapters(self, story_id: str) -> list[dict[str, Any]]:
        """pending 状態の chapter を挿入順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_chapters
            WHERE story_id = ? AND status = 'pending'
            ORDER BY id ASC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            data = dict(row)
            self._decode_json_field(data, "carry_over_json", {})
            result.append(data)
        return result

    async def get_closed_chapters(self, story_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """closed 状態の chapter を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_chapters
            WHERE story_id = ? AND status = 'closed'
            ORDER BY COALESCE(closed_turn, 0) DESC, id DESC
            LIMIT ?;
            """,
            (story_id, limit),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            data = dict(row)
            self._decode_json_field(data, "carry_over_json", {})
            result.append(data)
        return result

    async def activate_chapter(self, chapter_id: int, opened_turn: int) -> None:
        """chapter を pending → active に遷移させる。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE story_chapters
            SET status = 'active', opened_turn = ?
            WHERE id = ?;
            """,
            (opened_turn, chapter_id),
        )
        await self._conn.commit()

    async def update_chapter_beat(self, chapter_id: int, beat: str) -> None:
        """chapter の current_beat を更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE story_chapters SET current_beat = ? WHERE id = ?;",
            (beat, chapter_id),
        )
        await self._conn.commit()

    async def close_chapter(
        self,
        chapter_id: int,
        closed_turn: int,
        reason: str,
        carry_over: dict[str, Any],
    ) -> None:
        """chapter を closed に遷移させる。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE story_chapters
            SET status = 'closed', closed_turn = ?, close_reason = ?, carry_over_json = ?
            WHERE id = ?;
            """,
            (closed_turn, reason, json.dumps(carry_over, ensure_ascii=False), chapter_id),
        )
        await self._conn.commit()

    async def reopen_chapter(self, chapter_id: int, opened_turn: int) -> None:
        """closed chapter を active に戻す。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE story_chapters
            SET status = 'active',
                opened_turn = ?,
                closed_turn = NULL,
                close_reason = NULL,
                carry_over_json = '{}'
            WHERE id = ?;
            """,
            (opened_turn, chapter_id),
        )
        await self._conn.commit()

    async def upsert_chapter_definition(
        self,
        story_id: str,
        chapter: dict[str, Any],
    ) -> int:
        """chapter 定義を upsert し、story_chapters.id を返す。"""
        existing = await self.get_story_chapter_by_chapter_id(
            story_id,
            str(chapter["chapter_id"]),
        )
        if existing is None:
            return await self.insert_chapter(story_id, chapter)

        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE story_chapters
            SET title = ?, theme = ?, world_injection = ?, start_condition = ?
            WHERE id = ?;
            """,
            (
                chapter["title"],
                chapter.get("theme"),
                chapter.get("world_injection"),
                chapter.get("start_condition"),
                existing["id"],
            ),
        )
        await self._conn.commit()
        return int(existing["id"])

    async def replace_chapter_beats(
        self,
        chapter_db_id: int,
        beats: list[dict[str, Any]],
    ) -> None:
        """chapter の beat 定義を全置換する。"""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM story_chapter_beats WHERE chapter_db_id = ?;",
            (chapter_db_id,),
        )
        await self._conn.commit()
        for beat in beats:
            await self.insert_chapter_beat(chapter_db_id, beat)

    async def delete_pending_chapters_except(
        self,
        story_id: str,
        chapter_ids: list[str],
    ) -> int:
        """指定 list に含まれない pending chapter を削除し、削除件数を返す。"""
        assert self._conn is not None
        if not chapter_ids:
            cursor = await self._conn.execute(
                "DELETE FROM story_chapters WHERE story_id = ? AND status = 'pending';",
                (story_id,),
            )
            await self._conn.commit()
            return int(cursor.rowcount or 0)

        placeholders = ", ".join("?" for _ in chapter_ids)
        cursor = await self._conn.execute(
            f"""
            DELETE FROM story_chapters
            WHERE story_id = ? AND status = 'pending'
              AND chapter_id NOT IN ({placeholders});
            """,
            (story_id, *chapter_ids),
        )
        await self._conn.commit()
        return int(cursor.rowcount or 0)

    async def get_chapter_beats(self, chapter_db_id: int) -> list[dict[str, Any]]:
        """chapter に属する beat 一覧を phase 順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM story_chapter_beats
            WHERE chapter_db_id = ?
            ORDER BY id ASC;
            """,
            (chapter_db_id,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            data = dict(row)
            self._decode_json_field(data, "events_json", [])
            result.append(data)
        return result

    async def insert_chapter_beat(self, chapter_db_id: int, beat: dict[str, Any]) -> int:
        """story_chapter_beats に 1 件追加する。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO story_chapter_beats (
                chapter_db_id, phase, description, goal, events_json, status
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                chapter_db_id,
                beat["phase"],
                beat.get("description"),
                beat.get("goal"),
                json.dumps(beat.get("events_json", []), ensure_ascii=False),
                beat.get("status", "pending"),
            ),
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def update_beat_status(
        self, beat_id: int, status: str, reached_turn: int | None = None
    ) -> None:
        """beat の status（と reached_turn）を更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE story_chapter_beats
            SET status = ?, reached_turn = ?
            WHERE id = ?;
            """,
            (status, reached_turn, beat_id),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Chapter Proposals（Phase 4）
    # ------------------------------------------------------------------

    async def insert_chapter_proposal(self, story_id: str, proposal: dict[str, Any]) -> int:
        """chapter_proposals に 1 件挿入し、rowid を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO chapter_proposals (
                story_id, theme, proposed_at_turn,
                proposed_chapter_json, conflict_seeds_json,
                generated_by_persona_id, admin_status
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending');
            """,
            (
                story_id,
                str(proposal.get("theme", "")),
                int(proposal.get("proposed_at_turn", 0)),
                json.dumps(proposal.get("proposed_chapter_json", {}), ensure_ascii=False),
                json.dumps(proposal.get("conflict_seeds_json", []), ensure_ascii=False),
                proposal.get("generated_by_persona_id"),
            ),
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_chapter_proposals(
        self, story_id: str, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        """story の chapter_proposals を返す。status 指定で絞り込み可。"""
        assert self._conn is not None
        if status is not None:
            cursor = await self._conn.execute(
                """
                SELECT * FROM chapter_proposals
                WHERE story_id = ? AND admin_status = ?
                ORDER BY created_at DESC;
                """,
                (story_id, status),
            )
        else:
            cursor = await self._conn.execute(
                """
                SELECT * FROM chapter_proposals
                WHERE story_id = ?
                ORDER BY created_at DESC;
                """,
                (story_id,),
            )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            data = dict(row)
            self._decode_json_field(data, "proposed_chapter_json", {})
            self._decode_json_field(data, "conflict_seeds_json", [])
            result.append(data)
        return result

    async def get_chapter_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        """ID 指定で chapter_proposals を 1 件取得する。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM chapter_proposals WHERE id = ?;",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        self._decode_json_field(data, "proposed_chapter_json", {})
        self._decode_json_field(data, "conflict_seeds_json", [])
        return data

    async def update_chapter_proposal_status(
        self,
        proposal_id: int,
        status: str,
        *,
        approved_turn: int | None = None,
        notes: str | None = None,
    ) -> None:
        """chapter_proposals の admin_status / approved_turn / notes を更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            UPDATE chapter_proposals
            SET admin_status = ?,
                approved_turn = COALESCE(?, approved_turn),
                notes = COALESCE(?, notes)
            WHERE id = ?;
            """,
            (status, approved_turn, notes, proposal_id),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Director Persona（v2 upgrade）
    # ------------------------------------------------------------------

    async def upsert_director_persona(self, story_id: str, persona: dict[str, Any]) -> int:
        """director_personas を INSERT OR REPLACE する。行 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT OR REPLACE INTO director_personas (
                story_id, persona_id, name, aesthetic_json, values_json, traits_json, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                story_id,
                persona["persona_id"],
                persona["name"],
                json.dumps(persona.get("aesthetic_json", {}), ensure_ascii=False),
                json.dumps(persona.get("values_json", []), ensure_ascii=False),
                json.dumps(persona.get("traits_json", []), ensure_ascii=False),
                int(persona.get("is_active", 0)),
            ),
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_active_director_persona(self, story_id: str) -> dict[str, Any] | None:
        """現在 active な director persona を返す。存在しない場合は None。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM director_personas
            WHERE story_id = ? AND is_active = 1
            LIMIT 1;
            """,
            (story_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        self._decode_json_field(data, "aesthetic_json", {})
        self._decode_json_field(data, "values_json", [])
        self._decode_json_field(data, "traits_json", [])
        return data

    async def get_all_director_personas(self, story_id: str) -> list[dict[str, Any]]:
        """story に定義されている全 director persona を persona_id 順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM director_personas
            WHERE story_id = ?
            ORDER BY persona_id ASC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            data = dict(row)
            self._decode_json_field(data, "aesthetic_json", {})
            self._decode_json_field(data, "values_json", [])
            self._decode_json_field(data, "traits_json", [])
            result.append(data)
        return result

    async def set_persona_active(self, story_id: str, persona_id: str) -> None:
        """指定 persona_id を active にし、他を全て inactive にする。"""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE director_personas SET is_active = 0 WHERE story_id = ?;",
            (story_id,),
        )
        await self._conn.execute(
            """
            UPDATE director_personas
            SET is_active = 1
            WHERE story_id = ? AND persona_id = ?;
            """,
            (story_id, persona_id),
        )
        await self._conn.commit()

    async def delete_director_personas_except(
        self,
        story_id: str,
        persona_ids: list[str],
    ) -> int:
        """指定 list に含まれない director persona を削除し、削除件数を返す。"""
        assert self._conn is not None
        if not persona_ids:
            cursor = await self._conn.execute(
                "DELETE FROM director_personas WHERE story_id = ?;",
                (story_id,),
            )
            await self._conn.commit()
            return int(cursor.rowcount or 0)

        placeholders = ", ".join("?" for _ in persona_ids)
        cursor = await self._conn.execute(
            f"""
            DELETE FROM director_personas
            WHERE story_id = ? AND persona_id NOT IN ({placeholders});
            """,
            (story_id, *persona_ids),
        )
        await self._conn.commit()
        return int(cursor.rowcount or 0)

    async def insert_director_satisfaction(
        self, story_id: str, sat: dict[str, Any]
    ) -> int:
        """director_satisfaction に 1 件追加する。行 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO director_satisfaction (
                story_id, persona_id, turn_number,
                overall, tension_sat, character_depth_sat, pacing_sat,
                surprise_sat, dialogue_sat, atmosphere_sat,
                trend, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                story_id,
                sat["persona_id"],
                sat["turn_number"],
                float(sat.get("overall", 0.0)),
                float(sat.get("tension_sat", 0.0)),
                float(sat.get("character_depth_sat", 0.0)),
                float(sat.get("pacing_sat", 0.0)),
                float(sat.get("surprise_sat", 0.0)),
                float(sat.get("dialogue_sat", 0.0)),
                float(sat.get("atmosphere_sat", 0.0)),
                sat.get("trend", "flat"),
                json.dumps(sat.get("details_json", {}), ensure_ascii=False),
            ),
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_latest_director_satisfaction(
        self, story_id: str, persona_id: str
    ) -> dict[str, Any] | None:
        """指定 persona の最新の満足度レコードを返す。存在しない場合は None。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM director_satisfaction
            WHERE story_id = ? AND persona_id = ?
            ORDER BY turn_number DESC
            LIMIT 1;
            """,
            (story_id, persona_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        self._decode_json_field(data, "details_json", {})
        return data

    async def get_satisfaction_history(
        self, story_id: str, persona_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """指定 persona の満足度履歴を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM director_satisfaction
            WHERE story_id = ? AND persona_id = ?
            ORDER BY turn_number DESC
            LIMIT ?;
            """,
            (story_id, persona_id, limit),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            data = dict(row)
            self._decode_json_field(data, "details_json", {})
            result.append(data)
        return result

    # ------------------------------------------------------------------
    # Director Swap Log（v2 upgrade）
    # ------------------------------------------------------------------

    async def insert_director_swap_log(
        self, story_id: str, swap: dict[str, Any]
    ) -> int:
        """director_swap_log に 1 件追加する。行 id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO director_swap_log (
                story_id, turn_number, from_persona_id, to_persona_id, reason
            ) VALUES (?, ?, ?, ?, ?);
            """,
            (
                story_id,
                swap["turn_number"],
                swap.get("from_persona_id"),
                swap["to_persona_id"],
                swap.get("reason"),
            ),
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_director_swap_history(self, story_id: str) -> list[dict[str, Any]]:
        """story の監督交代履歴を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM director_swap_log
            WHERE story_id = ?
            ORDER BY turn_number DESC;
            """,
            (story_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Scene Scripts（pre-turn director scene descriptions）
    # ------------------------------------------------------------------

    async def insert_scene_script(self, story_id: str, payload: dict[str, Any]) -> int:
        """scene_scripts に 1 件追加する。挿入された行の id を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT OR REPLACE INTO scene_scripts (
                story_id, turn_number, round_number, script_text,
                director_persona_id, chapter_id, beat_phase,
                generation_mode, format_mode, generation_metadata,
                llm_provider, llm_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                story_id,
                int(payload["turn_number"]),
                payload.get("round_number"),
                str(payload["script_text"]),
                payload.get("director_persona_id"),
                payload.get("chapter_id"),
                payload.get("beat_phase"),
                payload.get("generation_mode"),
                payload.get("format_mode"),
                json.dumps(payload.get("generation_metadata") or {}, ensure_ascii=False)
                if payload.get("generation_metadata") is not None
                else None,
                payload.get("llm_provider"),
                payload.get("llm_model"),
            ),
        )
        await self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_latest_scene_script(self, story_id: str) -> dict[str, Any] | None:
        """story_id の最新 scene_script を返す。存在しない場合は None。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM scene_scripts
            WHERE story_id = ?
            ORDER BY turn_number DESC
            LIMIT 1;
            """,
            (story_id,),
        )
        row = await cursor.fetchone()
        return self._normalize_scene_script_row(row)

    async def get_scene_script_by_turn(
        self, story_id: str, turn_number: int
    ) -> dict[str, Any] | None:
        """指定ターンの scene_script を返す。存在しない場合は None。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM scene_scripts
            WHERE story_id = ? AND turn_number = ?;
            """,
            (story_id, turn_number),
        )
        row = await cursor.fetchone()
        return self._normalize_scene_script_row(row)

    async def get_recent_scene_scripts(
        self, story_id: str, limit: int = 1
    ) -> list[dict[str, Any]]:
        """story_id の最新 N 件の scene_scripts を新しい順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT * FROM scene_scripts
            WHERE story_id = ?
            ORDER BY turn_number DESC
            LIMIT ?;
            """,
            (story_id, max(1, limit)),
        )
        rows = await cursor.fetchall()
        return [normalized for row in rows if (normalized := self._normalize_scene_script_row(row))]

    async def delete_scene_scripts_from_turn(
        self, story_id: str, turn_number: int
    ) -> int:
        """story_id の turn_number 以降 (含む) の scene_scripts を全削除する。削除件数を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            DELETE FROM scene_scripts
            WHERE story_id = ? AND turn_number >= ?;
            """,
            (story_id, turn_number),
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    @staticmethod
    def _normalize_scene_script_row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        metadata = data.get("generation_metadata")
        if isinstance(metadata, str) and metadata.strip():
            try:
                parsed = json.loads(metadata)
            except json.JSONDecodeError:
                parsed = {}
            data["generation_metadata"] = parsed if isinstance(parsed, dict) else {}
        elif metadata is None:
            data["generation_metadata"] = {}
        return data

    # ------------------------------------------------------------------
    # 時事モード: per-story 設定
    # ------------------------------------------------------------------

    async def get_news_mode_settings(self, story_id: str) -> dict[str, Any]:
        """story_id の時事モード設定を返す。未設定の場合はデフォルト値を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT enabled, intensity FROM story_news_mode_settings WHERE story_id = ?",
            (story_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"enabled": False, "intensity": "low"}
        return {"enabled": bool(row["enabled"]), "intensity": str(row["intensity"])}

    async def upsert_news_mode_settings(
        self, story_id: str, enabled: bool, intensity: str
    ) -> None:
        """story_id の時事モード設定を更新（なければ作成）する。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO story_news_mode_settings (story_id, enabled, intensity, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(story_id) DO UPDATE SET
                enabled    = excluded.enabled,
                intensity  = excluded.intensity,
                updated_at = excluded.updated_at
            """,
            (story_id, int(enabled), intensity),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # 発話文字数設定（per-story）
    # ------------------------------------------------------------------

    async def get_utterance_settings(self, story_id: str) -> dict[str, Any]:
        """story_id の発話長設定を返す。未設定の場合はデフォルト値を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT max_chars, max_sentences FROM story_utterance_settings WHERE story_id = ?",
            (story_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"max_chars": 180, "max_sentences": 3}
        return {
            "max_chars": int(row["max_chars"]),
            "max_sentences": int(row["max_sentences"]),
        }

    async def upsert_utterance_settings(
        self,
        story_id: str,
        max_chars: int | None = None,
        max_sentences: int | None = None,
    ) -> None:
        """story_id の発話長設定を更新（なければ作成）する。未指定フィールドは現在値を保持。"""
        assert self._conn is not None
        current = await self.get_utterance_settings(story_id)
        eff_chars = max_chars if max_chars is not None else current["max_chars"]
        eff_sent = max_sentences if max_sentences is not None else current["max_sentences"]
        await self._conn.execute(
            """
            INSERT INTO story_utterance_settings (story_id, max_chars, max_sentences, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(story_id) DO UPDATE SET
                max_chars     = excluded.max_chars,
                max_sentences = excluded.max_sentences,
                updated_at    = excluded.updated_at
            """,
            (story_id, eff_chars, eff_sent),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # 時事モード: per-story タグフィルタ
    # ------------------------------------------------------------------

    async def get_news_tag_filter(self, story_id: str) -> list[str]:
        """story_id の時事モードタグフィルタ（タグ名のリスト）を返す。空 = フィルタなし。"""
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT tag_name FROM story_news_tag_filters "
            "WHERE story_id = ? ORDER BY tag_name COLLATE NOCASE",
            (story_id,),
        )
        return [r["tag_name"] for r in await cur.fetchall()]

    async def set_news_tag_filter(self, story_id: str, tag_names: list[str]) -> None:
        """story_id の時事モードタグフィルタを完全置換する。空リストならフィルタなし。"""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM story_news_tag_filters WHERE story_id = ?", (story_id,)
        )
        seen: set[str] = set()
        for name in tag_names:
            norm = " ".join((name or "").strip().split())
            key = norm.lower()
            if not norm or key in seen:
                continue
            seen.add(key)
            await self._conn.execute(
                "INSERT OR IGNORE INTO story_news_tag_filters (story_id, tag_name) VALUES (?, ?)",
                (story_id, norm),
            )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # 時事モード: コメント履歴（同一記事の重複コメント防止）
    # ------------------------------------------------------------------

    async def record_news_commentary(
        self, story_id: str, char_id: str, article_url: str, turn_number: int
    ) -> None:
        """キャラが時事感想を述べた記事 URL を記録する。重複は無視。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO news_commentary_history
                (story_id, char_id, article_url, turn_number)
            VALUES (?, ?, ?, ?)
            """,
            (story_id, char_id, article_url, turn_number),
        )
        await self._conn.commit()

    async def get_commented_article_urls(
        self, story_id: str, char_id: str
    ) -> set[str]:
        """そのキャラがこのストーリーで過去にコメント済みの article_url 集合を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT article_url FROM news_commentary_history
            WHERE story_id = ? AND char_id = ?
            """,
            (story_id, char_id),
        )
        rows = await cursor.fetchall()
        return {str(row["article_url"]) for row in rows}

    def _hash_secret(self, value: str, salt: str) -> str:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            value.encode("utf-8"),
            salt.encode("utf-8"),
            120_000,
        )
        return digest.hex()
