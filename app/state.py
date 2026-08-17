from __future__ import annotations

from pathlib import Path

from .database import DEFAULT_SQLITE_BUSY_TIMEOUT_MS, connect_sqlite
from .migrations import initialize_schema


class SqliteStateManager:
    def __init__(self, database_path: Path, busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = connect_sqlite(database_path, busy_timeout_ms)
        initialize_schema(self.connection)

    def is_processed(self, email_id: str, attachment_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM processed_attachments WHERE email_id = ? AND attachment_id = ?",
            (email_id, attachment_id),
        ).fetchone()
        return row is not None

    def mark_processed(self, email_id: str, attachment_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO processed_attachments (email_id, attachment_id) VALUES (?, ?)",
            (email_id, attachment_id),
        )
        self.connection.commit()
