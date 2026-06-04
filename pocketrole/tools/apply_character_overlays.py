#!/usr/bin/env python3
"""tools/apply_character_overlays.py — overlay YAML を canonical characters.yaml へ反映する CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

from tools.character_overlay_utils import deep_merge

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STORY_MISMATCH = 2


def apply_overlay_payload(
    char_data: dict[str, Any],
    overlay_payload: dict[str, Any],
    *,
    story_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """overlay payload の canonical_patch を characters data へ適用する。"""
    if str(overlay_payload.get("story_id") or "") != story_id:
        raise ValueError("overlay story_id does not match target story")

    warnings: list[str] = []
    overlay_chars = dict(overlay_payload.get("characters") or {})
    merged = {"characters": []}
    seen_char_ids: set[str] = set()

    for char in char_data.get("characters", []):
        char_id = str(char.get("id") or "")
        seen_char_ids.add(char_id)
        overlay_entry = overlay_chars.get(char_id)
        if not isinstance(overlay_entry, dict):
            merged["characters"].append(char)
            continue
        canonical_patch = overlay_entry.get("canonical_patch") or {}
        unmapped_overlay = overlay_entry.get("unmapped_overlay") or {}
        merged_char = deep_merge(char, canonical_patch) if isinstance(canonical_patch, dict) else dict(char)
        merged["characters"].append(merged_char)
        if isinstance(unmapped_overlay, dict) and unmapped_overlay:
            warnings.append(
                f"{char_id}: unmapped overlay keys: {', '.join(sorted(unmapped_overlay.keys()))}"
            )

    for char_id in sorted(set(overlay_chars.keys()) - seen_char_ids):
        warnings.append(f"{char_id}: character not found in characters.yaml")

    return merged, warnings


async def apply_character_overlays(
    *,
    story_dir: str | Path,
    overlay_path: str | Path,
    output_path: str | Path | None = None,
    in_place: bool = False,
) -> int:
    """overlay YAML を story_dir 配下の characters.yaml へ反映する。"""
    try:
        story_dir = Path(story_dir)
        characters_path = story_dir / "characters.yaml"
        story_id = story_dir.name
        overlay_payload = yaml.safe_load(Path(overlay_path).read_text(encoding="utf-8")) or {}
        char_data = yaml.safe_load(characters_path.read_text(encoding="utf-8")) or {}
        merged, _warnings = apply_overlay_payload(char_data, overlay_payload, story_id=story_id)
        rendered = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)

        if in_place:
            backup_path = story_dir / "characters.yaml.bak"
            shutil.copyfile(characters_path, backup_path)
            characters_path.write_text(rendered, encoding="utf-8")
        else:
            destination = Path(output_path) if output_path is not None else story_dir / "characters.merged.yaml"
            destination.write_text(rendered, encoding="utf-8")
        return EXIT_OK
    except ValueError:
        return EXIT_STORY_MISMATCH
    except Exception:
        logger.exception("overlay apply failed")
        return EXIT_ERROR


def main() -> int:
    parser = argparse.ArgumentParser(description="overlay YAML を canonical characters.yaml へ反映する")
    parser.add_argument("--story-dir", required=True, type=Path, help="story directory path")
    parser.add_argument("--overlay", required=True, type=Path, help="overlay YAML path")
    parser.add_argument("--output", type=Path, default=None, help="非破壊出力先")
    parser.add_argument("--in-place", action="store_true", help="characters.yaml を直接更新する")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(
        apply_character_overlays(
            story_dir=args.story_dir,
            overlay_path=args.overlay,
            output_path=args.output,
            in_place=args.in_place,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
