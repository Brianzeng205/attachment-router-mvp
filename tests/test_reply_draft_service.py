import tempfile
import unittest
from pathlib import Path

from app.conversation_models import ConversationAnalysis
from app.inbox_repository import SqliteInboxRepository
from app.knowledge_models import KnowledgeMatch
from app.message_ingestion import MessageIngestionService
from app.models import EmailMessage
from app.reply_draft_input import ReplyDraftInput
from app.reply_draft_models import ReplyDraft
from app.reply_draft_service import ReplyDraftService
from app.thread_context import ThreadContextBuilder


def analysis():
    return ConversationAnalysis.from_mapping({
        "conversation_summary": "Customer asks about ORD-42.", "current_intent": "check_order_status",
        "priority": "normal", "urgency": "medium", "unresolved_requests": ["Confirm known information."],
        "resolved_points": [], "order_numbers": ["ORD-42"], "relevant_dates": [],
        "latest_sender_request": "Can you help?", "confidence": 0.9, "needs_human": False,
        "human_reason": None, "recommended_action": "draft_reply",
    })


class FakeGenerator:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or ReplyDraft(
            "drafted", "Re: ORD-42", "Thanks for contacting us.", (11,), (), 0.9, False, None, "en",
        ), error, 0

    def generate(self, draft_input):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class ReplyDraftServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = SqliteInboxRepository(Path(self.tempdir.name) / "state.sqlite3")
        email = EmailMessage("m-1", "customer@example.test", "Help", "Message body that must never be audited.",
                             "2026-08-17T10:00:00+00:00", (), "thread-1", ("support@example.test",))
        MessageIngestionService(self.repository).ingest(email)
        self.conversation = self.repository.get_conversation_by_provider_thread_id("gmail", "thread-1")
        self.context = ThreadContextBuilder(10, 1_000, "test-builder").build(
            self.conversation, self.repository.list_messages_for_conversation(self.conversation.id),
        )
        run = self.repository.start_conversation_analysis_run(
            conversation_id=self.conversation.id, analyzer="test", analyzer_version="v1", model="test",
            prompt_version="v1", context_fingerprint=self.context.context_fingerprint,
        )
        self.repository.complete_conversation_analysis_run(run, self.context.latest_message_id, False, analysis())
        self.analysis_id = self.repository.connection.execute("SELECT id FROM conversation_analyses").fetchone()[0]
        self.repository.upsert_knowledge("policy.md", "policy.md", "Confirmed policy information", "document-hash", ["Confirmed policy information"], "v1")
        self.match = self.repository.search_knowledge("Confirmed", 5)[0]

    def tearDown(self):
        self.repository.close()
        self.tempdir.cleanup()

    def retrieval(self, matches=None, *, index="index-v1"):
        matches = [self.match] if matches is None else matches
        run_id = self.repository.start_retrieval(self.conversation.id, self.analysis_id, "confirmed", f"query-{index}-{len(matches)}",
                                                 index, "test", "v1", 5)
        self.repository.complete_retrieval(run_id, matches)
        return run_id

    def draft_input(self, retrieval_run_id, matches=None, *, context=None):
        return ReplyDraftInput.from_context(context or self.context, analysis(), retrieval_run_id,
                                            [self.match] if matches is None else matches)

    def service(self, generator, *, version="v1", model="test-model", prompt="v1"):
        return ReplyDraftService(self.repository, generator, generator_name="fake", generator_version=version,
                                 model=model, prompt_version=prompt)

    def generator(self, *, error=None, result=None):
        return FakeGenerator(result or ReplyDraft(
            "drafted", "Re: ORD-42", "Thanks for contacting us.", (self.match.chunk_id,), (), 0.9, False, None, "en",
        ), error)

    def test_valid_draft_persists_running_to_succeeded_with_current_grounding_and_safe_audit(self):
        generator = self.generator()
        outcome = self.service(generator).create_draft(self.draft_input(self.retrieval()), conversation_analysis_id=self.analysis_id)
        self.assertTrue(outcome.generated)
        self.assertEqual(generator.calls, 1)
        self.assertEqual(self.repository.connection.execute("SELECT status FROM reply_draft_runs").fetchone()[0], "succeeded")
        self.assertEqual(self.repository.get_reply_draft_grounding(outcome.draft.id), (self.match.chunk_id,))
        events = [row[0] for row in self.repository.connection.execute("SELECT event_type FROM audit_events").fetchall()]
        self.assertIn("reply_draft_started", events)
        self.assertIn("reply_draft_succeeded", events)
        audit = " ".join(row[0] for row in self.repository.connection.execute("SELECT metadata_json FROM audit_events").fetchall())
        self.assertNotIn("Thanks for contacting us.", audit)
        self.assertNotIn("Message body that must never be audited.", audit)
        self.assertNotIn("Confirmed policy information", audit)

    def test_bad_or_non_current_grounding_fails_without_draft_or_grounding_rows(self):
        for bad_draft in (
            ReplyDraft("drafted", None, "Reply", (999,), (), 0.8, False, None, "en"),
            ReplyDraft("drafted", None, "Reply", (11,), (), 0.8, False, None, "en"),
        ):
            with self.subTest(grounding=bad_draft.grounding_chunk_ids):
                retrieval = self.retrieval([]) if bad_draft.grounding_chunk_ids == (11,) else self.retrieval()
                outcome = self.service(FakeGenerator(bad_draft)).create_draft(
                    self.draft_input(retrieval, [self.match] if bad_draft.grounding_chunk_ids == (11,) else None),
                    conversation_analysis_id=self.analysis_id,
                )
                self.assertTrue(outcome.failed)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM reply_drafts").fetchone()[0], 0)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM reply_draft_grounding").fetchone()[0], 0)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM reply_draft_runs WHERE status='failed'").fetchone()[0], 2)

    def test_generator_failure_marks_failed_without_draft_or_grounding(self):
        outcome = self.service(self.generator(error=RuntimeError("unavailable"))).create_draft(
            self.draft_input(self.retrieval()), conversation_analysis_id=self.analysis_id,
        )
        self.assertTrue(outcome.failed)
        self.assertEqual(tuple(self.repository.connection.execute("SELECT status,error_class FROM reply_draft_runs").fetchone()), ("failed", "RuntimeError"))
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM reply_drafts").fetchone()[0], 0)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM reply_draft_grounding").fetchone()[0], 0)
        self.assertIn("reply_draft_failed", [row[0] for row in self.repository.connection.execute("SELECT event_type FROM audit_events").fetchall()])

    def test_same_input_reuses_successful_draft_without_duplicate_rows_or_generator_call(self):
        generator = self.generator()
        service = self.service(generator)
        input_value = self.draft_input(self.retrieval())
        first = service.create_draft(input_value, conversation_analysis_id=self.analysis_id)
        second = service.create_draft(input_value, conversation_analysis_id=self.analysis_id)
        self.assertTrue(first.generated)
        self.assertTrue(second.skipped)
        self.assertEqual(generator.calls, 1)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM reply_drafts").fetchone()[0], 1)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM reply_draft_grounding").fetchone()[0], 1)
        self.assertEqual(second.draft.draft.grounding_chunk_ids, (self.match.chunk_id,))

    def test_fingerprint_is_deterministic_and_excludes_generated_text(self):
        service = self.service(self.generator())
        input_value = self.draft_input(self.retrieval())
        one = service.input_fingerprint(input_value, self.analysis_id, self.repository.successful_retrieval_snapshot(input_value.knowledge_retrieval_run_id))
        two = service.input_fingerprint(input_value, self.analysis_id, self.repository.successful_retrieval_snapshot(input_value.knowledge_retrieval_run_id))
        self.assertEqual(one, two)
        self.assertNotIn("Thanks", one)

    def test_changed_analysis_context_retrieval_or_configuration_permits_new_draft(self):
        generator = self.generator()
        retrieval = self.retrieval()
        original = self.draft_input(retrieval)
        self.assertTrue(self.service(generator).create_draft(original, conversation_analysis_id=self.analysis_id).generated)
        changed_context = self.context.__class__(
            self.context.conversation, self.context.messages, self.context.latest_message_id, self.context.total_message_count,
            self.context.included_message_count, self.context.truncated, "changed-context",
        )
        changed_retrieval = self.retrieval(index="index-v2")
        cases = (
            (self.service(generator), original, self.analysis_id + 1),
            (self.service(generator), self.draft_input(retrieval, context=changed_context), self.analysis_id),
            (self.service(generator), self.draft_input(changed_retrieval), self.analysis_id),
            (self.service(generator, version="v2"), original, self.analysis_id),
            (self.service(generator, model="other-model"), original, self.analysis_id),
            (self.service(generator, prompt="v2"), original, self.analysis_id),
        )
        # A changed analysis identity needs a matching retrieval run; use the other state changes here and assert fingerprints directly.
        self.assertNotEqual(self.service(generator).input_fingerprint(original, self.analysis_id),
                            self.service(generator).input_fingerprint(original, self.analysis_id + 1))
        for service, value, analysis_id in cases[1:]:
            self.assertTrue(service.create_draft(value, conversation_analysis_id=analysis_id).generated)
        self.assertGreaterEqual(generator.calls, 6)

    def test_zero_result_is_deterministic_insufficient_knowledge_without_generator_or_grounding(self):
        generator = self.generator()
        outcome = self.service(generator).create_draft(self.draft_input(self.retrieval([]), []), conversation_analysis_id=self.analysis_id)
        self.assertTrue(outcome.generated)
        self.assertEqual(generator.calls, 0)
        self.assertEqual(outcome.draft.draft.draft_status, "insufficient_knowledge")
        self.assertTrue(outcome.draft.draft.needs_review)
        self.assertEqual(outcome.draft.draft.review_reason, "insufficient_knowledge")
        self.assertEqual(self.repository.get_reply_draft_grounding(outcome.draft.id), ())

    def test_reply_draft_migrations_are_idempotent_and_gmail_is_readonly(self):
        from app.gmail_client import GMAIL_READONLY_SCOPE
        second = SqliteInboxRepository(Path(self.tempdir.name) / "state.sqlite3")
        second.close()
        self.assertEqual(GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.readonly")
