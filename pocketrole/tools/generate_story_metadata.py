from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml
from web_placeholders import ensure_index_for_file_parent


def export_story_metadata(story_dir: Path, output_path: Path) -> None:
    characters_path = story_dir / "characters.yaml"
    payload = yaml.safe_load(characters_path.read_text(encoding="utf-8"))
    characters = payload.get("characters", [])
    story_id = characters[0]["story_id"] if characters else story_dir.name

    data = {
        "story_id": story_id,
        "characters": [
            {
                "id": character["id"],
                "name": character["name"],
                "name_reading": character.get("name_reading"),
            }
            for character in characters
        ],
    }
    ensure_index_for_file_parent(output_path.parent, output_path)
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate public story metadata JSON from characters.yaml")
    parser.add_argument("story_dir", type=Path, help="Story directory containing characters.yaml")
    parser.add_argument("output_path", type=Path, help="Output JSON path")
    args = parser.parse_args()
    export_story_metadata(args.story_dir, args.output_path)


if __name__ == "__main__":
    main()
