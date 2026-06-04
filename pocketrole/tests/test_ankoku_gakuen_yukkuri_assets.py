from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
STORY_DIR = ROOT / "stories" / "ankoku_gakuen"
PUBLIC_IMAGE_DIR = ROOT / "web" / "assets" / "character_images" / "ankoku_gakuen"
METADATA_PATH = ROOT / "web" / "assets" / "story_metadata" / "ankoku_gakuen.json"
EXPECTED_EXPRESSIONS = ("neutral", "happy", "angry", "sad", "surprised", "content")
EXPECTED_YUKKURI = {
    "yukkuri_reimu": "ゆっくり霊夢",
    "yukkuri_marisa": "ゆっくり魔理沙",
}


def _load_characters() -> dict[str, dict]:
    payload = yaml.safe_load((STORY_DIR / "characters.yaml").read_text(encoding="utf-8"))
    return {character["id"]: character for character in payload["characters"]}


def test_ankoku_gakuen_includes_yukkuri_commentators() -> None:
    characters = _load_characters()

    reimu = characters["yukkuri_reimu"]
    marisa = characters["yukkuri_marisa"]
    expected_places = {
        "yukkuri_reimu": ["corridor", "library", "practice_hall"],
        "yukkuri_marisa": ["junk_yard", "cafeteria", "corridor"],
    }

    for char_id, expected_name in EXPECTED_YUKKURI.items():
        character = characters[char_id]
        assert character["name"] == expected_name
        assert tuple(character["expressions"]) == EXPECTED_EXPRESSIONS
        assert character["favorite_places"] == expected_places[char_id]

    assert "ツッコミ役" in reimu["personality"]["type"]
    assert "〜ね" in reimu["personality"]["speech_style"] or "〜わ" in reimu["personality"]["speech_style"]
    assert any("いや、それは違うわ" in example for example in reimu["personality"]["speech_examples"])
    assert any("それは違うわ" in note for note in reimu["behavior_notes"])
    assert any("サーカス" in note for note in reimu["behavior_notes"])

    assert "ボケ役" in marisa["personality"]["type"]
    assert "〜だぜ" in marisa["personality"]["speech_style"]
    assert any("これが芸術ってもんだぜ" in example for example in marisa["personality"]["speech_examples"])
    assert any("謎楽器" in note for note in marisa["behavior_notes"])
    assert any("スケッチブック" in note for note in marisa["behavior_notes"])


def test_public_assets_exist_for_each_yukkuri_expression() -> None:
    for char_id in EXPECTED_YUKKURI:
        for expression in EXPECTED_EXPRESSIONS:
            asset_path = PUBLIC_IMAGE_DIR / char_id / f"{expression}.png"
            assert asset_path.is_file(), str(asset_path)


def test_public_story_metadata_lists_yukkuri_commentators() -> None:
    payload = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    names_by_id = {character["id"]: character["name"] for character in payload["characters"]}

    for char_id, expected_name in EXPECTED_YUKKURI.items():
        assert names_by_id[char_id] == expected_name
