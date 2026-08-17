import unittest

from app.conversation_models import ConversationAnalysis
from app.decision_policy import DefaultDecisionPolicy
from app.gmail_client import GMAIL_READONLY_SCOPE
from app.reply_draft_models import ReplyDraft


def conversation(**overrides):
    values = {
        "conversation_summary": "Customer asks a routine question.", "current_intent": "request_information",
        "priority": "normal", "urgency": "medium", "unresolved_requests": ["Answer the question."],
        "resolved_points": [], "order_numbers": [], "relevant_dates": [],
        "latest_sender_request": "Can you clarify?", "confidence": 0.9, "needs_human": False,
        "human_reason": None, "recommended_action": "draft_reply",
    }
    values.update(overrides)
    return ConversationAnalysis.from_mapping(values)


def draft(**overrides):
    values = {
        "draft_status": "drafted", "subject": "Re: Question", "body": "Here is the confirmed information.",
        "grounding_chunk_ids": (11,), "unsupported_claims": (), "confidence": 0.9,
        "needs_review": False, "review_reason": None, "response_language": "en",
    }
    values.update(overrides)
    return ReplyDraft(**values)


class DecisionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = DefaultDecisionPolicy()

    def evaluate(self, analysis=None, reply=None):
        return self.policy.evaluate(conversation_analysis=analysis or conversation(), reply_draft=reply or draft())

    def test_normal_grounded_draft_is_ready_for_review_but_not_approved_to_send(self):
        result = self.evaluate()
        self.assertEqual(result.decision, "ready_for_review")
        self.assertEqual(result.reason_codes, ("safe_for_review",))

    def test_insufficient_knowledge_requires_human_review(self):
        result = self.evaluate(reply=draft(draft_status="insufficient_knowledge", grounding_chunk_ids=(), needs_review=True,
                                           review_reason="insufficient_knowledge"))
        self.assertEqual(result.decision, "human_review_required")
        self.assertEqual(result.primary_reason, "insufficient_knowledge")

    def test_draft_or_conversation_human_signal_requires_review(self):
        cases = (
            (conversation(), draft(needs_review=True, review_reason="ambiguous_request"), "draft_needs_review"),
            (conversation(needs_human=True, human_reason="ambiguous_thread"), draft(), "conversation_needs_human"),
        )
        for analysis, reply, reason in cases:
            with self.subTest(reason=reason):
                result = self.evaluate(analysis, reply)
                self.assertEqual(result.decision, "human_review_required")
                self.assertIn(reason, result.reason_codes)

    def test_unsupported_claims_are_blocked_from_normal_review(self):
        result = self.evaluate(reply=draft(unsupported_claims=("Unverified refund approval",)))
        self.assertEqual(result.decision, "blocked")
        self.assertEqual(result.reason_codes, ("unsupported_claims",))

    def test_low_confidence_gates_are_independent(self):
        cases = (
            (conversation(), draft(confidence=0.74), "low_draft_confidence"),
            (conversation(confidence=0.74), draft(), "low_conversation_confidence"),
        )
        for analysis, reply, reason in cases:
            with self.subTest(reason=reason):
                result = self.evaluate(analysis, reply)
                self.assertEqual(result.decision, "human_review_required")
                self.assertIn(reason, result.reason_codes)

    def test_critical_priority_and_immediate_urgency_require_review(self):
        result = self.evaluate(conversation(priority="critical", urgency="immediate"))
        self.assertEqual(result.decision, "human_review_required")
        self.assertEqual(result.reason_codes, ("critical_priority", "immediate_urgency"))

    def test_supported_high_risk_reasons_require_review(self):
        for reason in ("financial_commitment", "financial_dispute", "sensitive_account_change", "legal_issue"):
            with self.subTest(reason=reason):
                result = self.evaluate(conversation(needs_human=True, human_reason=reason))
                self.assertEqual(result.decision, "human_review_required")
                self.assertIn(reason, result.reason_codes)

    def test_not_applicable_is_no_action_unless_higher_precedence_rule_fires(self):
        self.assertEqual(self.evaluate(reply=draft(draft_status="not_applicable", grounding_chunk_ids=())).decision, "no_action")
        blocked = self.evaluate(reply=draft(draft_status="not_applicable", grounding_chunk_ids=(),
                                            unsupported_claims=("unsafe",)))
        self.assertEqual(blocked.decision, "blocked")

    def test_invalid_grounding_invariant_is_blocked(self):
        result = self.evaluate(reply=draft(grounding_chunk_ids=(11, 11)))
        self.assertEqual(result.decision, "blocked")
        self.assertEqual(result.primary_reason, "grounding_invariant_failed")

    def test_multiple_rules_have_fixed_precedence_and_order(self):
        result = self.evaluate(
            conversation(priority="critical", urgency="immediate", confidence=0.5, needs_human=True,
                         human_reason="legal_issue"),
            draft(confidence=0.5, needs_review=True, review_reason="financial_commitment"),
        )
        self.assertEqual(result.decision, "human_review_required")
        self.assertEqual(result.reason_codes, (
            "draft_needs_review", "conversation_needs_human", "financial_commitment", "legal_issue",
            "low_draft_confidence", "low_conversation_confidence", "critical_priority", "immediate_urgency",
        ))

    def test_same_inputs_and_version_produce_identical_decision(self):
        first = self.evaluate()
        second = self.evaluate()
        self.assertEqual(first, second)
        self.assertEqual(first.rule_version, "v1")

    def test_ai_recommendation_is_advisory_and_cannot_override_policy_or_execute(self):
        result = self.evaluate(conversation(recommended_action="draft_reply"), draft(needs_review=True))
        self.assertEqual(result.decision, "human_review_required")
        self.assertFalse(hasattr(result, "execute"))
        self.assertFalse(hasattr(self.policy, "send"))

    def test_policy_is_offline_and_gmail_remains_readonly(self):
        self.assertFalse(hasattr(self.policy, "client"))
        self.assertFalse(hasattr(self.policy, "analyzer"))
        self.assertEqual(GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.readonly")
