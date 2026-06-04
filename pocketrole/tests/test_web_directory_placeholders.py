from __future__ import annotations

from pathlib import Path

import pytest

from web_placeholders import ensure_index_for_file_parent, ensure_index_tree


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = PROJECT_ROOT / "web"


def test_existing_web_directories_all_have_index_html() -> None:
    missing = sorted(
        str(path.relative_to(PROJECT_ROOT))
        for path in WEB_ROOT.rglob("*")
        if path.is_dir() and not (path / "index.html").exists()
    )
    assert missing == []


def test_ensure_index_tree_creates_index_html_for_each_level(tmp_path: Path) -> None:
    web_root = tmp_path / "web"
    target_dir = web_root / "published" / "stories" / "archive_story" / "pages"

    ensure_index_tree(web_root, target_dir)

    assert target_dir.is_dir()
    assert (web_root / "index.html").read_bytes() == b""
    assert (web_root / "published" / "index.html").read_bytes() == b""
    assert (web_root / "published" / "stories" / "index.html").read_bytes() == b""
    assert (web_root / "published" / "stories" / "archive_story" / "index.html").read_bytes() == b""
    assert (target_dir / "index.html").read_bytes() == b""


def test_ensure_index_for_file_parent_preserves_existing_index_html(tmp_path: Path) -> None:
    web_root = tmp_path / "web"
    existing_index = web_root / "assets" / "index.html"
    ensure_index_tree(web_root, existing_index.parent)
    existing_index.write_text("keep", encoding="utf-8")

    ensure_index_for_file_parent(web_root, web_root / "assets" / "story_metadata" / "story.json")

    assert existing_index.read_text(encoding="utf-8") == "keep"
    assert (web_root / "assets" / "story_metadata" / "index.html").read_bytes() == b""


def test_ensure_index_tree_rejects_targets_outside_root(tmp_path: Path) -> None:
    web_root = tmp_path / "web"
    outside = tmp_path / "outside"

    with pytest.raises(ValueError):
        ensure_index_tree(web_root, outside)
