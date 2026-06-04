from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STORY_DIR = ROOT / "stories" / "ankoku_gakuen"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_runtime_config_enables_story_emergence_stack() -> None:
    cfg = _load_yaml(CONFIG_PATH)

    assert cfg["story_memory"]["enabled"] is True
    assert cfg["narration"]["enabled"] is True
    assert cfg["story_director"]["enabled"] is True
    assert cfg["novel_generator"]["enabled"] is True
    assert cfg["scene_management"]["enabled"] is True
    assert cfg["participation_planner"]["enabled"] is True
    assert cfg["participation_planner"]["max_support_characters"] == 1
    assert cfg["story_hooks"]["enabled"] is True
    assert cfg["relationship_dynamics"]["enabled"] is True
    assert cfg["quality_guard"]["enabled"] is True
    assert cfg["quality_guard"]["max_group_chars"] == 120
    assert cfg["growth_engine"]["enabled"] is True
    assert cfg["character_evolution"]["enabled"] is False


def test_ankoku_gakuen_main_cast_has_mystery_tuned_prompt_seed() -> None:
    payload = _load_yaml(STORY_DIR / "characters.yaml")
    characters = {character["id"]: character for character in payload["characters"]}

    # コミカル再設計版: 各キャラの goal/worry に設定されたテーマキーワード
    expected_theme_terms = {
        "yokaze_yuuma": "サーカス",
        "hoshikaze_runa": "サーカス",
        "miritia": "サーカス",
        "kamiizumi_souma": "学校",
        "chururun": "ルナ",
    }

    for char_id, term in expected_theme_terms.items():
        character = characters[char_id]
        personality = character["personality"]
        assert personality["speech_examples"], char_id
        assert personality["never_say"], char_id
        assert term in character["goal"] or term in character["worry"], char_id


def test_ankoku_gakuen_world_keeps_dark_academia_mystery_in_foreground() -> None:
    world = _load_yaml(STORY_DIR / "world_config.yaml")

    # コミカル再設計版: 世界観はサーカス × 経営難設定
    story = world["story"]
    assert "逢魔ヶ刻学園" in story["world_rules"]
    assert "トージョー大サーカス" in story["world_rules"]
    assert "焼坂" in story["world_rules"]

    places = {place["id"]: place for place in world["places"]}
    assert any("謎" in item or "楽器" in item for item in places["junk_yard"]["events_likely"])
    assert any("密談" in item or "倉庫" in item or "考え" in item for item in places["basement_storage"]["events_likely"])
    assert any("練習" in item or "リハーサル" in item for item in places["practice_hall"]["events_likely"])

    anomaly_labels = {rule["label"] for rule in world["anomaly_rules"]}
    assert "マリサの謎楽器持参" in anomaly_labels
    assert "ユウマの独白スイッチ" in anomaly_labels
    assert "経営陣の封書着弾" in anomaly_labels
