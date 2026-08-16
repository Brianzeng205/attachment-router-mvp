import unittest

from app.errors import DriveFolderError, InvalidFilenameError
from app.google_drive import FOLDER_MIME_TYPE, GoogleDriveClient


class Request:
    def __init__(self, result=None, error=None): self.result, self.error = result, error
    def execute(self):
        if self.error: raise self.error
        return self.result


class FakeFiles:
    def __init__(self, existing=None, create_error=None, folder_metadata=None):
        self.existing, self.create_error, self.created = existing, create_error, []
        self.folder_metadata = folder_metadata or {"mimeType": FOLDER_MIME_TYPE, "trashed": False}
    def get(self, **kwargs): return Request({"id": kwargs["fileId"], **self.folder_metadata})
    def list(self, **kwargs): return Request({"files": [{"id": self.existing}]} if self.existing else {"files": []})
    def create(self, **kwargs):
        self.created.append(kwargs)
        return Request({"id": "new-file-id"}, self.create_error)


class FakeService:
    def __init__(self, files): self._files = files
    def files(self): return self._files


class GoogleDriveClientTests(unittest.TestCase):
    def client(self, files):
        return GoogleDriveClient(FakeService(files), {"approved-folder", "review-folder"}, media_factory=lambda content, mime: object())

    def test_successful_upload_to_approved_folder(self):
        files = FakeFiles()
        file_id = self.client(files).upload(folder_id="approved-folder", filename="Invoice.pdf", content=b"pdf", mime_type="application/pdf", idempotency_key="email-attachment-key")
        self.assertEqual(file_id, "new-file-id")
        self.assertEqual(files.created[0]["body"]["parents"], ["approved-folder"])
        self.assertIn("email-attac", files.created[0]["body"]["name"])

    def test_folder_allowlist_is_enforced_by_drive_client(self):
        with self.assertRaises(DriveFolderError):
            self.client(FakeFiles()).upload(folder_id="unapproved", filename="Invoice.pdf", content=b"pdf", mime_type=None, idempotency_key="key")

    def test_invalid_filename_is_rejected_before_drive_request(self):
        with self.assertRaises(InvalidFilenameError):
            self.client(FakeFiles()).upload(folder_id="approved-folder", filename="../bad.pdf", content=b"pdf", mime_type=None, idempotency_key="key")

    def test_configured_destination_must_be_a_real_folder(self):
        with self.assertRaises(DriveFolderError):
            self.client(FakeFiles(folder_metadata={"mimeType": "application/pdf", "trashed": False})).upload(
                folder_id="approved-folder", filename="Invoice.pdf", content=b"pdf", mime_type=None, idempotency_key="key",
            )

    def test_retry_after_crash_reuses_existing_drive_file(self):
        files = FakeFiles(existing="already-uploaded")
        file_id = self.client(files).upload(folder_id="approved-folder", filename="Invoice.pdf", content=b"pdf", mime_type=None, idempotency_key="stable-key")
        self.assertEqual(file_id, "already-uploaded")
        self.assertEqual(files.created, [])
