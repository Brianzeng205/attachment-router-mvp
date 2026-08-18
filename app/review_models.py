"""Application-owned models for persisted policy decisions and local human review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .policy_models import PolicyDecision


REVIEW_TYPES = frozenset({"standard_review", "required_review", "blocked_resolution"})
REVIEW_STATUSES = frozenset({"pending", "approved", "rejected", "changes_requested"})
REVIEW_EVENT_TYPES = frozenset({"created", "approved", "rejected", "changes_requested"})


@dataclass(frozen=True)
class PersistedPolicyDecision:
    id: int
    conversation_id: int
    reply_draft_id: int
    conversation_analysis_id: int
    input_fingerprint: str
    policy_decision: PolicyDecision


@dataclass(frozen=True)
class HumanReviewItem:
    id: int
    policy_decision_id: int
    conversation_id: int
    reply_draft_id: int
    review_type: str
    status: str
    reviewer_id: str | None = None
    resolved_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    approved_draft_body: str | None = None


@dataclass(frozen=True)
class HumanReviewEvent:
    id: int
    review_item_id: int
    event_type: str
    reviewer_id: str | None
    note: str | None
    created_at: str


@dataclass(frozen=True)
class ReviewQueueOutcome:
    policy_decision: PersistedPolicyDecision
    review_item: HumanReviewItem | None
    reused: bool


@dataclass(frozen=True)
class ReviewQueueEntry:
    id: int
    status: str
    review_type: str
    created_at: str
    updated_at: str
    confidence: float | None
    review_reason: str
    sender: str | None
    subject: str | None
    summary: str | None
    has_draft: bool


@dataclass(frozen=True)
class ReviewDetail:
    item: HumanReviewItem
    messages: tuple[dict[str, Any], ...]
    message_analysis: dict[str, Any] | None
    thread_analysis: dict[str, Any] | None
    retrieval_context: tuple[dict[str, Any], ...]
    policy: dict[str, Any]
    original_draft_body: str | None
    draft_subject: str | None
    draft_confidence: float | None
    draft_review_reason: str | None
    history: tuple[HumanReviewEvent, ...]


class ReviewNotFoundError(LookupError):
    pass


class ReviewConflictError(ValueError):
    pass


class ReviewValidationError(ValueError):
    pass
