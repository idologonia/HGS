"""SQLite access from asyncio.

All queries run in ONE dedicated thread, so there is a single writer and the
event loop never blocks on disk I/O. WAL mode, foreign keys on, versioned migrations.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrations() -> list[tuple[int, str]]:
    folder = resources.files("hgs.core.db") / "migrations"
    items = []
    for entry in folder.iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:4].isdigit():
            items.append((int(name[:4]), entry.read_text(encoding="utf-8")))
    return sorted(items)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hgs-db")
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle -----------------------------------------------------------------
    async def open(self) -> None:
        await self._run(self._open_sync)

    def _open_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        self._migrate_sync()

    def _migrate_sync(self) -> None:
        assert self._conn is not None
        c = self._conn
        c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        pending = [(v, sql) for v, sql in _migrations() if v > current]
        if not pending:
            return
        if current > 0 and self.path.exists():
            # back up the database before changing its structure
            shutil.copy2(self.path, self.path.with_suffix(f".v{current}.bak"))
        for version, sql in pending:
            c.execute("BEGIN")
            try:
                for stmt in _split_sql(sql):
                    c.execute(stmt)
                c.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    async def close(self) -> None:
        def _close() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        await self._run(_close)
        self._executor.shutdown(wait=True)

    # -- queries -------------------------------------------------------------------
    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        def _x() -> int:
            assert self._conn is not None
            cur = self._conn.execute(sql, tuple(params))
            return cur.rowcount

        return await self._run(_x)

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        def _x() -> dict[str, Any] | None:
            assert self._conn is not None
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

        return await self._run(_x)

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        def _x() -> list[dict[str, Any]]:
            assert self._conn is not None
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

        return await self._run(_x)

    async def transaction(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run fn(conn) atomically in the DB thread."""

        def _x() -> T:
            assert self._conn is not None
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                result = fn(c)
                c.execute("COMMIT")
                return result
            except BaseException:
                c.execute("ROLLBACK")
                raise

        return await self._run(_x)


def _split_sql(sql: str) -> list[str]:
    """Split a migration file into statements (migrations contain no ';' inside strings)."""
    return [s.strip() for s in sql.split(";") if s.strip()]
