"""One polling cycle; schedule this command externally when ready."""
import logging

from .config import Settings
from .claude_classifier import ClaudeDocumentClassifier
from .claude_inbox_analyzer import ClaudeInboxAnalyzer
from .claude_conversation_analyzer import ClaudeConversationAnalyzer
from .gmail_client import GmailClient
from .google_drive import GoogleDriveClient
from .inbox_repository import SqliteInboxRepository
from .inbox_analysis_service import InboxAnalysisService
from .conversation_analysis_service import ConversationAnalysisService
from .message_ingestion import MessageIngestionService
from .thread_context import ThreadContextBuilder
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
            try:
                analyzer = ClaudeInboxAnalyzer.from_settings(settings)
                analysis_summary = InboxAnalysisService(
                    repository, analyzer, analyzer_name=f"claude_inbox:{settings.inbox_analyzer_version}", model=settings.inbox_analyzer_model,
                    prompt_version=settings.inbox_analyzer_prompt_version,
                ).analyze_all(messages)
                if analysis_summary.errors:
                    logging.getLogger(__name__).error(
                        "Inbox analysis completed with errors analyzed=%s skipped=%s errors=%s",
                        analysis_summary.analyzed, analysis_summary.skipped, analysis_summary.errors,
                    )
            except Exception:
                # Analysis is independent from the existing attachment-routing workflow.
                logging.getLogger(__name__).exception("Inbox analysis path could not be initialised")
            try:
                conversation_analyzer = ClaudeConversationAnalyzer.from_settings(settings)
                context_builder = ThreadContextBuilder(settings.max_thread_messages, settings.max_thread_context_chars,
                                                       settings.thread_context_builder_version)
                conversation_summary = ConversationAnalysisService(
                    repository, context_builder, conversation_analyzer, analyzer_name="claude_conversation",
                    analyzer_version=settings.conversation_analyzer_version, model=settings.conversation_analyzer_model,
                    prompt_version=settings.conversation_analyzer_prompt_version,
                ).analyze_all(repository.list_conversations())
                if conversation_summary.errors:
                    logging.getLogger(__name__).error(
                        "Conversation analysis completed with errors analyzed=%s skipped=%s errors=%s",
                        conversation_summary.analyzed, conversation_summary.skipped, conversation_summary.errors,
                    )
            except Exception:
                logging.getLogger(__name__).exception("Conversation analysis path could not be initialised")
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
