"""Application-owned deterministic policy decision model."""

from __future__ import annotations

from dataclasses import dataclass


POLICY_DECISIONS = frozenset({"ready_for_review", "human_review_required", "blocked", "no_action"})
POLICY_REASON_CODES = frozenset({
    "invalid_upstream_state",
    "grounding_invariant_failed",
    "insufficient_knowledge",
    "draft_needs_review",
    "conversation_needs_human",
    "low_draft_confidence",
    "low_conversation_confidence",
    "financial_commitment",
    "financial_dispute",
    "sensitive_account_change",
    "legal_issue",
    "ambiguous_request",
    "ambiguous_thread",
    "low_confidence",
    "critical_priority",
    "immediate_urgency",
    "unsupported_claims",
    "not_applicable",
    "safe_for_review",
})


@dataclass(frozen=True)
class PolicyDecision:
    """A deterministic routing decision only; it grants no execution authority."""

    decision: str
    rule_version: str
    reason_codes: tuple[str, ...]
    primary_reason: str

    def __post_init__(self) -> None:
        if self.decision not in POLICY_DECISIONS:
            raise ValueError("decision must be an approved policy decision")
        if not self.rule_version.strip():
            raise ValueError("rule_version is required")
        if not self.reason_codes or len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must be non-empty and unique")
        if any(reason not in POLICY_REASON_CODES for reason in self.reason_codes):
            raise ValueError("reason_codes contain an uncontrolled value")
        if self.primary_reason != self.reason_codes[0]:
            raise ValueError("primary_reason must be the first deterministic reason code")
