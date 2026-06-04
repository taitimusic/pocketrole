"""tests/_async_harness.py の回帰テスト。"""

from __future__ import annotations

import asyncio

import pytest

from tests._async_harness import run_async


def test_run_async_returns_result() -> None:
    """完了する awaitable は結果を返す。"""
    assert run_async(asyncio.sleep(0, result=7), timeout_sec=1.0) == 7


def test_run_async_raises_timeout_for_long_running_awaitable() -> None:
    """長時間 awaitable はタイムアウトで失敗する。"""
    with pytest.raises(TimeoutError):
        run_async(asyncio.sleep(0.2), timeout_sec=0.01)
