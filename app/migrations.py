"""Small, additive SQLite schema initialisation for the MVP."""

from __future__ import annotations

import sqlite3


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create all tables without changing or discarding existing state."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS processed_attachments (
            email_id TEXT NOT NULL, attachment_id TEXT NOT NULL,
            processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (email_id, attachment_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_thread_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            latest_message_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (provider, provider_thread_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            provider TEXT NOT NULL,
            provider_message_id TEXT NOT NULL,
            provider_thread_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipients_json TEXT NOT NULL,
            subject TEXT NOT NULL,
            body_text TEXT NOT NULL,
            received_at TEXT NOT NULL,
            ingestion_state TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (provider, provider_message_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            actor TEXT NOT NULL,
            correlation_id TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON audit_events(entity_type, entity_id)")
    connection.commit()
