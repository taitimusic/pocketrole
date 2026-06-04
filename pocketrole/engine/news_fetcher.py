"""
engine/news_fetcher.py — RSS 定期巡回タスク（時事モード用）

engine/main.py から asyncio.create_task で起動し、バックグラウンドで常駐する。
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import re as _re
from typing import Any

import aiohttp

from db.news_store import NewsArticleStore
from engine.config import NewsModeConfig
from engine.news_url import FeedUrlValidationError, validate_feed_url

logger = logging.getLogger(__name__)


class NewsFetcher:
    """RSS フィードを定期巡回して NewsArticleStore に記事を蓄積するバックグラウンドタスク。

    LLM を呼ばない。DB 操作は NewsArticleStore 経由のみ。
    """

    def __init__(self, store: NewsArticleStore, config: NewsModeConfig) -> None:
        self._store = store
        self._config = config
        self._running = False

    # ------------------------------------------------------------------
    # バックグラウンドループ
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """永続ループ。engine/main.py から asyncio.create_task で起動する。"""
        self._running = True
        logger.info("NewsFetcher: started (interval=%ss)", self._config.fetch_interval_sec)
        while self._running:
            try:
                await self._fetch_all_feeds()
            except asyncio.CancelledError:
                self._running = False
                raise
            except Exception:
                logger.exception(
                    "NewsFetcher: cycle raised unhandled exception; will retry next interval"
                )
            try:
                await asyncio.sleep(self._config.fetch_interval_sec)
            except asyncio.CancelledError:
                self._running = False
                raise

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # 手動トリガ（admin の "fetch-now" ボタン用）
    # ------------------------------------------------------------------

    async def fetch_all_now(self) -> int:
        """全有効フィードを即時巡回し、挿入できた総件数を返す。"""
        return await self._fetch_all_feeds()

    # ------------------------------------------------------------------
    # 内部実装
    # ------------------------------------------------------------------

    async def _fetch_all_feeds(self) -> int:
        feeds = await self._store.list_feeds()
        enabled_feeds = [f for f in feeds if f["enabled"]]
        if not enabled_feeds:
            logger.debug("NewsFetcher: no enabled feeds, skipping cycle")
            return 0

        total_inserted = 0
        for feed in enabled_feeds:
            try:
                inserted = await self._fetch_one(feed)
                total_inserted += inserted
            except Exception as exc:
                logger.warning(
                    "NewsFetcher: fetch failed feed_id=%s url=%s err=%s",
                    feed["id"],
                    feed["url"],
                    exc,
                )
        logger.info("NewsFetcher: cycle complete inserted=%d", total_inserted)
        return total_inserted

    async def _fetch_one(self, feed: dict[str, Any]) -> int:
        """1 フィードを取得・パースして store に挿入する。挿入件数を返す。"""
        raw_url = str(feed["url"])
        feed_id = int(feed["id"])
        try:
            url = validate_feed_url(
                raw_url,
                allow_private_hosts=self._config.allow_private_feed_hosts,
            )
        except FeedUrlValidationError as exc:
            logger.warning(
                "NewsFetcher: unsafe feed skipped feed_id=%d url=%s err=%s",
                feed_id,
                raw_url,
                exc,
            )
            return 0

        raw_bytes = await self._http_get(url)
        if raw_bytes is None:
            return 0

        parsed = await asyncio.to_thread(self._parse_feed, raw_bytes)
        if parsed is None:
            return 0

        items = self._normalize_entries(
            parsed.get("entries", []),
            max_entries=self._config.max_entries_per_feed,
            allow_private_hosts=self._config.allow_private_feed_hosts,
        )

        inserted = await self._store.insert_articles(
            feed_id,
            items,
            max_articles_kept=self._config.max_articles_kept,
        )
        await self._store.update_feed_last_fetched(feed_id)

        logger.debug(
            "NewsFetcher: fetched feed_id=%d url=%s inserted=%d", feed_id, url, inserted
        )
        return inserted

    async def _http_get(self, url: str) -> bytes | None:
        """URL から raw bytes を取得する。失敗時は None を返す。"""
        try:
            url = validate_feed_url(
                url,
                allow_private_hosts=self._config.allow_private_feed_hosts,
            )
        except FeedUrlValidationError as exc:
            logger.warning("NewsFetcher: unsafe request skipped url=%s err=%s", url, exc)
            return None
        timeout = aiohttp.ClientTimeout(total=self._config.http_timeout_sec)
        headers = {"User-Agent": self._config.user_agent}
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status >= 400:
                        logger.warning(
                            "NewsFetcher: HTTP %s url=%s", resp.status, url
                        )
                        return None
                    content_length = resp.content_length
                    if (
                        content_length is not None
                        and content_length > self._config.max_feed_bytes
                    ):
                        logger.warning(
                            "NewsFetcher: response too large content_length=%s url=%s",
                            content_length,
                            url,
                        )
                        return None
                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > self._config.max_feed_bytes:
                            logger.warning(
                                "NewsFetcher: response exceeded max bytes url=%s limit=%d",
                                url,
                                self._config.max_feed_bytes,
                            )
                            return None
                    return bytes(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("NewsFetcher: request error url=%s err=%s", url, exc)
            return None

    @staticmethod
    def _parse_feed(raw: bytes) -> dict[str, Any] | None:
        """feedparser でバイト列を同期パース（asyncio.to_thread 内で呼ぶ）。"""
        try:
            import feedparser  # type: ignore[import-untyped]
        except ImportError:
            logger.error("feedparser が未インストールです。pip install feedparser を実行してください。")
            return None
        try:
            return feedparser.parse(raw)  # type: ignore[no-any-return]
        except Exception as exc:
            logger.warning("NewsFetcher: feedparser error err=%s", exc)
            return None

    @staticmethod
    def _clean_description(text: str | None, max_len: int = 300) -> str | None:
        """feedparser の description/summary から安全なプレーンテキストを抽出する。

        - HTML タグを除去（正規表現、標準ライブラリのみ）
        - HTML エンティティをデコード（&amp; → & 等）
        - 連続空白・改行・タブを単一空白に正規化
        - max_len 文字でトリミング（LLM コンテキスト節約と DB 肥大化防止）
        """
        if not text:
            return None
        # script・style タグはコンテンツごと除去（中身を LLM に渡さないため）
        text = _re.sub(
            r"<(script|style)[^>]*>.*?</(script|style)>",
            "",
            text,
            flags=_re.DOTALL | _re.IGNORECASE,
        )
        # 残りの HTML タグを除去
        text = _re.sub(r"<[^>]+>", "", text)
        # HTML エンティティをデコード
        text = _html.unescape(text)
        # 空白・改行・タブを正規化（プロンプトインジェクション対策兼）
        text = " ".join(text.split())
        text = text.strip()
        if not text:
            return None
        return text[:max_len]

    @staticmethod
    def _extract_entry_tags(entry: Any) -> list[str]:
        """feedparser の entry から RSS タグ名を最大 10 件抽出する。

        `entry.tags` (feedparser 形式: [{term, scheme, label}]) と
        `entry.categories` (旧 RSS 形式) の両方を参照する。
        term があれば優先し、なければ label を使用。
        """
        names: list[str] = []
        seen: set[str] = set()
        for item in (getattr(entry, "tags", None) or []):
            if not isinstance(item, dict):
                continue
            raw = item.get("term") or item.get("label") or ""
            name = " ".join(str(raw).strip().split())
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        for cat in (getattr(entry, "categories", None) or []):
            name = " ".join(str(cat).strip().split())
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        return names[:10]

    @staticmethod
    def _normalize_entries(
        entries: list[Any],
        *,
        max_entries: int = 50,
        allow_private_hosts: bool = False,
    ) -> list[dict[str, Any]]:
        """feedparser の entries を {"title", "url", "description", "published_at", "tags"} 形式に正規化する。"""
        result: list[dict[str, Any]] = []
        for entry in entries:
            if max_entries > 0 and len(result) >= max_entries:
                break
            title = (getattr(entry, "title", None) or "").strip()
            link = (getattr(entry, "link", None) or "").strip()
            if not title or not link:
                continue
            try:
                link = validate_feed_url(link, allow_private_hosts=allow_private_hosts)
            except FeedUrlValidationError:
                continue
            # published_parsed は time.struct_time | None
            published_at: str | None = None
            if hasattr(entry, "published"):
                raw_pub = getattr(entry, "published", None)
                if raw_pub:
                    published_at = str(raw_pub)[:20]  # ISO 風に切り出すだけで十分
            # description: summary → content[0].value の順で取得
            summary = (getattr(entry, "summary", None) or "").strip()
            if not summary:
                content_list = getattr(entry, "content", [])
                if content_list:
                    first = content_list[0]
                    summary = (
                        first.get("value", "") if isinstance(first, dict)
                        else getattr(first, "value", "") or ""
                    ).strip()
            description = NewsFetcher._clean_description(summary)
            result.append({
                "title": title,
                "url": link,
                "published_at": published_at,
                "description": description,
                "tags": NewsFetcher._extract_entry_tags(entry),
            })
        return result
