"""Local-only persistence and transitions for deterministic policy review."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from .policy_models import PolicyDecision
from .review_models import HumanReviewItem, ReviewQueueOutcome


_REVIEWER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+\-]{0,127}")
_REVIEW_TYPE_BY_DECISION = {
    "ready_for_review": "standard_review",
    "human_review_required": "required_review",
    "blocked": "blocked_resolution",
}


class ReviewQueueService:
    """Persists local review state only; approval carries no external execution authority."""

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

    def approve(self, review_item_id: int, reviewer_id: str, note: str | None = None) -> HumanReviewItem:
        return self._transition(review_item_id, "approved", reviewer_id, note)

    def reject(self, review_item_id: int, reviewer_id: str, note: str | None = None) -> HumanReviewItem:
        return self._transition(review_item_id, "rejected", reviewer_id, note)

    def request_changes(self, review_item_id: int, reviewer_id: str, note: str | None = None) -> HumanReviewItem:
        return self._transition(review_item_id, "changes_requested", reviewer_id, note)

    def get(self, review_item_id: int) -> HumanReviewItem | None:
        return self._repository.get_review_item(review_item_id)

    def list_pending(self) -> list[HumanReviewItem]:
        return self._repository.list_pending_review_items()

    def history(self, review_item_id: int):
        return self._repository.list_review_events(review_item_id)

    def _transition(self, review_item_id: int, status: str, reviewer_id: str, note: str | None) -> HumanReviewItem:
        normalized_reviewer = reviewer_id.strip() if isinstance(reviewer_id, str) else ""
        if not _REVIEWER_ID.fullmatch(normalized_reviewer):
            raise ValueError("reviewer_id must be a bounded normalized identifier")
        normalized_note = None
        if note is not None:
            if not isinstance(note, str) or not note.strip() or len(note.strip()) > 1_000:
                raise ValueError("review note must be non-empty and at most 1000 characters")
            normalized_note = note.strip()
        return self._repository.transition_review_item(review_item_id, status, normalized_reviewer, normalized_note)
