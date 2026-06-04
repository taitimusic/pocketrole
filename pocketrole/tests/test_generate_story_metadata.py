from __future__ import annotations

import json
from pathlib import Path

from tools.generate_story_metadata import export_story_metadata


def test_export_story_metadata_writes_character_name_map(tmp_path: Path) -> None:
    """characters.yaml から web 用 metadata JSON を生成できる。"""
    story_dir = Path(__file__).resolve().parent.parent / "stories" / "ankoku_gakuen"
    output_path = tmp_path / "ankoku_gakuen.json"

    export_story_metadata(story_dir, output_path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["story_id"] == "ankoku_gakuen"
    names_by_id = {character["id"]: character["name"] for character in payload["characters"]}
    assert names_by_id["yokaze_yuuma"] == "夜風ユウマ"
    assert names_by_id["hoshikaze_runa"] == "星風ルナ"
    assert names_by_id["kamiizumi_souma"] == "上泉ソーマ"
    assert (tmp_path / "index.html").exists()


def test_generated_story_metadata_assets_exist_for_public_web() -> None:
    """公開 web 用 metadata JSON が story ごとに配置されている。"""
    root = Path(__file__).resolve().parent.parent / "web" / "assets" / "story_metadata"

    assert (root / "ankoku_gakuen.json").exists()
    assert (root / "ankoku_gakuen_mystery.json").exists()
    assert (root / "index.html").exists()
