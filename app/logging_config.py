"""Centralized structured logging configuration for operational observability."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


class RunContextFilter(logging.Filter):
    """Inject run_id into log records for correlation."""

    def __init__(self) -> None:
        super().__init__()
        self._run_id: Optional[int] = None

    def set_run_id(self, run_id: Optional[int]) -> None:
        """Update the run_id for subsequent log records."""
        self._run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        if self._run_id is not None:
            record.run_id = self._run_id
        else:
            record.run_id = "none"
        return True


class SafeFormatter(logging.Formatter):
    """Format that safely handles missing run_id field."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, 'run_id'):
            record.run_id = "none"
        return super().format(record)


# Global context filter instance
_run_context_filter = RunContextFilter()


def configure_logging(
    log_level: str = "INFO",
    log_file: Optional[Path] = None,
    log_max_bytes: int = 5 * 1024 * 1024,
    log_backup_count: int = 3,
) -> None:
    """
    Configure root logger with console and optional file output.

    Args:
        log_level: Python logging level name (INFO, DEBUG, WARNING, etc.)
        log_file: Path to rotating log file, or None to disable file logging
        log_max_bytes: Maximum bytes per log file before rotation (~5 MB)
        log_backup_count: Number of backup files to retain (3)
    """
    root_logger = logging.getLogger()

    # Remove any existing handlers to avoid duplicates and release open files.
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()

    try:
        level = getattr(logging, log_level.upper())
    except AttributeError:
        level = logging.INFO

    log_format = "%(asctime)s %(levelname)s %(name)s run_id=%(run_id)s: %(message)s"

    # Console handler always enabled
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.addFilter(_run_context_filter)
    console_formatter = SafeFormatter(
        fmt=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # Rotating file handler if configured
    if log_file:
        log_file = Path(log_file)
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=log_max_bytes,
                backupCount=log_backup_count,
            )
            file_handler.setLevel(level)
            file_handler.addFilter(_run_context_filter)
            file_formatter = SafeFormatter(
                fmt=log_format,
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
        except (IOError, OSError) as exc:
            root_logger.warning(
                "event=log_file_unavailable error_class=%s", type(exc).__name__,
            )

    root_logger.setLevel(level)


def set_run_id(run_id: Optional[int]) -> None:
    """Set the current run_id for log correlation."""
    _run_context_filter.set_run_id(run_id)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name."""
    return logging.getLogger(name)
