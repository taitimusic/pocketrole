#!/usr/bin/env python3
"""tools/check_stability.py — DB + ログファイルから安定性レポートを生成する CLI ツール

Usage:
    python -m tools.check_stability --story ankoku_gakuen
    python -m tools.check_stability --story ankoku_gakuen --db db/pocketrole.db --log logs/engine.log
    python -m tools.check_stability --story ankoku_gakuen --v2-closeout --window-turns 40

終了コード:
    0 — OK (WARNING なし)
    1 — 異常検出あり (WARN / ERROR)
    2 — ストーリーが見つからない
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_WARN = 1
EXIT_STORY_NOT_FOUND = 2

_DB_SIZE_WARN_BYTES = 500 * 1024 * 1024  # 500MB
_P95_WARN_MS = 10_000.0
_RATE_LIMIT_WARN_RATIO = 0.05
_UNPOSTED_WARN_RATIO = 0.10
_CHAPTER_PROGRESS_STALL_TURNS = 20
_DIRECTOR_SATISFACTION_WARN_BELOW = 0.40
_SCENE_CLOSE_COMPLETION_WARN_BELOW = 0.50


# ──────────────────────────────────────────────────────────────────────────────
# データクラス
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class DbStats:
    story_id: str
    total_logs: int
    unposted_logs: int
    provider_breakdown: dict[str, int]
    db_size_bytes: int
    emotion_anomalies: int
    char_turn_counts: dict[str, int]
    first_log_at: str | None
    last_log_at: str | None
    avg_latency_ms: float | None = None  # ログファイルから設定


@dataclass
class LogStats:
    total_lines: int
    error_count: int
    warning_count: int
    rate_limit_count: int
    latency_samples: list[float] = field(default_factory=list)
    providers_seen: set[str] = field(default_factory=set)


@dataclass
class StabilityReport:
    story_id: str
    status: str  # "OK" | "WARN" | "ERROR"
    issues: list[str]
    db: DbStats
    log: LogStats
    avg_latency_ms: float | None
    p95_latency_ms: float | None
    closeout_issues: list[str] = field(default_factory=list)
    closeout_snapshot: dict[str, Any] | None = None


# ──────────────────────────────────────────────────────────────────────────────
# 純粋ヘルパー関数
# ──────────────────────────────────────────────────────────────────────────────


def compute_p95(samples: list[float]) -> float | None:
    """P95 レイテンシを計算する。サンプルが空の場合は None。"""
    if not samples:
        return None
    sorted_samples = sorted(samples)
    idx = min(int(len(sorted_samples) * 0.95), len(sorted_samples) - 1)
    return sorted_samples[idx]


# ──────────────────────────────────────────────────────────────────────────────
# DB 統計取得（非同期）
# ──────────────────────────────────────────────────────────────────────────────


async def fetch_db_stats(db_path: str, story_id: str) -> DbStats | None:
    """DB から安定性統計を取得する。ストーリーが存在しない場合は None。"""
    db_size_bytes = 0
    path = Path(db_path)
    if path.exists():
        db_size_bytes = path.stat().st_size

    db = DatabaseManager(db_path)
    await db.initialize()
    conn = db._conn
    assert conn is not None
    try:
        # ストーリーの存在確認
        cursor = await conn.execute(
            "SELECT id FROM stories WHERE id = ?", (story_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        # 総ログ件数 & 未送信件数
        cursor = await conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN posted_to_web=0 THEN 1 ELSE 0 END) "
            "FROM chat_logs WHERE story_id = ?",
            (story_id,),
        )
        row = await cursor.fetchone()
        total_logs: int = row[0] or 0
        unposted_logs: int = row[1] or 0

        # プロバイダー別件数
        cursor = await conn.execute(
            "SELECT llm_provider, COUNT(*) FROM chat_logs WHERE story_id = ? "
            "GROUP BY llm_provider",
            (story_id,),
        )
        rows = await cursor.fetchall()
        provider_breakdown: dict[str, int] = {
            (r[0] or "unknown"): r[1] for r in rows
        }

        # 情動値異常（chat_logs の emotion_snapshot JSON から）
        cursor = await conn.execute(
            "SELECT emotion_snapshot FROM chat_logs "
            "WHERE story_id = ? AND emotion_snapshot IS NOT NULL",
            (story_id,),
        )
        rows = await cursor.fetchall()
        emotion_anomalies = 0
        for (snapshot_str,) in rows:
            try:
                snapshot: Any = json.loads(snapshot_str)
                if isinstance(snapshot, dict):
                    for v in snapshot.values():
                        if isinstance(v, (int, float)) and not (0.0 <= v <= 1.0):
                            emotion_anomalies += 1
                            break
            except (json.JSONDecodeError, AttributeError):
                pass

        # キャラ別最大ターン番号
        cursor = await conn.execute(
            "SELECT char_id, MAX(turn_number) FROM character_states "
            "WHERE story_id = ? GROUP BY char_id",
            (story_id,),
        )
        rows = await cursor.fetchall()
        char_turn_counts: dict[str, int] = {r[0]: r[1] or 0 for r in rows}

        # 最古・最新 created_at
        cursor = await conn.execute(
            "SELECT MIN(created_at), MAX(created_at) FROM chat_logs WHERE story_id = ?",
            (story_id,),
        )
        row = await cursor.fetchone()
        first_log_at: str | None = row[0]
        last_log_at: str | None = row[1]
    finally:
        await db.close()

    return DbStats(
        story_id=story_id,
        total_logs=total_logs,
        unposted_logs=unposted_logs,
        provider_breakdown=provider_breakdown,
        db_size_bytes=db_size_bytes,
        emotion_anomalies=emotion_anomalies,
        char_turn_counts=char_turn_counts,
        first_log_at=first_log_at,
        last_log_at=last_log_at,
    )


# ──────────────────────────────────────────────────────────────────────────────
# ログファイル解析（同期）
# ──────────────────────────────────────────────────────────────────────────────


def parse_log_file(log_path: str) -> LogStats:
    """JSON ログファイルを解析して LogStats を返す。ファイルが存在しない場合はゼロ統計。"""
    path = Path(log_path)
    if not path.exists():
        return LogStats(
            total_lines=0,
            error_count=0,
            warning_count=0,
            rate_limit_count=0,
        )

    total_lines = 0
    error_count = 0
    warning_count = 0
    rate_limit_count = 0
    latency_samples: list[float] = []
    providers_seen: set[str] = set()

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_lines += 1

            # JSON パース試行
            try:
                entry: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                entry = {}

            level = entry.get("level", "")
            if level == "ERROR":
                error_count += 1
            elif level == "WARNING":
                warning_count += 1

            # LLMRateLimitError は行内文字列検索（JSON 未パースでも検出）
            if "LLMRateLimitError" in line:
                rate_limit_count += 1

            latency = entry.get("llm_latency_ms")
            if isinstance(latency, (int, float)):
                latency_samples.append(float(latency))

            provider = entry.get("llm_provider")
            if provider:
                providers_seen.add(str(provider))

    return LogStats(
        total_lines=total_lines,
        error_count=error_count,
        warning_count=warning_count,
        rate_limit_count=rate_limit_count,
        latency_samples=latency_samples,
        providers_seen=providers_seen,
    )


# ──────────────────────────────────────────────────────────────────────────────
# レポート生成（純粋関数）
# ──────────────────────────────────────────────────────────────────────────────


def build_report(db_stats: DbStats, log_stats: LogStats) -> StabilityReport:
    """安定性レポートを生成する（純粋関数）。"""
    issues: list[str] = []
    has_error = False

    # エラー件数チェック
    if log_stats.error_count > 0:
        issues.append(f"ERROR ログが {log_stats.error_count} 件あります")

    # レート制限エラー率チェック
    if log_stats.total_lines > 0:
        rate_limit_ratio = log_stats.rate_limit_count / log_stats.total_lines
        if rate_limit_ratio > _RATE_LIMIT_WARN_RATIO:
            issues.append(
                f"レート制限エラー率が高い: "
                f"{rate_limit_ratio:.1%} ({log_stats.rate_limit_count}/{log_stats.total_lines})"
            )

    # 未送信ログ率チェック
    if db_stats.total_logs > 0:
        unposted_ratio = db_stats.unposted_logs / db_stats.total_logs
        if unposted_ratio > _UNPOSTED_WARN_RATIO:
            issues.append(
                f"未送信ログ率が高い: "
                f"{unposted_ratio:.1%} ({db_stats.unposted_logs}/{db_stats.total_logs})"
            )

    # 情動値異常チェック
    if db_stats.emotion_anomalies > 0:
        issues.append(f"情動値が範囲外のログが {db_stats.emotion_anomalies} 件あります")

    # キャラ活動なしチェック（ERROR レベル）
    inactive_chars = [c for c, t in db_stats.char_turn_counts.items() if t == 0]
    if inactive_chars:
        issues.append(f"活動のないキャラ: {', '.join(sorted(inactive_chars))}")
        has_error = True

    # DB ファイルサイズチェック
    if db_stats.db_size_bytes > _DB_SIZE_WARN_BYTES:
        size_mb = db_stats.db_size_bytes / (1024 * 1024)
        issues.append(f"DB サイズが大きい: {size_mb:.1f} MB")

    # レイテンシ計算とチェック
    avg_latency: float | None = (
        sum(log_stats.latency_samples) / len(log_stats.latency_samples)
        if log_stats.latency_samples
        else None
    )
    p95_latency = compute_p95(log_stats.latency_samples)
    if p95_latency is not None and p95_latency > _P95_WARN_MS:
        issues.append(f"P95 レイテンシが高い: {p95_latency:.1f} ms")

    # ステータス決定
    if has_error:
        status = "ERROR"
    elif issues:
        status = "WARN"
    else:
        status = "OK"

    return StabilityReport(
        story_id=db_stats.story_id,
        status=status,
        issues=issues,
        db=db_stats,
        log=log_stats,
        avg_latency_ms=avg_latency,
        p95_latency_ms=p95_latency,
    )


def build_v2_closeout_issues(snapshot: dict[str, Any]) -> list[str]:
    """v2 closeout で見る chapter/director/scene-close の warning を返す。"""
    issues: list[str] = []

    active_chapter_id = snapshot.get("active_chapter_id")
    active_chapter_age = int(snapshot.get("active_chapter_age") or 0)
    if active_chapter_id is not None and active_chapter_age >= _CHAPTER_PROGRESS_STALL_TURNS:
        issues.append(
            "chapter_progress_stalled: "
            f"active_chapter_id={active_chapter_id}, "
            f"active_chapter_beat={snapshot.get('active_chapter_beat') or 'none'}, "
            f"active_chapter_age={active_chapter_age}"
        )

    active_persona_id = snapshot.get("active_director_persona_id")
    overall = snapshot.get("director_satisfaction_overall")
    if active_persona_id is not None and isinstance(overall, (int, float)):
        if float(overall) < _DIRECTOR_SATISFACTION_WARN_BELOW:
            issues.append(
                "director_satisfaction_low: "
                f"active_persona={active_persona_id}, "
                f"director_satisfaction_overall={float(overall):.3f}"
            )

    scene_close_completion_rate = snapshot.get("scene_close_completion_rate")
    closed_scenes_recent = int(snapshot.get("closed_scenes_recent") or 0)
    if isinstance(scene_close_completion_rate, (int, float)) and closed_scenes_recent > 0:
        if float(scene_close_completion_rate) < _SCENE_CLOSE_COMPLETION_WARN_BELOW:
            issues.append(
                "scene_close_weak: "
                f"scene_close_completion_rate={float(scene_close_completion_rate):.2f}, "
                f"closed_scenes_recent={closed_scenes_recent}"
            )

    reply_quality_fallbacks_recent = int(snapshot.get("reply_quality_fallbacks_recent") or 0)
    reply_focus_misses_recent = int(snapshot.get("reply_focus_misses_recent") or 0)
    voice_flat_replies_recent = int(snapshot.get("voice_flat_replies_recent") or 0)
    if (
        reply_quality_fallbacks_recent > 0
        or reply_focus_misses_recent > 0
        or voice_flat_replies_recent > 0
    ):
        issues.append(
            "reply_quality_residual: "
            f"reply_quality_fallbacks_recent={reply_quality_fallbacks_recent}, "
            f"reply_focus_misses_recent={reply_focus_misses_recent}, "
            f"voice_flat_replies_recent={voice_flat_replies_recent}"
        )

    return issues


# ──────────────────────────────────────────────────────────────────────────────
# レポート表示
# ──────────────────────────────────────────────────────────────────────────────


def print_report(report: StabilityReport) -> None:
    """レポートを stdout に出力する。"""
    print("=== PocketRole Stability Report ===")
    print(f"Story   : {report.story_id}")
    print(f"Status  : {report.status}")
    print()

    db = report.db
    total_mb = db.db_size_bytes / (1024 * 1024)
    unposted_pct = (
        f"{db.unposted_logs / db.total_logs:.1%}" if db.total_logs > 0 else "N/A"
    )
    provider_str = ", ".join(
        f"{k}={v}" for k, v in sorted(db.provider_breakdown.items())
    )
    char_turn_str = ", ".join(
        f"{k}={v}" for k, v in sorted(db.char_turn_counts.items())
    )

    print("[DB]")
    print(f"  Total logs     : {db.total_logs}")
    print(f"  Unposted logs  : {db.unposted_logs}  ({unposted_pct})")
    print(f"  DB size        : {total_mb:.1f} MB")
    print(f"  Provider stats : {provider_str or '(none)'}")
    print(f"  Char turns     : {char_turn_str or '(none)'}")
    print(f"  Emotion anomaly: {db.emotion_anomalies}")
    print(f"  First log      : {db.first_log_at or '(none)'}")
    print(f"  Last log       : {db.last_log_at or '(none)'}")
    print()

    log = report.log
    rate_limit_pct = (
        f"{log.rate_limit_count / log.total_lines:.1%}" if log.total_lines > 0 else "N/A"
    )
    providers_str = (
        ", ".join(sorted(log.providers_seen)) if log.providers_seen else "(none)"
    )

    print("[Log file]")
    print(f"  Total lines    : {log.total_lines}")
    print(f"  Errors         : {log.error_count}")
    print(f"  Warnings       : {log.warning_count}")
    print(f"  Rate limit err : {log.rate_limit_count}  ({rate_limit_pct})")
    print(f"  Providers seen : {providers_str}")
    print()

    print("[Latency]")
    if report.avg_latency_ms is not None:
        print(f"  Average        : {report.avg_latency_ms:.1f} ms")
    else:
        print("  Average        : N/A")
    if report.p95_latency_ms is not None:
        print(f"  P95            : {report.p95_latency_ms:.1f} ms")
    else:
        print("  P95            : N/A")
    print()

    print("[Issues]")
    if report.issues:
        for issue in report.issues:
            print(f"  - {issue}")
    else:
        print("  (none)")

    if report.closeout_snapshot is not None:
        snapshot = report.closeout_snapshot
        print()
        print("[V2 Closeout]")
        print(
            "  Chapter        : "
            f"{snapshot.get('active_chapter_id') or '(none)'} / "
            f"beat={snapshot.get('active_chapter_beat') or 'none'} / "
            f"age={int(snapshot.get('active_chapter_age') or 0)}"
        )
        print(
            "  Director       : "
            f"{snapshot.get('active_director_persona_id') or '(none)'} / "
            f"overall={snapshot.get('director_satisfaction_overall') if snapshot.get('director_satisfaction_overall') is not None else 'N/A'}"
        )
        print(
            "  Scene close    : "
            f"completion_rate={float(snapshot.get('scene_close_completion_rate') or 0.0):.2f} / "
            f"closed_scenes_recent={int(snapshot.get('closed_scenes_recent') or 0)}"
        )
        print(
            "  Reply residual : "
            f"fallbacks={int(snapshot.get('reply_quality_fallbacks_recent') or 0)} / "
            f"focus_misses={int(snapshot.get('reply_focus_misses_recent') or 0)} / "
            f"voice_flat={int(snapshot.get('voice_flat_replies_recent') or 0)}"
        )
        print()
        print("  Closeout issues")
        if report.closeout_issues:
            for issue in report.closeout_issues:
                print(f"  - {issue}")
        else:
            print("  (none)")


# ──────────────────────────────────────────────────────────────────────────────
# 非同期コア関数
# ──────────────────────────────────────────────────────────────────────────────


async def run_check(
    story_id: str,
    db_path: str = "db/pocketrole.db",
    log_path: str = "logs/engine.log",
    *,
    v2_closeout: bool = False,
    window_turns: int = 20,
) -> int:
    """安定性チェックを実行して終了コードを返す。"""
    db_stats = await fetch_db_stats(db_path, story_id)
    if db_stats is None:
        logger.error("ストーリーが見つかりません: story_id=%s", story_id)
        return EXIT_STORY_NOT_FOUND

    log_stats = parse_log_file(log_path)
    report = build_report(db_stats, log_stats)
    if v2_closeout:
        from tools.monitor_engine_runtime import _collect_story_quality_metrics

        db = DatabaseManager(db_path)
        await db.initialize()
        try:
            snapshot = await _collect_story_quality_metrics(db, story_id, window_turns=window_turns)
        finally:
            await db.close()

        closeout_issues = build_v2_closeout_issues(snapshot)
        report.closeout_snapshot = snapshot
        report.closeout_issues = closeout_issues
        if closeout_issues and report.status == "OK":
            report.status = "WARN"
        report.issues.extend(closeout_issues)

    print_report(report)

    return EXIT_OK if report.status == "OK" else EXIT_WARN


# ──────────────────────────────────────────────────────────────────────────────
# CLI エントリーポイント
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    """CLI エントリーポイント。"""
    parser = argparse.ArgumentParser(
        description="DB + ログファイルから安定性レポートを生成する"
    )
    parser.add_argument("--story", required=True, help="ストーリーID")
    parser.add_argument(
        "--db", default="db/pocketrole.db", help="SQLite DB ファイルパス"
    )
    parser.add_argument(
        "--log", default="logs/engine.log", help="JSON ログファイルパス"
    )
    parser.add_argument(
        "--v2-closeout",
        action="store_true",
        help="chapter/director/scene-close を含む v2 closeout 判定も行う",
    )
    parser.add_argument(
        "--window-turns",
        type=int,
        default=20,
        help="v2 closeout 集計に使う turn 窓（デフォルト: 20）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    return asyncio.run(
        run_check(
            story_id=args.story,
            db_path=args.db,
            log_path=args.log,
            v2_closeout=args.v2_closeout,
            window_turns=args.window_turns,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
