import tempfile
import unittest
from pathlib import Path

from app.conversation_models import ConversationAnalysis
from app.decision_policy import DefaultDecisionPolicy
from app.gmail_client import GMAIL_READONLY_SCOPE
from app.inbox_repository import SqliteInboxRepository
from app.message_ingestion import MessageIngestionService
from app.models import EmailMessage
from app.policy_models import PolicyDecision
from app.reply_draft_models import ReplyDraft
from app.review_queue_service import ReviewQueueService
from app.thread_context import ThreadContextBuilder


def analysis(**overrides):
    value = {
        "conversation_summary": "Routine customer request.", "current_intent": "request_information",
        "priority": "normal", "urgency": "medium", "unresolved_requests": ["Answer question."],
        "resolved_points": [], "order_numbers": [], "relevant_dates": [], "latest_sender_request": "Please help.",
        "confidence": 0.9, "needs_human": False, "human_reason": None, "recommended_action": "draft_reply",
    }
    value.update(overrides)
    return ConversationAnalysis.from_mapping(value)


def draft(**overrides):
    value = {
        "draft_status": "drafted", "subject": "Re: Help", "body": "Confirmed local reply body.",
        "grounding_chunk_ids": (1,), "unsupported_claims": (), "confidence": 0.9,
        "needs_review": False, "review_reason": None, "response_language": "en",
    }
    value.update(overrides)
    return ReplyDraft(**value)


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "state.sqlite3"
        self.repository = SqliteInboxRepository(self.path)
        email = EmailMessage("m-1", "customer@example.test", "Help", "private conversation body",
                             "2026-08-17T10:00:00+00:00", (), "thread-1", ("support@example.test",))
        MessageIngestionService(self.repository).ingest(email)
        self.conversation = self.repository.get_conversation_by_provider_thread_id("gmail", "thread-1")
        context = ThreadContextBuilder(10, 1_000).build(
            self.conversation, self.repository.list_messages_for_conversation(self.conversation.id),
        )
        run = self.repository.start_conversation_analysis_run(
            conversation_id=self.conversation.id, analyzer="test", analyzer_version="v1", model="test",
            prompt_version="v1", context_fingerprint=context.context_fingerprint,
        )
        self.repository.complete_conversation_analysis_run(run, context.latest_message_id, False, analysis())
        self.analysis_id = self.repository.connection.execute("SELECT id FROM conversation_analyses").fetchone()[0]
        self.repository.upsert_knowledge("policy.md", "policy.md", "private knowledge content", "doc-hash",
                                         ["private knowledge content"], "v1")
        match = self.repository.search_knowledge("private", 1)[0]
        retrieval_id = self.repository.start_retrieval(
            self.conversation.id, self.analysis_id, "query", "query-fingerprint", "index", "test", "v1", 1,
        )
        self.repository.complete_retrieval(retrieval_id, [match])
        self.retrieval_id = retrieval_id
        self.latest_message_id = context.latest_message_id
        self.service = ReviewQueueService(self.repository, policy_configuration={"draft_threshold": 0.75,
                                                                                 "conversation_threshold": 0.75})
        self.draft_number = 0

    def tearDown(self):
        self.repository.close()
        self.tempdir.cleanup()

    def create_draft(self):
        self.draft_number += 1
        run = self.repository.start_reply_draft_run(
            conversation_id=self.conversation.id, conversation_analysis_id=self.analysis_id,
            knowledge_retrieval_run_id=self.retrieval_id, generator="test", generator_version="v1", model="test",
            prompt_version="v1", input_fingerprint=f"draft-{self.draft_number}",
        )
        persisted = self.repository.complete_reply_draft_run(
            run, latest_message_id=self.latest_message_id, draft=draft(), grounding_chunk_ids=(1,),
        )
        return persisted.id, run.input_fingerprint

    def record(self, decision=None, *, draft_source=None):
        draft_id, fingerprint = draft_source or self.create_draft()
        decision = decision or DefaultDecisionPolicy().evaluate(conversation_analysis=analysis(), reply_draft=draft())
        return self.service.record_decision(
            conversation_id=self.conversation.id, conversation_analysis_id=self.analysis_id,
            reply_draft_id=draft_id, reply_draft_fingerprint=fingerprint, decision=decision,
        )

    def decision(self, name, reasons, version="v1"):
        return PolicyDecision(name, version, tuple(reasons), reasons[0])

    def test_decision_mappings_persist_provenance_and_created_history(self):
        cases = (
            ("ready_for_review", ("safe_for_review",), "standard_review"),
            ("human_review_required", ("draft_needs_review",), "required_review"),
            ("blocked", ("unsupported_claims",), "blocked_resolution"),
        )
        for name, reasons, review_type in cases:
            with self.subTest(decision=name):
                outcome = self.record(self.decision(name, reasons))
                self.assertEqual(outcome.policy_decision.policy_decision.decision, name)
                self.assertEqual(outcome.policy_decision.policy_decision.reason_codes, reasons)
                self.assertEqual(outcome.policy_decision.policy_decision.rule_version, "v1")
                self.assertEqual((outcome.review_item.review_type, outcome.review_item.status), (review_type, "pending"))
                self.assertEqual([event.event_type for event in self.service.history(outcome.review_item.id)], ["created"])

    def test_no_action_persists_decision_without_review_item(self):
        outcome = self.record(self.decision("no_action", ("not_applicable",)))
        self.assertIsNone(outcome.review_item)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM policy_decisions").fetchone()[0], 1)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM human_review_items").fetchone()[0], 0)

    def test_policy_and_review_item_idempotency_reuse_same_rows(self):
        source = self.create_draft()
        first = self.record(draft_source=source)
        second = self.record(draft_source=source)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first.policy_decision.id, second.policy_decision.id)
        self.assertEqual(first.review_item.id, second.review_item.id)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM policy_decisions").fetchone()[0], 1)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM human_review_items").fetchone()[0], 1)

    def test_changed_draft_policy_version_or_configuration_permits_new_decision(self):
        first = self.record()
        changed_draft = self.record()
        source = self.create_draft()
        changed_version = self.record(self.decision("ready_for_review", ("safe_for_review",), "v2"), draft_source=source)
        other_service = ReviewQueueService(self.repository, policy_configuration={"draft_threshold": 0.8})
        changed_config = other_service.record_decision(
            conversation_id=self.conversation.id, conversation_analysis_id=self.analysis_id,
            reply_draft_id=source[0], reply_draft_fingerprint=source[1],
            decision=self.decision("ready_for_review", ("safe_for_review",), "v1"),
        )
        self.assertEqual(len({first.policy_decision.id, changed_draft.policy_decision.id,
                              changed_version.policy_decision.id, changed_config.policy_decision.id}), 4)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM human_review_items").fetchone()[0], 4)

    def test_pending_standard_and_required_reviews_allow_human_resolution(self):
        transitions = (
            ("approve", "approved"), ("reject", "rejected"), ("request_changes", "changes_requested"),
        )
        for method, expected in transitions:
            with self.subTest(transition=method):
                item = self.record().review_item
                updated = getattr(self.service, method)(item.id, "human@example.com", "bounded private note")
                self.assertEqual((updated.status, updated.reviewer_id), (expected, "human@example.com"))
                self.assertEqual([event.event_type for event in self.service.history(item.id)], ["created", expected])
        required = self.record(self.decision("human_review_required", ("draft_needs_review",))).review_item
        self.assertEqual(self.service.approve(required.id, "reviewer-1").status, "approved")

    def test_blocked_resolution_cannot_be_approved_but_can_be_rejected_or_changed(self):
        blocked = self.decision("blocked", ("unsupported_claims",))
        with self.assertRaises(ValueError):
            self.service.approve(self.record(blocked).review_item.id, "reviewer-1")
        rejected = self.record(blocked).review_item
        changed = self.record(blocked).review_item
        self.assertEqual(self.service.reject(rejected.id, "reviewer-1").status, "rejected")
        self.assertEqual(self.service.request_changes(changed.id, "reviewer-1").status, "changes_requested")

    def test_terminal_items_cannot_be_reviewed_again(self):
        for initial, later in (("approve", "approve"), ("approve", "reject"), ("reject", "approve"),
                               ("request_changes", "approve")):
            with self.subTest(initial=initial, later=later):
                item = self.record().review_item
                getattr(self.service, initial)(item.id, "reviewer-1")
                with self.assertRaises(ValueError):
                    getattr(self.service, later)(item.id, "reviewer-2")

    def test_reviewer_and_note_validation(self):
        item = self.record().review_item
        for reviewer in ("", "contains space", "x" * 129):
            with self.subTest(reviewer=reviewer), self.assertRaises(ValueError):
                self.service.approve(item.id, reviewer)
        with self.assertRaises(ValueError):
            self.service.approve(item.id, "reviewer-1", "x" * 1001)

    def test_policy_is_immutable_and_approval_is_local_only(self):
        outcome = self.record()
        before = self.repository.connection.execute("SELECT * FROM policy_decisions WHERE id=?",
                                                    (outcome.policy_decision.id,)).fetchone()
        self.service.approve(outcome.review_item.id, "reviewer-1")
        after = self.repository.connection.execute("SELECT * FROM policy_decisions WHERE id=?",
                                                   (outcome.policy_decision.id,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertFalse(hasattr(self.service, "send"))
        self.assertFalse(hasattr(self.service, "claude"))

    def test_audits_are_safe_and_history_keeps_note_only_locally(self):
        outcome = self.record()
        note = "private reviewer note"
        self.service.reject(outcome.review_item.id, "reviewer-1", note)
        events = [row[0] for row in self.repository.connection.execute("SELECT event_type FROM audit_events").fetchall()]
        self.assertIn("policy_decision_recorded", events)
        self.assertIn("human_review_created", events)
        self.assertIn("human_review_rejected", events)
        audit = " ".join(row[0] for row in self.repository.connection.execute("SELECT metadata_json FROM audit_events").fetchall())
        self.assertNotIn("Confirmed local reply body.", audit)
        self.assertNotIn("private conversation body", audit)
        self.assertNotIn("private knowledge content", audit)
        self.assertNotIn(note, audit)
        self.assertEqual(self.service.history(outcome.review_item.id)[-1].note, note)

    def test_pending_listing_is_deterministic_and_schema_is_idempotent(self):
        ids = [self.record().review_item.id for _ in range(3)]
        self.assertEqual([item.id for item in self.service.list_pending()], ids)
        second = SqliteInboxRepository(self.path)
        second.close()
        self.assertEqual(GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.readonly")
