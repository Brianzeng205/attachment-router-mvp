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
    connection.execute("""CREATE TABLE IF NOT EXISTS policy_decisions (
        id INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        reply_draft_id INTEGER NOT NULL REFERENCES reply_drafts(id),
        conversation_analysis_id INTEGER NOT NULL REFERENCES conversation_analyses(id),
        policy_version TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('ready_for_review','human_review_required','blocked','no_action')),
        reason_codes_json TEXT NOT NULL,
        primary_reason TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(conversation_id, input_fingerprint)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS human_review_items (
        id INTEGER PRIMARY KEY,
        policy_decision_id INTEGER NOT NULL UNIQUE REFERENCES policy_decisions(id),
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        reply_draft_id INTEGER NOT NULL REFERENCES reply_drafts(id),
        review_type TEXT NOT NULL CHECK(review_type IN ('standard_review','required_review','blocked_resolution')),
        status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','changes_requested')),
        reviewer_id TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        resolved_at TEXT
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS human_review_events (
        id INTEGER PRIMARY KEY,
        review_item_id INTEGER NOT NULL REFERENCES human_review_items(id),
        event_type TEXT NOT NULL CHECK(event_type IN ('created','approved','rejected','changes_requested')),
        reviewer_id TEXT,
        note TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_policy_decisions_draft ON policy_decisions(reply_draft_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_human_review_pending ON human_review_items(status, created_at, id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_human_review_events_item ON human_review_events(review_item_id, id)")
    review_columns = {row[1] for row in connection.execute("PRAGMA table_info(human_review_items)").fetchall()}
    if "approved_draft_body" not in review_columns:
        connection.execute("ALTER TABLE human_review_items ADD COLUMN approved_draft_body TEXT")
    connection.execute("""CREATE TABLE IF NOT EXISTS execution_intents (
        execution_id TEXT PRIMARY KEY,
        source_review_item_id INTEGER NOT NULL UNIQUE REFERENCES human_review_items(id),
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        provider_thread_id TEXT NOT NULL,
        in_reply_to_provider_message_id TEXT NOT NULL,
        action_type TEXT NOT NULL CHECK(action_type IN ('send_approved_reply')),
        approved_body TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('pending','processing','retry_wait','completed','failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        next_attempt_at TEXT,
        claim_token TEXT,
        claimed_by TEXT,
        claimed_at TEXT,
        lease_expires_at TEXT,
        completed_at TEXT,
        failure_code TEXT,
        failure_metadata_json TEXT,
        schema_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(length(approved_body) BETWEEN 1 AND 50000),
        CHECK((status = 'processing' AND claim_token IS NOT NULL AND claimed_by IS NOT NULL
               AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL)
              OR (status != 'processing' AND claim_token IS NULL AND claimed_by IS NULL
                  AND claimed_at IS NULL AND lease_expires_at IS NULL)),
        CHECK((status = 'retry_wait' AND next_attempt_at IS NOT NULL)
              OR (status != 'retry_wait' AND next_attempt_at IS NULL)),
        CHECK((status = 'completed' AND completed_at IS NOT NULL)
              OR (status != 'completed' AND completed_at IS NULL))
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_claimable "
        "ON execution_intents(status, next_attempt_at, created_at, execution_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_processing_lease "
        "ON execution_intents(status, lease_expires_at)"
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS execution_events (
        id INTEGER PRIMARY KEY,
        execution_id TEXT NOT NULL REFERENCES execution_intents(execution_id),
        event_type TEXT NOT NULL CHECK(event_type IN
            ('created','claimed','completed','retry_scheduled','failed','claim_recovered')),
        attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
        failure_code TEXT,
        created_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_events_intent ON execution_events(execution_id, id)"
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS runtime_runs (
        id INTEGER PRIMARY KEY,
        trigger_type TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed','interrupted','abandoned')),
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        error_class TEXT,
        lock_outcome TEXT NOT NULL,
        messages_polled INTEGER CHECK(messages_polled >= 0),
        inbox_errors INTEGER CHECK(inbox_errors >= 0),
        attachments_uploaded INTEGER CHECK(attachments_uploaded >= 0),
        attachments_skipped INTEGER CHECK(attachments_skipped >= 0),
        attachment_errors INTEGER CHECK(attachment_errors >= 0),
        outcome_status TEXT CHECK(outcome_status IS NULL OR outcome_status = 'partial'),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    runtime_columns = {row[1] for row in connection.execute("PRAGMA table_info(runtime_runs)").fetchall()}
    for column_name in (
        "messages_polled", "inbox_errors", "attachments_uploaded",
        "attachments_skipped", "attachment_errors",
    ):
        if column_name not in runtime_columns:
            connection.execute(
                f"ALTER TABLE runtime_runs ADD COLUMN {column_name} INTEGER CHECK({column_name} >= 0)"
            )
    if "outcome_status" not in runtime_columns:
        connection.execute(
            "ALTER TABLE runtime_runs ADD COLUMN outcome_status TEXT "
            "CHECK(outcome_status IS NULL OR outcome_status = 'partial')"
        )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_runtime_runs_status ON runtime_runs(status, id)")
    connection.commit()
