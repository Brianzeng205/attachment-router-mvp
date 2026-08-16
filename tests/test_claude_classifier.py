import json
import unittest
from types import SimpleNamespace

from app.claude_classifier import ClaudeDocumentClassifier
from app.errors import ClassifierAPIError, ClassifierResponseError
from app.models import Attachment, EmailMessage


def payload(**overrides):
    value = {
        "document_type": "invoice", "company_or_sender": "Acme Ltd", "document_date": "2026-08-01",
        "reference_number": "INV-42", "suggested_filename": "Acme invoice", "target_folder": "invoices", "confidence": 0.94,
    }
    value.update(overrides)
    return value


class FakeMessages:
    def __init__(self, response=None, error=None): self.response, self.error, self.requests = response, error, []
    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error: raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None): self.messages = FakeMessages(response, error)


def claude_response(value):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(value))])


class ClaudeDocumentClassifierTests(unittest.TestCase):
    def setUp(self):
        self.attachment = Attachment("a-1", "source.pdf", b"example", "application/pdf")
        self.message = EmailMessage("e-1", "billing@acme.test", "Invoice", "Attached invoice", "2026-08-02T00:00:00Z", (self.attachment,))

    def classifier(self, response=None, error=None, labels=None, extractor=lambda attachment, limit: "invoice text"):
        return ClaudeDocumentClassifier(FakeClient(response, error), "claude-test", labels or {"invoices", "signed_documents"}, 20, extractor)

    def test_valid_invoice_classification(self):
        classifier = self.classifier(claude_response(payload()))
        result = classifier.classify(self.message, self.attachment)
        self.assertEqual(result["target_folder"], "invoices")
        self.assertEqual(result["document_type"], "invoice")

    def test_valid_signed_document_classification(self):
        classifier = self.classifier(claude_response(payload(document_type="signed agreement", target_folder="signed_documents")))
        self.assertEqual(classifier.classify(self.message, self.attachment)["target_folder"], "signed_documents")

    def test_low_confidence_classification_is_preserved_for_orchestrator(self):
        classifier = self.classifier(claude_response(payload(confidence=0.2)))
        self.assertEqual(classifier.classify(self.message, self.attachment)["confidence"], 0.2)

    def test_malformed_response_is_rejected(self):
        classifier = self.classifier(SimpleNamespace(content=[SimpleNamespace(text="not json")]))
        with self.assertRaises(ClassifierResponseError):
            classifier.classify(self.message, self.attachment)

    def test_raw_drive_id_or_unknown_target_label_is_rejected(self):
        classifier = self.classifier(claude_response(payload(target_folder="1DriveFolderId")))
        with self.assertRaises(ClassifierResponseError):
            classifier.classify(self.message, self.attachment)

    def test_missing_optional_metadata_can_be_null(self):
        classifier = self.classifier(claude_response(payload(document_type=None, company_or_sender=None, document_date=None, reference_number=None)))
        result = classifier.classify(self.message, self.attachment)
        self.assertIsNone(result["document_date"])
        self.assertIsNone(result["reference_number"])

    def test_api_exception_is_mapped_to_application_error(self):
        classifier = self.classifier(error=RuntimeError("network down"))
        with self.assertRaises(ClassifierAPIError):
            classifier.classify(self.message, self.attachment)

    def test_extraction_failure_still_sends_metadata(self):
        def broken_extractor(attachment, limit): raise ValueError("bad PDF")
        client = FakeClient(claude_response(payload()))
        classifier = ClaudeDocumentClassifier(client, "claude-test", {"invoices"}, 20, broken_extractor)
        classifier.classify(self.message, self.attachment)
        sent_context = json.loads(client.messages.requests[0]["messages"][0]["content"])
        self.assertIsNone(sent_context["attachment_text"])

    def test_extracted_text_is_truncated(self):
        client = FakeClient(claude_response(payload()))
        classifier = ClaudeDocumentClassifier(client, "claude-test", {"invoices"}, 20, lambda attachment, limit: "x" * 100)
        classifier.classify(self.message, self.attachment)
        sent_context = json.loads(client.messages.requests[0]["messages"][0]["content"])
        self.assertEqual(sent_context["attachment_text"], "x" * 20)
        self.assertEqual(client.messages.requests[0]["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(client.messages.requests[0]["output_config"]["format"]["schema"]["properties"]["target_folder"]["enum"], ["invoices"])
        self.assertNotIn("invoice-folder-id", client.messages.requests[0]["messages"][0]["content"])
