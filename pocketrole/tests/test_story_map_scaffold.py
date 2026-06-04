from __future__ import annotations

import json
from pathlib import Path

from story_map_scaffold import ensure_story_map_scaffold


def test_ensure_story_map_scaffold_creates_story_scoped_manifest_and_preserves_manual_assets(
    tmp_path: Path,
) -> None:
    story_maps_root = tmp_path / "web" / "assets" / "story_maps"
    story_dir = story_maps_root / "test_story"
    images_dir = story_dir / "images"
    bgm_dir = story_dir / "bgm"
    manual_map = story_dir / "map.json"
    manual_png = images_dir / "classroom_320.png"
    manual_bgm_manifest = story_dir / "bgm_manifest.json"

    story_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    bgm_dir.mkdir(parents=True, exist_ok=True)
    manual_map.write_text('{"keep": true}\n', encoding="utf-8")
    manual_png.write_bytes(b"png")
    manual_bgm_manifest.write_text('{"story_default": "bgm/custom.mp3"}\n', encoding="utf-8")

    ensure_story_map_scaffold(
        story_id="test_story",
        places=[
            {"id": "classroom", "label": "教室"},
            {"id": "corridor", "label": "廊下"},
        ],
        story_maps_root=story_maps_root,
    )

    manifest = json.loads((story_dir / "place_manifest.json").read_text(encoding="utf-8"))
    assert manifest["story_id"] == "test_story"
    assert manifest["places"] == [
        {
            "place_id": "classroom",
            "label": "教室",
            "expected_image": "images/classroom_320.png",
        },
        {
            "place_id": "corridor",
            "label": "廊下",
            "expected_image": "images/corridor_320.png",
        },
    ]
    assert (story_maps_root / "index.html").read_bytes() == b""
    assert (story_dir / "index.html").read_bytes() == b""
    assert (images_dir / "index.html").read_bytes() == b""
    assert (bgm_dir / "index.html").read_bytes() == b""
    assert manual_map.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert manual_png.read_bytes() == b"png"
    assert manual_bgm_manifest.read_text(encoding="utf-8") == '{"story_default": "bgm/custom.mp3"}\n'
