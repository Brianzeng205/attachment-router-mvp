"""Persistence-backed analysis orchestration with no action execution."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Iterable

from .analysis_models import InboxAnalysis
from .inbox_analyzer import InboxAnalyzer
from .inbox_models import AuditEvent
from .inbox_repository import SqliteInboxRepository
from .models import EmailMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalysisSummary:
    analyzed: int = 0
    skipped: int = 0
    errors: int = 0


class InboxAnalysisService:
    def __init__(self, repository: SqliteInboxRepository, analyzer: InboxAnalyzer, *, analyzer_name: str,
                 model: str, prompt_version: str) -> None:
        self._repository = repository
        self._analyzer = analyzer
        self._analyzer_name = analyzer_name
        self._model = model
        self._prompt_version = prompt_version

    def analyze_all(self, messages: Iterable[EmailMessage], provider: str = "gmail") -> AnalysisSummary:
        summary = AnalysisSummary()
        for email in messages:
            try:
                outcome = self.analyze_email(email, provider)
                summary = AnalysisSummary(summary.analyzed + int(outcome == "analyzed"),
                                          summary.skipped + int(outcome == "skipped"), summary.errors)
            except Exception:
                logger.exception("Inbox analysis failed message_id=%s", email.id)
                summary = AnalysisSummary(summary.analyzed, summary.skipped, summary.errors + 1)
        return summary

    def analyze_email(self, email: EmailMessage, provider: str = "gmail") -> str:
        message = self._repository.get_message_by_provider_id(provider, email.id)
        if message is None or message.id is None:
            raise ValueError("Message must be successfully persisted before analysis")
        fingerprint = _fingerprint(message.content_hash, self._analyzer_name, self._model, self._prompt_version)
        if self._repository.get_successful_analysis_run(message.id, fingerprint):
            return "skipped"
        run = self._repository.start_analysis_run(
            message_id=message.id, analyzer=self._analyzer_name, model=self._model,
            prompt_version=self._prompt_version, input_fingerprint=fingerprint,
        )
        self._repository.record_audit_event(AuditEvent(
            "analysis_started", "analysis_run", run.id,
            metadata={"message_id": message.id, "analyzer": self._analyzer_name, "model": self._model,
                      "prompt_version": self._prompt_version},
        ))
        try:
            analysis = self._analyzer.analyze(message)
            if not isinstance(analysis, InboxAnalysis):
                raise ValueError("Inbox Analyzer returned an invalid analysis object")
            self._repository.complete_analysis_run(run, analysis)
        except Exception as exc:
            error_class = type(exc).__name__
            self._repository.fail_analysis_run(run, error_class)
            self._repository.record_audit_event(AuditEvent(
                "analysis_failed", "analysis_run", run.id,
                metadata={"message_id": message.id, "analyzer": self._analyzer_name,
                          "failure_class": error_class},
            ))
            raise
        self._repository.record_audit_event(AuditEvent(
            "analysis_succeeded", "analysis_run", run.id,
            metadata={"message_id": message.id, "analyzer": self._analyzer_name, "model": self._model,
                      "category": analysis.category, "priority": analysis.priority},
        ))
        return "analyzed"


def _fingerprint(content_hash: str, analyzer_name: str, model: str, prompt_version: str) -> str:
    value = ":".join((content_hash, analyzer_name, model, prompt_version)).encode("utf-8")
    return hashlib.sha256(value).hexdigest()
