"""Central SQLite connection settings for bounded local lock contention."""

from __future__ import annotations

import sqlite3
from pathlib import Path


DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 5_000


def connect_sqlite(database_path: Path, busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    if type(busy_timeout_ms) is not int or busy_timeout_ms < 0 or busy_timeout_ms > 60_000:
        raise ValueError("busy_timeout_ms must be an integer between 0 and 60000")
    connection = sqlite3.connect(database_path, timeout=busy_timeout_ms / 1000)
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    return connection
