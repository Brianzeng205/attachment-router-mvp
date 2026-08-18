import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.execution_models import ExecutionClaimConflictError, ExecutionEligibilityError
from app.execution_queue_service import ExecutionQueueService
from app.inbox_repository import SqliteInboxRepository
from tests.test_review_queue import ReviewQueueTests


class MutableClock:
    def __init__(self, value=None):
        self.value = value or datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class Phase8BExecutionQueueTests(ReviewQueueTests):
    def setUp(self):
        super().setUp()
        self.clock = MutableClock()
        self.queue = ExecutionQueueService(
            self.repository, clock=self.clock, lease_seconds=60, max_attempts=3,
            initial_retry_seconds=10, max_retry_seconds=40,
        )

    def approved(self, body="Approved exact snapshot"):
        item = self.record().review_item
        self.service.approve(item.id, "operator", approved_draft_body=body)
        return item, self.queue.get(self.repository.get_execution_for_review(item.id).execution_id)

    def test_approval_atomically_creates_immutable_unicode_intent_and_preserves_ai_draft(self):
        body = "Hei 👋\nExact approved response"
        item, intent = self.approved(body)
        self.assertEqual(intent.approved_body, body)
        self.assertEqual(intent.source_review_item_id, item.id)
        self.assertEqual(intent.action_type, "create_gmail_draft")
        self.assertEqual(intent.status, "pending")
        self.assertEqual(self.service.detail(item.id).original_draft_body, "Confirmed local reply body.")
        self.repository.connection.execute(
            "UPDATE human_review_items SET approved_draft_body='later mutation' WHERE id=?", (item.id,),
        )
        self.repository.connection.commit()
        self.assertEqual(self.queue.get(intent.execution_id).approved_body, body)

    def test_pending_rejected_missing_and_oversized_legacy_reviews_cannot_enqueue(self):
        pending = self.record().review_item
        rejected = self.record().review_item
        self.service.reject(rejected.id, "operator")
        for review_id in (pending.id, rejected.id):
            with self.subTest(review_id=review_id), self.assertRaises(ExecutionEligibilityError):
                self.queue.enqueue_approved_review(review_id)
        legacy = self.record().review_item
        self.repository.connection.execute(
            "UPDATE human_review_items SET status='approved', approved_draft_body=NULL WHERE id=?", (legacy.id,),
        )
        self.repository.connection.commit()
        with self.assertRaises(ExecutionEligibilityError):
            self.queue.enqueue_approved_review(legacy.id)

    def test_repeated_and_concurrent_enqueue_returns_one_durable_intent(self):
        item, expected = self.approved()
        results = [self.queue.enqueue_approved_review(item.id).intent.execution_id]
        errors = []

        def enqueue_again():
            repo = SqliteInboxRepository(self.path)
            try:
                results.append(ExecutionQueueService(repo).enqueue_approved_review(item.id).intent.execution_id)
            except Exception as exc:  # pragma: no cover - assertion reports unexpected race failures
                errors.append(exc)
            finally:
                repo.close()

        threads = [threading.Thread(target=enqueue_again) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(set(results), {expected.execution_id})
        self.assertEqual(self.repository.connection.execute(
            "SELECT COUNT(*) FROM execution_intents WHERE source_review_item_id=?", (item.id,),
        ).fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.connection.execute(
                """INSERT INTO execution_intents (
                   execution_id, source_review_item_id, conversation_id, provider_thread_id,
                   in_reply_to_provider_message_id, action_type, approved_body, idempotency_key,
                   status, attempt_count, created_at, updated_at)
                   SELECT 'exec_00000000000000000000000000000000', source_review_item_id,
                          conversation_id, provider_thread_id, in_reply_to_provider_message_id,
                          action_type, approved_body, 'different-key', status, attempt_count,
                          created_at, updated_at FROM execution_intents WHERE execution_id=?""",
                (expected.execution_id,),
            )

    def test_legacy_reconciliation_is_safe_and_idempotent(self):
        item = self.record().review_item
        self.repository.connection.execute(
            "UPDATE human_review_items SET status='approved', reviewer_id='legacy', resolved_at=CURRENT_TIMESTAMP, "
            "approved_draft_body='legacy snapshot' WHERE id=?", (item.id,),
        )
        self.repository.connection.commit()
        first = self.queue.reconcile_approved_reviews()
        second = self.queue.reconcile_approved_reviews()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(first[0].approved_body, "legacy snapshot")

    def test_claim_is_atomic_and_only_one_worker_gets_item(self):
        _, intent = self.approved()
        first = self.queue.claim_next("worker-a")
        second = self.queue.claim_next("worker-b")
        self.assertEqual(first.execution_id, intent.execution_id)
        self.assertIsNone(second)
        self.assertEqual(first.status, "processing")
        self.assertTrue(first.claim_token)

    def test_two_repository_connections_cannot_both_claim_one_item(self):
        self.approved()
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def claim(worker):
            repo = SqliteInboxRepository(self.path)
            try:
                barrier.wait()
                results.append(ExecutionQueueService(repo, clock=self.clock).claim_next(worker))
            except Exception as exc:
                errors.append(exc)
            finally:
                repo.close()

        threads = [threading.Thread(target=claim, args=(f"worker-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_completion_is_terminal_and_stale_or_duplicate_completion_is_rejected(self):
        self.approved()
        claimed = self.queue.claim_next("worker-a")
        completed = self.queue.mark_gmail_draft_completed(
            claimed.execution_id, claimed.claim_token, draft_id="draft-a", message_id="message-a",
            thread_id=claimed.provider_thread_id,
        )
        self.assertEqual(completed.status, "completed")
        self.assertIsNotNone(completed.completed_at)
        self.assertIsNone(self.queue.claim_next("worker-b"))
        with self.assertRaises(ExecutionClaimConflictError):
            self.queue.mark_gmail_draft_completed(
                claimed.execution_id, claimed.claim_token, draft_id="draft-b", message_id="message-b",
                thread_id=claimed.provider_thread_id,
            )

    def test_retry_schedule_due_time_and_max_attempts(self):
        self.approved()
        claimed = self.queue.claim_next("worker")
        retry = self.queue.mark_failed(claimed.execution_id, claimed.claim_token,
                                       retryable=True, error_code="temporary_failure")
        self.assertEqual((retry.status, retry.attempt_count), ("retry_wait", 1))
        self.assertIsNone(self.queue.claim_next("early"))
        self.clock.advance(10)
        second = self.queue.claim_next("second")
        self.assertIsNotNone(second)
        retry2 = self.queue.mark_failed(second.execution_id, second.claim_token,
                                        retryable=True, error_code="temporary_failure")
        self.clock.advance(20)
        third = self.queue.claim_next("third")
        terminal = self.queue.mark_failed(third.execution_id, third.claim_token,
                                          retryable=True, error_code="temporary_failure")
        self.assertEqual((retry2.status, terminal.status, terminal.attempt_count),
                         ("retry_wait", "failed", 3))
        self.assertIsNone(self.queue.claim_next("never"))

    def test_permanent_failure_is_terminal_and_metadata_is_sanitized(self):
        self.approved()
        claimed = self.queue.claim_next("worker")
        failed = self.queue.mark_failed(
            claimed.execution_id, claimed.claim_token, retryable=False, error_code="invalid_action",
            metadata={"provider_status": 400, "operation": "dispatch"},
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.failure_metadata["provider_status"], 400)
        with self.assertRaises(ValueError):
            self.queue.mark_failed(claimed.execution_id, claimed.claim_token, retryable=False,
                                   error_code="invalid_action", metadata={"email_body": "secret"})

    def test_expired_claim_recovery_rotates_ownership_and_blocks_stale_worker(self):
        self.approved()
        stale = self.queue.claim_next("stale-worker")
        self.assertEqual(self.queue.recover_expired_claims(), [])
        self.clock.advance(61)
        self.assertEqual(self.queue.recover_expired_claims(), [stale.execution_id])
        self.assertIsNone(self.queue.claim_next("too-early"))
        self.clock.advance(10)
        current = self.queue.claim_next("current-worker")
        self.assertNotEqual(current.claim_token, stale.claim_token)
        with self.assertRaises(ExecutionClaimConflictError):
            self.queue.mark_gmail_draft_completed(
                stale.execution_id, stale.claim_token, draft_id="stale-draft", message_id=None,
                thread_id=stale.provider_thread_id,
            )
        with self.assertRaises(ExecutionClaimConflictError):
            self.queue.mark_failed(stale.execution_id, stale.claim_token, retryable=False,
                                   error_code="stale_worker")
        self.assertEqual(self.queue.mark_gmail_draft_completed(
            current.execution_id, current.claim_token, draft_id="current-draft", message_id=None,
            thread_id=current.provider_thread_id,
        ).status, "completed")

    def test_restart_persistence_counts_and_audit_history(self):
        _, intent = self.approved()
        other = SqliteInboxRepository(self.path)
        try:
            queue = ExecutionQueueService(other, clock=self.clock)
            self.assertEqual(queue.get(intent.execution_id).approved_body, intent.approved_body)
            self.assertEqual(queue.status_counts()["pending"], 1)
            events = other.list_execution_events(intent.execution_id)
            self.assertEqual(events[0]["event_type"], "created")
        finally:
            other.close()

    def test_phase_8b_state_flow_invokes_no_gmail_or_claude_and_logs_no_body(self):
        secret = "PRIVATE-APPROVED-BODY-MUST-NOT-LOG"
        with mock.patch("app.gmail_client.GmailClient") as gmail, \
             mock.patch("app.claude_reply_draft_generator.ClaudeGroundedReplyGenerator") as claude, \
             self.assertLogs(level=logging.INFO) as captured:
            _, intent = self.approved(secret)
            claimed = self.queue.claim_next("no-side-effects")
            self.queue.mark_gmail_draft_completed(
                intent.execution_id, claimed.claim_token, draft_id="test-draft", message_id=None,
                thread_id=intent.provider_thread_id,
            )
        gmail.assert_not_called()
        claude.assert_not_called()
        self.assertNotIn(secret, "\n".join(captured.output))

    def test_schema_migration_is_repeatable_and_contains_claim_indexes(self):
        other = SqliteInboxRepository(self.path)
        other.close()
        indexes = {row[1] for row in self.repository.connection.execute(
            "PRAGMA index_list(execution_intents)"
        ).fetchall()}
        self.assertIn("idx_execution_claimable", indexes)
        self.assertIn("idx_execution_processing_lease", indexes)
