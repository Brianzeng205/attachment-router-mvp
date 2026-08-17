import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.claude_classifier import ClaudeDocumentClassifier
from app.claude_conversation_analyzer import ClaudeConversationAnalyzer
from app.claude_inbox_analyzer import ClaudeInboxAnalyzer
from app.claude_reply_draft_generator import ClaudeGroundedReplyGenerator
from app.config import Settings
from app.errors import (
    DrivePermissionError, DriveUploadError, GmailAPIError, GmailAuthenticationError,
    GmailPayloadError, GmailRateLimitError, InboxAnalyzerAPIError, InboxAnalyzerResponseError,
)
from app.gmail_client import GmailClient
from app.google_drive import FOLDER_MIME_TYPE, GoogleDriveClient
from app.inbox_models import InboxMessage
from app.retry import RetryPolicy


class HttpFailure(Exception):
    def __init__(self, status):
        super().__init__("provider detail must not drive retry policy")
        self.resp = SimpleNamespace(status=status)


class _SequenceRequest:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def execute(self):
        self.calls += 1
        value = self.outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class _GmailMessages:
    def __init__(self, list_outcomes):
        self.list_request = _SequenceRequest(list_outcomes)

    def list(self, **_kwargs):
        return self.list_request


class _GmailService:
    def __init__(self, outcomes):
        self.api = _GmailMessages(outcomes)

    def users(self):
        return self

    def messages(self):
        return self.api


def _policy(max_attempts=3):
    return RetryPolicy(
        max_attempts=max_attempts, initial_delay_seconds=0, max_delay_seconds=0,
        jitter_ratio=0, sleeper=lambda _delay: None,
    )


def _inbox_payload(**overrides):
    value = {
        "category": "order_support", "intent": "check_status", "priority": "normal",
        "urgency": "medium", "summary": "Customer requests status.", "customer_name": None,
        "order_numbers": [], "dates": [], "requirements": [], "confidence": 0.9,
        "needs_human": False, "human_reason": None, "recommended_action": "draft_reply",
    }
    value.update(overrides)
    return value


def _response(value):
    text = value if isinstance(value, str) else json.dumps(value)
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


class GmailRetryTests(unittest.TestCase):
    def client(self, outcomes):
        service = _GmailService(outcomes)
        return GmailClient(service, retry_policy=_policy()), service.api.list_request

    def test_rate_limit_and_transient_server_failures_retry(self):
        for failure in (HttpFailure(429), HttpFailure(500), HttpFailure(503)):
            with self.subTest(status=failure.resp.status):
                client, request = self.client([failure, {"messages": []}])
                self.assertEqual(list(client.list_messages()), [])
                self.assertEqual(request.calls, 2)

    def test_timeout_and_network_failure_retry(self):
        for failure in (TimeoutError(), ConnectionError()):
            with self.subTest(error=type(failure).__name__):
                client, request = self.client([failure, {"messages": []}])
                self.assertEqual(list(client.list_messages()), [])
                self.assertEqual(request.calls, 2)

    def test_authentication_and_permission_failures_do_not_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                client, request = self.client([HttpFailure(status)])
                with self.assertRaises(GmailAuthenticationError):
                    list(client.list_messages())
                self.assertEqual(request.calls, 1)

    def test_malformed_payload_does_not_retry(self):
        client, request = self.client([{"messages": "not-a-list"}])
        with self.assertRaises(GmailPayloadError):
            list(client.list_messages())
        self.assertEqual(request.calls, 1)

    def test_retry_exhaustion_uses_existing_normalized_gmail_error(self):
        client, request = self.client([HttpFailure(429), HttpFailure(429), HttpFailure(429)])
        with self.assertRaises(GmailRateLimitError):
            list(client.list_messages())
        self.assertEqual(request.calls, 3)


class _ClaudeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        value = self.outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class _ClaudeClient:
    def __init__(self, outcomes):
        self.messages = _ClaudeMessages(outcomes)


