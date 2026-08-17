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
from .knowledge import KnowledgeIngestionService, SqliteKnowledgeRetriever
from .knowledge_retrieval_service import KnowledgeRetrievalService
from .claude_reply_draft_generator import ClaudeGroundedReplyGenerator
from .reply_draft_input import ReplyDraftInput
from .reply_draft_service import ReplyDraftService
from .decision_policy import DefaultDecisionPolicy
from .review_queue_service import ReviewQueueService
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
            ingestion = MessageIngestionService(repository)
            inbox = InboxAnalysisService(repository, ClaudeInboxAnalyzer.from_settings(settings), analyzer_name=f"claude_inbox:{settings.inbox_analyzer_version}", model=settings.inbox_analyzer_model, prompt_version=settings.inbox_analyzer_prompt_version)
            context_builder = ThreadContextBuilder(settings.max_thread_messages, settings.max_thread_context_chars, settings.thread_context_builder_version)
            conversation = ConversationAnalysisService(repository, context_builder, ClaudeConversationAnalyzer.from_settings(settings), analyzer_name="claude_conversation", analyzer_version=settings.conversation_analyzer_version, model=settings.conversation_analyzer_model, prompt_version=settings.conversation_analyzer_prompt_version)
            try: KnowledgeIngestionService(repository, settings.knowledge_dir, settings.knowledge_chunk_max_chars, settings.knowledge_chunk_overlap_chars, settings.knowledge_index_version).ingest_all()
            except FileNotFoundError: logging.getLogger(__name__).info("Knowledge directory is unavailable; retrieval skipped")
            retrieval = KnowledgeRetrievalService(repository, SqliteKnowledgeRetriever(repository), limit=settings.knowledge_retrieval_limit, retriever_version=settings.knowledge_retriever_version, index_version=settings.knowledge_index_version)
            drafting = ReplyDraftService(
                repository, ClaudeGroundedReplyGenerator.from_settings(settings), generator_name="claude_grounded_reply",
                generator_version=settings.reply_draft_generator_version, model=settings.reply_draft_generator_model,
                prompt_version=settings.reply_draft_prompt_version,
            )
            decision_policy = DefaultDecisionPolicy()
            review_queue = ReviewQueueService(repository, policy_configuration={
                "rule_version": decision_policy.rule_version,
                "min_draft_confidence": decision_policy.min_draft_confidence,
                "min_conversation_confidence": decision_policy.min_conversation_confidence,
            })
            processor = AttachmentProcessor(_StaticEmailClient(messages), build_classifier(settings), build_drive(settings), SqliteStateManager(settings.state_db_path), settings)
            return process_poll_cycle(messages=messages, repository=repository, message_ingestion_service=ingestion, inbox_analysis_service=inbox, conversation_analysis_service=conversation, knowledge_retrieval_service=retrieval, reply_draft_service=drafting, thread_context_builder=context_builder, decision_policy=decision_policy, review_queue_service=review_queue, attachment_processor=processor)
        finally:
            repository.close()
    except Exception:
        # The existing attachment router remains independently operable.
        logging.getLogger(__name__).exception("Inbox persistence path could not be initialised")
    return AttachmentProcessor(_StaticEmailClient(messages), build_classifier(settings), build_drive(settings), SqliteStateManager(settings.state_db_path), settings).process_all()


def process_poll_cycle(*, messages, repository, message_ingestion_service, inbox_analysis_service,
                       conversation_analysis_service, knowledge_retrieval_service, attachment_processor,
                       reply_draft_service=None, thread_context_builder=None,
                       decision_policy=None, review_queue_service=None):
    """Injectable Phase-6 seam; domain services own policy rules and persistence idempotency."""
    try:
        message_ingestion_service.ingest_all(messages)
        inbox_analysis_service.analyze_all(messages)
        conversation_analysis_service.analyze_all(repository.list_conversations())
        for conversation in repository.list_conversations():
            persisted = repository.latest_successful_conversation_analysis(conversation.id)
            if persisted:
                analysis_id, analysis = persisted
                knowledge_retrieval_service.retrieve(conversation.id, analysis_id, analysis)
                if reply_draft_service and thread_context_builder:
                    retrieval = repository.latest_successful_retrieval(conversation.id, analysis_id)
                    if not retrieval:
                        continue
                    retrieval_run_id, matches = retrieval
                    context = thread_context_builder.build(
                        conversation, repository.list_messages_for_conversation(conversation.id),
                    )
                    try:
                        draft_outcome = reply_draft_service.create_draft(
                            ReplyDraftInput.from_context(context, analysis, retrieval_run_id, matches),
                            conversation_analysis_id=analysis_id,
                        )
                        if decision_policy and review_queue_service and draft_outcome.draft:
                            persisted_draft = draft_outcome.draft
                            draft_fingerprint = repository.get_reply_draft_run_fingerprint(persisted_draft.draft_run_id)
                            if not draft_fingerprint:
                                raise ValueError("Successful reply draft is missing its input fingerprint")
                            policy_decision = decision_policy.evaluate(
                                conversation_analysis=analysis, reply_draft=persisted_draft.draft,
                            )
                            review_queue_service.record_decision(
                                conversation_id=conversation.id, conversation_analysis_id=analysis_id,
                                reply_draft_id=persisted_draft.id, reply_draft_fingerprint=draft_fingerprint,
                                decision=policy_decision,
                            )
                    except Exception:
                        logging.getLogger(__name__).exception("Local drafting/policy/review failed conversation_id=%s", conversation.id)
    except Exception:
        logging.getLogger(__name__).exception("Inbox agent branch failed")
    return attachment_processor.process_all()


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
