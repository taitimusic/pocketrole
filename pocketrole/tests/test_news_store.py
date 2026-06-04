"""
tests/test_news_store.py — NewsArticleStore のテスト（in-memory SQLite）
"""

from __future__ import annotations

import asyncio

import pytest

from db.news_store import NewsArticleStore


# ------------------------------------------------------------------
# フィクスチャ
# ------------------------------------------------------------------

def _make_store() -> NewsArticleStore:
    return NewsArticleStore(":memory:")


async def _init() -> NewsArticleStore:
    store = _make_store()
    await store.initialize()
    return store


# ------------------------------------------------------------------
# フィード CRUD
# ------------------------------------------------------------------

def test_upsert_feed_returns_id() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss", "Example Feed")
        assert isinstance(feed_id, int)
        assert feed_id > 0
        await store.close()

    asyncio.run(_run())


def test_upsert_feed_deduplicates_by_url() -> None:
    async def _run() -> None:
        store = await _init()
        id1 = await store.upsert_feed("https://example.com/rss", "Title A")
        id2 = await store.upsert_feed("https://example.com/rss", "Title B")
        assert id1 == id2
        feeds = await store.list_feeds()
        assert len(feeds) == 1
        await store.close()

    asyncio.run(_run())


def test_list_feeds_returns_all() -> None:
    async def _run() -> None:
        store = await _init()
        await store.upsert_feed("https://a.com/rss")
        await store.upsert_feed("https://b.com/rss")
        feeds = await store.list_feeds()
        assert len(feeds) == 2
        urls = {f["url"] for f in feeds}
        assert "https://a.com/rss" in urls
        await store.close()

    asyncio.run(_run())


def test_delete_feed_removes_feed_and_articles() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss")
        await store.insert_articles(
            feed_id, [{"title": "Hello", "url": "https://example.com/1"}]
        )
        await store.delete_feed(feed_id)
        feeds = await store.list_feeds()
        assert feeds == []
        articles = await store.get_recent_articles(10)
        assert articles == []
        await store.close()

    asyncio.run(_run())


def test_update_feed_last_fetched() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss")
        feeds_before = await store.list_feeds()
        assert feeds_before[0]["last_fetched_at"] is None
        await store.update_feed_last_fetched(feed_id)
        feeds_after = await store.list_feeds()
        assert feeds_after[0]["last_fetched_at"] is not None
        await store.close()

    asyncio.run(_run())


# ------------------------------------------------------------------
# 記事 INSERT / 重複防止
# ------------------------------------------------------------------

def test_insert_articles_returns_count() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss")
        items = [
            {"title": "Article 1", "url": "https://example.com/1"},
            {"title": "Article 2", "url": "https://example.com/2"},
        ]
        count = await store.insert_articles(feed_id, items)
        assert count == 2
        await store.close()

    asyncio.run(_run())


def test_insert_articles_deduplicates_by_url() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss")
        items = [{"title": "Article 1", "url": "https://example.com/1"}]
        count1 = await store.insert_articles(feed_id, items)
        count2 = await store.insert_articles(feed_id, items)
        assert count1 == 1
        assert count2 == 0
        total = await store.count_articles()
        assert total == 1
        await store.close()

    asyncio.run(_run())


def test_insert_articles_skips_empty_title_or_url() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss")
        items = [
            {"title": "", "url": "https://example.com/1"},
            {"title": "Good", "url": ""},
            {"title": "Good", "url": "https://example.com/2"},
        ]
        count = await store.insert_articles(feed_id, items)
        assert count == 1
        await store.close()

    asyncio.run(_run())


def test_get_recent_articles_returns_latest_first() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss")
        for i in range(5):
            await store.insert_articles(
                feed_id, [{"title": f"Article {i}", "url": f"https://example.com/{i}"}]
            )
        articles = await store.get_recent_articles(limit=3)
        assert len(articles) == 3
        assert all("title" in a for a in articles)
        await store.close()

    asyncio.run(_run())


# ------------------------------------------------------------------
# max_articles_kept による古い記事の削除
# ------------------------------------------------------------------

def test_insert_articles_prunes_old_entries() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss")
        for i in range(5):
            await store.insert_articles(
                feed_id,
                [{"title": f"A{i}", "url": f"https://example.com/{i}"}],
                max_articles_kept=3,
            )
        total = await store.count_articles()
        assert total == 3
        await store.close()

    asyncio.run(_run())


# ------------------------------------------------------------------
# リセット
# ------------------------------------------------------------------

def test_reset_articles_only_keeps_feeds() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss")
        await store.insert_articles(
            feed_id, [{"title": "A", "url": "https://example.com/1"}]
        )
        await store.reset_articles_only()
        assert await store.count_articles() == 0
        feeds = await store.list_feeds()
        assert len(feeds) == 1
        await store.close()

    asyncio.run(_run())


def test_reset_all_removes_feeds_and_articles() -> None:
    async def _run() -> None:
        store = await _init()
        feed_id = await store.upsert_feed("https://example.com/rss")
        await store.insert_articles(
            feed_id, [{"title": "A", "url": "https://example.com/1"}]
        )
        await store.reset_all()
        assert await store.count_articles() == 0
        assert len(await store.list_feeds()) == 0
        await store.close()

    asyncio.run(_run())
