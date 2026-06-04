from __future__ import annotations

from pathlib import Path

from PIL import Image

from tools.generate_character_images import PRESET_MAPPINGS, generate_expression_images


def test_generate_expression_images_exports_expected_files(tmp_path: Path) -> None:
    """4x3 シートから指定表情の PNG を出力できる。"""
    source = tmp_path / "sheet.png"
    output_dir = tmp_path / "out"
    rows = 3
    cols = 4
    tile = 40
    sheet = Image.new("RGB", (cols * tile, rows * tile), color=(255, 255, 255))
    colors = {
        (1, 1): (255, 0, 0),
        (1, 2): (0, 255, 0),
        (1, 4): (0, 0, 255),
        (2, 1): (255, 255, 0),
        (2, 4): (255, 0, 255),
        (3, 2): (0, 255, 255),
        (3, 4): (64, 64, 64),
    }
    for (row, col), color in colors.items():
        left = (col - 1) * tile
        top = (row - 1) * tile
        for x in range(left, left + tile):
            for y in range(top, top + tile):
                sheet.putpixel((x, y), color)
    sheet.save(source)

    generated = generate_expression_images(
        source_path=source,
        output_dir=output_dir,
        grid_rows=rows,
        grid_cols=cols,
        mapping={
            "happy": (1, 1),
            "content": (1, 2),
            "surprised": (1, 4),
            "worried": (2, 1),
            "sad": (2, 4),
            "neutral": (3, 2),
            "angry": (3, 4),
        },
        size=52,
    )

    assert [path.name for path in generated] == [
        "angry.png",
        "content.png",
        "happy.png",
        "neutral.png",
        "sad.png",
        "surprised.png",
        "worried.png",
    ]
    for path in generated:
        assert path.exists()
        image = Image.open(path)
        assert image.size == (52, 52)
    assert (output_dir / "index.html").exists()


def test_character_presets_cover_story_specific_expressions() -> None:
    """表情シート preset は固有表情を含めて定義されている。"""
    assert "hoshikaze_runa" in PRESET_MAPPINGS
    assert "chururun" in PRESET_MAPPINGS
    assert "kamiizumi_souma" in PRESET_MAPPINGS
    assert "miritia" in PRESET_MAPPINGS
    assert "yokaze_yuuma" in PRESET_MAPPINGS
    assert "determined" in PRESET_MAPPINGS["chururun"]
    assert "tired" in PRESET_MAPPINGS["kamiizumi_souma"]
    for mapping in PRESET_MAPPINGS.values():
        assert mapping["neutral"] == mapping["happy"]
        assert set(mapping) == {
            "neutral",
            "happy",
            "angry",
            "sad",
            "surprised",
            "worried",
            "content",
            "lonely",
            "tired",
            "determined",
        }


def test_generated_assets_exist_for_all_available_ankoku_gakuen_faces() -> None:
    """今回対応する暗黒学園キャラの表情 PNG が揃っている。"""
    root = Path(__file__).resolve().parent.parent / "web" / "assets" / "character_images" / "ankoku_gakuen"
    all_faces = [
        "neutral.png",
        "happy.png",
        "angry.png",
        "sad.png",
        "surprised.png",
        "worried.png",
        "content.png",
        "lonely.png",
        "tired.png",
        "determined.png",
    ]
    expected = {
        "hoshikaze_runa": all_faces,
        "chururun": all_faces,
        "kamiizumi_souma": all_faces,
        "miritia": all_faces,
        "yokaze_yuuma": all_faces,
    }

    for char_id, filenames in expected.items():
        for filename in filenames:
            assert (root / char_id / filename).exists()
        assert (root / char_id / "index.html").exists()
    assert (root / "index.html").exists()
