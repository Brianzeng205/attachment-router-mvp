import base64
import unittest
from pathlib import Path

from app.config import Settings
from app.gmail_client import GmailAPIError, GmailClient
from app.models import Attachment, EmailMessage
from app.orchestrator import AttachmentProcessor


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def message(message_id="m1", parts=None):
    return {
        "id": message_id, "threadId": "thread-1", "internalDate": "1760000000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "From", "value": "sender@example.com"}, {"name": "Subject", "value": "Subject"}],
            "parts": parts or [],
        },
    }


def text_part(mime_type="text/plain", content=b"email body"):
    return {"mimeType": mime_type, "body": {"data": encoded(content)}}


def attachment_part(name="invoice.pdf", attachment_id="a1", size=3, mime_type="application/pdf", headers=None):
    return {"partId": attachment_id, "mimeType": mime_type, "filename": name, "headers": headers or [], "body": {"attachmentId": attachment_id, "size": size}}


class Request:
    def __init__(self, value=None, error=None): self.value, self.error = value, error
    def execute(self):
        if self.error: raise self.error
        return self.value


class FakeAttachments:
    def __init__(self, data, failures=()): self.data, self.failures = data, set(failures)
    def get(self, **kwargs):
        attachment_id = kwargs["id"]
        return Request(error=RuntimeError("attachment failed")) if attachment_id in self.failures else Request({"data": self.data[attachment_id]})


class FakeMessagesApi:
    def __init__(self, messages, attachment_data, list_error=None, attachment_failures=()):
        self.message_data, self.attachment_api, self.list_error = messages, FakeAttachments(attachment_data, attachment_failures), list_error
        self.list_calls = []
    def list(self, **kwargs): self.list_calls.append(kwargs); return Request({"messages": [{"id": key} for key in self.message_data]}, self.list_error)
    def get(self, **kwargs): return Request(self.message_data[kwargs["id"]])
    def attachments(self): return self.attachment_api


class FakeUsers:
    def __init__(self, messages): self.messages_api = messages
    def messages(self): return self.messages_api


class FakeService:
    def __init__(self, messages): self.messages_api = messages
    def users(self): return FakeUsers(self.messages_api)


