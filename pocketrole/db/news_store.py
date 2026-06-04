"""
db/news_store.py — ニュース記事 DB（本体 pocketrole.db とは別ファイル）

時事モード用に RSS から収集した記事タイトル + URL を保管する。
pocketrole.db の DatabaseManager とは独立した軽量クラス。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS feeds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    title           TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_fetched_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id         INTEGER NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    description     TEXT,
    published_at    TEXT,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_articles_fetched_at ON articles(fetched_at DESC);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS feed_tags (
    feed_id INTEGER NOT NULL,
    tag_id  INTEGER NOT NULL,
    PRIMARY KEY (feed_id, tag_id),
    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)  REFERENCES tags(id)  ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS article_tags (
    article_id INTEGER NOT NULL,
    tag_id     INTEGER NOT NULL,
    PRIMARY KEY (article_id, tag_id),
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)     REFERENCES tags(id)     ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feed_tags_tag    ON feed_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class NewsArticleStore:
    """ニュース記事を管理する軽量 SQLite ラッパ（本体 DB と独立）。

    使い方::

        store = NewsArticleStore("db/news_articles.db")
        await store.initialize()
        feed_id = await store.upsert_feed("https://example.com/rss", "Example")
        await store.close()
    """

    def __init__(self, db_path: str | Path = "db/news_articles.db") -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """DB 接続・スキーマ作成。"""
        logger.info("NewsArticleStore: initialize path=%s", self._db_path)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        await self._conn.executescript(_SCHEMA_DDL)
        await self._conn.commit()
        # 既存 DB への description カラム追加（カラムが既に存在する場合は無視）
        # SQLite は ADD COLUMN IF NOT EXISTS をサポートしないため try/except で対応
        try:
            await self._conn.execute("ALTER TABLE articles ADD COLUMN description TEXT")
            await self._conn.commit()
            logger.info("NewsArticleStore: added description column to articles")
        except Exception:
            pass  # OperationalError: duplicate column name — 無視して継続
        logger.info("NewsArticleStore: ready path=%s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "NewsArticleStore":
        await self.initialize()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # フィード管理
    # ------------------------------------------------------------------

    async def upsert_feed(
        self,
        url: str,
        title: str | None = None,
        enabled: bool = True,
    ) -> int:
        """フィードを追加または更新し、feed_id を返す。"""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO feeds (url, title, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title   = COALESCE(excluded.title, feeds.title),
                enabled = excluded.enabled
            """,
            (url, title, int(enabled)),
        )
        await self._conn.commit()
        cursor = await self._conn.execute("SELECT id FROM feeds WHERE url = ?", (url,))
        row = await cursor.fetchone()
        return int(row["id"])

    async def update_feed(
        self,
        feed_id: int,
        *,
        url: str | None = None,
        title: str | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """フィードのフィールドを部分更新する。対象がなければ False を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute("SELECT 1 FROM feeds WHERE id = ?", (feed_id,))
        if await cursor.fetchone() is None:
            return False
        if url is not None:
            await self._conn.execute(
                "UPDATE feeds SET url = ? WHERE id = ?", (url, feed_id)
            )
        if title is not None:
            await self._conn.execute(
                "UPDATE feeds SET title = ? WHERE id = ?", (title, feed_id)
            )
        if enabled is not None:
            await self._conn.execute(
                "UPDATE feeds SET enabled = ? WHERE id = ?", (int(enabled), feed_id)
            )
        await self._conn.commit()
        return True

    async def list_feeds(self) -> list[dict[str, Any]]:
        """全フィード一覧を tags 配列付きで返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT id, url, title, enabled, last_fetched_at, created_at FROM feeds ORDER BY id"
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            feed_dict: dict[str, Any] = {
                "id": row["id"],
                "url": row["url"],
                "title": row["title"],
                "enabled": bool(row["enabled"]),
                "last_fetched_at": row["last_fetched_at"],
                "created_at": row["created_at"],
                "tags": await self.get_feed_tags(int(row["id"])),
            }
            result.append(feed_dict)
        return result

    # ------------------------------------------------------------------
    # タグ管理
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_tag(name: str) -> str:
        """前後・連続空白を整理してタグ名を正規化する（大小は DB 側 COLLATE NOCASE で吸収）。"""
        return " ".join(name.strip().split())

    async def _upsert_tag(self, name: str) -> int | None:
        """タグを登録または取得して tag_id を返す。空文字は None を返す。"""
        assert self._conn is not None
        name = self._normalize_tag(name)
        if not name:
            return None
        await self._conn.execute(
            "INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,)
        )
        cur = await self._conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)
        )
        row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def set_feed_tags(self, feed_id: int, tag_names: list[str]) -> None:
        """フィードのタグを tag_names で完全置換する（既存は全削除）。"""
        assert self._conn is not None
        await self._conn.execute("DELETE FROM feed_tags WHERE feed_id = ?", (feed_id,))
        seen: set[int] = set()
        for name in tag_names:
            tag_id = await self._upsert_tag(name)
            if tag_id is None or tag_id in seen:
                continue
            seen.add(tag_id)
            await self._conn.execute(
                "INSERT OR IGNORE INTO feed_tags (feed_id, tag_id) VALUES (?, ?)",
                (feed_id, tag_id),
            )
        await self._conn.commit()

    async def get_feed_tags(self, feed_id: int) -> list[str]:
        """フィードに付与されたタグ名リストを返す（アルファベット順）。"""
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT t.name FROM tags t "
            "JOIN feed_tags ft ON ft.tag_id = t.id "
            "WHERE ft.feed_id = ? ORDER BY t.name COLLATE NOCASE",
            (feed_id,),
        )
        return [r["name"] for r in await cur.fetchall()]

    async def list_all_tags(self) -> list[str]:
        """登録済みの全タグ名を返す（autocomplete 用）。"""
        assert self._conn is not None
        cur = await self._conn.execute("SELECT name FROM tags ORDER BY name COLLATE NOCASE")
        return [r["name"] for r in await cur.fetchall()]

    async def delete_feed(self, feed_id: int) -> bool:
        """フィードと紐付く記事を削除する（CASCADE）。対象がなければ False。"""
        assert self._conn is not None
        cursor = await self._conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        await self._conn.commit()
        return bool(cursor.rowcount)

    async def update_feed_last_fetched(self, feed_id: int) -> None:
        """最終取得日時を現在時刻で更新する。"""
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE feeds SET last_fetched_at = ? WHERE id = ?",
            (_now_iso(), feed_id),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # 記事管理
    # ------------------------------------------------------------------

    async def insert_articles(
        self,
        feed_id: int,
        items: list[dict[str, Any]],
        max_articles_kept: int = 1000,
    ) -> int:
        """記事を挿入し、挿入成功件数を返す。URL 重複はスキップ。

        items の各要素:
            {"title": str, "url": str, "published_at": str | None,
             "description": str | None, "tags": list[str] | None}

        "tags" が含まれる場合は article_tags にも保存する。
        max_articles_kept を超えた古い記事は削除する。
        """
        assert self._conn is not None
        inserted = 0
        ts = _now_iso()
        for item in items:
            url = (item.get("url") or "").strip()
            title = (item.get("title") or "").strip()
            if not url or not title:
                continue
            cursor = await self._conn.execute(
                """
                INSERT OR IGNORE INTO articles (feed_id, title, url, description, published_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (feed_id, title, url, item.get("description"), item.get("published_at"), ts),
            )
            if cursor.rowcount:
                inserted += 1
                article_id = cursor.lastrowid
                for tag_name in (item.get("tags") or []):
                    tag_id = await self._upsert_tag(tag_name)
                    if tag_id is not None:
                        await self._conn.execute(
                            "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
                            (article_id, tag_id),
                        )

        await self._conn.commit()

        # 上限を超えた古い記事を削除
        if max_articles_kept > 0:
            await self._conn.execute(
                """
                DELETE FROM articles
                WHERE id NOT IN (
                    SELECT id FROM articles
                    ORDER BY fetched_at DESC, id DESC
                    LIMIT ?
                )
                """,
                (max_articles_kept,),
            )
            await self._conn.commit()

        logger.debug("NewsArticleStore: inserted=%d feed_id=%d", inserted, feed_id)
        return inserted

    async def get_recent_articles(self, limit: int = 100) -> list[dict[str, Any]]:
        """最新の記事を取得日時降順で返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT a.id, a.feed_id, a.title, a.url, a.description, a.published_at, a.fetched_at,
                   f.title AS feed_title
            FROM articles a
            JOIN feeds f ON a.feed_id = f.id
            ORDER BY a.fetched_at DESC, a.id DESC
            LIMIT ?
            """,
            (max(1, limit),),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_recent_articles_for_tag_names(
        self,
        tag_names: list[str],
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """tag_names のいずれかにマッチする記事を最新順で返す。

        マッチ条件: 記事の article_tags に直接付与 OR フィードの feed_tags 経由で継承。
        tag_names が空の場合は get_recent_articles() にフォールバック（後方互換）。
        """
        assert self._conn is not None
        if not tag_names:
            return await self.get_recent_articles(limit=limit)
        placeholders = ",".join("?" * len(tag_names))
        sql = f"""
          SELECT DISTINCT a.id, a.feed_id, a.title, a.url, a.description,
                          a.published_at, a.fetched_at, f.title AS feed_title
          FROM articles a
          JOIN feeds f ON f.id = a.feed_id
          WHERE a.id IN (
              SELECT at2.article_id FROM article_tags at2
              JOIN tags t ON t.id = at2.tag_id
              WHERE t.name COLLATE NOCASE IN ({placeholders})
              UNION
              SELECT a2.id FROM articles a2
              JOIN feed_tags ft ON ft.feed_id = a2.feed_id
              JOIN tags t ON t.id = ft.tag_id
              WHERE t.name COLLATE NOCASE IN ({placeholders})
          )
          ORDER BY COALESCE(a.published_at, a.fetched_at) DESC, a.id DESC
          LIMIT ?
        """
        params: list[Any] = list(tag_names) + list(tag_names) + [max(1, limit)]
        cur = await self._conn.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]

    async def count_articles(self) -> int:
        """総記事数を返す。"""
        assert self._conn is not None
        cursor = await self._conn.execute("SELECT COUNT(*) FROM articles")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # リセット
    # ------------------------------------------------------------------

    async def reset_articles_only(self) -> None:
        """記事のみ全削除（フィードは保持）。"""
        assert self._conn is not None
        await self._conn.execute("DELETE FROM articles")
        await self._conn.commit()
        await self._conn.execute("VACUUM")
        logger.info("NewsArticleStore: articles reset (feeds preserved)")

    async def reset_all(self) -> None:
        """記事とフィードをすべて削除して VACUUM。"""
        assert self._conn is not None
        await self._conn.execute("DELETE FROM articles")
        await self._conn.execute("DELETE FROM feeds")
        await self._conn.commit()
        await self._conn.execute("VACUUM")
        logger.info("NewsArticleStore: full reset (feeds + articles)")
