"""db/backends.py — SQLite 接続バックエンド抽象化。"""

from __future__ import annotations

from collections.abc import Awaitable
import sqlite3
from typing import Any, Protocol


class SupportsAsyncCursor(Protocol):
    async def fetchone(self) -> Any: ...
    async def fetchall(self) -> list[Any]: ...
    async def close(self) -> None: ...


class SupportsAsyncConnection(Protocol):
    row_factory: Any

    def execute(self, sql: str, parameters: Any = None) -> Awaitable[SupportsAsyncCursor]: ...
    def executemany(self, sql: str, parameters: Any) -> Awaitable[SupportsAsyncCursor]: ...
    def executescript(self, sql_script: str) -> Awaitable[SupportsAsyncCursor]: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
    async def close(self) -> None: ...


class SyncSqliteCursor:
    def __init__(self, connection: "SyncSqliteConnection", cursor: sqlite3.Cursor) -> None:
        self._connection = connection
        self._cursor = cursor

    async def fetchone(self) -> Any:
        return self._connection._call(self._cursor.fetchone)

    async def fetchall(self) -> list[Any]:
        rows = self._connection._call(self._cursor.fetchall)
        return list(rows)

    async def close(self) -> None:
        self._connection._call(self._cursor.close)

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class _ExecuteContext:
    def __init__(self, awaitable: Awaitable[SyncSqliteCursor]) -> None:
        self._awaitable = awaitable
        self._cursor: SyncSqliteCursor | None = None

    def __await__(self):  # type: ignore[no-untyped-def]
        return self._awaitable.__await__()

    async def __aenter__(self) -> SyncSqliteCursor:
        self._cursor = await self._awaitable
        return self._cursor

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._cursor is not None:
            await self._cursor.close()


class SyncSqliteConnection:
    """sqlite3 を同期で呼ぶ async 互換バックエンド。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    @property
    def row_factory(self) -> Any:
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._conn.row_factory = value

    def _call(self, fn, *args: Any) -> Any:
        return fn(*args)

    async def _execute(self, sql: str, parameters: Any = None) -> SyncSqliteCursor:
        if parameters is None:
            parameters = []
        cursor = self._call(self._conn.execute, sql, parameters)
        return SyncSqliteCursor(self, cursor)

    async def _executemany(self, sql: str, parameters: Any) -> SyncSqliteCursor:
        cursor = self._call(self._conn.executemany, sql, parameters)
        return SyncSqliteCursor(self, cursor)

    async def _executescript(self, sql_script: str) -> SyncSqliteCursor:
        cursor = self._call(self._conn.executescript, sql_script)
        return SyncSqliteCursor(self, cursor)

    def execute(self, sql: str, parameters: Any = None) -> _ExecuteContext:
        return _ExecuteContext(self._execute(sql, parameters))

    def executemany(self, sql: str, parameters: Any) -> _ExecuteContext:
        return _ExecuteContext(self._executemany(sql, parameters))

    def executescript(self, sql_script: str) -> _ExecuteContext:
        return _ExecuteContext(self._executescript(sql_script))

    async def commit(self) -> None:
        self._call(self._conn.commit)

    async def rollback(self) -> None:
        self._call(self._conn.rollback)

    async def close(self) -> None:
        self._call(self._conn.close)
