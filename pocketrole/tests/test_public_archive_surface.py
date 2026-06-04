"""公開 archive surface のソースベース回帰テスト。"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_archive_viewer_exists_and_uses_published_json() -> None:
    viewer = _read("web/archive.php")
    assert "publishedBaseUrl" in viewer
    assert "chatlog" not in viewer
    assert "pocketrole.db" not in viewer


def test_archive_index_exists_and_uses_catalog_json() -> None:
    index_php = _read("web/archive_index.php")
    assert "publishedBaseUrl" in index_php

    app_js = _read("web/assets/archive_index.js")
    assert "catalog.json" in app_js


def test_archive_client_loads_manifest_and_page_json() -> None:
    app_js = _read("web/assets/archive_app.js")
    assert "manifest.json" in app_js
    assert "/pages/" in app_js
    assert "page=" in app_js