class GmailClientTests(unittest.TestCase):
    def client(self, messages, data, **kwargs):
        return GmailClient(FakeService(FakeMessagesApi(messages, data, **kwargs)), "has:attachment", 10)

    def test_one_pdf_attachment_and_metadata(self):
        client = self.client({"m1": message(parts=[text_part(), attachment_part()])}, {"a1": encoded(b"pdf")})
        result = list(client.list_messages())
        self.assertEqual(result[0].attachments[0], Attachment("part:a1", "invoice.pdf", b"pdf", "application/pdf"))
        self.assertEqual(result[0].thread_id, "thread-1")
        self.assertEqual(client._service.messages_api.list_calls[0]["q"], "has:attachment")

    def test_multiple_and_same_named_attachments_have_distinct_identities(self):
        client = self.client({"m1": message(parts=[attachment_part("same.pdf", "a1"), attachment_part("same.pdf", "a2")])}, {"a1": encoded(b"one"), "a2": encoded(b"two")})
        attachments = list(client.list_messages())[0].attachments
        self.assertEqual([item.id for item in attachments], ["part:a1", "part:a2"])
        self.assertEqual([item.filename for item in attachments], ["same.pdf", "same.pdf"])

    def test_message_without_attachments_is_returned_empty(self):
        client = self.client({"m1": message(parts=[text_part()])}, {})
        self.assertEqual(list(client.list_messages())[0].attachments, ())

    def test_prefers_plain_body_and_falls_back_to_html(self):
        plain = self.client({"m1": message(parts=[text_part("text/html", b"<p>HTML</p>"), text_part("text/plain", b"Plain")])}, {})
        html = self.client({"m1": message(parts=[text_part("text/html", b"<p>HTML <b>body</b></p>")])}, {})
        self.assertEqual(list(plain.list_messages())[0].body, "Plain")
        self.assertEqual(list(html.list_messages())[0].body, "HTML body")

    def test_inline_signature_image_is_ignored(self):
        part = attachment_part("signature.png", "sig", mime_type="image/png", headers=[{"name": "Content-Disposition", "value": "inline"}])
        client = self.client({"m1": message(parts=[part])}, {"sig": encoded(b"image")})
        self.assertEqual(list(client.list_messages())[0].attachments, ())

    def test_malformed_message_is_skipped(self):
        client = self.client({"m1": {"id": "m1"}}, {})
        self.assertEqual(list(client.list_messages()), [])

    def test_list_api_error_is_raised(self):
        client = self.client({}, {}, list_error=RuntimeError("network"))
        with self.assertRaises(GmailAPIError):
            list(client.list_messages())

    def test_oversized_attachment_is_logged_and_skipped(self):
        client = self.client({"m1": message(parts=[attachment_part(size=11)])}, {"a1": encoded(b"x" * 11)})
        self.assertEqual(list(client.list_messages())[0].attachments, ())

    def test_attachment_retrieval_failure_does_not_stop_message(self):
        client = self.client({"m1": message(parts=[attachment_part()])}, {"a1": encoded(b"pdf")}, attachment_failures={"a1"})
        self.assertEqual(list(client.list_messages())[0].attachments, ())

    def test_two_attachments_are_independently_processed(self):
        email = list(self.client({"m1": message(parts=[attachment_part("a.pdf", "a1"), attachment_part("b.pdf", "a2")])}, {"a1": encoded(b"a"), "a2": encoded(b"b")}).list_messages())[0]
        state, drive = MemoryState(), MemoryDrive()
        settings = Settings(.85, "review", {"invoices": "folder"}, Path(":memory:"))
        result = AttachmentProcessor(StaticEmail([email]), InvoiceClassifier(), drive, state, settings).process_all()
        self.assertEqual(result.uploaded, 2)
        self.assertEqual(len(drive.uploads), 2)
        self.assertTrue(state.is_processed("m1", "part:a1"))
        self.assertTrue(state.is_processed("m1", "part:a2"))

    def test_previously_processed_attachments_are_skipped_on_later_poll(self):
        email = list(self.client({"m1": message(parts=[attachment_part("a.pdf", "a1")])}, {"a1": encoded(b"a")}).list_messages())[0]
        state, drive = MemoryState(), MemoryDrive()
        settings = Settings(.85, "review", {"invoices": "folder"}, Path(":memory:"))
        processor = AttachmentProcessor(StaticEmail([email]), InvoiceClassifier(), drive, state, settings)
        processor.process_all()
        repeat = processor.process_all()
        self.assertEqual(repeat.skipped, 1)
        self.assertEqual(len(drive.uploads), 1)

    def test_changed_gmail_retrieval_token_keeps_part_identity_stable_across_polls(self):
        first = message(parts=[{
            "partId": "0.1", "mimeType": "application/pdf", "filename": "invoice.pdf",
            "body": {"attachmentId": "transient-token-one", "size": 3},
        }])
        second = message(parts=[{
            "partId": "0.1", "mimeType": "application/pdf", "filename": "invoice.pdf",
            "body": {"attachmentId": "transient-token-two", "size": 3},
        }])
        first_email = list(self.client({"m1": first}, {"transient-token-one": encoded(b"pdf")}).list_messages())[0]
        second_email = list(self.client({"m1": second}, {"transient-token-two": encoded(b"pdf")}).list_messages())[0]
        self.assertEqual(first_email.attachments[0].id, "part:0.1")
        self.assertEqual(second_email.attachments[0].id, "part:0.1")

        state, drive = MemoryState(), MemoryDrive()
        settings = Settings(.85, "review", {"invoices": "folder"}, Path(":memory:"))
        first_run = AttachmentProcessor(StaticEmail([first_email]), InvoiceClassifier(), drive, state, settings).process_all()
        second_run = AttachmentProcessor(StaticEmail([second_email]), InvoiceClassifier(), drive, state, settings).process_all()
        self.assertEqual(first_run.uploaded, 1)
        self.assertEqual(second_run.skipped, 1)
        self.assertEqual(len(drive.uploads), 1)


class StaticEmail:
    def __init__(self, messages): self.messages = messages
    def list_messages(self): return self.messages


class InvoiceClassifier:
    def classify(self, message, attachment):
        return {"document_type": "invoice", "company_or_sender": "Sender", "document_date": None, "reference_number": None, "suggested_filename": attachment.filename, "target_folder": "invoices", "confidence": .9}


class MemoryDrive:
    def __init__(self): self.uploads = []
    def upload(self, **kwargs): self.uploads.append(kwargs); return "id"


class MemoryState:
    def __init__(self): self.rows = set()
    def is_processed(self, email_id, attachment_id): return (email_id, attachment_id) in self.rows
    def mark_processed(self, email_id, attachment_id): self.rows.add((email_id, attachment_id))
