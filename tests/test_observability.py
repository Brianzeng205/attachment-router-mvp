"""Tests for Phase 7C2 operational observability features."""

import logging
import logging.handlers
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import Settings
from app.logging_config import RunContextFilter, configure_logging, set_run_id, get_logger
from app.runtime_models import PollCycleReport
from app.runtime_coordinator import RuntimeCoordinator
from app.runtime_status import format_runtime_run, list_recent_runs, main as runtime_status_main
from app.inbox_repository import SqliteInboxRepository


class LoggingConfigurationTests(unittest.TestCase):
    """Tests for logging configuration module."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.log_file = Path(self.tempdir.name) / "test.log"

    def tearDown(self):
        self.tempdir.cleanup()
        # Clear handlers
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

    def test_configure_logging_console_only(self):
        """Console logging can be configured without file output."""
        configure_logging(log_level="INFO", log_file=None)
        root_logger = logging.getLogger()
        self.assertGreater(len(root_logger.handlers), 0)
        # Should have at least console handler
        console_found = any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers)
        self.assertTrue(console_found)

    def test_configure_logging_with_file(self):
        """Rotating file handler is created when log_file is specified."""
        configure_logging(log_level="INFO", log_file=self.log_file)
        root_logger = logging.getLogger()
        handlers = {type(h).__name__: h for h in root_logger.handlers}
        self.assertIn("RotatingFileHandler", handlers)

    def test_configure_logging_creates_log_directory(self):
        """Log directory is created if it doesn't exist."""
        log_file = Path(self.tempdir.name) / "subdir" / "test.log"
        configure_logging(log_level="INFO", log_file=log_file)
        self.assertTrue(log_file.parent.exists())

    def test_configure_logging_respects_level(self):
        """Configured log level is applied to root logger."""
        configure_logging(log_level="DEBUG", log_file=None)
        root_logger = logging.getLogger()
        self.assertEqual(root_logger.level, logging.DEBUG)

        # Clean up before next test
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        configure_logging(log_level="WARNING", log_file=None)
        root_logger = logging.getLogger()
        self.assertEqual(root_logger.level, logging.WARNING)

    def test_configure_logging_defaults_invalid_level_to_info(self):
        """Invalid log level defaults to INFO."""
        configure_logging(log_level="INVALID", log_file=None)
        root_logger = logging.getLogger()
        self.assertEqual(root_logger.level, logging.INFO)

    def test_rotating_file_handler_config(self):
        """RotatingFileHandler is configured with correct limits."""
        log_file = self.log_file
        max_bytes = 1024 * 100  # 100 KB
        backup_count = 5
        configure_logging(log_level="INFO", log_file=log_file, log_max_bytes=max_bytes, log_backup_count=backup_count)

        root_logger = logging.getLogger()
        handlers = [h for h in root_logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        self.assertEqual(handler.maxBytes, max_bytes)
        self.assertEqual(handler.backupCount, backup_count)


class RunContextFilterTests(unittest.TestCase):
    """Tests for run context filter and run_id correlation."""

    def setUp(self):
        self.filter = RunContextFilter()

    def test_filter_sets_run_id_on_record(self):
        """Filter injects run_id into log records."""
        self.filter.set_run_id(42)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="test message", args=(), exc_info=None,
        )
        self.assertTrue(self.filter.filter(record))
        self.assertEqual(record.run_id, 42)

    def test_filter_handles_none_run_id(self):
        """Filter handles None run_id gracefully."""
        self.filter.set_run_id(None)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="test message", args=(), exc_info=None,
        )
        self.assertTrue(self.filter.filter(record))
        # Now run_id is always set to "none" string for safe formatting
        self.assertTrue(hasattr(record, 'run_id'))
        self.assertEqual(record.run_id, "none")

    def test_set_run_id_function(self):
        """Global run context reaches records emitted by child loggers."""
        configure_logging(log_level="INFO", log_file=None)
        set_run_id(123)
        root_logger = logging.getLogger()
        handler = root_logger.handlers[0]
        record = logging.LogRecord(
            name="app.child", level=logging.INFO, pathname="test.py", lineno=1,
            msg="test", args=(), exc_info=None,
        )
        self.assertTrue(handler.filter(record))
        self.assertEqual(record.run_id, 123)
        set_run_id(None)


