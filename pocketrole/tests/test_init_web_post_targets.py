from __future__ import annotations

from pathlib import Path

import yaml

from tools.init_web_post_targets import init_web_post_targets_file


def test_init_web_post_targets_creates_file_with_requested_stories(tmp_path: Path) -> None:
    target_path = tmp_path / "web_post_targets.local.yaml"

    init_web_post_targets_file(target_path, ["ankoku_gakuen", "mystery_story"])

    data = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"ankoku_gakuen", "mystery_story"}
    assert data["ankoku_gakuen"]["enabled"] is True
    assert data["ankoku_gakuen"]["receiver_url"] == ""
    assert data["ankoku_gakuen"]["auth_token"] == ""


def test_init_web_post_targets_preserves_existing_values(tmp_path: Path) -> None:
    target_path = tmp_path / "web_post_targets.local.yaml"
    target_path.write_text(
        "ankoku_gakuen:\n"
        "  enabled: true\n"
        "  receiver_url: https://existing.example.com/receiver.php\n"
        "  auth_token: keep_me\n",
        encoding="utf-8",
    )

    init_web_post_targets_file(target_path, ["ankoku_gakuen", "new_story"])

    data = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    assert data["ankoku_gakuen"]["receiver_url"] == "https://existing.example.com/receiver.php"
    assert data["ankoku_gakuen"]["auth_token"] == "keep_me"
    assert data["new_story"]["enabled"] is True
