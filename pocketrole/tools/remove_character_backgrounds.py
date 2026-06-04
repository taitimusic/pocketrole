from __future__ import annotations

from collections import deque
from pathlib import Path
from shutil import copy2

from PIL import Image


DEFAULT_THRESHOLD = 245
DEFAULT_STORY_ID = "ankoku_gakuen"
DEFAULT_TARGET_CHARACTERS = (
    "hoshikaze_runa",
    "kamiizumi_souma",
    "miritia",
    "yokaze_yuuma",
)


def _is_background(pixel: tuple[int, int, int, int], threshold: int) -> bool:
    r, g, b, a = pixel
    return a > 0 and r >= threshold and g >= threshold and b >= threshold


def compute_background_mask(image: Image.Image, threshold: int = DEFAULT_THRESHOLD) -> list[list[bool]]:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    mask = [[False for _ in range(width)] for _ in range(height)]
    queue: deque[tuple[int, int]] = deque()

    def enqueue_if_background(x: int, y: int) -> None:
        if mask[y][x]:
            return
        if _is_background(rgba.getpixel((x, y)), threshold):
            mask[y][x] = True
            queue.append((x, y))

    for x in range(width):
        enqueue_if_background(x, 0)
        enqueue_if_background(x, height - 1)
    for y in range(height):
        enqueue_if_background(0, y)
        enqueue_if_background(width - 1, y)

    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and not mask[ny][nx]:
                if _is_background(rgba.getpixel((nx, ny)), threshold):
                    mask[ny][nx] = True
                    queue.append((nx, ny))

    return mask


def apply_transparency(image: Image.Image, threshold: int = DEFAULT_THRESHOLD) -> Image.Image:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    mask = compute_background_mask(rgba, threshold=threshold)
    pixels = rgba.load()

    for y in range(height):
        for x in range(width):
            if mask[y][x]:
                r, g, b, _a = pixels[x, y]
                pixels[x, y] = (r, g, b, 0)

    return rgba


def process_image(path: Path, threshold: int = DEFAULT_THRESHOLD) -> None:
    backup_path = path.with_name(f"{path.stem}.pre_transparency_backup{path.suffix}")
    if not backup_path.exists():
        copy2(path, backup_path)

    image = Image.open(path)
    transparent = apply_transparency(image, threshold=threshold)
    transparent.save(path)


def iter_target_images(base_dir: Path) -> list[Path]:
    targets: list[Path] = []
    for char_id in DEFAULT_TARGET_CHARACTERS:
        char_dir = base_dir / char_id
        targets.extend(
            sorted(
                path
                for path in char_dir.glob("*.png")
                if ".pre_transparency_backup" not in path.name and not path.name.endswith("_bk.png")
            )
        )
    return targets


def main() -> None:
    base_dir = Path(__file__).resolve().parent.parent / "web" / "assets" / "character_images" / DEFAULT_STORY_ID
    for path in iter_target_images(base_dir):
        process_image(path)
        print(f"processed {path}")


if __name__ == "__main__":
    main()
