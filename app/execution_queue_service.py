"""Policy and orchestration for the durable approved-action handoff queue."""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone

from .execution_models import (
    EXECUTION_ACTION_TYPES, EXECUTION_STATUSES, EnqueueResult, ExecutionClaimConflictError,
    ExecutionEligibilityError, ExecutionIntent, ExecutionNotFoundError, ExecutionTransitionError,
)


logger = logging.getLogger(__name__)
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SAFE_METADATA_KEYS = frozenset({"provider_status", "provider_state", "operation", "reason_code", "retry_source"})


class ExecutionQueueService:
    """Owns queue eligibility, leases, bounded retries, and safe transitions."""

    def __init__(self, repository, *, clock: Callable[[], datetime] | None = None,
                 lease_seconds: int = 300, max_attempts: int = 3,
                 initial_retry_seconds: float = 30, max_retry_seconds: float = 900,
                 retry_multiplier: float = 2) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if type(lease_seconds) is not int or lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        if type(max_attempts) is not int or max_attempts < 1 or max_attempts > 100:
            raise ValueError("max_attempts must be between 1 and 100")
        if not all(math.isfinite(value) and value >= 0 for value in
                   (initial_retry_seconds, max_retry_seconds, retry_multiplier)):
            raise ValueError("retry settings must be finite and non-negative")
        if initial_retry_seconds > max_retry_seconds or retry_multiplier < 1:
            raise ValueError("invalid retry backoff settings")
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.initial_retry_seconds = initial_retry_seconds
        self.max_retry_seconds = max_retry_seconds
        self.retry_multiplier = retry_multiplier

    def enqueue_approved_review(self, review_item_id: int) -> EnqueueResult:
        self._positive_id(review_item_id, "review_item_id")
        try:
            intent, created = self._repository.enqueue_execution_for_review(review_item_id)
        except ValueError as exc:
            raise ExecutionEligibilityError(str(exc)) from exc
        logger.info("event=execution_intent_%s execution_id=%s review_item_id=%s action_type=%s status=%s",
                    "created" if created else "existing", intent.execution_id, review_item_id,
                    intent.action_type, intent.status)
        return EnqueueResult(intent, created)

    def reconcile_approved_reviews(self) -> list[ExecutionIntent]:
        created = []
        for review_id in self._repository.list_approved_reviews_without_execution():
            try:
                result = self.enqueue_approved_review(review_id)
            except ExecutionEligibilityError:
                logger.warning("event=execution_reconciliation_skipped review_item_id=%s reason=invalid_snapshot",
                               review_id)
                continue
            if result.created:
                created.append(result.intent)
                logger.info("event=execution_reconciliation_created execution_id=%s review_item_id=%s",
                            result.intent.execution_id, review_id)
        return created

    def get(self, execution_id: str) -> ExecutionIntent:
        execution_id = self._execution_id(execution_id)
        intent = self._repository.get_execution_intent(execution_id)
        if not intent:
            raise ExecutionNotFoundError("Execution intent not found")
        return intent

    def list_items(self, status: str | None = None) -> list[ExecutionIntent]:
        if status is not None and status not in EXECUTION_STATUSES:
            raise ValueError("Unknown execution status")
        return self._repository.list_execution_intents(status)

    def status_counts(self) -> dict[str, int]:
        return self._repository.execution_status_counts()

    def get_gmail_draft_result(self, execution_id: str):
        return self._repository.get_gmail_draft_result(self._execution_id(execution_id))

    def record_gmail_attempt_started(self, intent: ExecutionIntent) -> None:
        if intent.status != "processing" or not intent.claim_token:
            raise ExecutionClaimConflictError("Execution is not owned by the current worker")
        self._repository.record_execution_event(
            intent.execution_id, "gmail_draft_attempt_started", intent.attempt_count + 1,
            self._format(self._now()),
        )

    def record_gmail_definitive_failure(self, intent: ExecutionIntent, error_code: str,
                                        *, dispatched: bool = False) -> None:
        self._repository.record_execution_event(
            intent.execution_id, "gmail_draft_definitive_failure",
            intent.attempt_count + int(dispatched),
            self._format(self._now()), failure_code=error_code,
        )

    def claim_next(self, worker_id: str) -> ExecutionIntent | None:
        if not isinstance(worker_id, str) or not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("worker_id must be a bounded normalized identifier")
        now = self._now()
        token = secrets.token_urlsafe(24)
        intent = self._repository.claim_next_execution(
            worker_id=worker_id, claim_token=token, now=self._format(now),
            lease_expires_at=self._format(now + timedelta(seconds=self.lease_seconds)),
        )
        if intent:
            if intent.action_type not in EXECUTION_ACTION_TYPES:
                raise ExecutionTransitionError("Unknown execution action type")
            logger.info("event=execution_claimed execution_id=%s review_item_id=%s action_type=%s attempt=%s",
                        intent.execution_id, intent.source_review_item_id, intent.action_type,
                        intent.attempt_count + 1)
        return intent

    def mark_completed(self, execution_id: str, claim_token: str) -> ExecutionIntent:
        execution_id, claim_token = self._claim_arguments(execution_id, claim_token)
        current = self.get(execution_id)
        if current.action_type == "create_gmail_draft":
            raise ExecutionTransitionError(
                "Gmail draft executions require an atomically persisted provider result"
            )
        intent = self._repository.complete_execution(execution_id, claim_token, self._format(self._now()))
        if not intent:
            logger.warning("event=execution_claim_conflict execution_id=%s operation=complete", execution_id)
            raise ExecutionClaimConflictError("Execution claim is missing, stale, or no longer processing")
        logger.info("event=execution_completed execution_id=%s review_item_id=%s status=completed",
                    intent.execution_id, intent.source_review_item_id)
        return intent

    def mark_gmail_draft_completed(self, execution_id: str, claim_token: str, *, draft_id: str,
                                   message_id: str | None, thread_id: str) -> ExecutionIntent:
        execution_id, claim_token = self._claim_arguments(execution_id, claim_token)
        for value, name in ((draft_id, "draft_id"), (thread_id, "thread_id")):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError(f"{name} must be a bounded provider identifier")
        if message_id is not None and (not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 512):
            raise ValueError("message_id must be a bounded provider identifier")
        intent = self._repository.complete_gmail_draft_execution(
            execution_id, claim_token, draft_id=draft_id.strip(),
            message_id=message_id.strip() if message_id else None, thread_id=thread_id.strip(),
            now=self._format(self._now()),
        )
        if not intent:
            raise ExecutionClaimConflictError("Execution claim is missing, stale, or no longer processing")
        logger.info("event=gmail_draft_created execution_id=%s review_item_id=%s provider=gmail provider_state=created draft_id=%s",
                    intent.execution_id, intent.source_review_item_id, draft_id)
        return intent

    def mark_gmail_outcome_unknown(self, execution_id: str, claim_token: str,
                                   *, error_code: str = "gmail_create_outcome_unknown") -> ExecutionIntent:
        execution_id, claim_token = self._claim_arguments(execution_id, claim_token)
        intent = self._repository.mark_gmail_outcome_unknown(
            execution_id, claim_token, failure_code=error_code, now=self._format(self._now()),
        )
        if not intent:
            raise ExecutionClaimConflictError("Execution claim is missing, stale, or no longer processing")
        logger.warning("event=gmail_draft_outcome_unknown execution_id=%s review_item_id=%s provider=gmail provider_state=outcome_unknown error_category=%s",
                       execution_id, intent.source_review_item_id, error_code)
        return intent

    def reconcile_gmail_draft(self, execution_id: str, *, draft_id: str,
                              thread_id: str, message_id: str | None = None) -> ExecutionIntent:
        execution_id = self._execution_id(execution_id)
        for value, name in ((draft_id, "draft_id"), (thread_id, "thread_id")):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError(f"{name} must be a bounded provider identifier")
        if message_id is not None and (not message_id.strip() or len(message_id) > 512):
            raise ValueError("message_id must be a bounded provider identifier")
        intent = self._repository.reconcile_gmail_draft_result(
            execution_id, draft_id=draft_id.strip(), message_id=message_id.strip() if message_id else None,
            thread_id=thread_id.strip(), now=self._format(self._now()),
        )
        if not intent:
            raise ExecutionTransitionError(
                "Only an outcome_unknown execution with a matching thread may be reconciled"
            )
        logger.info("event=gmail_draft_reconciliation_confirmed execution_id=%s review_item_id=%s provider=gmail provider_state=created draft_id=%s",
                    execution_id, intent.source_review_item_id, draft_id)
        return intent

    def mark_failed(self, execution_id: str, claim_token: str, *, retryable: bool,
                    error_code: str, metadata: Mapping[str, object] | None = None,
                    count_attempt: bool = True) -> ExecutionIntent:
        execution_id, claim_token = self._claim_arguments(execution_id, claim_token)
        if not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code):
            raise ValueError("error_code must be a safe lowercase identifier")
        safe_metadata = self._safe_metadata(metadata)
        current = self.get(execution_id)
        if current.status != "processing" or current.claim_token != claim_token:
            raise ExecutionClaimConflictError("Execution claim is missing, stale, or no longer processing")
        attempt_count = current.attempt_count + int(bool(count_attempt))
        can_retry = bool(retryable) and attempt_count < self.max_attempts
        now = self._now()
        next_attempt = self._format(now + timedelta(seconds=self._retry_delay(attempt_count))) if can_retry else None
        status = "retry_wait" if can_retry else "failed"
        intent = self._repository.fail_execution(
            execution_id, claim_token, status=status, attempt_count=attempt_count,
            next_attempt_at=next_attempt, failure_code=error_code,
            failure_metadata_json=(json.dumps(safe_metadata, sort_keys=True, separators=(",", ":"))
                                   if safe_metadata else None), now=self._format(now),
        )
        if not intent:
            logger.warning("event=execution_claim_conflict execution_id=%s operation=fail", execution_id)
            raise ExecutionClaimConflictError("Execution claim is missing, stale, or no longer processing")
        event = "execution_retry_scheduled" if can_retry else "execution_failed"
        logger.info("event=%s execution_id=%s review_item_id=%s status=%s attempt=%s error_category=%s",
                    event, execution_id, intent.source_review_item_id, status, attempt_count, error_code)
        return intent

    def recover_expired_claims(self) -> list[str]:
        now = self._now()
        # Recovery itself is a failed attempt and uses the same bounded delay. Items already at
        # the limit are made terminal separately through claim-token-safe repository logic.
        ids = self._repository.recover_expired_executions(
            now=self._format(now), next_attempt_at=self._format(now + timedelta(
                seconds=self._retry_delay(1))), failure_code="claim_lease_expired",
            max_attempts=self.max_attempts,
        )
        for execution_id in ids:
            logger.warning("event=execution_claim_recovered execution_id=%s error_category=claim_lease_expired",
                           execution_id)
        return ids

    def _retry_delay(self, failed_attempt: int) -> float:
        return min(self.max_retry_seconds,
                   self.initial_retry_seconds * (self.retry_multiplier ** max(0, failed_attempt - 1)))

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _format(value: datetime) -> str:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _positive_id(value: int, name: str) -> None:
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _execution_id(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"exec_[0-9a-f]{32}", value):
            raise ExecutionNotFoundError("Execution intent not found")
        return value

    def _claim_arguments(self, execution_id: str, claim_token: str) -> tuple[str, str]:
        execution_id = self._execution_id(execution_id)
        if not isinstance(claim_token, str) or not (16 <= len(claim_token) <= 128):
            raise ExecutionClaimConflictError("Invalid execution claim token")
        return execution_id, claim_token

    @staticmethod
    def _safe_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
        if metadata is None:
            return {}
        if not isinstance(metadata, Mapping) or len(metadata) > 8:
            raise ValueError("failure metadata must be a small mapping")
        result = {}
        for key, value in metadata.items():
            if key not in _SAFE_METADATA_KEYS:
                raise ValueError("failure metadata contains an unsupported key")
            if isinstance(value, str):
                if len(value) > 128:
                    raise ValueError("failure metadata strings are limited to 128 characters")
                result[key] = value
            elif type(value) in (int, bool) or value is None:
                result[key] = value
            else:
                raise ValueError("failure metadata values must be simple scalars")
        return result
