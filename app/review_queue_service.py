"""Local-only persistence and transitions for deterministic policy review."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping

from .policy_models import PolicyDecision
from .review_models import (
    HumanReviewItem, ReviewConflictError, ReviewDetail, ReviewNotFoundError,
    ReviewQueueEntry, ReviewQueueOutcome, ReviewValidationError,
)


_REVIEWER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+\-]{0,127}")
_REVIEW_TYPE_BY_DECISION = {
    "ready_for_review": "standard_review",
    "human_review_required": "required_review",
    "blocked": "blocked_resolution",
}
MAX_APPROVED_DRAFT_CHARS = 50_000
_ALLOWED_LIST_STATUSES = frozenset({"pending", "approved", "rejected"})
logger = logging.getLogger(__name__)


class ReviewQueueService:
    """Persists review state and atomically hands approved snapshots to the local execution queue."""

    def __init__(self, repository, *, policy_configuration: Mapping[str, object] | None = None) -> None:
        self._repository = repository
        self._policy_configuration = dict(policy_configuration or {})

    def record_decision(
        self,
        *,
        conversation_id: int,
        conversation_analysis_id: int,
        reply_draft_id: int,
        reply_draft_fingerprint: str,
        decision: PolicyDecision,
    ) -> ReviewQueueOutcome:
        if min(conversation_id, conversation_analysis_id, reply_draft_id) < 1:
            raise ValueError("Persisted policy sources require positive identifiers")
        if not isinstance(reply_draft_fingerprint, str) or not reply_draft_fingerprint.strip():
            raise ValueError("reply_draft_fingerprint is required")
        fingerprint = self.input_fingerprint(
            conversation_analysis_id=conversation_analysis_id, reply_draft_id=reply_draft_id,
            reply_draft_fingerprint=reply_draft_fingerprint, policy_version=decision.rule_version,
        )
        existing = self._repository.get_policy_decision_by_fingerprint(conversation_id, fingerprint)
        if existing:
            return ReviewQueueOutcome(
                existing, self._repository.get_review_item_for_policy_decision(existing.id), True,
            )
        review_type = _REVIEW_TYPE_BY_DECISION.get(decision.decision)
        persisted, item = self._repository.create_policy_decision_and_review(
            conversation_id=conversation_id, conversation_analysis_id=conversation_analysis_id,
            reply_draft_id=reply_draft_id, input_fingerprint=fingerprint, decision=decision,
            review_type=review_type,
        )
        return ReviewQueueOutcome(persisted, item, False)

    def input_fingerprint(
        self, *, conversation_analysis_id: int, reply_draft_id: int,
        reply_draft_fingerprint: str, policy_version: str,
    ) -> str:
        state = {
            "conversation_analysis_id": conversation_analysis_id,
            "reply_draft_id": reply_draft_id,
            "reply_draft_fingerprint": reply_draft_fingerprint,
            "policy_version": policy_version,
            "policy_configuration": self._policy_configuration,
        }
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def approve(self, review_item_id: int, reviewer_id: str, note: str | None = None,
                *, approved_draft_body: str | None = None,
                expected_updated_at: str | None = None) -> HumanReviewItem:
        detail = self.detail(review_item_id)
        body = detail.original_draft_body if approved_draft_body is None else approved_draft_body
        body = self._validate_draft(body)
        return self._transition(review_item_id, "approved", reviewer_id, note,
                                expected_updated_at=expected_updated_at, approved_draft_body=body)

    def reject(self, review_item_id: int, reviewer_id: str, note: str | None = None,
               *, expected_updated_at: str | None = None) -> HumanReviewItem:
        return self._transition(review_item_id, "rejected", reviewer_id, note,
                                expected_updated_at=expected_updated_at)

    def request_changes(self, review_item_id: int, reviewer_id: str, note: str | None = None) -> HumanReviewItem:
        return self._transition(review_item_id, "changes_requested", reviewer_id, note)

    def get(self, review_item_id: int) -> HumanReviewItem | None:
        return self._repository.get_review_item(review_item_id)

    def list_pending(self) -> list[HumanReviewItem]:
        return self._repository.list_pending_review_items()

    def list_items(self, status: str = "pending") -> list[ReviewQueueEntry]:
        if status not in _ALLOWED_LIST_STATUSES:
            raise ReviewValidationError("Unknown review status filter")
        return [ReviewQueueEntry(
            id=row["id"], status=row["status"], review_type=row["review_type"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            review_reason=row["primary_reason"] or row["draft_review_reason"] or row["review_type"],
            sender=row["sender"], subject=row["subject"], summary=row["conversation_summary"],
            has_draft=bool(row["has_draft"]),
        ) for row in self._repository.list_review_queue_rows(status)]

    def detail(self, review_item_id: int) -> ReviewDetail:
        if type(review_item_id) is not int or review_item_id < 1:
            raise ReviewNotFoundError("Review item not found")
        result = self._repository.get_review_detail_rows(review_item_id)
        if result is None:
            raise ReviewNotFoundError("Review item not found")
        row, messages, message_analysis, thread_analysis, retrieval = result
        item = self._repository.get_review_item(review_item_id)
        policy = {
            "decision": row["decision"], "version": row["policy_version"],
            "reason_codes": self._json_list(row["reason_codes_json"]),
            "primary_reason": row["primary_reason"], "review_type": row["review_type"],
        }
        return ReviewDetail(
            item=item,
            messages=tuple(dict(message) for message in messages),
            message_analysis=self._readable_analysis(message_analysis),
            thread_analysis=self._readable_analysis(thread_analysis),
            retrieval_context=tuple(dict(value) for value in retrieval), policy=policy,
            original_draft_body=row["original_draft_body"], draft_subject=row["draft_subject"],
            draft_confidence=float(row["draft_confidence"]) if row["draft_confidence"] is not None else None,
            draft_review_reason=row["draft_review_reason"], history=tuple(self.history(review_item_id)),
        )

    def history(self, review_item_id: int):
        return self._repository.list_review_events(review_item_id)

    def _transition(self, review_item_id: int, status: str, reviewer_id: str, note: str | None,
                    *, expected_updated_at: str | None = None,
                    approved_draft_body: str | None = None) -> HumanReviewItem:
        normalized_reviewer = reviewer_id.strip() if isinstance(reviewer_id, str) else ""
        if not _REVIEWER_ID.fullmatch(normalized_reviewer):
            raise ValueError("reviewer_id must be a bounded normalized identifier")
        normalized_note = None
        if note is not None:
            if not isinstance(note, str) or not note.strip() or len(note.strip()) > 1_000:
                raise ValueError("review note must be non-empty and at most 1000 characters")
            normalized_note = note.strip()
        try:
            item = self._repository.transition_review_item(
                review_item_id, status, normalized_reviewer, normalized_note,
                expected_updated_at=expected_updated_at, approved_draft_body=approved_draft_body,
            )
        except RuntimeError as exc:
            logger.warning("event=review_conflict review_item_id=%s attempted_status=%s", review_item_id, status)
            raise ReviewConflictError(str(exc)) from exc
        except ValueError as exc:
            if "does not exist" in str(exc):
                raise ReviewNotFoundError("Review item not found") from exc
            raise ReviewConflictError(str(exc)) from exc
        logger.info("event=review_%s review_item_id=%s status=%s", status, review_item_id, status)
        if status == "approved":
            intent = self._repository.get_execution_for_review(review_item_id)
            logger.info(
                "event=execution_intent_created execution_id=%s review_item_id=%s action_type=%s status=%s",
                intent.execution_id, review_item_id, intent.action_type, intent.status,
            )
        return item

    @staticmethod
    def _validate_draft(body: str | None) -> str:
        if not isinstance(body, str):
            logger.warning("event=review_validation_failed field=draft reason=missing")
            raise ReviewValidationError("A reply draft is required for approval")
        normalized = body.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.strip():
            logger.warning("event=review_validation_failed field=draft reason=empty")
            raise ReviewValidationError("Approved draft cannot be empty")
        if len(normalized) > MAX_APPROVED_DRAFT_CHARS:
            logger.warning("event=review_validation_failed field=draft reason=oversized")
            raise ReviewValidationError(f"Approved draft cannot exceed {MAX_APPROVED_DRAFT_CHARS} characters")
        return normalized

    @staticmethod
    def _json_list(value) -> tuple:
        try:
            parsed = json.loads(value or "[]")
            return tuple(parsed) if isinstance(parsed, list) else ()
        except (TypeError, ValueError):
            return ()

    @classmethod
    def _readable_analysis(cls, row):
        if row is None:
            return None
        result = dict(row)
        for key in tuple(result):
            if key.endswith("_json"):
                result[key.removesuffix("_json")] = cls._json_list(result.pop(key))
        for key in ("needs_human", "context_truncated"):
            if key in result:
                result[key] = bool(result[key])
        return result
