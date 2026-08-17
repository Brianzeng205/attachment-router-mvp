"""Deterministic policy gates for validated analysis and local reply drafts."""

from __future__ import annotations

from typing import Protocol

from .conversation_models import ConversationAnalysis
from .policy_models import PolicyDecision
from .reply_draft_models import DRAFT_STATUSES, ReplyDraft


DEFAULT_DECISION_POLICY_VERSION = "v1"
DEFAULT_MIN_DRAFT_CONFIDENCE_FOR_REVIEW = 0.75
DEFAULT_MIN_CONVERSATION_CONFIDENCE_FOR_REVIEW = 0.75

# Fixed ordering makes reason output stable regardless of the order in which inputs are inspected.
_HUMAN_REASON_ORDER = (
    "insufficient_knowledge",
    "draft_needs_review",
    "conversation_needs_human",
    "financial_commitment",
    "financial_dispute",
    "sensitive_account_change",
    "legal_issue",
    "ambiguous_request",
    "ambiguous_thread",
    "low_confidence",
    "low_draft_confidence",
    "low_conversation_confidence",
    "critical_priority",
    "immediate_urgency",
)
_HIGH_RISK_REASONS = frozenset({
    "financial_commitment", "financial_dispute", "sensitive_account_change", "legal_issue",
    "ambiguous_request", "ambiguous_thread", "low_confidence",
})


class DecisionPolicy(Protocol):
    def evaluate(self, *, conversation_analysis: ConversationAnalysis, reply_draft: ReplyDraft) -> PolicyDecision: ...


class DefaultDecisionPolicy:
    """Conservative MVP policy with precedence: blocked, human review, no action, ready for review."""

    def __init__(
        self,
        *,
        rule_version: str = DEFAULT_DECISION_POLICY_VERSION,
        min_draft_confidence: float = DEFAULT_MIN_DRAFT_CONFIDENCE_FOR_REVIEW,
        min_conversation_confidence: float = DEFAULT_MIN_CONVERSATION_CONFIDENCE_FOR_REVIEW,
    ) -> None:
        if not rule_version.strip():
            raise ValueError("rule_version is required")
        if not 0 <= min_draft_confidence <= 1 or not 0 <= min_conversation_confidence <= 1:
            raise ValueError("policy confidence thresholds must be between 0 and 1")
        self.rule_version = rule_version
        self.min_draft_confidence = min_draft_confidence
        self.min_conversation_confidence = min_conversation_confidence

    def evaluate(self, *, conversation_analysis: ConversationAnalysis, reply_draft: ReplyDraft) -> PolicyDecision:
        # BLOCKED has highest precedence. Unsupported claims are model-identified claims that cannot safely
        # enter a normal review queue; correction/re-analysis is required rather than silent filtering.
        blocked = self._blocked_reasons(reply_draft)
        if blocked:
            return self._decision("blocked", blocked)

        human_reasons = set()
        if reply_draft.draft_status == "insufficient_knowledge":
            human_reasons.add("insufficient_knowledge")
        if reply_draft.needs_review:
            human_reasons.add("draft_needs_review")
        if conversation_analysis.needs_human:
            human_reasons.add("conversation_needs_human")
        for reason in (reply_draft.review_reason, conversation_analysis.human_reason):
            if reason in _HIGH_RISK_REASONS:
                human_reasons.add(reason)
        if reply_draft.confidence < self.min_draft_confidence:
            human_reasons.add("low_draft_confidence")
        if conversation_analysis.confidence < self.min_conversation_confidence:
            human_reasons.add("low_conversation_confidence")
        if conversation_analysis.priority == "critical":
            human_reasons.add("critical_priority")
        if conversation_analysis.urgency == "immediate":
            human_reasons.add("immediate_urgency")
        if human_reasons:
            return self._decision(
                "human_review_required", tuple(reason for reason in _HUMAN_REASON_ORDER if reason in human_reasons),
            )

        if reply_draft.draft_status == "not_applicable":
            return self._decision("no_action", ("not_applicable",))

        # recommended_action is intentionally absent: an AI recommendation cannot bypass these gates.
        return self._decision("ready_for_review", ("safe_for_review",))

    @staticmethod
    def _blocked_reasons(reply_draft: ReplyDraft) -> tuple[str, ...]:
        reasons: list[str] = []
        if reply_draft.draft_status not in DRAFT_STATUSES or not isinstance(reply_draft.body, str) or not reply_draft.body.strip():
            reasons.append("invalid_upstream_state")
        if len(reply_draft.grounding_chunk_ids) != len(set(reply_draft.grounding_chunk_ids)) or any(
            isinstance(chunk_id, bool) or not isinstance(chunk_id, int) or chunk_id < 1
            for chunk_id in reply_draft.grounding_chunk_ids
        ):
            reasons.append("grounding_invariant_failed")
        if reply_draft.unsupported_claims:
            reasons.append("unsupported_claims")
        return tuple(reasons)

    def _decision(self, decision: str, reasons: tuple[str, ...]) -> PolicyDecision:
        return PolicyDecision(decision, self.rule_version, reasons, reasons[0])
