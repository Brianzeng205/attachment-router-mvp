"""Message-level ingestion only; it performs no AI analysis or actions."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Iterable

from .inbox_models import InboxMessage
from .inbox_repository import SqliteInboxRepository
from .models import EmailMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionSummary:
    ingested: int = 0
    duplicates: int = 0
    errors: int = 0


class MessageIngestionService:
    def __init__(self, repository: SqliteInboxRepository, provider: str = "gmail") -> None:
        self._repository = repository
        self._provider = provider

    def ingest_all(self, messages: Iterable[EmailMessage]) -> IngestionSummary:
        summary = IngestionSummary()
        for message in messages:
            try:
                created = self.ingest(message)
                summary = IngestionSummary(
                    summary.ingested + int(created), summary.duplicates + int(not created), summary.errors,
                )
            except Exception:
                logger.exception("Inbox message persistence failed message_id=%s", message.id)
                summary = IngestionSummary(summary.ingested, summary.duplicates, summary.errors + 1)
        return summary

    def ingest(self, message: EmailMessage) -> bool:
        if not message.id or not message.thread_id:
            raise ValueError("Gmail message and thread IDs are required for inbox ingestion")
        normalized = InboxMessage(
            provider=self._provider,
            provider_message_id=message.id,
            provider_thread_id=message.thread_id,
            sender=message.sender.strip(),
            recipients=tuple(item.strip() for item in message.recipients if item.strip()),
            subject=message.subject.strip(),
            body_text=message.body,
            received_at=message.received_at,
            ingestion_state="ingested",
            content_hash=_content_hash(message, self._provider),
        )
        _, _, message_created, _ = self._repository.ingest(normalized)
        return message_created


def _content_hash(message: EmailMessage, provider: str) -> str:
    """Stable fingerprint of normalized provider facts, not an idempotency key."""
    value = {
        "provider": provider,
        "provider_message_id": message.id,
        "provider_thread_id": message.thread_id,
        "sender": message.sender.strip(),
        "recipients": [item.strip() for item in message.recipients if item.strip()],
        "subject": message.subject.strip(),
        "body_text": message.body,
        "received_at": message.received_at,
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
