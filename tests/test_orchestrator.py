import unittest
from pathlib import Path

from app.config import Settings
from app.errors import ClassifierAPIError
from app.models import Attachment, EmailMessage
from app.orchestrator import AttachmentProcessor


class FakeEmail:
    def __init__(self, messages): self.messages = messages
    def list_messages(self): return self.messages


class FakeClassifier:
    def __init__(self, result=None, error=None): self.result, self.error = result, error
    def classify(self, message, attachment):
        if self.error: raise self.error
        return self.result


class FakeDrive:
    def __init__(self, failure=None): self.uploads, self.failure = [], failure
    def upload(self, **kwargs):
        if self.failure:
            raise self.failure
        self.uploads.append(kwargs)
        return "drive-file-id"


class FakeState:
    def __init__(self, processed=()): self.processed = set(processed)
    def is_processed(self, email_id, attachment_id): return (email_id, attachment_id) in self.processed
    def mark_processed(self, email_id, attachment_id): self.processed.add((email_id, attachment_id))


def classification(**overrides):
    value = {"document_type": "invoice", "company_or_sender": "Acme", "document_date": "2026-08-01", "reference_number": "INV-1", "suggested_filename": "Acme Invoice.pdf", "target_folder": "invoices", "confidence": 0.95}
    value.update(overrides)
    return value


class AttachmentProcessorTests(unittest.TestCase):
    def setUp(self):
        attachment = Attachment("a-1", "original.pdf", b"pdf", "application/pdf")
        self.message = EmailMessage("e-1", "billing@acme.test", "Invoice", "Please see attached", "2026-08-02T00:00:00Z", (attachment,))
        self.settings = Settings(.85, "review-id", {"invoices": "invoice-folder-id"}, Path(":memory:"))

    def processor(self, drive, state=None, result=None, classifier_error=None):
        return AttachmentProcessor(FakeEmail([self.message]), FakeClassifier(result or classification(), classifier_error), drive, state or FakeState(), self.settings)

    def test_successful_upload_to_approved_folder_marks_state_after_upload(self):
        drive, state = FakeDrive(), FakeState()
        result = self.processor(drive, state).process_all()
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(drive.uploads[0]["folder_id"], "invoice-folder-id")
        self.assertTrue(state.is_processed("e-1", "a-1"))

    def test_low_confidence_uploads_to_needs_review(self):
        drive = FakeDrive()
        self.processor(drive, result=classification(confidence=.2)).process_all()
        self.assertEqual(drive.uploads[0]["folder_id"], "review-id")

    def test_classifier_unapproved_folder_uploads_to_needs_review(self):
        drive = FakeDrive()
        self.processor(drive, result=classification(target_folder="secret-folder")).process_all()
        self.assertEqual(drive.uploads[0]["folder_id"], "review-id")

    def test_invalid_suggested_filename_uses_safe_original_extension(self):
        drive = FakeDrive()
        self.processor(drive, result=classification(suggested_filename="../../A:bad")).process_all()
        self.assertEqual(drive.uploads[0]["filename"], "_.._A_bad.pdf")

    def test_upload_failure_does_not_mark_processed_state(self):
        state = FakeState()
        result = self.processor(FakeDrive(RuntimeError("Drive unavailable")), state).process_all()
        self.assertEqual(result.errors, 1)
        self.assertFalse(state.is_processed("e-1", "a-1"))

    def test_classifier_failure_routes_to_review_and_marks_only_after_review_upload(self):
        state, drive = FakeState(), FakeDrive()
        result = self.processor(drive, state, classifier_error=ClassifierAPIError("API unavailable")).process_all()
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(drive.uploads[0]["folder_id"], "review-id")
        self.assertTrue(state.is_processed("e-1", "a-1"))

    def test_processed_attachment_is_not_uploaded_again(self):
        drive = FakeDrive()
        result = self.processor(drive, FakeState({("e-1", "a-1")})).process_all()
        self.assertEqual(result.skipped, 1)
        self.assertEqual(drive.uploads, [])
