import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.database import connect_sqlite
from app.inbox_repository import SqliteInboxRepository


class SqliteBusyHandlingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "state.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_configured_busy_timeout_is_applied(self):
        connection = connect_sqlite(self.database_path, 1234)
        try:
            timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(timeout, 1234)

    def test_locked_write_wait_is_bounded_by_configured_timeout(self):
        owner = connect_sqlite(self.database_path, 20)
        contender = connect_sqlite(self.database_path, 20)
        try:
            owner.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
            owner.commit()
            owner.execute("BEGIN EXCLUSIVE")
            owner.execute("INSERT INTO sample DEFAULT VALUES")
            started = time.monotonic()
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked|busy"):
                contender.execute("INSERT INTO sample DEFAULT VALUES")
            self.assertLess(time.monotonic() - started, 1)
        finally:
            owner.rollback()
            owner.close()
            contender.close()

    def test_integrity_and_invalid_sql_errors_are_not_converted_to_busy_retries(self):
        connection = connect_sqlite(self.database_path, 10)
        try:
            connection.execute("CREATE TABLE unique_values (value TEXT UNIQUE)")
            connection.execute("INSERT INTO unique_values VALUES ('x')")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO unique_values VALUES ('x')")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("THIS IS NOT SQL")
        finally:
            connection.close()

    def test_repository_uses_requested_timeout_and_schema_remains_idempotent(self):
        first = SqliteInboxRepository(self.database_path, 77)
        try:
            self.assertEqual(first.connection.execute("PRAGMA busy_timeout").fetchone()[0], 77)
        finally:
            first.close()
        second = SqliteInboxRepository(self.database_path, 77)
        second.close()


if __name__ == "__main__":
    unittest.main()
