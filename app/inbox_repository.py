"""SQLite persistence for normalized messages, conversations, and audit events."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from .inbox_models import AuditEvent, Conversation, InboxMessage
from .inbox_models import AnalysisRun
from .analysis_models import InboxAnalysis
from .conversation_models import ConversationAnalysis, ConversationAnalysisRun
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

    def list_conversations(self) -> list[Conversation]:
        rows = self.connection.execute("SELECT * FROM conversations ORDER BY id").fetchall()
        return [_conversation_from_row(row) for row in rows]

    def list_messages_for_conversation(self, conversation_id: int) -> list[InboxMessage]:
        rows = self.connection.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY received_at ASC, id ASC", (conversation_id,),
        ).fetchall()
        return [_message_from_row(row) for row in rows]

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

    def get_successful_analysis_run(self, message_id: int, input_fingerprint: str) -> AnalysisRun | None:
        row = self.connection.execute(
            "SELECT * FROM analysis_runs WHERE message_id = ? AND input_fingerprint = ? AND status = 'succeeded'",
            (message_id, input_fingerprint),
        ).fetchone()
        return _analysis_run_from_row(row) if row else None

    def start_analysis_run(self, *, message_id: int, analyzer: str, model: str, prompt_version: str,
                           input_fingerprint: str) -> AnalysisRun:
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM analysis_runs WHERE message_id = ? AND input_fingerprint = ?",
                (message_id, input_fingerprint),
            ).fetchone()
            if row:
                self.connection.execute(
                    """UPDATE analysis_runs SET status = 'running', error_class = NULL,
                       started_at = CURRENT_TIMESTAMP, completed_at = NULL WHERE id = ?""",
                    (row["id"],),
                )
                return AnalysisRun(row["id"], message_id, analyzer, model, prompt_version, input_fingerprint, "running")
            cursor = self.connection.execute(
                """INSERT INTO analysis_runs (message_id, analyzer, model, prompt_version, input_fingerprint, status)
                   VALUES (?, ?, ?, ?, ?, 'running')""",
                (message_id, analyzer, model, prompt_version, input_fingerprint),
            )
            return AnalysisRun(cursor.lastrowid, message_id, analyzer, model, prompt_version, input_fingerprint, "running")

    def complete_analysis_run(self, run: AnalysisRun, analysis: InboxAnalysis) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO message_analyses (
                    analysis_run_id, message_id, category, intent, priority, urgency, summary, customer_name,
                    order_numbers_json, dates_json, requirements_json, confidence, needs_human, human_reason,
                    recommended_action
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.id, run.message_id, analysis.category, analysis.intent, analysis.priority, analysis.urgency,
                 analysis.summary, analysis.customer_name, json.dumps(list(analysis.order_numbers)),
                 json.dumps(list(analysis.dates)), json.dumps(list(analysis.requirements)), analysis.confidence,
                 int(analysis.needs_human), analysis.human_reason, analysis.recommended_action),
            )
            self.connection.execute(
                "UPDATE analysis_runs SET status = 'succeeded', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (run.id,),
            )

    def fail_analysis_run(self, run: AnalysisRun, error_class: str) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE analysis_runs SET status = 'failed', error_class = ?, completed_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (error_class, run.id),
            )

    def get_successful_conversation_analysis_run(self, conversation_id: int, context_fingerprint: str) -> ConversationAnalysisRun | None:
        row = self.connection.execute(
            """SELECT * FROM conversation_analysis_runs WHERE conversation_id = ? AND context_fingerprint = ?
               AND status = 'succeeded'""", (conversation_id, context_fingerprint),
        ).fetchone()
        return _conversation_analysis_run_from_row(row) if row else None

    def start_conversation_analysis_run(self, *, conversation_id: int, analyzer: str, analyzer_version: str,
                                        model: str, prompt_version: str, context_fingerprint: str) -> ConversationAnalysisRun:
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM conversation_analysis_runs WHERE conversation_id = ? AND context_fingerprint = ?",
                (conversation_id, context_fingerprint),
            ).fetchone()
            if row:
                self.connection.execute(
                    """UPDATE conversation_analysis_runs SET status = 'running', error_class = NULL,
                       started_at = CURRENT_TIMESTAMP, completed_at = NULL WHERE id = ?""", (row["id"],),
                )
                return ConversationAnalysisRun(row["id"], conversation_id, analyzer, analyzer_version, model,
                                               prompt_version, context_fingerprint, "running")
            cursor = self.connection.execute(
                """INSERT INTO conversation_analysis_runs (
                    conversation_id, analyzer, analyzer_version, model, prompt_version, context_fingerprint, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'running')""",
                (conversation_id, analyzer, analyzer_version, model, prompt_version, context_fingerprint),
            )
            return ConversationAnalysisRun(cursor.lastrowid, conversation_id, analyzer, analyzer_version, model,
                                           prompt_version, context_fingerprint, "running")

    def complete_conversation_analysis_run(self, run: ConversationAnalysisRun, latest_message_id: int,
                                           context_truncated: bool, analysis: ConversationAnalysis) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO conversation_analyses (
                    conversation_analysis_run_id, conversation_id, latest_message_id, conversation_summary,
                    current_intent, priority, urgency, unresolved_requests_json, resolved_points_json,
                    order_numbers_json, relevant_dates_json, latest_sender_request, confidence, needs_human,
                    human_reason, recommended_action, context_truncated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.id, run.conversation_id, latest_message_id, analysis.conversation_summary, analysis.current_intent,
                 analysis.priority, analysis.urgency, json.dumps(list(analysis.unresolved_requests)),
                 json.dumps(list(analysis.resolved_points)), json.dumps(list(analysis.order_numbers)),
                 json.dumps(list(analysis.relevant_dates)), analysis.latest_sender_request, analysis.confidence,
                 int(analysis.needs_human), analysis.human_reason, analysis.recommended_action, int(context_truncated)),
            )
            self.connection.execute(
                """UPDATE conversation_analysis_runs SET status = 'succeeded', completed_at = CURRENT_TIMESTAMP
                   WHERE id = ?""", (run.id,),
            )

    def fail_conversation_analysis_run(self, run: ConversationAnalysisRun, error_class: str) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE conversation_analysis_runs SET status = 'failed', error_class = ?,
                   completed_at = CURRENT_TIMESTAMP WHERE id = ?""", (error_class, run.id),
            )

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


def _analysis_run_from_row(row: sqlite3.Row) -> AnalysisRun:
    return AnalysisRun(
        row["id"], row["message_id"], row["analyzer"], row["model"], row["prompt_version"],
        row["input_fingerprint"], row["status"], row["error_class"],
    )


def _conversation_analysis_run_from_row(row: sqlite3.Row) -> ConversationAnalysisRun:
    return ConversationAnalysisRun(row["id"], row["conversation_id"], row["analyzer"], row["analyzer_version"],
                                   row["model"], row["prompt_version"], row["context_fingerprint"],
                                   row["status"], row["error_class"])


def _safe_metadata(message: InboxMessage) -> Mapping[str, object]:
    """Deliberately omit body text and other unnecessary sensitive content."""
    return {
        "provider": message.provider,
        "provider_message_id": message.provider_message_id,
        "provider_thread_id": message.provider_thread_id,
        "content_hash": message.content_hash,
    }