class ClaudeRetryTests(unittest.TestCase):
    message = InboxMessage(
        provider="gmail", provider_message_id="m", provider_thread_id="t", sender="sender",
        recipients=("recipient",), subject="subject", body_text="body", received_at="time",
        ingestion_state="ingested", content_hash="a" * 64, id=1, conversation_id=1,
    )

    def analyzer(self, outcomes):
        client = _ClaudeClient(outcomes)
        return ClaudeInboxAnalyzer(client, "test", 100, retry_policy=_policy()), client.messages

    def test_rate_limit_server_overload_and_timeout_retry_then_validate(self):
        rate_limit = type("RateLimitError", (Exception,), {})()
        for failure in (rate_limit, HttpFailure(503), TimeoutError()):
            with self.subTest(error=type(failure).__name__):
                analyzer, messages = self.analyzer([failure, _response(_inbox_payload())])
                self.assertEqual(analyzer.analyze(self.message).category, "order_support")
                self.assertEqual(messages.calls, 2)

    def test_invalid_json_and_controlled_enum_do_not_retry(self):
        for response in (_response("not-json"), _response(_inbox_payload(priority="rush"))):
            with self.subTest(response=response):
                analyzer, messages = self.analyzer([response])
                with self.assertRaises(InboxAnalyzerResponseError):
                    analyzer.analyze(self.message)
                self.assertEqual(messages.calls, 1)

    def test_retry_exhaustion_maps_to_existing_analyzer_error(self):
        analyzer, messages = self.analyzer([TimeoutError(), TimeoutError(), TimeoutError()])
        with self.assertRaises(InboxAnalyzerAPIError):
            analyzer.analyze(self.message)
        self.assertEqual(messages.calls, 3)

    def test_all_production_claude_adapters_use_the_shared_policy_type(self):
        policy = _policy()
        client = _ClaudeClient([_response(_inbox_payload())])
        adapters = (
            ClaudeDocumentClassifier(client, "test", {"folder"}, retry_policy=policy),
            ClaudeInboxAnalyzer(client, "test", 100, retry_policy=policy),
            ClaudeConversationAnalyzer(client, "test", retry_policy=policy),
            ClaudeGroundedReplyGenerator(client, "test", retry_policy=policy),
        )
        self.assertTrue(all(adapter._retry_policy is policy for adapter in adapters))

    def test_from_settings_disables_sdk_retries_and_sets_bounded_timeout(self):
        anthropic = Mock(return_value=_ClaudeClient([_response(_inbox_payload())]))
        settings = Settings(
            0.85, "review", {"folder": "folder-id"}, Path("state.sqlite3"),
            anthropic_api_key="test-key", provider_request_timeout_seconds=17,
        )
        with patch.dict(sys.modules, {"anthropic": SimpleNamespace(Anthropic=anthropic)}):
            ClaudeDocumentClassifier.from_settings(settings)
            ClaudeInboxAnalyzer.from_settings(settings)
            ClaudeConversationAnalyzer.from_settings(settings)
            ClaudeGroundedReplyGenerator.from_settings(settings)
        self.assertEqual(anthropic.call_count, 4)
        for call in anthropic.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 17)
            self.assertEqual(call.kwargs["max_retries"], 0)


class _DriveFiles:
    def __init__(self, *, folder_error=None, list_outcomes=None, create_outcomes=None):
        self.folder_error = folder_error
        self.list_outcomes = list(list_outcomes or [{"files": []}])
        self.create_outcomes = list(create_outcomes or [{"id": "created"}])
        self.get_calls = self.list_calls = self.create_calls = 0
        self.create_bodies = []

    def get(self, **_kwargs):
        self.get_calls += 1
        value = self.folder_error or {"mimeType": FOLDER_MIME_TYPE, "trashed": False}
        return _SequenceRequest([value])

    def list(self, **_kwargs):
        self.list_calls += 1
        return _SequenceRequest([self.list_outcomes.pop(0)])

    def create(self, **kwargs):
        self.create_calls += 1
        self.create_bodies.append(kwargs["body"])
        return _SequenceRequest([self.create_outcomes.pop(0)])


class _DriveService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


class DriveRetryTests(unittest.TestCase):
    def client(self, files):
        return GoogleDriveClient(
            _DriveService(files), {"folder"}, media_factory=lambda _content, _mime: object(),
            retry_policy=_policy(),
        )

    def upload(self, files):
        return self.client(files).upload(
            folder_id="folder", filename="file.pdf", content=b"data",
            mime_type="application/pdf", idempotency_key="stable-key",
        )

    def test_transient_rate_limit_server_and_timeout_failures_retry(self):
        for failure in (HttpFailure(429), HttpFailure(503), TimeoutError()):
            with self.subTest(error=type(failure).__name__):
                files = _DriveFiles(
                    list_outcomes=[{"files": []}, {"files": []}],
                    create_outcomes=[failure, {"id": "created"}],
                )
                self.assertEqual(self.upload(files), "created")
                self.assertEqual(files.create_calls, 2)
                self.assertEqual(files.list_calls, 2)

    def test_permission_failure_does_not_retry(self):
        files = _DriveFiles(folder_error=HttpFailure(403))
        with self.assertRaises(DrivePermissionError):
            self.upload(files)
        self.assertEqual(files.get_calls, 1)

    def test_uncertain_upload_rechecks_idempotency_marker_before_second_create(self):
        files = _DriveFiles(
            list_outcomes=[{"files": []}, {"files": [{"id": "accepted-before-timeout"}]}],
            create_outcomes=[TimeoutError()],
        )
        self.assertEqual(self.upload(files), "accepted-before-timeout")
        self.assertEqual(files.create_calls, 1)
        self.assertEqual(files.list_calls, 2)
        self.assertEqual(files.create_bodies[0]["appProperties"]["attachment_router_key"], "stable-key")

    def test_exhausted_transient_upload_raises_existing_drive_error(self):
        files = _DriveFiles(
            list_outcomes=[{"files": []}] * 3,
            create_outcomes=[TimeoutError()] * 3,
        )
        with self.assertRaises(DriveUploadError):
            self.upload(files)
        self.assertEqual(files.create_calls, 3)


if __name__ == "__main__":
    unittest.main()
