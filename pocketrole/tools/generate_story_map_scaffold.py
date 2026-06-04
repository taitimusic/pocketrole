#!/usr/bin/env python3
"""tools/generate_story_map_scaffold.py — story_maps scaffold 生成 CLI。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from story_map_scaffold import ensure_story_map_scaffold
from tools.import_story import EXIT_ERROR, EXIT_FILE_NOT_FOUND, EXIT_OK
from tools.validate_story import StoryValidationError, validate_story


def generate_story_map_scaffold(
    story_dir: str | Path,
    output_root: str | Path | None = None,
) -> Path:
    story_path = Path(story_dir)
    errors = validate_story(story_path)
    if errors:
        raise StoryValidationError(errors)

    world_data = yaml.safe_load((story_path / "world_config.yaml").read_text(encoding="utf-8"))
    story = world_data["story"]
    places = world_data.get("places") or []
    return ensure_story_map_scaffold(story["id"], places, output_root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate story_maps scaffold from a story directory")
    parser.add_argument("story_dir", help="Story directory containing world_config.yaml")
    parser.add_argument(
        "--output-root",
        default=None,
        help="story_maps output root. Defaults to pocketrole/web/assets/story_maps",
    )
    args = parser.parse_args()

    try:
        scaffold_dir = generate_story_map_scaffold(args.story_dir, args.output_root)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FILE_NOT_FOUND
    except StoryValidationError as exc:
        for err in exc.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # pragma: no cover - CLI safety net
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    manifest = json.loads((scaffold_dir / "place_manifest.json").read_text(encoding="utf-8"))
    print(f"OK: {scaffold_dir.name} story_maps scaffold generated — {len(manifest['places'])} places")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
