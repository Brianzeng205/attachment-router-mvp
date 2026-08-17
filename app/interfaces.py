from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from .models import Attachment, EmailMessage
from .analysis_models import InboxAnalysis
from .inbox_models import InboxMessage


class EmailClient(Protocol):
    """Provider adapter: return messages with attachment bytes populated."""

    def list_messages(self) -> Iterable[EmailMessage]: ...


class DocumentClassifier(Protocol):
    """Classifier adapter: return Claude's decoded JSON object, not free text."""

    def classify(self, message: EmailMessage, attachment: Attachment) -> Mapping[str, object]: ...


class DriveClient(Protocol):
    def upload(
        self, *, folder_id: str, filename: str, content: bytes,
        mime_type: str | None, idempotency_key: str,
    ) -> str: ...


class StateManager(Protocol):
    def is_processed(self, email_id: str, attachment_id: str) -> bool: ...

    def mark_processed(self, email_id: str, attachment_id: str) -> None: ...


class InboxAnalyzer(Protocol):
    def analyze(self, message: InboxMessage) -> InboxAnalysis: ...
