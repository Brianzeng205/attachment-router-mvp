import json
import tempfile
import unittest
from pathlib import Path

from app.inbox_repository import SqliteInboxRepository
from app.message_ingestion import MessageIngestionService
from app.models import EmailMessage
from app.state import SqliteStateManager


def email(message_id="m-1", thread_id="t-1", received_at="2026-08-17T08:00:00+00:00", body="Private message body"):
    return EmailMessage(
        message_id, "sender@example.test", "Subject", body, received_at, (), thread_id,
        ("recipient@example.test",),
    )


class MessageIngestionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "state.sqlite3"
        self.repository = SqliteInboxRepository(self.path)
        self.service = MessageIngestionService(self.repository)

    def tearDown(self):
        self.repository.connection.close()
        self.tempdir.cleanup()

    def test_message_is_persisted_with_provider_idempotency(self):
        self.assertTrue(self.service.ingest(email()))
        stored = self.repository.get_message_by_provider_id("gmail", "m-1")
        self.assertEqual(stored.provider_thread_id, "t-1")
        self.assertEqual(stored.recipients, ("recipient@example.test",))
        self.assertEqual(stored.ingestion_state, "ingested")
        self.assertEqual(len(stored.content_hash), 64)

    def test_duplicate_poll_does_not_create_another_message_or_audit_events(self):
        self.assertTrue(self.service.ingest(email()))
        self.assertFalse(self.service.ingest(email()))
        count = self.repository.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        audit_count = self.repository.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(audit_count, 2)

    def test_messages_in_same_thread_reuse_conversation_and_newer_updates_latest_time(self):
        self.service.ingest(email("m-1", "t-1", "2026-08-17T08:00:00+00:00"))
        self.service.ingest(email("m-2", "t-1", "2026-08-17T09:00:00+00:00"))
        conversation = self.repository.get_conversation_by_provider_thread_id("gmail", "t-1")
        self.assertEqual(conversation.status, "open")
        self.assertEqual(conversation.latest_message_at, "2026-08-17T09:00:00+00:00")
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)

    def test_different_threads_create_different_conversations(self):
        self.service.ingest(email("m-1", "t-1"))
        self.service.ingest(email("m-2", "t-2"))
        self.assertEqual(self.repository.connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 2)

    def test_existing_processed_attachment_rows_remain_usable(self):
        state = SqliteStateManager(self.path)
        state.mark_processed("old-message", "part:0.1")
        self.assertTrue(state.is_processed("old-message", "part:0.1"))
        self.service.ingest(email())
        self.assertTrue(state.is_processed("old-message", "part:0.1"))
        state.connection.close()

    def test_schema_initialization_is_idempotent(self):
        second = SqliteInboxRepository(self.path)
        self.assertIsNotNone(second.connection.execute("SELECT name FROM sqlite_master WHERE name = 'messages'").fetchone())
        second.connection.close()

    def test_safe_audit_events_are_recorded_without_message_body(self):
        self.service.ingest(email(body="do not store this in audit metadata"))
        rows = self.repository.connection.execute("SELECT event_type, metadata_json FROM audit_events ORDER BY id").fetchall()
        self.assertEqual([row["event_type"] for row in rows], ["message_ingested", "conversation_created"])
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertNotIn("body_text", metadata)
        self.assertNotIn("do not store this in audit metadata", rows[0]["metadata_json"])

    def test_persistence_failure_is_reported_as_an_error_not_success(self):
        class BrokenRepository:
            def ingest(self, message):
                raise RuntimeError("database unavailable")

        summary = MessageIngestionService(BrokenRepository()).ingest_all([email()])
        self.assertEqual(summary.ingested, 0)
        self.assertEqual(summary.duplicates, 0)
        self.assertEqual(summary.errors, 1)
