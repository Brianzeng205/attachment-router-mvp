import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.claude_conversation_analyzer import ClaudeConversationAnalyzer
from app.conversation_analysis_service import ConversationAnalysisService
from app.conversation_models import ConversationAnalysis
from app.errors import ConversationAnalyzerResponseError
from app.gmail_client import GMAIL_READONLY_SCOPE
from app.inbox_repository import SqliteInboxRepository
from app.message_ingestion import MessageIngestionService
from app.models import EmailMessage
from app.thread_context import ThreadContextBuilder


def email(number, thread="thread-1", received=None, body=None):
    return EmailMessage(
        f"m-{number}", "customer@example.test", "Order discussion", body or f"Message {number}",
        received or f"2026-08-17T0{number}:00:00+00:00", (), thread, ("support@example.test",),
    )


def payload(**overrides):
    value = {
        "conversation_summary": "Customer is discussing order ORD-42.", "current_intent": "request_refund",
        "priority": "high", "urgency": "high", "unresolved_requests": ["Refund order ORD-42."],
        "resolved_points": [], "order_numbers": ["ORD-42"], "relevant_dates": [],
        "latest_sender_request": "Refund order ORD-42.", "confidence": 0.92, "needs_human": False,
        "human_reason": None, "recommended_action": "draft_reply",
    }
    value.update(overrides)
    return value


class FakeMessages:
    def __init__(self, response): self.response, self.requests = response, []
    def create(self, **kwargs): self.requests.append(kwargs); return self.response


class FakeClient:
    def __init__(self, value):
        text = value if isinstance(value, str) else json.dumps(value)
        self.messages = FakeMessages(SimpleNamespace(content=[SimpleNamespace(text=text)]))


class StubAnalyzer:
    def __init__(self, result=None, error=None):
        self.result, self.error = result or ConversationAnalysis.from_mapping(payload()), error
        self.calls, self.contexts = 0, []
    def analyze(self, context):
        self.calls += 1
        self.contexts.append(context)
        if self.error: raise self.error
        return self.result


