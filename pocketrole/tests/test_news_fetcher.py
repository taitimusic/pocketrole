"""NewsFetcher safety and normalization tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.config import NewsModeConfig
from engine.news_fetcher import NewsFetcher
from engine.news_url import (
    FeedUrlValidationError,
    PublicHttpUrlValidationError,
    validate_feed_url,
    validate_public_http_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/rss",
        "http://example.com/feed.xml",
    ],
)
def test_validate_feed_url_accepts_public_http_urls(url: str) -> None:
    assert validate_feed_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/rss",
        "http://127.0.0.1/rss",
        "http://10.0.0.1/rss",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:pass@example.com/rss",
        "ftp://example.com/rss",
    ],
)
def test_validate_feed_url_rejects_ssrf_shapes_by_default(url: str) -> None:
    with pytest.raises(FeedUrlValidationError):
        validate_feed_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/receiver.php",
        "http://example.com/chururun/receiver.php",
    ],
)
def test_validate_public_http_url_accepts_public_receiver_urls(url: str) -> None:
    assert validate_public_http_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/receiver.php",
        "http://127.0.0.1/receiver.php",
        "http://10.0.0.1/receiver.php",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:pass@example.com/receiver.php",
        "ftp://example.com/receiver.php",
    ],
)
def test_validate_public_http_url_rejects_ssrf_shapes_by_default(url: str) -> None:
    with pytest.raises(PublicHttpUrlValidationError):
        validate_public_http_url(url)


@pytest.mark.asyncio
async def test_fetcher_skips_dangerous_feed_url_before_http() -> None:
    store = AsyncMock()
    store.list_feeds = AsyncMock(
        return_value=[{"id": 1, "url": "http://127.0.0.1/rss", "enabled": True}]
    )
    fetcher = NewsFetcher(store, NewsModeConfig())
    fetcher._http_get = AsyncMock(return_value=b"<rss />")  # type: ignore[method-assign]

    inserted = await fetcher.fetch_all_now()

    assert inserted == 0
    fetcher._http_get.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_http_get_rejects_content_length_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeContent:
        async def iter_chunked(self, _: int):
            yield b"x"

    class FakeResponse:
        status = 200
        content_length = 6
        content = FakeContent()

        async def __aenter__(self) -> "FakeResponse":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeSession:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def get(self, _: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("engine.news_fetcher.aiohttp.ClientSession", FakeSession)
    fetcher = NewsFetcher(AsyncMock(), NewsModeConfig(max_feed_bytes=5))

    assert await fetcher._http_get("https://example.com/rss") is None


@pytest.mark.asyncio
async def test_http_get_rejects_streaming_body_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeContent:
        async def iter_chunked(self, _: int):
            yield b"abc"
            yield b"def"

    class FakeResponse:
        status = 200
        content_length = None
        content = FakeContent()

        async def __aenter__(self) -> "FakeResponse":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeSession:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def get(self, _: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("engine.news_fetcher.aiohttp.ClientSession", FakeSession)
    fetcher = NewsFetcher(AsyncMock(), NewsModeConfig(max_feed_bytes=5))

    assert await fetcher._http_get("https://example.com/rss") is None


def test_normalize_entries_caps_entries_and_skips_invalid_urls() -> None:
    entries = [
        SimpleNamespace(title="A", link="https://example.com/a", published="2026-01-01"),
        SimpleNamespace(title="Private", link="http://127.0.0.1/private", published=None),
        SimpleNamespace(title="B", link="https://example.com/b", published=None),
        SimpleNamespace(title="C", link="https://example.com/c", published=None),
    ]

    normalized = NewsFetcher._normalize_entries(entries, max_entries=2)

    assert [item["title"] for item in normalized] == ["A", "B"]


# ── _clean_description のテスト ─────────────────────────────────────────────


def test_clean_description_removes_html_tags() -> None:
    result = NewsFetcher._clean_description("<p>こんにちは<br>世界</p>")
    # タグ除去後は連結されるが、空白正規化で余白は除去される
    assert result == "こんにちは世界"


def test_clean_description_decodes_html_entities() -> None:
    result = NewsFetcher._clean_description("&lt;b&gt;テスト&amp;確認&lt;/b&gt;")
    assert result == "<b>テスト&確認</b>"


def test_clean_description_truncates_to_300_chars() -> None:
    long_text = "あ" * 400
    result = NewsFetcher._clean_description(long_text)
    assert result is not None
    assert len(result) == 300


def test_clean_description_returns_none_for_empty_or_none() -> None:
    assert NewsFetcher._clean_description(None) is None
    assert NewsFetcher._clean_description("") is None
    assert NewsFetcher._clean_description("  <br>  ") is None


def test_clean_description_normalizes_whitespace() -> None:
    result = NewsFetcher._clean_description("  テスト\n\nの\t記事  ")
    assert result == "テスト の 記事"


def test_clean_description_strips_script_tags() -> None:
    result = NewsFetcher._clean_description('<script>alert("xss")</script>テスト')
    assert result == "テスト"
    assert "<script>" not in (result or "")


def test_normalize_entries_includes_description() -> None:
    entries = [
        SimpleNamespace(
            title="記事A",
            link="https://example.com/a",
            published="2026-01-01",
            summary="<p>これは概要です</p>",
        ),
        SimpleNamespace(
            title="記事B",
            link="https://example.com/b",
            published=None,
            summary="",
        ),
    ]

    normalized = NewsFetcher._normalize_entries(entries)

    assert normalized[0]["description"] == "これは概要です"
    assert normalized[1]["description"] is None
