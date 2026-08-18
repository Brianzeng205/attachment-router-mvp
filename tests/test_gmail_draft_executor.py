import base64
import email
import logging
import tempfile
from dataclasses import replace
from pathlib import Path
from email.policy import default
from unittest import mock

from app.execution_models import ExecutionClaimConflictError
from app.execution_queue_service import ExecutionQueueService
from app.gmail_draft import (
    GMAIL_COMPOSE_SCOPE, CreatedGmailDraft, GmailDraftClient, GmailDraftDefinitiveError,
    GmailDraftOutcomeUnknown, GmailDraftValidationError, GmailComposeAuthorizationError,
    _build_compose_service, build_gmail_reply_command,
)
from app.gmail_draft_executor import GmailDraftExecutor, preview_next
from app.inbox_repository import SqliteInboxRepository
from tests.test_review_queue import ReviewQueueTests


def decode(command):
    return email.message_from_bytes(base64.urlsafe_b64decode(command.raw + "=" * (-len(command.raw) % 4)),
                                    policy=default)


class FakeDraftClient:
    def __init__(self, result=None, error=None):
        self.result = result or CreatedGmailDraft("draft-1", "gmail-message-1", "thread-1")
        self.error, self.calls = error, []

    def create_reply_draft(self, command):
        self.calls.append(command)
        if self.error:
            raise self.error
        return self.result