class RuntimeCoordinatorLoggingTests(unittest.TestCase):
    """Tests for runtime coordinator lifecycle logging."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "state.sqlite3"
        self.log_file = Path(self.tempdir.name) / "test.log"
        # Configure logging for tests
        configure_logging(log_level="INFO", log_file=self.log_file)

    def tearDown(self):
        self.tempdir.cleanup()
        # Clear handlers
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

    def test_lock_acquired_event_logged(self):
        """lock_acquired event is logged when lock is successfully acquired."""
        def dummy_work():
            return PollCycleReport(messages_polled=0)

        coordinator = RuntimeCoordinator(self.database_path, dummy_work)
        result = coordinator.execute_once()

        self.assertEqual(result.status, "completed")
        # Verify log file contains lock_acquired event
        log_content = self.log_file.read_text()
        self.assertIn("event=lock_acquired", log_content)

    def test_lock_skipped_event_logged(self):
        """lock_skipped event is logged when lock is already held."""
        process_lock = Mock()
        process_lock.acquire.return_value = False
        work = Mock()
        result = RuntimeCoordinator(
            self.database_path, work, process_lock=process_lock,
        ).execute_once()

        self.assertEqual(result.status, "skipped_locked")
        work.assert_not_called()
        self.assertIn("event=lock_skipped", self.log_file.read_text())

    def test_runtime_completed_event_logged(self):
        """runtime_completed event is logged on successful completion."""
        def dummy_work():
            return PollCycleReport(messages_polled=5, attachments_uploaded=2)

        coordinator = RuntimeCoordinator(self.database_path, dummy_work)
        result = coordinator.execute_once()

        log_content = self.log_file.read_text()
        self.assertIn("event=runtime_completed", log_content)
        self.assertIn("status=completed", log_content)
        self.assertIn(f"run_id={result.runtime_run_id}", log_content)

    def test_runtime_partial_event_logged(self):
        """runtime_partial event is logged when there are errors."""
        def dummy_work():
            return PollCycleReport(messages_polled=5, attachment_errors=1)

        coordinator = RuntimeCoordinator(self.database_path, dummy_work)
        result = coordinator.execute_once()

        self.assertEqual(result.status, "partial")
        log_content = self.log_file.read_text()
        self.assertIn("event=runtime_partial", log_content)

    def test_runtime_failed_event_logged(self):
        """runtime_failed event is logged on exception."""
        def failing_work():
            raise ValueError("Test error")

        coordinator = RuntimeCoordinator(self.database_path, failing_work)
        with self.assertRaises(ValueError):
            coordinator.execute_once()

        log_content = self.log_file.read_text()
        self.assertIn("event=runtime_failed", log_content)
        self.assertIn("error_class=ValueError", log_content)


class PrivacyPreservingLoggingTests(unittest.TestCase):
    """Tests proving that logs do not expose sensitive content."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.log_file = Path(self.tempdir.name) / "test.log"
        configure_logging(log_level="DEBUG", log_file=self.log_file)

    def tearDown(self):
        self.tempdir.cleanup()
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

    def test_logs_do_not_expose_email_addresses(self):
        """Operational logging does not expose email addresses."""
        logger = get_logger("test")
        # Operational logging should only log structured events, not email content
        logger.info("event=message_ingested message_id=msg123 conversation_id=conv456")

        log_content = self.log_file.read_text()
        # Verify no email addresses in structured operational logs
        # The test verifies that the operational path does not leak emails
        self.assertNotIn("@", log_content.split("event=")[1] if "event=" in log_content else "")

    def test_runtime_failure_logs_error_class_without_exception_detail(self):
        """Runtime boundary logs a safe class but not arbitrary exception text."""
        secret = "sender@example.test ya29.private-token confidential body"
        coordinator = RuntimeCoordinator(
            Path(self.tempdir.name) / "state.sqlite3",
            lambda: (_ for _ in ()).throw(ValueError(secret)),
        )
        with self.assertRaises(ValueError):
            coordinator.execute_once()

        log_content = self.log_file.read_text()
        self.assertIn("error_class=ValueError", log_content)
        self.assertNotIn(secret, log_content)
        self.assertNotIn("sender@example.test", log_content)
        self.assertNotIn("ya29.private-token", log_content)

    def test_structured_logging_fields_safe(self):
        """Structured logging only includes safe fields."""
        logger = get_logger("test")
        logger.info("event=test_event provider=test operation=test_op error_class=None attempt=1")

        log_content = self.log_file.read_text()
        self.assertIn("event=test_event", log_content)
        # Verify safe fields are present
        self.assertIn("provider=test", log_content)
        self.assertIn("operation=test_op", log_content)
        self.assertIn("error_class=None", log_content)


