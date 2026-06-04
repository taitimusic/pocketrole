"""tests/test_web_poster.py — WebPoster の単体テスト（14件）。"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.config import WebPosterConfig
from engine.web_poster import WebPoster


# ============================================================
# ヘルパー
# ============================================================


def make_config(**kwargs: object) -> WebPosterConfig:
    defaults = dict(
        receiver_url="http://example.com/receiver.php",
        batch_size=2,
        retry_interval_sec=0,  # テストで wait なし
        max_consecutive_failures=3,
        pause_duration_sec=10,
        auth_token="test_token",
    )
    defaults.update(kwargs)
    return WebPosterConfig(**defaults)  # type: ignore[arg-type]


def make_mock_response(status: int = 200) -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.request_info = MagicMock()
    resp.history = []
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def make_mock_session(mock_resp: AsyncMock) -> MagicMock:
    session = MagicMock()
    session.post = MagicMock(return_value=mock_resp)
    session.close = AsyncMock()
    return session


def make_mock_db(logs: list[dict] | None = None) -> MagicMock:
    db = MagicMock()
    db.get_unposted_logs = AsyncMock(return_value=logs or [])
    db.mark_logs_posted = AsyncMock()
    return db


def make_poster(
    *,
    logs: list[dict] | None = None,
    config: WebPosterConfig | None = None,
) -> tuple[WebPoster, MagicMock]:
    db = make_mock_db(logs)
    poster = WebPoster("story1", config or make_config(), db)
    return poster, db


SAMPLE_LOGS = [
    {
        "id": 1,
        "sim_datetime": "2024-01-01 08:00",
        "turn_number": 1,
        "char_id": "char_a",
        "msg_type": "monologue",
        "target_char_id": None,
        "place_id": "classroom",
        "expression": "neutral",
        "message": "今日も学校だ。",
        "emotion_snapshot": '{"stress": 0.3}',
    },
    {
        "id": 2,
        "sim_datetime": "2024-01-01 08:05",
        "turn_number": 1,
        "char_id": "char_b",
        "msg_type": "talk",
        "target_char_id": "char_a",
        "place_id": "classroom",
        "expression": "happy",
        "message": "おはよう！",
        "emotion_snapshot": '{"stress": 0.1}',
    },
]


# ============================================================
# テスト
# ============================================================


@pytest.mark.asyncio
async def test_start_creates_session() -> None:
    """test_1: start() 後 _session が None でない。"""
    poster, _ = make_poster()
    with patch("engine.web_poster.aiohttp.ClientSession") as mock_cls:
        mock_cls.return_value = MagicMock()
        await poster.start()
    assert poster._session is not None


@pytest.mark.asyncio
async def test_stop_closes_session() -> None:
    """test_2: stop() 後 session.close() が呼ばれる。"""
    poster, _ = make_poster()
    mock_session = MagicMock()
    mock_session.close = AsyncMock()
    poster._session = mock_session

    await poster.stop()

    mock_session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_sets_running_false() -> None:
    """test_3: stop() 後 _running == False。"""
    poster, _ = make_poster()
    poster._running = True
    poster._session = MagicMock()
    poster._session.close = AsyncMock()

    await poster.stop()

    assert poster._running is False


@pytest.mark.asyncio
async def test_send_batch_no_logs_returns_false() -> None:
    """test_4: ログ 0 件 → False を返し、POST しない。"""
    poster, db = make_poster(logs=[])
    mock_resp = make_mock_response(200)
    poster._session = make_mock_session(mock_resp)

    result = await poster._send_batch()

    assert result is False
    poster._session.post.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_send_batch_success_returns_true() -> None:
    """test_5: 200 レスポンス → True を返す。"""
    poster, db = make_poster(logs=SAMPLE_LOGS)
    mock_resp = make_mock_response(200)
    poster._session = make_mock_session(mock_resp)

    result = await poster._send_batch()

    assert result is True


@pytest.mark.asyncio
async def test_send_batch_success_marks_posted() -> None:
    """test_6: 成功後 mark_logs_posted が正しい引数で呼ばれる。"""
    poster, db = make_poster(logs=SAMPLE_LOGS)
    mock_resp = make_mock_response(200)
    poster._session = make_mock_session(mock_resp)

    await poster._send_batch()

    db.mark_logs_posted.assert_awaited_once_with("story1", [1, 2])


@pytest.mark.asyncio
async def test_send_batch_http_error_raises() -> None:
    """test_7: 500 レスポンス → ClientResponseError が送出される。"""
    import aiohttp

    poster, db = make_poster(logs=SAMPLE_LOGS)
    mock_resp = make_mock_response(500)
    poster._session = make_mock_session(mock_resp)

    with pytest.raises(aiohttp.ClientResponseError):
        await poster._send_batch()


def test_build_payload_structure() -> None:
    """test_8: auth_token / story_id / logs キーを持つ。"""
    poster, _ = make_poster()
    payload = poster._build_payload(SAMPLE_LOGS)

    assert "auth_token" in payload
    assert "story_id" in payload
    assert "logs" in payload
    assert isinstance(payload["logs"], list)
    assert len(payload["logs"]) == 2


def test_build_payload_auth_token() -> None:
    """test_9: auth_token がコンフィグの値と一致する。"""
    poster, _ = make_poster(config=make_config(auth_token="secret123"))
    payload = poster._build_payload(SAMPLE_LOGS)

    assert payload["auth_token"] == "secret123"


def test_build_payload_parses_emotion_snapshot() -> None:
    """test_10: emotion_snapshot の JSON 文字列を dict に変換している。"""
    poster, _ = make_poster()
    payload = poster._build_payload(SAMPLE_LOGS)

    first = payload["logs"][0]
    assert isinstance(first["emotion_snapshot"], dict)
    assert first["emotion_snapshot"] == {"stress": 0.3}


def test_is_paused_false_initially() -> None:
    """test_11: 初期状態 → _is_paused() == False。"""
    poster, _ = make_poster()
    assert poster._is_paused() is False


def test_pause_sets_is_paused_true() -> None:
    """test_12: _pause() 後 → _is_paused() == True。"""
    poster, _ = make_poster(config=make_config(pause_duration_sec=60))
    poster._pause()
    assert poster._is_paused() is True


def test_pause_auto_resumes() -> None:
    """test_13: _paused_until を過去に設定 → _is_paused() == False（自動復帰）。"""
    poster, _ = make_poster()
    poster._paused_until = time.monotonic() - 1.0  # 過去の時刻
    assert poster._is_paused() is False
    assert poster._paused_until is None


@pytest.mark.asyncio
async def test_run_resets_failure_count_on_success() -> None:
    """test_14: _consecutive_failures=2 の状態で成功 → 0 になる。"""
    poster, db = make_poster()
    poster._consecutive_failures = 2

    # AsyncMock は制御をイベントループに返さないため、sleep(0) を含む実装に差し替え
    async def mock_send_batch() -> bool:
        await asyncio.sleep(0)  # イベントループへの制御返却
        return True

    poster._send_batch = mock_send_batch  # type: ignore[method-assign]

    task = asyncio.create_task(poster.run())
    # イベントループを数回回して run() を進める
    for _ in range(5):
        await asyncio.sleep(0)
    await poster.stop()
    await task

    assert poster._consecutive_failures == 0
