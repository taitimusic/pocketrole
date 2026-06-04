from __future__ import annotations

import logging
from datetime import datetime, timedelta
from datetime import date as DateType
from typing import Any

logger = logging.getLogger(__name__)


class WorldClock:
    """シミュレーション世界時計。DB非依存の純粋クラス。"""

    def __init__(
        self,
        season_start: str,
        turn_minutes: int,
        event_calendar: list[dict[str, Any]] | None = None,
        initial_datetime: str | None = None,
    ) -> None:
        """
        Args:
            season_start: シーズン開始日 "YYYY-MM-DD"
            turn_minutes: 1ターンの分数（例: 30）
            event_calendar: DBから事前ロードしたイベント一覧
            initial_datetime: 再開時の現在時刻 "YYYY-MM-DDTHH:MM"（None なら season_start T00:00）
        """
        self._turn_minutes = turn_minutes
        self._event_calendar: list[dict[str, Any]] = event_calendar or []

        season_start_dt = datetime.fromisoformat(f"{season_start}T00:00")

        if initial_datetime is None:
            self._current_dt = season_start_dt
            self._turn_number = 0
        else:
            self._current_dt = datetime.fromisoformat(initial_datetime)
            elapsed_minutes = int(
                (self._current_dt - season_start_dt).total_seconds() // 60
            )
            self._turn_number = elapsed_minutes // turn_minutes

        logger.debug(
            "WorldClock initialized",
            extra={
                "sim_datetime": self.sim_datetime,
                "turn_number": self._turn_number,
                "turn_minutes": self._turn_minutes,
            },
        )

    # ── プロパティ ──────────────────────────────────────────────────

    @property
    def sim_datetime(self) -> str:
        """現在のシミュレーション時刻 "YYYY-MM-DDTHH:MM"。"""
        return self._current_dt.strftime("%Y-%m-%dT%H:%M")

    @property
    def turn_number(self) -> int:
        """シーズン開始からの累積ターン数。"""
        return self._turn_number

    @property
    def turn_minutes(self) -> int:
        """1ターンの分数。"""
        return self._turn_minutes

    @property
    def weekday_type(self) -> str:
        """曜日種別。土(5)・日(6) → "weekend"、それ以外 → "weekday"。"""
        return "weekend" if self._current_dt.weekday() in (5, 6) else "weekday"

    @property
    def current_date_str(self) -> str:
        """現在の日付文字列 "YYYY-MM-DD"。"""
        return self._current_dt.strftime("%Y-%m-%d")

    # ── メソッド ────────────────────────────────────────────────────

    def tick(self) -> None:
        """turn_minutes 分だけ時間を進める（同期）。"""
        self._current_dt += timedelta(minutes=self._turn_minutes)
        self._turn_number += 1
        logger.debug(
            "WorldClock ticked",
            extra={
                "sim_datetime": self.sim_datetime,
                "turn_number": self._turn_number,
            },
        )

    def get_current_events(self) -> list[dict[str, Any]]:
        """現在日に有効なイベント一覧を返す。"""
        current_date = self._current_dt.date()
        return [
            event
            for event in self._event_calendar
            if self._is_within_event(current_date, event)
        ]

    def is_event_day(self) -> bool:
        """現在日がいずれかのイベント期間内なら True。"""
        return len(self.get_current_events()) > 0

    # ── 内部ヘルパー ────────────────────────────────────────────────

    def _is_within_event(self, current_date: DateType, event: dict[str, Any]) -> bool:
        """指定日がイベント期間内かどうか判定する（年またぎ対応）。

        event_date は "MM-DD" 形式。前年・当年・翌年の3パターンを試す。
        """
        month, day = map(int, event["event_date"].split("-"))
        duration = event.get("duration_days", 1)
        for year in (current_date.year - 1, current_date.year, current_date.year + 1):
            try:
                event_start = DateType(year, month, day)
            except ValueError:
                continue
            event_end = event_start + timedelta(days=duration - 1)
            if event_start <= current_date <= event_end:
                return True
        return False
