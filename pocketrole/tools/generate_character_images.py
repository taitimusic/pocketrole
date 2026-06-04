from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image
from web_placeholders import ensure_index_tree


DEFAULT_MAPPING: dict[str, tuple[int, int]] = {
    "happy": (1, 1),
    "content": (1, 2),
    "surprised": (1, 4),
    "worried": (2, 1),
    "sad": (2, 4),
    "neutral": (3, 2),
    "angry": (3, 4),
}

ANKOKU_GAKUEN_NON_NEUTRAL_3X3_MAPPING: dict[str, tuple[int, int]] = {
    "neutral": (1, 1),
    "happy": (1, 1),
    "angry": (1, 2),
    "sad": (1, 3),
    "surprised": (2, 1),
    "worried": (2, 2),
    "content": (2, 3),
    "lonely": (3, 1),
    "tired": (3, 2),
    "determined": (3, 3),
}

PRESET_MAPPINGS: dict[str, dict[str, tuple[int, int]]] = {
    "hoshikaze_runa": ANKOKU_GAKUEN_NON_NEUTRAL_3X3_MAPPING.copy(),
    "chururun": ANKOKU_GAKUEN_NON_NEUTRAL_3X3_MAPPING.copy(),
    "kamiizumi_souma": ANKOKU_GAKUEN_NON_NEUTRAL_3X3_MAPPING.copy(),
    "miritia": ANKOKU_GAKUEN_NON_NEUTRAL_3X3_MAPPING.copy(),
    "yokaze_yuuma": ANKOKU_GAKUEN_NON_NEUTRAL_3X3_MAPPING.copy(),
}


def generate_expression_images(
    source_path: Path,
    output_dir: Path,
    grid_rows: int,
    grid_cols: int,
    mapping: dict[str, tuple[int, int]],
    size: int = 52,
) -> list[Path]:
    image = Image.open(source_path).convert("RGBA")
    tile_width = image.width // grid_cols
    tile_height = image.height // grid_rows
    ensure_index_tree(output_dir, output_dir)
    generated: list[Path] = []

    for expression, (row, col) in sorted(mapping.items()):
        left = (col - 1) * tile_width
        top = (row - 1) * tile_height
        cropped = image.crop((left, top, left + tile_width, top + tile_height))
        resized = cropped.resize((size, size), Image.Resampling.LANCZOS)
        output_path = output_dir / f"{expression}.png"
        resized.save(output_path)
        generated.append(output_path)

    return generated


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate character expression PNGs from a sheet")
    parser.add_argument("source", type=Path, help="Path to the source expression sheet")
    parser.add_argument(
        "--output-dir",
        action="append",
        dest="output_dirs",
        type=Path,
        required=True,
        help="Directory to write generated PNGs to. Can be specified multiple times.",
    )
    parser.add_argument("--rows", type=int, default=3, help="Grid rows in the source sheet")
    parser.add_argument("--cols", type=int, default=4, help="Grid columns in the source sheet")
    parser.add_argument("--size", type=int, default=52, help="Square PNG size in pixels")
    parser.add_argument(
        "--preset",
        choices=sorted(PRESET_MAPPINGS),
        help="Use a built-in expression mapping for a known character",
    )
    args = parser.parse_args()
    mapping = PRESET_MAPPINGS.get(args.preset, DEFAULT_MAPPING)

    for output_dir in args.output_dirs:
        generate_expression_images(
            source_path=args.source,
            output_dir=output_dir,
            grid_rows=args.rows,
            grid_cols=args.cols,
            mapping=mapping,
            size=args.size,
        )


if __name__ == "__main__":
    main()
