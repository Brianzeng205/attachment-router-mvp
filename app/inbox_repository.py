"""SQLite persistence for normalized messages, conversations, and audit events."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from .inbox_models import AuditEvent, Conversation, InboxMessage
from .migrations import initialize_schema


class SqliteInboxRepository:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        initialize_schema(self.connection)

    def close(self) -> None:
        self.connection.close()

    def get_message_by_provider_id(self, provider: str, provider_message_id: str) -> InboxMessage | None:
        row = self.connection.execute(
            "SELECT * FROM messages WHERE provider = ? AND provider_message_id = ?",
            (provider, provider_message_id),
        ).fetchone()
        return _message_from_row(row) if row else None

    def get_conversation_by_provider_thread_id(self, provider: str, provider_thread_id: str) -> Conversation | None:
        row = self.connection.execute(
            "SELECT * FROM conversations WHERE provider = ? AND provider_thread_id = ?",
            (provider, provider_thread_id),
        ).fetchone()
        return _conversation_from_row(row) if row else None

    def get_or_create_conversation(self, provider: str, provider_thread_id: str, latest_message_at: str) -> tuple[Conversation, bool]:
        with self.connection:
            return self._get_or_create_conversation(provider, provider_thread_id, latest_message_at)

    def upsert_message(self, message: InboxMessage, conversation_id: int) -> tuple[InboxMessage, bool]:
        with self.connection:
            return self._upsert_message(message, conversation_id)

    def record_audit_event(self, event: AuditEvent) -> AuditEvent:
        with self.connection:
            return self._record_audit_event(event)

    def ingest(self, message: InboxMessage) -> tuple[InboxMessage, Conversation, bool, bool]:
        """Atomically persist one message and its safe ingestion audit trail."""
        with self.connection:
            conversation, conversation_created = self._get_or_create_conversation(
                message.provider, message.provider_thread_id, message.received_at,
            )
            stored, message_created = self._upsert_message(message, conversation.id)
            if message_created:
                self._record_audit_event(AuditEvent(
                    "message_ingested", "message", stored.id or 0,
                    metadata=_safe_metadata(message),
                ))
                event_type = "conversation_created" if conversation_created else "conversation_updated"
                self._record_audit_event(AuditEvent(
                    event_type, "conversation", conversation.id,
                    metadata={"provider": message.provider, "provider_thread_id": message.provider_thread_id},
                ))
            return stored, conversation, message_created, conversation_created

    def _get_or_create_conversation(self, provider: str, provider_thread_id: str, latest_message_at: str) -> tuple[Conversation, bool]:
        existing = self.get_conversation_by_provider_thread_id(provider, provider_thread_id)
        if existing:
            if latest_message_at > existing.latest_message_at:
                self.connection.execute(
                    "UPDATE conversations SET latest_message_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (latest_message_at, existing.id),
                )
                existing = Conversation(existing.id, existing.provider, existing.provider_thread_id, existing.status, latest_message_at)
            return existing, False
        cursor = self.connection.execute(
            """INSERT INTO conversations (provider, provider_thread_id, status, latest_message_at)
               VALUES (?, ?, 'open', ?)""",
            (provider, provider_thread_id, latest_message_at),
        )
        return Conversation(cursor.lastrowid, provider, provider_thread_id, "open", latest_message_at), True

    def _upsert_message(self, message: InboxMessage, conversation_id: int) -> tuple[InboxMessage, bool]:
        existing = self.get_message_by_provider_id(message.provider, message.provider_message_id)
        recipients_json = json.dumps(list(message.recipients), ensure_ascii=False, separators=(",", ":"))
        if existing:
            self.connection.execute(
                """UPDATE messages SET conversation_id = ?, provider_thread_id = ?, sender = ?, recipients_json = ?,
                   subject = ?, body_text = ?, received_at = ?, ingestion_state = ?, content_hash = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (conversation_id, message.provider_thread_id, message.sender, recipients_json, message.subject,
                 message.body_text, message.received_at, message.ingestion_state, message.content_hash, existing.id),
            )
            return InboxMessage(**{**message.__dict__, "id": existing.id, "conversation_id": conversation_id}), False
        cursor = self.connection.execute(
            """INSERT INTO messages (
                conversation_id, provider, provider_message_id, provider_thread_id, sender, recipients_json,
                subject, body_text, received_at, ingestion_state, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (conversation_id, message.provider, message.provider_message_id, message.provider_thread_id,
             message.sender, recipients_json, message.subject, message.body_text, message.received_at,
             message.ingestion_state, message.content_hash),
        )
        return InboxMessage(**{**message.__dict__, "id": cursor.lastrowid, "conversation_id": conversation_id}), True

    def _record_audit_event(self, event: AuditEvent) -> AuditEvent:
        metadata = json.dumps(dict(event.metadata or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cursor = self.connection.execute(
            """INSERT INTO audit_events (event_type, entity_type, entity_id, actor, correlation_id, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event.event_type, event.entity_type, event.entity_id, event.actor, event.correlation_id, metadata),
        )
        return AuditEvent(**{**event.__dict__, "id": cursor.lastrowid})


def _message_from_row(row: sqlite3.Row) -> InboxMessage:
    return InboxMessage(
        id=row["id"], conversation_id=row["conversation_id"], provider=row["provider"],
        provider_message_id=row["provider_message_id"], provider_thread_id=row["provider_thread_id"],
        sender=row["sender"], recipients=tuple(json.loads(row["recipients_json"])), subject=row["subject"],
        body_text=row["body_text"], received_at=row["received_at"], ingestion_state=row["ingestion_state"],
        content_hash=row["content_hash"],
    )


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(row["id"], row["provider"], row["provider_thread_id"], row["status"], row["latest_message_at"])


def _safe_metadata(message: InboxMessage) -> Mapping[str, object]:
    """Deliberately omit body text and other unnecessary sensitive content."""
    return {
        "provider": message.provider,
        "provider_message_id": message.provider_message_id,
        "provider_thread_id": message.provider_thread_id,
        "content_hash": message.content_hash,
    }
