from __future__ import annotations

import logging
import hashlib
from dataclasses import dataclass

from .config import Settings
from .errors import ClassifierError
from .interfaces import DocumentClassifier, DriveClient, EmailClient, StateManager
from .filenames import sanitize_filename
from .models import Attachment, Classification, EmailMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessingSummary:
    uploaded: int = 0
    skipped: int = 0
    errors: int = 0


class AttachmentProcessor:
    def __init__(self, email: EmailClient, classifier: DocumentClassifier, drive: DriveClient, state: StateManager, settings: Settings) -> None:
        self.email, self.classifier, self.drive, self.state, self.settings = email, classifier, drive, state, settings

    def process_all(self) -> ProcessingSummary:
        summary = ProcessingSummary()
        for message in self.email.list_messages():
            for attachment in message.attachments:
                if self.state.is_processed(message.id, attachment.id):
                    summary = ProcessingSummary(summary.uploaded, summary.skipped + 1, summary.errors)
                    continue
                try:
                    self._process_attachment(message, attachment)
                    self.state.mark_processed(message.id, attachment.id)
                    logger.info("Processed attachment email_id=%s attachment_id=%s", message.id, attachment.id)
                    summary = ProcessingSummary(summary.uploaded + 1, summary.skipped, summary.errors)
                except Exception as exc:
                    logger.error(
                        "event=attachment_failed email_id=%s attachment_id=%s error_class=%s",
                        message.id, attachment.id, type(exc).__name__,
                    )
                    summary = ProcessingSummary(summary.uploaded, summary.skipped, summary.errors + 1)
        return summary

    def _process_attachment(self, message: EmailMessage, attachment: Attachment) -> None:
        try:
            classification = Classification.from_mapping(self.classifier.classify(message, attachment))
            folder_id = self._approved_folder(classification)
            filename = self._safe_filename(classification.suggested_filename, attachment.filename)
        except (ClassifierError, ValueError, TypeError):
            logger.warning("Classification unavailable or invalid; routing attachment %s to Needs Review", attachment.id)
            folder_id = self.settings.needs_review_folder_id
            filename = self._safe_filename(attachment.filename, attachment.filename)
        logger.info(
            "Routing attachment email_id=%s attachment_id=%s folder_id=%s",
            message.id, attachment.id, folder_id,
        )
        drive_file_id = self.drive.upload(
            folder_id=folder_id,
            filename=filename,
            content=attachment.content,
            mime_type=attachment.mime_type,
            idempotency_key=self._idempotency_key(message, attachment),
        )
        logger.info(
            "Uploaded attachment email_id=%s attachment_id=%s drive_file_id=%s",
            message.id, attachment.id, drive_file_id,
        )

    def _approved_folder(self, classification: Classification) -> str:
        if classification.confidence < self.settings.confidence_threshold:
            return self.settings.needs_review_folder_id
        folder = self.settings.allowed_drive_folders.get(classification.target_folder)
        if not folder:
            raise ValueError("Classifier selected an unapproved target folder")
        return folder

    @staticmethod
    def _safe_filename(suggested: str, fallback: str) -> str:
        return sanitize_filename(suggested, fallback)

    @staticmethod
    def _idempotency_key(message: EmailMessage, attachment: Attachment) -> str:
        raw_key = f"{message.id}:{attachment.id}".encode("utf-8")
        return hashlib.sha256(raw_key).hexdigest()
