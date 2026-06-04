"""pytest-asyncio を介さず asyncio.Runner で async テストを実行する helper。"""

from __future__ import annotations

import asyncio
import functools
from queue import Queue
from threading import Thread
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
_DEFAULT_THREAD_JOIN_TIMEOUT_SEC = 30.0


def run_async(awaitable: Awaitable[T], timeout_sec: float = _DEFAULT_THREAD_JOIN_TIMEOUT_SEC) -> T:
    """専用 thread の asyncio.run() で awaitable を完走させる。"""
    queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def _target() -> None:
        try:
            queue.put((True, asyncio.run(awaitable)))
        except BaseException as exc:
            queue.put((False, exc))

    thread = Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)
    if thread.is_alive():
        raise TimeoutError(
            f"Async test thread did not finish within {timeout_sec:.1f} seconds."
        )

    ok, value = queue.get()
    if ok:
        return value  # type: ignore[return-value]
    raise value  # type: ignore[misc]


def async_to_sync(func: Callable[P, Awaitable[T]]) -> Callable[P, T]:
    """async test を同期 pytest test として実行するデコレータ。"""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return run_async(func(*args, **kwargs))

    return wrapper
