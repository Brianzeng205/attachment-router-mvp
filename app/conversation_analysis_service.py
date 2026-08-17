"""Conversation analysis orchestration; recommendations remain passive data."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Iterable

from .conversation_analyzer import ConversationAnalyzer
from .conversation_models import ConversationAnalysis, ConversationContext
from .inbox_models import AuditEvent, Conversation
from .inbox_repository import SqliteInboxRepository
from .thread_context import ThreadContextBuilder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationAnalysisSummary:
    analyzed: int = 0
    skipped: int = 0
    errors: int = 0


class ConversationAnalysisService:
    def __init__(self, repository: SqliteInboxRepository, context_builder: ThreadContextBuilder,
                 analyzer: ConversationAnalyzer, *, analyzer_name: str, analyzer_version: str,
                 model: str, prompt_version: str) -> None:
        self._repository, self._context_builder, self._analyzer = repository, context_builder, analyzer
        self._analyzer_name, self._analyzer_version = analyzer_name, analyzer_version
        self._model, self._prompt_version = model, prompt_version

    def analyze_all(self, conversations: Iterable[Conversation]) -> ConversationAnalysisSummary:
        summary = ConversationAnalysisSummary()
        for conversation in conversations:
            try:
                outcome = self.analyze_conversation(conversation)
                summary = ConversationAnalysisSummary(summary.analyzed + int(outcome == "analyzed"),
                    summary.skipped + int(outcome == "skipped"), summary.errors)
            except Exception:
                logger.exception("Conversation analysis failed conversation_id=%s", conversation.id)
                summary = ConversationAnalysisSummary(summary.analyzed, summary.skipped, summary.errors + 1)
        return summary

    def analyze_conversation(self, conversation: Conversation) -> str:
        context = self._context_builder.build(conversation, self._repository.list_messages_for_conversation(conversation.id))
        fingerprint = _analysis_fingerprint(context, self._analyzer_name, self._analyzer_version, self._model,
                                            self._prompt_version)
        if self._repository.get_successful_conversation_analysis_run(conversation.id, fingerprint):
            return "skipped"
        run = self._repository.start_conversation_analysis_run(
            conversation_id=conversation.id, analyzer=self._analyzer_name, analyzer_version=self._analyzer_version,
            model=self._model, prompt_version=self._prompt_version, context_fingerprint=fingerprint,
        )
        self._repository.record_audit_event(AuditEvent(
            "conversation_analysis_started", "conversation_analysis_run", run.id,
            metadata={"conversation_id": conversation.id, "included_message_count": context.included_message_count,
                      "context_truncated": context.truncated, "analyzer_version": self._analyzer_version,
                      "model": self._model},
        ))
        try:
            analysis = self._analyzer.analyze(context)
            if not isinstance(analysis, ConversationAnalysis):
                raise ValueError("Conversation Analyzer returned an invalid analysis object")
            self._repository.complete_conversation_analysis_run(run, context.latest_message_id, context.truncated, analysis)
        except Exception as exc:
            error_class = type(exc).__name__
            self._repository.fail_conversation_analysis_run(run, error_class)
            self._repository.record_audit_event(AuditEvent(
                "conversation_analysis_failed", "conversation_analysis_run", run.id,
                metadata={"conversation_id": conversation.id, "included_message_count": context.included_message_count,
                          "context_truncated": context.truncated, "failure_class": error_class},
            ))
            raise
        self._repository.record_audit_event(AuditEvent(
            "conversation_analysis_succeeded", "conversation_analysis_run", run.id,
            metadata={"conversation_id": conversation.id, "included_message_count": context.included_message_count,
                      "context_truncated": context.truncated, "priority": analysis.priority,
                      "analyzer_version": self._analyzer_version, "model": self._model},
        ))
        return "analyzed"


def _analysis_fingerprint(context: ConversationContext, analyzer_name: str, analyzer_version: str,
                          model: str, prompt_version: str) -> str:
    value = ":".join((context.context_fingerprint, analyzer_name, analyzer_version, model, prompt_version))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