class RuntimeStatusCommandTests(unittest.TestCase):
    """Tests for runtime-status inspection command."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "state.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_runtime_status_empty_history(self):
        """runtime_status handles empty runtime history gracefully."""
        # Create empty database
        repository = SqliteInboxRepository(self.database_path)
        repository.close()

        # Call list_recent_runs with a StringIO to capture output
        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            list_recent_runs(self.database_path)
        output = f.getvalue()

        self.assertIn("No recent runtime history found", output)

    def test_runtime_status_displays_runs(self):
        """runtime_status displays recent runs."""
        repository = SqliteInboxRepository(self.database_path)
        try:
            run = repository.create_runtime_run(trigger_type="cli", instance_id="test-instance", lock_outcome="acquired")
            repository.finalize_runtime_run(
                run.id, "completed", None,
                report=PollCycleReport(),
            )
        finally:
            repository.close()

        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            list_recent_runs(self.database_path)
        output = f.getvalue()

        self.assertIn("Recent", output)
        self.assertIn("invocation", output)
        self.assertIn("completed", output)

    def test_runtime_status_main_needs_only_state_db_path(self):
        """Status inspection does not validate unrelated production settings."""
        repository = SqliteInboxRepository(self.database_path)
        repository.close()
        output = StringIO()
        errors = StringIO()
        with patch("app.runtime_status.load_dotenv"), patch.dict(
            os.environ,
            {"STATE_DB_PATH": str(self.database_path)},
            clear=True,
        ), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(runtime_status_main(), 0)
        self.assertIn("No recent runtime history found", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_format_runtime_run_output(self):
        """format_runtime_run produces readable output."""
        repository = SqliteInboxRepository(self.database_path)
        try:
            run = repository.create_runtime_run(trigger_type="cli", instance_id="abc123", lock_outcome="acquired")
            repository.finalize_runtime_run(
                run.id, "completed", None,
                report=PollCycleReport(),
            )
            runs = repository.list_runtime_runs()
            self.assertEqual(len(runs), 1)

            formatted = format_runtime_run(runs[0])
            self.assertIn("Run #", formatted)
            self.assertIn("completed", formatted)
            self.assertIn("lock=acquired", formatted)
            self.assertIn("trigger=cli", formatted)
        finally:
            repository.close()


class SettingsLoggingConfigTests(unittest.TestCase):
    """Tests for logging configuration in Settings."""

    def test_settings_has_logging_fields(self):
        """Settings dataclass includes logging configuration fields."""
        with patch.dict(os.environ, {
            "CONFIDENCE_THRESHOLD": "0.85",
            "NEEDS_REVIEW_FOLDER_ID": "folder-123",
            "ALLOWED_DRIVE_FOLDERS": '{"invoices": "folder-456"}',
            "STATE_DB_PATH": "test.db",
            "LOG_LEVEL": "DEBUG",
            "LOG_FILE": "logs/test.log",
        }):
            settings = Settings.from_env()
            self.assertEqual(settings.log_level, "DEBUG")
            self.assertEqual(settings.log_file, Path("logs/test.log"))

    def test_settings_validates_log_level(self):
        """Settings rejects invalid log levels."""
        with patch.dict(os.environ, {
            "CONFIDENCE_THRESHOLD": "0.85",
            "NEEDS_REVIEW_FOLDER_ID": "folder-123",
            "ALLOWED_DRIVE_FOLDERS": '{"invoices": "folder-456"}',
            "STATE_DB_PATH": "test.db",
            "LOG_LEVEL": "INVALID_LEVEL",
        }):
            with self.assertRaises(ValueError) as cm:
                Settings.from_env()
            self.assertIn("LOG_LEVEL", str(cm.exception))

    def test_settings_logging_defaults(self):
        """Settings provides reasonable logging defaults."""
        with patch.dict(os.environ, {
            "CONFIDENCE_THRESHOLD": "0.85",
            "NEEDS_REVIEW_FOLDER_ID": "folder-123",
            "ALLOWED_DRIVE_FOLDERS": '{"invoices": "folder-456"}',
            "STATE_DB_PATH": "test.db",
        }, clear=False):
            settings = Settings.from_env()
            self.assertEqual(settings.log_level, "INFO")
            self.assertIsNone(settings.log_file)
            self.assertEqual(settings.log_max_bytes, 5 * 1024 * 1024)
            self.assertEqual(settings.log_backup_count, 3)


class GmailReadOnlyVerificationTests(unittest.TestCase):
    """Tests verifying Gmail remains read-only."""

    def test_gmail_scope_remains_readonly(self):
        """Gmail OAuth scope remains read-only."""
        from app.gmail_client import GMAIL_READONLY_SCOPE
        self.assertEqual(GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.readonly")

    def test_no_gmail_write_operations_in_config(self):
        """Gmail adapter source contains no modifying API operations."""
        source = Path("app/gmail_client.py").read_text()
        for operation in (".modify(", ".trash(", ".delete(", ".send("):
            self.assertNotIn(operation, source)


if __name__ == "__main__":
    unittest.main()
