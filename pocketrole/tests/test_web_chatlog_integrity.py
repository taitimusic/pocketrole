"""Web chatlog の整合性と cleanup tool の回帰テスト。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _load_normalizer_module():
    try:
        return importlib.import_module("tools.normalize_chatlog")
    except ModuleNotFoundError as exc:  # pragma: no cover - red phase only
        pytest.fail(f"tools.normalize_chatlog module is missing: {exc}")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_normalize_chatlog_archives_old_days_and_keeps_latest_copy(tmp_path: Path) -> None:
    """古い日付ファイルを退避し、最新日ファイル内の重複は後勝ちで正規化する。"""
    mod = _load_normalizer_module()

    story_dir = tmp_path / "chatlog" / "ankoku_gakuen"
    archive_root = tmp_path / "archive"
    story_dir.mkdir(parents=True)

    _write_jsonl(
        story_dir / "20260305.dat",
        [
            {
                "sim_datetime": "2025-04-01 10:00",
                "turn_number": 1,
                "char_id": "legacy_char",
                "message": "legacy",
            }
        ],
    )
    _write_jsonl(
        story_dir / "20260309.dat",
        [
            {
                "sim_datetime": "2025-04-01T01:00",
                "turn_number": 2,
                "char_id": "yokaze_yuuma",
                "message": "old body",
            },
            {
                "sim_datetime": "2025-04-01T01:00",
                "turn_number": 2,
                "char_id": "yokaze_yuuma",
                "message": "new body",
            },
            {
                "sim_datetime": "2025-04-01T01:30",
                "turn_number": 3,
                "char_id": "chururun",
                "message": "latest turn",
            },
        ],
    )

    result = mod.normalize_story_chatlog(
        story_dir=story_dir,
        archive_root=archive_root,
        keep_latest_public_only=True,
    )

    assert result.archived_files == ["20260305.dat"]
    assert result.latest_file == "20260309.dat"
    assert result.public_file_count == 1
    assert result.public_log_count == 2

    assert sorted(p.name for p in story_dir.glob("*.dat")) == ["20260309.dat"]
    assert sorted((archive_root / "ankoku_gakuen").glob("*.dat"))[0].name == "20260305.dat"

    cleaned_rows = _read_jsonl(story_dir / "20260309.dat")
    assert [row["message"] for row in cleaned_rows] == ["new body", "latest turn"]


def test_normalize_chatlog_sorts_public_rows_by_datetime_turn_and_char(tmp_path: Path) -> None:
    """正規化後の公開 rows は日時・ターン・char_id で安定順になる。"""
    mod = _load_normalizer_module()

    story_dir = tmp_path / "chatlog" / "ankoku_gakuen"
    story_dir.mkdir(parents=True)

    _write_jsonl(
        story_dir / "20260309.dat",
        [
            {
                "sim_datetime": "2025-04-01T01:30",
                "turn_number": 3,
                "char_id": "yokaze_yuuma",
                "message": "later",
            },
            {
                "sim_datetime": "2025-04-01T01:00",
                "turn_number": 2,
                "char_id": "hoshikaze_runa",
                "message": "middle",
            },
            {
                "sim_datetime": "2025-04-01T01:00",
                "turn_number": 2,
                "char_id": "chururun",
                "message": "same time earlier char",
            },
        ],
    )

    mod.normalize_story_chatlog(story_dir=story_dir)

    cleaned_rows = _read_jsonl(story_dir / "20260309.dat")
    ordered = [
        (row["sim_datetime"], row["turn_number"], row["char_id"])
        for row in cleaned_rows
    ]
    assert ordered == [
        ("2025-04-01T01:00", 2, "chururun"),
        ("2025-04-01T01:00", 2, "hoshikaze_runa"),
        ("2025-04-01T01:30", 3, "yokaze_yuuma"),
    ]


def test_api_php_uses_normalized_dedupe_instead_of_raw_string_sort() -> None:
    """latest/history/characters は raw 文字列比較ではなく正規化済み key を使う。"""
    api_php = _read("web/api.php")

    assert "normalize_sim_datetime" in api_php
    assert "make_logical_key" in api_php
    assert "dedupe_entries_latest_wins" in api_php
    assert "strcmp((string)($a['sim_datetime'] ?? ''), (string)($b['sim_datetime'] ?? ''))" not in api_php


def test_receiver_php_upserts_day_file_instead_of_blind_append() -> None:
    """receiver は同一 logical key の再送で blind append しない。"""
    receiver_php = _read("web/receiver.php")

    assert "FILE_APPEND" not in receiver_php
    assert "upsert_day_logs" in receiver_php
    assert "write_jsonl_file" in receiver_php
    assert "ensure_directory_with_index_html" in receiver_php


def test_receiver_php_accepts_authenticated_story_reset_before_logs_validation() -> None:
    """receiver は reset signal を logs 必須判定より前に処理する。"""
    receiver_php = _read("web/receiver.php")

    reset_pos = receiver_php.index("reset_story")
    logs_validation_pos = receiver_php.index("$logs = $input['logs'] ?? null;")

    assert reset_pos < logs_validation_pos
    assert "delete_story_dat_logs" in receiver_php
    assert "confirm" in receiver_php


def test_chatlog_php_ensures_directory_index_placeholders() -> None:
    """chatlog helper は作成ディレクトリに index.html を置く。"""
    chatlog_php = _read("web/chatlog_lib.php")

    assert "function ensure_directory_with_index_html" in chatlog_php
    assert "index.html" in chatlog_php
    assert "touch($index_path)" in chatlog_php
