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
    connection.execute(
        """CREATE TABLE IF NOT EXISTS analysis_runs (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            analyzer TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            error_class TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (message_id, input_fingerprint)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS message_analyses (
            id INTEGER PRIMARY KEY,
            analysis_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
            message_id INTEGER NOT NULL REFERENCES messages(id),
            category TEXT NOT NULL,
            intent TEXT NOT NULL,
            priority TEXT NOT NULL,
            urgency TEXT NOT NULL,
            summary TEXT NOT NULL,
            customer_name TEXT,
            order_numbers_json TEXT NOT NULL,
            dates_json TEXT NOT NULL,
            requirements_json TEXT NOT NULL,
            confidence REAL NOT NULL,
            needs_human INTEGER NOT NULL,
            human_reason TEXT,
            recommended_action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (analysis_run_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS conversation_analysis_runs (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            analyzer TEXT NOT NULL,
            analyzer_version TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            context_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            error_class TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (conversation_id, context_fingerprint)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS conversation_analyses (
            id INTEGER PRIMARY KEY,
            conversation_analysis_run_id INTEGER NOT NULL REFERENCES conversation_analysis_runs(id),
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            latest_message_id INTEGER NOT NULL REFERENCES messages(id),
            conversation_summary TEXT NOT NULL,
            current_intent TEXT NOT NULL,
            priority TEXT NOT NULL,
            urgency TEXT NOT NULL,
            unresolved_requests_json TEXT NOT NULL,
            resolved_points_json TEXT NOT NULL,
            order_numbers_json TEXT NOT NULL,
            relevant_dates_json TEXT NOT NULL,
            latest_sender_request TEXT,
            confidence REAL NOT NULL,
            needs_human INTEGER NOT NULL,
            human_reason TEXT,
            recommended_action TEXT NOT NULL,
            context_truncated INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (conversation_analysis_run_id)
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON audit_events(entity_type, entity_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_analysis_runs_message ON analysis_runs(message_id, status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_conversation_analysis_runs ON conversation_analysis_runs(conversation_id, status)")
    connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_documents (id INTEGER PRIMARY KEY, source_key TEXT UNIQUE NOT NULL, source_filename TEXT NOT NULL, title TEXT, content_hash TEXT NOT NULL, source_type TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_chunks (id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES knowledge_documents(id), chunk_index INTEGER NOT NULL, chunk_text TEXT NOT NULL, chunk_hash TEXT NOT NULL, character_count INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(document_id, chunk_index, chunk_hash))""")
    connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(chunk_id UNINDEXED, chunk_text)")
    connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_retrieval_runs (id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id), conversation_analysis_id INTEGER, query_text TEXT NOT NULL, query_fingerprint TEXT NOT NULL, retriever TEXT NOT NULL, retriever_version TEXT NOT NULL, status TEXT NOT NULL, result_count INTEGER NOT NULL DEFAULT 0, error_class TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, UNIQUE(conversation_id,query_fingerprint))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_retrieval_results (id INTEGER PRIMARY KEY, retrieval_run_id INTEGER NOT NULL REFERENCES knowledge_retrieval_runs(id), knowledge_chunk_id INTEGER NOT NULL REFERENCES knowledge_chunks(id), rank INTEGER NOT NULL, score REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(retrieval_run_id,rank))""")
    for statement in ("ALTER TABLE knowledge_retrieval_runs ADD COLUMN knowledge_index_fingerprint TEXT", "ALTER TABLE knowledge_retrieval_runs ADD COLUMN retrieval_limit INTEGER"):
        try: connection.execute(statement)
        except sqlite3.OperationalError: pass
    connection.execute("""CREATE TABLE IF NOT EXISTS reply_draft_runs (
        id INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        conversation_analysis_id INTEGER NOT NULL REFERENCES conversation_analyses(id),
        knowledge_retrieval_run_id INTEGER NOT NULL REFERENCES knowledge_retrieval_runs(id),
        generator TEXT NOT NULL, generator_version TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
        error_class TEXT, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(conversation_id, input_fingerprint)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS reply_drafts (
        id INTEGER PRIMARY KEY, draft_run_id INTEGER NOT NULL UNIQUE REFERENCES reply_draft_runs(id),
        conversation_id INTEGER NOT NULL REFERENCES conversations(id), latest_message_id INTEGER NOT NULL REFERENCES messages(id),
        draft_status TEXT NOT NULL, subject TEXT, body TEXT NOT NULL, confidence REAL NOT NULL,
        needs_review INTEGER NOT NULL, review_reason TEXT, unsupported_claims_json TEXT NOT NULL,
        response_language TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS reply_draft_grounding (
        reply_draft_id INTEGER NOT NULL REFERENCES reply_drafts(id),
        knowledge_chunk_id INTEGER NOT NULL REFERENCES knowledge_chunks(id),
        PRIMARY KEY(reply_draft_id, knowledge_chunk_id)
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_reply_draft_runs_conversation ON reply_draft_runs(conversation_id, status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_reply_draft_grounding_chunk ON reply_draft_grounding(knowledge_chunk_id)")
    connection.commit()
