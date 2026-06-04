from __future__ import annotations

from pathlib import Path

import yaml
from PIL import Image

from tools.remove_character_backgrounds import apply_transparency


ROOT = Path(__file__).resolve().parent.parent


def test_apply_transparency_removes_edge_connected_white_background() -> None:
    image = Image.new("RGBA", (6, 6), (255, 255, 255, 255))
    pixels = image.load()

    for y in range(2, 5):
        for x in range(2, 5):
            pixels[x, y] = (20, 40, 60, 255)

    result = apply_transparency(image)

    assert result.getpixel((0, 0))[3] == 0
    assert result.getpixel((5, 5))[3] == 0
    assert result.getpixel((3, 3))[3] == 255


def test_apply_transparency_preserves_internal_white_island() -> None:
    image = Image.new("RGBA", (7, 7), (255, 255, 255, 255))
    pixels = image.load()

    for y in range(1, 6):
        for x in range(1, 6):
            pixels[x, y] = (40, 40, 40, 255)

    pixels[3, 3] = (255, 255, 255, 255)

    result = apply_transparency(image)

    assert result.getpixel((0, 0))[3] == 0
    assert result.getpixel((3, 3))[3] == 255


def test_ankoku_gakuen_neutral_portraits_exist_for_current_characters() -> None:
    base = ROOT / "web" / "assets" / "character_images" / "ankoku_gakuen"
    story_dir = ROOT / "stories" / "ankoku_gakuen"
    payload = yaml.safe_load((story_dir / "characters.yaml").read_text(encoding="utf-8"))

    for character in payload["characters"]:
        char_id = character["id"]
        image_path = base / char_id / "neutral.png"
        assert image_path.exists(), char_id
        image = Image.open(image_path).convert("RGBA")

        assert image.width > 0
        assert image.height > 0
