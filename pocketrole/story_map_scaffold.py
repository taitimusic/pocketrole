from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from web_placeholders import ensure_index_for_file_parent, ensure_index_tree


DEFAULT_STORY_MAPS_ROOT = Path(__file__).parent / "web" / "assets" / "story_maps"
PLACE_IMAGE_SUFFIX = "_320.png"
BGM_MANIFEST_FILENAME = "bgm_manifest.json"


def resolve_story_maps_root(story_maps_root: str | Path | None = None) -> Path:
    if story_maps_root is None:
        return DEFAULT_STORY_MAPS_ROOT
    return Path(story_maps_root)


def build_place_manifest(story_id: str, places: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "story_id": story_id,
        "places": [
            {
                "place_id": str(place["id"]),
                "label": str(place.get("label") or place["id"]),
                "expected_image": f"images/{place['id']}{PLACE_IMAGE_SUFFIX}",
            }
            for place in places
        ],
    }


def build_bgm_manifest(story_id: str) -> dict[str, Any]:
    return {
        "story_id": story_id,
        "enabled": True,
        "story_default": None,
        "place_overrides": {},
        "mood_overrides": {},
    }


def ensure_story_map_scaffold(
    story_id: str,
    places: list[dict[str, Any]],
    story_maps_root: str | Path | None = None,
) -> Path:
    root = resolve_story_maps_root(story_maps_root)
    story_dir = root / story_id
    images_dir = story_dir / "images"
    bgm_dir = story_dir / "bgm"
    manifest_path = story_dir / "place_manifest.json"
    bgm_manifest_path = story_dir / BGM_MANIFEST_FILENAME

    ensure_index_tree(root, images_dir)
    ensure_index_tree(root, bgm_dir)
    ensure_index_for_file_parent(root, manifest_path)
    manifest_path.write_text(
        json.dumps(build_place_manifest(story_id, places), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not bgm_manifest_path.exists():
        ensure_index_for_file_parent(root, bgm_manifest_path)
        bgm_manifest_path.write_text(
            json.dumps(build_bgm_manifest(story_id), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return story_dir
