"""tests/test_check_stability.py — tools/check_stability の単体テスト (14テスト)

純粋関数（build_report, parse_log_file, compute_p95）と
データ変換をテストする。DB/ファイル I/O は最小限のモック。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.check_stability import (
    DbStats,
    LogStats,
    build_v2_closeout_issues,
    build_report,
    compute_p95,
    parse_log_file,
)

_STORY_ID = "test_story"


# ──────────────────────────────────────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────────────────────────────────────


def _make_db_stats(**kwargs: Any) -> DbStats:
    defaults: dict[str, Any] = dict(
        story_id=_STORY_ID,
        total_logs=100,
        unposted_logs=0,
        provider_breakdown={"ollama": 100},
        db_size_bytes=1024 * 1024,  # 1 MB
        emotion_anomalies=0,
        char_turn_counts={"char_001": 50, "char_002": 50},
        first_log_at="2025-03-01T10:00:00",
        last_log_at="2025-03-02T10:00:00",
    )
    defaults.update(kwargs)
    return DbStats(**defaults)


def _make_log_stats(**kwargs: Any) -> LogStats:
    defaults: dict[str, Any] = dict(
        total_lines=1000,
        error_count=0,
        warning_count=0,
        rate_limit_count=0,
        latency_samples=[100.0, 200.0, 300.0],
        providers_seen={"ollama"},
    )
    defaults.update(kwargs)
    return LogStats(**defaults)


def _write_log(tmp_path: Path, lines: list[dict[str, Any]]) -> str:
    log_file = tmp_path / "test.log"
    log_file.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines),
        encoding="utf-8",
    )
    return str(log_file)


# ──────────────────────────────────────────────────────────────────────────────
# build_report テスト
# ──────────────────────────────────────────────────────────────────────────────


def test_build_report_ok() -> None:
    """正常系: 全チェック通過 → status=OK, issues=[]"""
    report = build_report(_make_db_stats(), _make_log_stats())
    assert report.status == "OK"
    assert report.issues == []


def test_build_report_error_count_warn() -> None:
    """error_count > 0 → WARN"""
    report = build_report(_make_db_stats(), _make_log_stats(error_count=1))
    assert report.status == "WARN"
    assert any("ERROR" in issue for issue in report.issues)


def test_build_report_rate_limit_warn() -> None:
    """rate_limit_count / total_lines > 5% → WARN"""
    # 60/1000 = 6% > 5%
    report = build_report(
        _make_db_stats(),
        _make_log_stats(rate_limit_count=60, total_lines=1000),
    )
    assert report.status == "WARN"
    assert any("レート制限" in issue for issue in report.issues)


def test_build_report_unposted_warn() -> None:
    """unposted_logs / total_logs > 10% → WARN"""
    # 15/100 = 15% > 10%
    report = build_report(
        _make_db_stats(total_logs=100, unposted_logs=15),
        _make_log_stats(),
    )
    assert report.status == "WARN"
    assert any("未送信" in issue for issue in report.issues)


def test_build_report_emotion_anomaly_warn() -> None:
    """emotion_anomalies > 0 → WARN"""
    report = build_report(_make_db_stats(emotion_anomalies=1), _make_log_stats())
    assert report.status == "WARN"
    assert any("情動値" in issue for issue in report.issues)


def test_build_report_inactive_char_error() -> None:
    """turn=0 のキャラあり → ERROR"""
    report = build_report(
        _make_db_stats(char_turn_counts={"char_001": 50, "char_002": 0}),
        _make_log_stats(),
    )
    assert report.status == "ERROR"
    assert any("活動のないキャラ" in issue for issue in report.issues)


def test_build_report_large_db_warn() -> None:
    """db_size_bytes > 500MB → WARN"""
    big_size = 501 * 1024 * 1024  # 501 MB
    report = build_report(_make_db_stats(db_size_bytes=big_size), _make_log_stats())
    assert report.status == "WARN"
    assert any("DB サイズ" in issue for issue in report.issues)


def test_build_report_high_latency_warn() -> None:
    """P95 レイテンシ > 10000ms → WARN"""
    # 94 件の 100ms + 6 件の 15000ms → P95 index=95 → 15000ms > 10000ms
    samples = [100.0] * 94 + [15000.0] * 6
    report = build_report(
        _make_db_stats(), _make_log_stats(latency_samples=samples)
    )
    assert report.status == "WARN"
    assert any("レイテンシ" in issue for issue in report.issues)


def test_build_report_multiple_issues() -> None:
    """複数の問題が同時に検出される場合 → issues が複数, WARN"""
    report = build_report(
        _make_db_stats(emotion_anomalies=2, unposted_logs=20, total_logs=100),
        _make_log_stats(error_count=3, warning_count=5),
    )
    assert report.status == "WARN"
    assert len(report.issues) >= 2


def test_build_v2_closeout_issues_detects_chapter_director_and_scene_close_gaps() -> None:
    """v2 closeout は chapter/director/scene close の残課題を拾う。"""
    issues = build_v2_closeout_issues(
        {
            "active_chapter_id": "festival_arc",
            "active_chapter_beat": "complication",
            "active_chapter_age": 24,
            "active_director_persona_id": "sharp_cut",
            "director_satisfaction_overall": 0.31,
            "scene_close_completion_rate": 0.25,
            "closed_scenes_recent": 4,
            "reply_quality_fallbacks_recent": 3,
            "reply_focus_misses_recent": 2,
            "voice_flat_replies_recent": 1,
        }
    )

    assert any("chapter_progress_stalled" in issue for issue in issues)
    assert any("director_satisfaction_low" in issue for issue in issues)
    assert any("scene_close_weak" in issue for issue in issues)
    assert any("reply_quality_residual" in issue for issue in issues)


def test_build_v2_closeout_issues_skips_scene_close_warning_without_closed_scenes() -> None:
    """closed scene が無い窓では scene_close_completion_rate だけで warning にしない。"""
    issues = build_v2_closeout_issues(
        {
            "active_chapter_id": None,
            "active_chapter_beat": None,
            "active_chapter_age": 0,
            "active_director_persona_id": None,
            "director_satisfaction_overall": None,
            "scene_close_completion_rate": 0.0,
            "closed_scenes_recent": 0,
            "reply_quality_fallbacks_recent": 0,
            "reply_focus_misses_recent": 0,
            "voice_flat_replies_recent": 0,
        }
    )

    assert issues == []


# ──────────────────────────────────────────────────────────────────────────────
# parse_log_file テスト
# ──────────────────────────────────────────────────────────────────────────────


def test_parse_log_file_empty(tmp_path: Path) -> None:
    """空ファイル → ゼロ統計"""
    log_file = tmp_path / "empty.log"
    log_file.write_text("", encoding="utf-8")
    stats = parse_log_file(str(log_file))
    assert stats.total_lines == 0
    assert stats.error_count == 0
    assert stats.warning_count == 0
    assert stats.rate_limit_count == 0
    assert stats.latency_samples == []
    assert stats.providers_seen == set()


def test_parse_log_file_counts_errors(tmp_path: Path) -> None:
    """ERROR / WARNING 行を正しくカウントする"""
    log_file = tmp_path / "errors.log"
    lines = [
        {"ts": "2025-01-01T00:00:00", "level": "ERROR", "msg": "something failed"},
        {"ts": "2025-01-01T00:00:01", "level": "WARNING", "msg": "something off"},
        {"ts": "2025-01-01T00:00:02", "level": "INFO", "msg": "all good"},
        {"ts": "2025-01-01T00:00:03", "level": "ERROR", "msg": "failed again"},
    ]
    log_file.write_text(
        "\n".join(json.dumps(l) for l in lines), encoding="utf-8"
    )
    stats = parse_log_file(str(log_file))
    assert stats.total_lines == 4
    assert stats.error_count == 2
    assert stats.warning_count == 1


def test_parse_log_file_rate_limit(tmp_path: Path) -> None:
    """LLMRateLimitError を含む行を rate_limit_count にカウントする"""
    log_file = tmp_path / "ratelimit.log"
    lines = [
        {"ts": "2025-01-01T00:00:00", "level": "ERROR", "msg": "LLMRateLimitError raised"},
        {"ts": "2025-01-01T00:00:01", "level": "INFO", "msg": "retrying"},
    ]
    log_file.write_text(
        "\n".join(json.dumps(l) for l in lines), encoding="utf-8"
    )
    stats = parse_log_file(str(log_file))
    assert stats.rate_limit_count == 1


def test_parse_log_file_latency_samples(tmp_path: Path) -> None:
    """llm_latency_ms フィールドをサンプル収集し、llm_provider を providers_seen に追加する"""
    lines = [
        {
            "ts": "2025-01-01T00:00:00",
            "level": "INFO",
            "msg": "ok",
            "llm_latency_ms": 100.0,
            "llm_provider": "openai",
        },
        {
            "ts": "2025-01-01T00:00:01",
            "level": "INFO",
            "msg": "ok",
            "llm_latency_ms": 200.0,
            "llm_provider": "ollama",
        },
        {"ts": "2025-01-01T00:00:02", "level": "INFO", "msg": "no latency here"},
    ]
    log_file = tmp_path / "latency.log"
    log_file.write_text(
        "\n".join(json.dumps(l) for l in lines), encoding="utf-8"
    )
    stats = parse_log_file(str(log_file))
    assert stats.latency_samples == [100.0, 200.0]
    assert "openai" in stats.providers_seen
    assert "ollama" in stats.providers_seen


# ──────────────────────────────────────────────────────────────────────────────
# compute_p95 テスト
# ──────────────────────────────────────────────────────────────────────────────


def test_compute_p95_latency() -> None:
    """P95 計算: 100サンプル [1..100] → index=95 → 96.0、空リスト → None"""
    samples = [float(x) for x in range(1, 101)]
    # int(100 * 0.95) = 95, sorted[95] = 96.0
    assert compute_p95(samples) == 96.0

    # 空リスト
    assert compute_p95([]) is None
