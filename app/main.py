"""One polling cycle; schedule this command externally when ready."""
import logging

from .config import Settings
from .claude_classifier import ClaudeDocumentClassifier
from .gmail_client import GmailClient
from .google_drive import GoogleDriveClient
from .inbox_repository import SqliteInboxRepository
from .message_ingestion import MessageIngestionService
from .orchestrator import AttachmentProcessor, ProcessingSummary
from .state import SqliteStateManager


def build_state() -> SqliteStateManager:
    return SqliteStateManager(Settings.from_env().state_db_path)


def build_drive(settings: Settings | None = None) -> GoogleDriveClient:
    """Create the real Drive adapter; email/classifier composition comes later."""
    return GoogleDriveClient.from_settings(settings or Settings.from_env())


def build_classifier(settings: Settings | None = None) -> ClaudeDocumentClassifier:
    """Create the Claude adapter; email/classifier composition comes later."""
    return ClaudeDocumentClassifier.from_settings(settings or Settings.from_env())


def build_email(settings: Settings | None = None) -> GmailClient:
    return GmailClient.from_settings(settings or Settings.from_env())


def run_once(settings: Settings | None = None) -> ProcessingSummary:
    settings = settings or Settings.from_env()
    email = build_email(settings)
    messages = tuple(email.list_messages())
    try:
        repository = SqliteInboxRepository(settings.state_db_path)
        try:
            ingestion_summary = MessageIngestionService(repository).ingest_all(messages)
            if ingestion_summary.errors:
                logging.getLogger(__name__).error(
                    "Inbox ingestion completed with errors ingested=%s duplicates=%s errors=%s",
                    ingestion_summary.ingested, ingestion_summary.duplicates, ingestion_summary.errors,
                )
        finally:
            repository.close()
    except Exception:
        # The existing attachment router remains independently operable.
        logging.getLogger(__name__).exception("Inbox persistence path could not be initialised")
    processor = AttachmentProcessor(
        _StaticEmailClient(messages),
        build_classifier(settings),
        build_drive(settings),
        SqliteStateManager(settings.state_db_path),
        settings,
    )
    summary = processor.process_all()
    logging.getLogger(__name__).info(
        "Polling cycle complete uploaded=%s skipped=%s errors=%s",
        summary.uploaded, summary.skipped, summary.errors,
    )
    return summary


class _StaticEmailClient:
    """Reuse the single Gmail retrieval for independent ingestion and routing paths."""

    def __init__(self, messages: tuple) -> None:
        self._messages = messages

    def list_messages(self):
        return self._messages


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        run_once()
    except Exception:
        logging.getLogger(__name__).exception("Polling cycle failed")
        raise SystemExit(1)
