"""tests/test_apply_character_overlays.py — overlay apply CLI のテスト。"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests._async_harness import async_to_sync
from tools.apply_character_overlays import (
    EXIT_OK,
    EXIT_STORY_MISMATCH,
    apply_character_overlays,
    apply_overlay_payload,
)


def _write_characters_yaml(story_dir: Path) -> None:
    payload = {
        "characters": [
            {
                "id": "char_a",
                "name": "A",
                "personality": {"type": "静か", "speech_style": "丁寧"},
                "goal": "様子を見る",
                "worry": "まだ迷う",
            },
            {
                "id": "char_b",
                "name": "B",
                "personality": {"type": "強気"},
                "goal": "勝つ",
                "worry": "負けること",
            },
        ]
    }
    (story_dir / "characters.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _overlay_payload(story_id: str = "ankoku_gakuen") -> dict:
    return {
        "story_id": story_id,
        "generated_at": "2026-03-24T00:00:00+09:00",
        "source_db": "db/pocketrole.db",
        "characters": {
            "char_a": {
                "db_overlay": {
                    "current_goal": "踏み込む",
                    "mood_tag": "restless",
                },
                "canonical_patch": {
                    "goal": "踏み込む",
                },
                "unmapped_overlay": {
                    "mood_tag": "restless",
                },
            },
            "char_c": {
                "db_overlay": {"current_worry": "孤立する"},
                "canonical_patch": {"worry": "孤立する"},
                "unmapped_overlay": {},
            },
        },
    }


def test_apply_overlay_payload_merges_registry_paths_and_reports_warnings() -> None:
    char_data = {
        "characters": [
            {
                "id": "char_a",
                "name": "A",
                "personality": {"type": "静か"},
                "goal": "様子を見る",
                "worry": "まだ迷う",
            }
        ]
    }

    merged, warnings = apply_overlay_payload(char_data, _overlay_payload(), story_id="ankoku_gakuen")

    assert merged["characters"][0]["goal"] == "踏み込む"
    assert merged["characters"][0]["personality"]["type"] == "静か"
    assert "char_a: unmapped overlay keys: mood_tag" in warnings
    assert "char_c: character not found in characters.yaml" in warnings


@async_to_sync
async def test_apply_character_overlays_rejects_story_mismatch(tmp_path: Path) -> None:
    story_dir = tmp_path / "ankoku_gakuen"
    story_dir.mkdir()
    _write_characters_yaml(story_dir)
    overlay_path = tmp_path / "overlay.yaml"
    overlay_path.write_text(
        yaml.safe_dump(_overlay_payload(story_id="other_story"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    code = await apply_character_overlays(story_dir=story_dir, overlay_path=overlay_path)

    assert code == EXIT_STORY_MISMATCH


@async_to_sync
async def test_apply_character_overlays_in_place_creates_backup_and_updates_yaml(
    tmp_path: Path,
) -> None:
    story_dir = tmp_path / "ankoku_gakuen"
    story_dir.mkdir()
    _write_characters_yaml(story_dir)
    overlay_path = tmp_path / "overlay.yaml"
    overlay_path.write_text(
        yaml.safe_dump(_overlay_payload(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    code = await apply_character_overlays(
        story_dir=story_dir,
        overlay_path=overlay_path,
        in_place=True,
    )

    assert code == EXIT_OK
    assert (story_dir / "characters.yaml.bak").exists()
    merged = yaml.safe_load((story_dir / "characters.yaml").read_text(encoding="utf-8"))
    assert merged["characters"][0]["goal"] == "踏み込む"
    assert merged["characters"][1]["goal"] == "勝つ"
