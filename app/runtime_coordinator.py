"""Outer single-worker coordination for one existing business polling cycle."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .inbox_repository import SqliteInboxRepository
from .process_lock import ProcessLock, poll_lock_path
from .runtime_models import PollCycleReport, RuntimeExecutionResult
from .database import DEFAULT_SQLITE_BUSY_TIMEOUT_MS
from .logging_config import set_run_id


logger = logging.getLogger(__name__)
_ERROR_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


class RuntimeCoordinator:
    """Coordinates lifecycle only; business work remains in app.main.run_once."""

    def __init__(
        self,
        state_database_path: Path,
        business_run_once: Callable[[], Any],
        *,
        trigger_type: str = "cli",
        instance_id: str | None = None,
        process_lock: ProcessLock | None = None,
        sqlite_busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    ) -> None:
        self._database_path = state_database_path
        self._business_run_once = business_run_once
        self._trigger_type = trigger_type
        self._instance_id = instance_id or uuid.uuid4().hex
        self._lock = process_lock or ProcessLock(poll_lock_path(state_database_path))
        self._sqlite_busy_timeout_ms = sqlite_busy_timeout_ms

    def execute_once(self) -> RuntimeExecutionResult:
        if not self._lock.acquire():
            logger.info("event=lock_skipped lock_outcome=already_held")
            return RuntimeExecutionResult("skipped_locked")
        run_id: int | None = None
        try:
            repository = SqliteInboxRepository(self._database_path, self._sqlite_busy_timeout_ms)
            try:
                repository.mark_running_runtime_runs_abandoned()
                run = repository.create_runtime_run(
                    trigger_type=self._trigger_type, instance_id=self._instance_id, lock_outcome="acquired",
                )
                run_id = run.id
                set_run_id(run_id)
                logger.info("event=lock_acquired instance_id=%s trigger_type=%s", self._instance_id[:8], self._trigger_type)
            finally:
                repository.close()

            try:
                logger.info("event=poll_started")
                result = self._business_run_once()
                logger.info("event=poll_completed")
            except KeyboardInterrupt as exc:
                self._finalize(run_id, "interrupted", exc)
                raise
            except Exception as exc:
                self._finalize(run_id, "failed", exc)
                raise
            status = "partial" if isinstance(result, PollCycleReport) and result.has_partial_failures else "completed"
            self._finalize(run_id, status, report=result if isinstance(result, PollCycleReport) else None)
            return RuntimeExecutionResult(status, run_id, result)
        finally:
            self._lock.release()
            set_run_id(None)

    def _finalize(
        self, run_id: int, status: str, error: BaseException | None = None,
        report: PollCycleReport | None = None,
    ) -> None:
        error_class = _normalized_error_class(error)
        repository = SqliteInboxRepository(self._database_path, self._sqlite_busy_timeout_ms)
        try:
            repository.finalize_runtime_run(run_id, status, error_class, report)
            if status == "completed":
                logger.info("event=runtime_completed status=%s", status)
            elif status == "partial":
                logger.info("event=runtime_partial status=%s", status)
            elif status == "failed":
                logger.warning("event=runtime_failed status=%s error_class=%s", status, error_class or "unknown")
            elif status == "interrupted":
                logger.warning("event=runtime_interrupted status=%s", status)
        finally:
            repository.close()


def _normalized_error_class(error: BaseException | None) -> str | None:
    if error is None:
        return None
    name = type(error).__name__
    return name if _ERROR_CLASS.fullmatch(name) else "RuntimeError"
