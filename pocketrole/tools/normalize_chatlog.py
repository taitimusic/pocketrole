"""tools.normalize_chatlog — 公開 chatlog の重複整理と退避ツール。"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


_SIM_DT_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


@dataclass(slots=True)
class NormalizeChatlogResult:
    latest_file: str | None
    archived_files: list[str]
    public_file_count: int
    public_log_count: int


def _parse_sim_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    for fmt in _SIM_DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _normalized_sim_datetime(raw: Any) -> str:
    dt = _parse_sim_datetime(raw)
    if dt is None:
        return str(raw).strip()
    return dt.strftime("%Y-%m-%dT%H:%M")


def _logical_key(entry: dict[str, Any]) -> tuple[str, str, int]:
    turn_number = entry.get("turn_number")
    try:
        turn = int(turn_number)
    except (TypeError, ValueError):
        turn = 0
    return (
        _normalized_sim_datetime(entry.get("sim_datetime")),
        str(entry.get("char_id", "")),
        turn,
    )


def _entry_sort_key(entry: dict[str, Any]) -> tuple[datetime, str, int, str]:
    normalized_dt = _normalized_sim_datetime(entry.get("sim_datetime"))
    parsed = _parse_sim_datetime(normalized_dt)
    if parsed is None:
        parsed = datetime.min
    _, char_id, turn = _logical_key(entry)
    return (parsed, normalized_dt, turn, char_id)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        decoded = json.loads(line)
        if isinstance(decoded, dict):
            rows.append(decoded)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(body, encoding="utf-8")


def _dedupe_latest_wins(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        deduped[_logical_key(row)] = row
    return sorted(deduped.values(), key=_entry_sort_key)


def normalize_story_chatlog(
    *,
    story_dir: Path,
    archive_root: Path | None = None,
    keep_latest_public_only: bool = False,
) -> NormalizeChatlogResult:
    story_dir = story_dir.resolve()
    if not story_dir.is_dir():
        raise FileNotFoundError(f"story_dir not found: {story_dir}")

    dat_files = sorted(story_dir.glob("*.dat"))
    if not dat_files:
        return NormalizeChatlogResult(
            latest_file=None,
            archived_files=[],
            public_file_count=0,
            public_log_count=0,
        )

    latest_file = dat_files[-1]
    archived_files: list[str] = []

    if keep_latest_public_only:
        if archive_root is None:
            raise ValueError("archive_root is required when keep_latest_public_only=True")
        archive_story_dir = archive_root.resolve() / story_dir.name
        archive_story_dir.mkdir(parents=True, exist_ok=True)
        for path in dat_files[:-1]:
            target = archive_story_dir / path.name
            if target.exists():
                target.unlink()
            shutil.move(str(path), str(target))
            archived_files.append(path.name)
        dat_files = [latest_file]

    for path in dat_files:
        cleaned = _dedupe_latest_wins(_read_jsonl(path))
        _write_jsonl(path, cleaned)

    public_files = sorted(story_dir.glob("*.dat"))
    public_log_count = 0
    for path in public_files:
        public_log_count += len(_read_jsonl(path))

    return NormalizeChatlogResult(
        latest_file=latest_file.name,
        archived_files=archived_files,
        public_file_count=len(public_files),
        public_log_count=public_log_count,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize public chatlog files.")
    parser.add_argument("--story-dir", required=True, help="Path to chatlog/<story_id>/ directory")
    parser.add_argument("--archive-root", default=None, help="Archive root for retired .dat files")
    parser.add_argument(
        "--keep-latest-public-only",
        action="store_true",
        help="Archive older .dat files out of the public story directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    archive_root = Path(args.archive_root) if args.archive_root else None
    result = normalize_story_chatlog(
        story_dir=Path(args.story_dir),
        archive_root=archive_root,
        keep_latest_public_only=bool(args.keep_latest_public_only),
    )
    print(
        json.dumps(
            {
                "latest_file": result.latest_file,
                "archived_files": result.archived_files,
                "public_file_count": result.public_file_count,
                "public_log_count": result.public_log_count,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
