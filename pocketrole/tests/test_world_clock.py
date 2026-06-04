"""tests/test_world_clock.py — WorldClock のユニットテスト（同期）。"""

import pytest
from engine.world_clock import WorldClock

SEASON_START = "2025-04-01"  # 火曜日
TURN_MINUTES = 30

TEST_EVENTS = [
    {"event_date": "04-01", "name": "入学式", "duration_days": 1, "force_place": "classroom"},
    {"event_date": "05-03", "name": "GW", "duration_days": 4, "force_place": None},
    {"event_date": "02-14", "name": "バレンタイン", "duration_days": 1},
]


# ── 初期化 ──────────────────────────────────────────────────────────


def test_init_default_datetime() -> None:
    """initial_datetime=None → season_start + T00:00 で開始。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES)
    assert clock.sim_datetime == "2025-04-01T00:00"


def test_init_custom_datetime() -> None:
    """initial_datetime を指定するとその値が使われる。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES, initial_datetime="2025-04-02T08:30")
    assert clock.sim_datetime == "2025-04-02T08:30"


# ── sim_datetime フォーマット ────────────────────────────────────────


def test_sim_datetime_format() -> None:
    """sim_datetime は 'YYYY-MM-DDTHH:MM' 形式（秒なし）。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES)
    dt = clock.sim_datetime
    # 長さと区切り文字で確認
    assert len(dt) == 16
    assert dt[4] == "-"
    assert dt[7] == "-"
    assert dt[10] == "T"
    assert dt[13] == ":"


# ── tick() ──────────────────────────────────────────────────────────


def test_tick_advances_time() -> None:
    """tick() で turn_minutes 分だけ時間が進む。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES)
    clock.tick()
    assert clock.sim_datetime == "2025-04-01T00:30"


def test_tick_increments_turn_number() -> None:
    """tick() で turn_number が +1 される。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES)
    assert clock.turn_number == 0
    clock.tick()
    assert clock.turn_number == 1


def test_tick_multiple() -> None:
    """3回 tick() → 90分進む。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES)
    clock.tick()
    clock.tick()
    clock.tick()
    assert clock.sim_datetime == "2025-04-01T01:30"
    assert clock.turn_number == 3


def test_date_rollover() -> None:
    """23:30 から tick() すると翌日 00:00 になる。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES, initial_datetime="2025-04-01T23:30")
    clock.tick()
    assert clock.sim_datetime == "2025-04-02T00:00"


# ── weekday_type ────────────────────────────────────────────────────


def test_weekday_tuesday() -> None:
    """2025-04-01（火曜日）は 'weekday'。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES)
    assert clock.weekday_type == "weekday"


def test_weekday_saturday() -> None:
    """2025-04-05（土曜日）は 'weekend'。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES, initial_datetime="2025-04-05T00:00")
    assert clock.weekday_type == "weekend"


def test_weekday_sunday() -> None:
    """2025-04-06（日曜日）は 'weekend'。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES, initial_datetime="2025-04-06T00:00")
    assert clock.weekday_type == "weekend"


# ── is_event_day() / get_current_events() ───────────────────────────


def test_is_event_day_match() -> None:
    """04-01（入学式の日）は is_event_day() == True。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES, event_calendar=TEST_EVENTS)
    assert clock.is_event_day() is True


def test_is_event_day_no_match() -> None:
    """04-02 はイベント日ではない。"""
    clock = WorldClock(
        SEASON_START, TURN_MINUTES,
        event_calendar=TEST_EVENTS,
        initial_datetime="2025-04-02T00:00",
    )
    assert clock.is_event_day() is False


def test_event_duration_first_day() -> None:
    """GW: 05-03（初日）はイベント日。"""
    clock = WorldClock(
        SEASON_START, TURN_MINUTES,
        event_calendar=TEST_EVENTS,
        initial_datetime="2025-05-03T00:00",
    )
    assert clock.is_event_day() is True


def test_event_duration_last_day() -> None:
    """GW: 05-06（4日目・最終日）もイベント日。"""
    clock = WorldClock(
        SEASON_START, TURN_MINUTES,
        event_calendar=TEST_EVENTS,
        initial_datetime="2025-05-06T00:00",
    )
    assert clock.is_event_day() is True


def test_event_duration_after() -> None:
    """GW: 05-07（5日目）はイベント日ではない。"""
    clock = WorldClock(
        SEASON_START, TURN_MINUTES,
        event_calendar=TEST_EVENTS,
        initial_datetime="2025-05-07T00:00",
    )
    assert clock.is_event_day() is False


def test_get_current_events_found() -> None:
    """04-01 → get_current_events() に入学式が含まれる。"""
    clock = WorldClock(SEASON_START, TURN_MINUTES, event_calendar=TEST_EVENTS)
    events = clock.get_current_events()
    assert len(events) == 1
    assert events[0]["name"] == "入学式"


def test_get_current_events_empty() -> None:
    """04-02 → get_current_events() は空リスト。"""
    clock = WorldClock(
        SEASON_START, TURN_MINUTES,
        event_calendar=TEST_EVENTS,
        initial_datetime="2025-04-02T00:00",
    )
    assert clock.get_current_events() == []


# ── turn_number（再開）──────────────────────────────────────────────


def test_turn_number_from_resume() -> None:
    """initial_datetime="2025-04-01T01:00" → turn_number=2 (60÷30)。"""
    clock = WorldClock(
        SEASON_START, TURN_MINUTES,
        initial_datetime="2025-04-01T01:00",
    )
    assert clock.turn_number == 2