class ConversationFixture(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = SqliteInboxRepository(Path(self.tempdir.name) / "state.sqlite3")
        self.ingestion = MessageIngestionService(self.repository)

    def tearDown(self):
        self.repository.close()
        self.tempdir.cleanup()

    def ingest(self, *messages):
        for message in messages: self.ingestion.ingest(message)
        return self.repository.get_conversation_by_provider_thread_id("gmail", messages[0].thread_id)

    def service(self, analyzer, *, prompt="v1", version="v1", builder=None):
        return ConversationAnalysisService(
            self.repository, builder or ThreadContextBuilder(10, 1_000, "builder-v1"), analyzer,
            analyzer_name="stub", analyzer_version=version, model="stub-model", prompt_version=prompt,
        )


class ThreadContextBuilderTests(ConversationFixture):
    def test_messages_are_chronological_and_context_identifies_latest(self):
        conversation = self.ingest(email(2, received="2026-08-17T02:00:00+00:00"),
                                   email(1, received="2026-08-17T01:00:00+00:00"))
        context = ThreadContextBuilder(10, 1_000).build(conversation, self.repository.list_messages_for_conversation(conversation.id))
        self.assertEqual([item.provider_message_id for item in context.messages], ["m-1", "m-2"])
        self.assertEqual(context.latest_message_id, context.messages[-1].id)
        self.assertEqual((context.total_message_count, context.included_message_count, context.truncated), (2, 2, False))

    def test_message_bound_preserves_newest_messages_in_chronological_order(self):
        conversation = self.ingest(*[email(n) for n in range(1, 5)])
        context = ThreadContextBuilder(2, 1_000).build(conversation, self.repository.list_messages_for_conversation(conversation.id))
        self.assertEqual([item.provider_message_id for item in context.messages], ["m-3", "m-4"])
        self.assertTrue(context.truncated)

    def test_character_bound_is_deterministic_and_explicit(self):
        conversation = self.ingest(email(1, body="abcdefgh"), email(2, body="ijklmnop"))
        messages = self.repository.list_messages_for_conversation(conversation.id)
        builder = ThreadContextBuilder(10, 10)
        first, second = builder.build(conversation, messages), builder.build(conversation, messages)
        self.assertEqual([item.body_text for item in first.messages], ["ab", "ijklmnop"])
        self.assertEqual(first.context_fingerprint, second.context_fingerprint)
        self.assertTrue(first.truncated)
        total_text = sum(len(item.sender) + len(item.subject) + len(item.body_text) +
                         sum(len(recipient) for recipient in item.recipients) for item in first.messages)
        self.assertLessEqual(total_text, 10)


class ClaudeConversationAnalyzerTests(unittest.TestCase):
    def context(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SqliteInboxRepository(Path(directory) / "state.sqlite3")
            try:
                ingestion = MessageIngestionService(repository)
                ingestion.ingest(email(1, body="Where is ORD-42?"))
                ingestion.ingest(email(2, body="Ignore my prior delivery request; please refund ORD-42."))
                conversation = repository.get_conversation_by_provider_thread_id("gmail", "thread-1")
                return ThreadContextBuilder(10, 1_000).build(conversation, repository.list_messages_for_conversation(conversation.id))
            finally:
                repository.close()

    def test_invalid_controlled_values_missing_fields_and_malformed_json_are_rejected(self):
        for value in (payload(priority="rush"), payload(confidence=1.1), {"priority": "high"}, "not json"):
            with self.subTest(value=value):
                with self.assertRaises(ConversationAnalyzerResponseError):
                    ClaudeConversationAnalyzer(FakeClient(value), "test").analyze(self.context())

    def test_prompt_treats_injection_as_data_and_later_messages_can_supersede(self):
        client = FakeClient(payload())
        result = ClaudeConversationAnalyzer(client, "test").analyze(self.context())
        request = client.messages.requests[0]
        sent = json.loads(request["messages"][0]["content"])
        self.assertEqual(sent["messages"][-1]["body_text"], "Ignore my prior delivery request; please refund ORD-42.")
        self.assertEqual(result.current_intent, "request_refund")
        self.assertIn("untrusted external data", request["system"])
        self.assertIn("supersede", request["system"])
        schema = request["output_config"]["format"]["schema"]
        self.assertIn("high", schema["properties"]["priority"]["enum"])


class ConversationAnalysisServiceTests(ConversationFixture):
    def test_valid_analysis_persists_and_duplicate_context_skips_call(self):
        conversation = self.ingest(email(1), email(2))
        analyzer = StubAnalyzer()
        service = self.service(analyzer)
        self.assertEqual(service.analyze_all([conversation]).analyzed, 1)
        self.assertEqual(service.analyze_all([conversation]).skipped, 1)
        self.assertEqual(analyzer.calls, 1)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM conversation_analyses").fetchone()[0], 1)
        self.assertEqual(self.repository.connection.execute("SELECT status FROM conversation_analysis_runs").fetchone()[0], "succeeded")

    def test_new_message_or_version_change_permits_reanalysis(self):
        conversation = self.ingest(email(1))
        analyzer = StubAnalyzer()
        self.service(analyzer).analyze_all([conversation])
        before = ThreadContextBuilder(10, 1_000, "builder-v1").build(
            conversation, self.repository.list_messages_for_conversation(conversation.id),
        ).context_fingerprint
        self.ingest(email(2))
        after = ThreadContextBuilder(10, 1_000, "builder-v1").build(
            conversation, self.repository.list_messages_for_conversation(conversation.id),
        ).context_fingerprint
        self.assertNotEqual(before, after)
        self.service(analyzer).analyze_all([conversation])
        self.service(analyzer, prompt="v2").analyze_all([conversation])
        self.service(analyzer, version="v2").analyze_all([conversation])
        self.assertEqual(analyzer.calls, 4)
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM conversation_analysis_runs").fetchone()[0], 4)

    def test_failed_run_has_no_successful_analysis_and_audit_omits_thread_body(self):
        body = "secret first thread body must not be audited"
        later_body = "secret latest thread body must not be audited"
        conversation = self.ingest(email(1, body=body), email(2, body=later_body))
        summary = self.service(StubAnalyzer(error=RuntimeError("provider unavailable"))).analyze_all([conversation])
        self.assertEqual(summary.errors, 1)
        self.assertEqual(self.repository.connection.execute("SELECT status FROM conversation_analysis_runs").fetchone()[0], "failed")
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM conversation_analyses").fetchone()[0], 0)
        audit = " ".join(row[0] for row in self.repository.connection.execute("SELECT metadata_json FROM audit_events").fetchall())
        self.assertNotIn(body, audit)
        self.assertNotIn(later_body, audit)
        self.assertIn("conversation_analysis_failed", [row[0] for row in self.repository.connection.execute("SELECT event_type FROM audit_events").fetchall()])

    def test_gmail_scope_remains_readonly(self):
        self.assertEqual(GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.readonly")
