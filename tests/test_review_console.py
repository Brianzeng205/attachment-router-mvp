import logging
import re
from unittest import mock

from app.inbox_repository import SqliteInboxRepository
from app.review_console import create_app
from app.review_models import ReviewConflictError, ReviewNotFoundError, ReviewValidationError
from app.review_queue_service import MAX_APPROVED_DRAFT_CHARS, ReviewQueueService
from tests.test_review_queue import ReviewQueueTests


class Phase8AReviewConsoleTests(ReviewQueueTests):
    """Uses the established persisted pipeline fixture, then exercises console-only behavior."""

    def setUp(self):
        super().setUp()
        self.app = create_app(self.path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_queue_filters_pending_approved_and_rejected(self):
        pending = self.record().review_item
        approved = self.record().review_item
        rejected = self.record().review_item
        self.service.approve(approved.id, "reviewer", approved_draft_body="Approved")
        self.service.reject(rejected.id, "reviewer")
        for status, included, excluded in (
            ("pending", pending.id, approved.id),
            ("approved", approved.id, rejected.id),
            ("rejected", rejected.id, pending.id),
        ):
            response = self.client.get(f"/?status={status}")
            self.assertEqual(response.status_code, 200)
            self.assertIn(f"href='/reviews/{included}'".encode(), response.data)
            self.assertNotIn(f"href='/reviews/{excluded}'".encode(), response.data)

    def test_detail_contains_context_analysis_policy_retrieval_and_draft(self):
        item = self.record().review_item
        response = self.client.get(f"/reviews/{item.id}")
        self.assertEqual(response.status_code, 200)
        for expected in (b"private conversation body", b"Routine customer request",
                         b"private knowledge content", b"safe_for_review", b"Confirmed local reply body"):
            self.assertIn(expected, response.data)

    def test_edit_and_approve_preserves_original_and_unicode(self):
        item = self.record().review_item
        token = self.service.detail(item.id).item.updated_at
        edited = "Hei, takk! \U0001f44b\nGodkjent svar."
        updated = self.service.approve(item.id, "operator", approved_draft_body=edited,
                                       expected_updated_at=token)
        detail = self.service.detail(item.id)
        self.assertEqual(updated.approved_draft_body, edited)
        self.assertEqual(detail.original_draft_body, "Confirmed local reply body.")
        self.assertEqual(detail.item.approved_draft_body, edited)

    def test_reject_persists_note_and_has_no_approved_body(self):
        item = self.record().review_item
        updated = self.service.reject(item.id, "operator", "Not appropriate")
        self.assertEqual(updated.status, "rejected")
        self.assertIsNone(updated.approved_draft_body)
        self.assertEqual(self.service.history(item.id)[-1].note, "Not appropriate")

    def test_empty_whitespace_and_oversized_approved_drafts_are_rejected(self):
        for body in ("", " \r\n\t", "x" * (MAX_APPROVED_DRAFT_CHARS + 1)):
            with self.subTest(length=len(body)), self.assertRaises(ReviewValidationError):
                self.service.approve(self.record().review_item.id, "operator", approved_draft_body=body)

    def test_line_endings_are_normalized(self):
        item = self.record().review_item
        updated = self.service.approve(item.id, "operator", approved_draft_body="one\r\ntwo\rthree")
        self.assertEqual(updated.approved_draft_body, "one\ntwo\nthree")

    def test_duplicate_and_stale_decisions_cannot_overwrite_first(self):
        item = self.record().review_item
        second_repository = SqliteInboxRepository(self.path)
        try:
            second = ReviewQueueService(second_repository)
            stale_token = second.detail(item.id).item.updated_at
            self.service.approve(item.id, "first", approved_draft_body="first", expected_updated_at=stale_token)
            with self.assertRaises(ReviewConflictError):
                second.reject(item.id, "second", expected_updated_at=stale_token)
            with self.assertRaises(ReviewConflictError):
                self.service.approve(item.id, "first", approved_draft_body="first")
            final = self.service.detail(item.id).item
            self.assertEqual((final.status, final.reviewer_id, final.approved_draft_body),
                             ("approved", "first", "first"))
        finally:
            second_repository.close()

    def test_reviewed_detail_is_read_only(self):
        item = self.record().review_item
        self.service.approve(item.id, "operator", approved_draft_body="final")
        response = self.client.get(f"/reviews/{item.id}")
        self.assertIn(b"completed review is read-only", response.data)
        self.assertNotIn(b"<textarea", response.data)

    def test_html_and_script_content_is_escaped_everywhere(self):
        item = self.record().review_item
        script = "<script>alert('unsafe')</script>"
        self.repository.connection.execute("UPDATE messages SET body_text=? WHERE conversation_id=?",
                                           (script, self.conversation.id))
        self.repository.connection.execute("UPDATE reply_drafts SET body=? WHERE id=?", (script, item.reply_draft_id))
        self.repository.connection.commit()
        response = self.client.get(f"/reviews/{item.id}")
        self.assertNotIn(b"<script>", response.data)
        self.assertIn(b"&lt;script&gt;", response.data)

    def test_missing_optional_analysis_and_retrieval_are_graceful(self):
        item = self.record().review_item
        self.repository.connection.execute("DELETE FROM message_analyses")
        self.repository.connection.execute("DELETE FROM knowledge_retrieval_results")
        self.repository.connection.commit()
        response = self.client.get(f"/reviews/{item.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Not available", response.data)
        self.assertIn(b"No retrieval context", response.data)

    def test_item_with_missing_draft_disables_approval_but_allows_reject(self):
        item = self.record().review_item
        self.repository.connection.execute("DELETE FROM reply_draft_grounding WHERE reply_draft_id=?", (item.reply_draft_id,))
        self.repository.connection.execute("DELETE FROM reply_drafts WHERE id=?", (item.reply_draft_id,))
        self.repository.connection.commit()
        response = self.client.get(f"/reviews/{item.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Approval is disabled", response.data)
        with self.assertRaises(ReviewValidationError):
            self.service.approve(item.id, "operator")
        self.assertEqual(self.service.reject(item.id, "operator").status, "rejected")

    def test_nonexistent_and_invalid_items_are_404(self):
        self.assertEqual(self.client.get("/reviews/999999").status_code, 404)
        self.assertEqual(self.client.get("/reviews/not-an-id").status_code, 404)
        with self.assertRaises(ReviewNotFoundError):
            self.service.detail(0)

    def test_decision_route_validates_and_protects_duplicate_form_submission(self):
        item = self.record().review_item
        token = self.service.detail(item.id).item.updated_at
        detail_response = self.client.get(f"/reviews/{item.id}")
        csrf = re.search(rb"name=csrf_token value='([^']+)'", detail_response.data).group(1).decode()
        form = {"action": "approve", "reviewer_id": "operator", "draft_body": "web approved",
                "expected_updated_at": token, "csrf_token": csrf}
        first = self.client.post(f"/reviews/{item.id}/decision", data=form)
        second = self.client.post(f"/reviews/{item.id}/decision", data=form)
        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 409)

    def test_decision_route_rejects_missing_csrf_token(self):
        item = self.record().review_item
        self.client.get(f"/reviews/{item.id}")
        response = self.client.post(f"/reviews/{item.id}/decision", data={
            "action": "reject", "reviewer_id": "operator",
            "expected_updated_at": self.service.detail(item.id).item.updated_at,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.service.detail(item.id).item.status, "pending")

    def test_state_survives_repository_and_service_recreation(self):
        item = self.record().review_item
        self.service.approve(item.id, "operator", approved_draft_body="durable")
        other_repository = SqliteInboxRepository(self.path)
        try:
            detail = ReviewQueueService(other_repository).detail(item.id)
            self.assertEqual((detail.item.status, detail.item.approved_draft_body), ("approved", "durable"))
        finally:
            other_repository.close()

    def test_console_requires_no_credentials_and_invokes_no_providers(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("app.main.build_email") as gmail, \
             mock.patch("app.claude_reply_draft_generator.ClaudeGroundedReplyGenerator") as claude:
            isolated = create_app(self.path)
            response = isolated.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        gmail.assert_not_called()
        claude.assert_not_called()

    def test_structured_logs_omit_message_and_draft_content(self):
        item = self.record().review_item
        secret_body = "sensitive-draft-do-not-log"
        with self.assertLogs("app.review_queue_service", level=logging.INFO) as captured:
            self.service.approve(item.id, "operator", approved_draft_body=secret_body)
        output = " ".join(captured.output)
        self.assertIn(f"review_item_id={item.id}", output)
        self.assertNotIn(secret_body, output)
        self.assertNotIn("private conversation body", output)
