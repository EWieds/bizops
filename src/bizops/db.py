"""SQLite access (WAL mode) and the migration runner.

One short-lived connection per unit of work keeps this safe across the asyncio loop,
uvicorn's threadpool and tests. SQL stays portable so Postgres is a config change later.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from .util import iso


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. Use for anything that must be atomic (checks + writes)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        with self.tx() as conn:
            conn.execute(sql, params)

    def migrate(self) -> list[str]:
        """Apply any migrations in bizops/migrations not yet recorded. Returns applied versions."""
        conn = self._connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                         "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        finally:
            conn.close()
        applied = []
        files = sorted(
            (f for f in resources.files("bizops.migrations").iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        for f in files:
            version = f.name.removesuffix(".sql")
            if version in done:
                continue
            with self.tx() as conn:
                for stmt in split_sql(f.read_text()):
                    conn.execute(stmt)
                conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, iso()))
            applied.append(version)
        return applied


def split_sql(script: str) -> list[str]:
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
