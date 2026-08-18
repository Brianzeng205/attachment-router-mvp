"""Durable authorization records for future approved-action executors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


ACTION_SEND_APPROVED_REPLY = "send_approved_reply"
EXECUTION_ACTION_TYPES = frozenset({ACTION_SEND_APPROVED_REPLY})
EXECUTION_STATUSES = frozenset({"pending", "processing", "retry_wait", "completed", "failed"})
EXECUTION_SCHEMA_VERSION = 1
MAX_EXECUTION_PAYLOAD_CHARS = 50_000


@dataclass(frozen=True)
class ExecutionIntent:
    execution_id: str
    source_review_item_id: int
    conversation_id: int
    provider_thread_id: str
    in_reply_to_provider_message_id: str
    action_type: str
    approved_body: str
    idempotency_key: str
    status: str
    attempt_count: int
    created_at: str
    updated_at: str
    next_attempt_at: str | None = None
    claim_token: str | None = None
    claimed_by: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    completed_at: str | None = None
    failure_code: str | None = None
    failure_metadata: Mapping[str, object] | None = None
    schema_version: int = EXECUTION_SCHEMA_VERSION


@dataclass(frozen=True)
class EnqueueResult:
    intent: ExecutionIntent
    created: bool


class ActionExecutor(Protocol):
    """Future adapter boundary. Phase 8B deliberately provides no implementation."""

    def execute(self, intent: ExecutionIntent) -> Mapping[str, object] | None:
        ...


class ExecutionQueueError(ValueError):
    pass


class ExecutionEligibilityError(ExecutionQueueError):
    pass


class ExecutionNotFoundError(ExecutionQueueError):
    pass


class ExecutionClaimConflictError(ExecutionQueueError):
    pass


class ExecutionTransitionError(ExecutionQueueError):
    pass
