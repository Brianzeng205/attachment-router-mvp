"""Safe lifecycle models for coordinated polling invocations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


RUNTIME_RUN_STATUSES = frozenset({"running", "completed", "partial", "failed", "interrupted", "abandoned"})
RUNTIME_EXECUTION_STATUSES = frozenset({"completed", "partial", "skipped_locked"})


@dataclass(frozen=True)
class PollCycleReport:
    """Content-free operational counts for one normally returned polling cycle."""

    messages_polled: int = 0
    inbox_errors: int = 0
    attachments_uploaded: int = 0
    attachments_skipped: int = 0
    attachment_errors: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "messages_polled", "inbox_errors", "attachments_uploaded",
            "attachments_skipped", "attachment_errors",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")

    @property
    def has_partial_failures(self) -> bool:
        return self.inbox_errors > 0 or self.attachment_errors > 0


@dataclass(frozen=True)
class RuntimeRun:
    id: int
    trigger_type: str
    instance_id: str
    status: str
    started_at: str
    completed_at: str | None
    error_class: str | None
    lock_outcome: str
    messages_polled: int | None = None
    inbox_errors: int | None = None
    attachments_uploaded: int | None = None
    attachments_skipped: int | None = None
    attachment_errors: int | None = None


@dataclass(frozen=True)
class RuntimeExecutionResult:
    status: str
    runtime_run_id: int | None = None
    business_result: Any = None

    def __post_init__(self) -> None:
        if self.status not in RUNTIME_EXECUTION_STATUSES:
            raise ValueError("Invalid runtime execution status")