class Phase8C1GmailDraftExecutorTests(ReviewQueueTests):
    def setUp(self):
        super().setUp()
        self.queue = ExecutionQueueService(self.repository, initial_retry_seconds=0, max_retry_seconds=0)

    def approved(self, body="Approved immutable body ✓"):
        item = self.record().review_item
        self.service.approve(item.id, "operator", approved_draft_body=body)
        return self.repository.get_execution_for_review(item.id)

    def execute(self, client=None):
        client = client or FakeDraftClient()
        outcome = GmailDraftExecutor(
            self.queue, client, authenticated_account="support@example.test",
        ).execute_once("test-worker")
        return client, outcome

    def test_approved_snapshot_builds_unicode_threaded_base64url_mime_and_persists_result(self):
        intent = self.approved("Hei 👋\nApproved exact snapshot")
        client, outcome = self.execute()
        self.assertTrue(outcome.provider_called)
        self.assertEqual(len(client.calls), 1)
        command = client.calls[0]
        message = decode(command)
        self.assertEqual(message["To"], "customer@example.test")
        self.assertEqual(message["Subject"], "Re: Help")
        self.assertEqual(message["In-Reply-To"], "<source-m1@example.test>")
        self.assertEqual(message["References"], "<older@example.test> <source-m1@example.test>")
        self.assertIn("Hei 👋", message.get_content())
        self.assertEqual(command.thread_id, "thread-1")
        self.assertNotIn("=", command.raw)
        self.assertEqual(outcome.intent.status, "completed")
        result = self.repository.get_gmail_draft_result(intent.execution_id)
        self.assertEqual((result.provider_draft_id, result.provider_message_id, result.provider_thread_id),
                         ("draft-1", "gmail-message-1", "thread-1"))

    def test_immutable_execution_body_wins_over_later_review_and_ai_mutation(self):
        intent = self.approved("THE APPROVED SNAPSHOT")
        self.repository.connection.execute("UPDATE human_review_items SET approved_draft_body='changed' WHERE id=?",
                                           (intent.source_review_item_id,))
        self.repository.connection.execute("UPDATE reply_drafts SET body='changed AI' WHERE id=(SELECT reply_draft_id FROM human_review_items WHERE id=?)",
                                           (intent.source_review_item_id,))
        self.repository.connection.commit()
        client, _ = self.execute()
        content = decode(client.calls[0]).get_content()
        self.assertIn("THE APPROVED SNAPSHOT", content)
        self.assertNotIn("changed", content)

    def test_approval_and_preview_do_not_cross_external_boundary(self):
        intent = self.approved()
        client = FakeDraftClient()
        command = preview_next(self.queue, authenticated_account="support@example.test")
        self.assertEqual(command.execution_id, intent.execution_id)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.queue.get(intent.execution_id).status, "pending")

    def test_pending_rejected_and_unapproved_sources_never_reach_client(self):
        pending = self.record().review_item
        rejected = self.record().review_item
        self.service.reject(rejected.id, "operator")
        client, outcome = self.execute(FakeDraftClient())
        self.assertIsNone(outcome.intent)
        self.assertEqual(client.calls, [])
        self.assertIsNotNone(pending)

    def test_missing_threading_metadata_invalid_recipient_injection_self_reply_and_size_fail_pre_dispatch(self):
        cases = (
            {"provider_thread_id": ""}, {"in_reply_to_header": None},
            {"recipient": "bad-address"}, {"recipient": "victim@example.test\r\nBcc: x@example.test"},
            {"recipient": "support@example.test"}, {"subject": "Hi\r\nBcc: x@example.test"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                intent = replace(self.approved(), **changes)
                with self.assertRaises(GmailDraftValidationError):
                    build_gmail_reply_command(intent, authenticated_account="support@example.test")
                # Remove this item so the next subtest can create another pending execution.
                self.repository.connection.execute("UPDATE execution_intents SET status='failed', failure_code='test' WHERE execution_id=?", (intent.execution_id,))
                self.repository.connection.commit()
        oversized = self.approved("x" * 50000)
        with self.assertRaises(GmailDraftValidationError):
            build_gmail_reply_command(oversized, authenticated_account="support@example.test", max_mime_bytes=100)

    def test_confirmed_result_is_terminal_and_repeated_worker_does_not_recreate(self):
        self.approved()
        client, first = self.execute()
        second = GmailDraftExecutor(self.queue, client, authenticated_account="support@example.test").execute_once("again")
        self.assertEqual(first.intent.status, "completed")
        self.assertIsNone(second.intent)
        self.assertEqual(len(client.calls), 1)

    def test_ambiguous_timeout_is_durable_nonclaimable_and_requires_reconciliation(self):
        intent = self.approved()
        client = FakeDraftClient(error=GmailDraftOutcomeUnknown("timeout after dispatch"))
        _, outcome = self.execute(client)
        self.assertEqual((outcome.intent.status, len(client.calls)), ("outcome_unknown", 1))
        other = SqliteInboxRepository(self.path)
        try:
            restarted = ExecutionQueueService(other)
            self.assertEqual(restarted.get(intent.execution_id).status, "outcome_unknown")
            second = GmailDraftExecutor(restarted, client, authenticated_account="support@example.test").execute_once("restart")
            self.assertIsNone(second.intent)
            self.assertEqual(len(client.calls), 1)
            reconciled = restarted.reconcile_gmail_draft(
                intent.execution_id, draft_id="manually-found", thread_id="thread-1",
            )
            self.assertEqual(reconciled.status, "completed")
            self.assertEqual(restarted.get_gmail_draft_result(intent.execution_id).reconciliation_method,
                             "operator_confirmed")
        finally:
            other.close()

    def test_definitive_permanent_and_retryable_failures_are_classified(self):
        self.approved()
        permanent = FakeDraftClient(error=GmailDraftDefinitiveError("gmail_authorization_failed"))
        _, outcome = self.execute(permanent)
        self.assertEqual(outcome.intent.status, "failed")
        self.approved()
        transient = FakeDraftClient(error=GmailDraftDefinitiveError("gmail_rate_limited", retryable=True,
                                                                    provider_status=429))
        _, outcome = self.execute(transient)
        self.assertEqual(outcome.intent.status, "retry_wait")

    def test_stale_claim_cannot_persist_result_or_complete(self):
        self.approved()
        claimed = self.queue.claim_next("owner")
        self.repository.connection.execute(
            "UPDATE execution_intents SET claim_token='different-token-123456789', claimed_by='other' WHERE execution_id=?",
            (claimed.execution_id,),
        )
        self.repository.connection.commit()
        with self.assertRaises(ExecutionClaimConflictError):
            self.queue.mark_gmail_draft_completed(claimed.execution_id, claimed.claim_token,
                                                  draft_id="d", message_id="m", thread_id="thread-1")
        self.assertIsNone(self.repository.get_gmail_draft_result(claimed.execution_id))

    def test_logs_omit_approved_content_recipient_and_tokens(self):
        secret = "PRIVATE APPROVED CONTENT TOKEN refresh-token@example.test"
        self.approved(secret)
        with self.assertLogs(level=logging.INFO) as captured:
            self.execute()
        output = "\n".join(captured.output)
        self.assertNotIn(secret, output)
        self.assertNotIn("customer@example.test", output)
        self.assertNotIn("refresh-token", output)

    def test_no_claude_or_gmail_call_from_approval_status_or_console_start(self):
        with mock.patch("app.gmail_draft.GmailDraftClient.create_reply_draft") as create, \
             mock.patch("app.claude_reply_draft_generator.ClaudeGroundedReplyGenerator") as claude:
            intent = self.approved()
            self.queue.status_counts()
            from app.review_console import create_app
            create_app(self.path)
        create.assert_not_called()
        claude.assert_not_called()
        self.assertEqual(self.queue.get(intent.execution_id).status, "pending")

    def test_compose_scope_is_exact_least_privilege_and_adapter_surface_has_no_send_modify(self):
        self.assertEqual(GMAIL_COMPOSE_SCOPE, "https://www.googleapis.com/auth/gmail.compose")
        forbidden = ("send", "modify", "delete", "trash")
        for name in forbidden:
            self.assertFalse(hasattr(GmailDraftClient, name))

    def test_narrow_adapter_calls_only_drafts_create_exactly_once(self):
        request = mock.Mock()
        request.execute.return_value = {
            "id": "draft-id", "message": {"id": "message-id", "threadId": "thread-1"},
        }
        drafts = mock.Mock()
        drafts.create.return_value = request
        users = mock.Mock()
        users.drafts.return_value = drafts
        service = mock.Mock()
        service.users.return_value = users
        command = build_gmail_reply_command(self.approved(), authenticated_account="support@example.test")
        result = GmailDraftClient(service).create_reply_draft(command)
        self.assertEqual(result.draft_id, "draft-id")
        drafts.create.assert_called_once_with(
            userId="me", body={"message": {"raw": command.raw, "threadId": "thread-1"}},
        )
        self.assertFalse(users.messages.called)
        self.assertFalse(drafts.send.called)

    def test_readonly_only_compose_token_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token.json"
            token.write_text("readonly-token-placeholder", encoding="utf-8")
            before = token.read_bytes()
            credentials = mock.Mock()
            credentials.has_scopes.return_value = False
            with mock.patch("google.oauth2.credentials.Credentials.from_authorized_user_file",
                            return_value=credentials), self.assertRaises(GmailComposeAuthorizationError):
                _build_compose_service(Path(directory) / "client.json", token)
            self.assertEqual(token.read_bytes(), before)

    def test_phase8b_action_migration_preserves_provenance_and_snapshot(self):
        intent = self.approved("legacy approved snapshot")
        connection = self.repository.connection
        connection.execute("DROP TRIGGER trg_gmail_draft_completion_requires_result")
        connection.execute("DROP TRIGGER trg_gmail_draft_completed_insert_requires_result")
        connection.execute("DROP TRIGGER trg_gmail_draft_result_requires_execution")
        connection.execute("DROP TABLE gmail_draft_results")
        connection.execute("DROP TABLE execution_events")
        connection.execute("ALTER TABLE execution_intents RENAME TO new_execution_intents")
        connection.execute("""CREATE TABLE execution_intents (
            execution_id TEXT PRIMARY KEY, source_review_item_id INTEGER NOT NULL UNIQUE,
            conversation_id INTEGER NOT NULL, provider_thread_id TEXT NOT NULL,
            in_reply_to_provider_message_id TEXT NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN ('send_approved_reply')),
            approved_body TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('pending','processing','retry_wait','completed','failed')),
            attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, claim_token TEXT,
            claimed_by TEXT, claimed_at TEXT, lease_expires_at TEXT, completed_at TEXT,
            failure_code TEXT, failure_metadata_json TEXT, schema_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        connection.execute("""INSERT INTO execution_intents
            SELECT execution_id, source_review_item_id, conversation_id, provider_thread_id,
            in_reply_to_provider_message_id, 'send_approved_reply', approved_body, idempotency_key,
            status, attempt_count, next_attempt_at, claim_token, claimed_by, claimed_at,
            lease_expires_at, completed_at, failure_code, failure_metadata_json, 1, created_at, updated_at
            FROM new_execution_intents""")
        connection.execute("""CREATE TABLE execution_events (
            id INTEGER PRIMARY KEY, execution_id TEXT NOT NULL, event_type TEXT NOT NULL,
            attempt_count INTEGER NOT NULL, failure_code TEXT, created_at TEXT NOT NULL)""")
        connection.execute("DROP TABLE new_execution_intents")
        connection.commit()
        from app.migrations import initialize_schema
        initialize_schema(connection)
        migrated = self.repository.get_execution_intent(intent.execution_id)
        self.assertEqual(migrated.action_type, "create_gmail_draft")
        self.assertEqual(migrated.legacy_action_type, "send_approved_reply")
        self.assertEqual(migrated.approved_body, "legacy approved snapshot")
        initialize_schema(connection)
