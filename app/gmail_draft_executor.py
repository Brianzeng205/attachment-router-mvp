"""Explicit orchestration for one human-approved Gmail draft execution."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .execution_models import ACTION_CREATE_GMAIL_DRAFT, ExecutionIntent
from .execution_queue_service import ExecutionQueueService
from .gmail_draft import (
    GmailDraftClient, GmailDraftCommand, GmailDraftDefinitiveError, GmailDraftOutcomeUnknown,
    GmailDraftValidationError, build_gmail_reply_command,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftExecutionOutcome:
    intent: ExecutionIntent | None
    provider_called: bool
    command: GmailDraftCommand | None = None


class GmailDraftExecutor:
    def __init__(self, queue: ExecutionQueueService, client: GmailDraftClient, *,
                 authenticated_account: str, max_mime_bytes: int = 1_000_000) -> None:
        self._queue, self._client = queue, client
        self._authenticated_account, self._max_mime_bytes = authenticated_account, max_mime_bytes

    def execute_once(self, worker_id: str) -> DraftExecutionOutcome:
        intent = self._queue.claim_next(worker_id)
        if intent is None:
            return DraftExecutionOutcome(None, False)
        if self._queue.get_gmail_draft_result(intent.execution_id):
            # Defensive invariant: claim selection should never expose a confirmed provider result.
            failed = self._queue.mark_failed(intent.execution_id, intent.claim_token, retryable=False,
                                             error_code="provider_result_already_exists", count_attempt=False)
            return DraftExecutionOutcome(failed, False)
        try:
            command = build_gmail_reply_command(
                intent, authenticated_account=self._authenticated_account,
                max_mime_bytes=self._max_mime_bytes,
            )
        except GmailDraftValidationError:
            self._queue.record_gmail_definitive_failure(intent, "invalid_draft_input")
            failed = self._queue.mark_failed(intent.execution_id, intent.claim_token, retryable=False,
                                             error_code="invalid_draft_input", count_attempt=False)
            logger.warning("event=gmail_draft_definitive_failure execution_id=%s review_item_id=%s provider=gmail provider_state=not_dispatched error_category=invalid_draft_input",
                           intent.execution_id, intent.source_review_item_id)
            return DraftExecutionOutcome(failed, False)

        self._queue.record_gmail_attempt_started(intent)
        logger.info("event=gmail_draft_attempt_started execution_id=%s review_item_id=%s action_type=%s attempt=%s provider=gmail provider_state=dispatching",
                    intent.execution_id, intent.source_review_item_id, intent.action_type, intent.attempt_count + 1)
        try:
            created = self._client.create_reply_draft(command)
        except GmailDraftOutcomeUnknown as exc:
            unknown = self._queue.mark_gmail_outcome_unknown(
                intent.execution_id, intent.claim_token, error_code=exc.code,
            )
            return DraftExecutionOutcome(unknown, True, command)
        except GmailDraftDefinitiveError as exc:
            self._queue.record_gmail_definitive_failure(intent, exc.code, dispatched=True)
            failed = self._queue.mark_failed(
                intent.execution_id, intent.claim_token, retryable=exc.retryable, error_code=exc.code,
                metadata={"provider_status": exc.provider_status, "provider_state": "definitive_failure"},
            )
            logger.warning("event=gmail_draft_definitive_failure execution_id=%s review_item_id=%s provider=gmail provider_state=definitive_failure error_category=%s",
                           intent.execution_id, intent.source_review_item_id, exc.code)
            return DraftExecutionOutcome(failed, True, command)
        completed = self._queue.mark_gmail_draft_completed(
            intent.execution_id, intent.claim_token, draft_id=created.draft_id,
            message_id=created.message_id, thread_id=created.thread_id,
        )
        return DraftExecutionOutcome(completed, True, command)


def preview_next(queue: ExecutionQueueService, *, authenticated_account: str,
                 max_mime_bytes: int = 1_000_000) -> GmailDraftCommand | None:
    """Non-mutating, credential-free validation of the next pending item."""
    items = queue.list_items("pending")
    if not items:
        return None
    return build_gmail_reply_command(items[0], authenticated_account=authenticated_account,
                                     max_mime_bytes=max_mime_bytes)
