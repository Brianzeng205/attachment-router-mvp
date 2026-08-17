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
from .knowledge_models import KnowledgeMatch
from .reply_draft_models import PersistedReplyDraft, ReplyDraft, ReplyDraftRun
from .policy_models import PolicyDecision
from .review_models import HumanReviewEvent, HumanReviewItem, PersistedPolicyDecision
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

    def upsert_knowledge(self, source_key, filename, text, content_hash, chunks, index_version):
        with self.connection:
            row=self.connection.execute("SELECT id,content_hash FROM knowledge_documents WHERE source_key=?",(source_key,)).fetchone()
            if row and row["content_hash"] == content_hash: return row["id"]
            if row:
                doc_id=row["id"]; self.connection.execute("UPDATE knowledge_documents SET content_hash=?,source_filename=?,active=1,updated_at=CURRENT_TIMESTAMP WHERE id=?",(content_hash,filename,doc_id))
                ids=[r[0] for r in self.connection.execute("SELECT id FROM knowledge_chunks WHERE document_id=?",(doc_id,))]
                for ident in ids: self.connection.execute("DELETE FROM knowledge_chunks_fts WHERE chunk_id=?",(str(ident),))
                self.connection.execute("DELETE FROM knowledge_chunks WHERE document_id=?",(doc_id,))
            else:
                doc_id=self.connection.execute("INSERT INTO knowledge_documents(source_key,source_filename,title,content_hash,source_type) VALUES(?,?,?,?,?)",(source_key,filename,Path(filename).stem,content_hash,"local_text")).lastrowid
            for index, chunk in enumerate(chunks):
                h=__import__('hashlib').sha256(chunk.encode()).hexdigest()
                cid=self.connection.execute("INSERT INTO knowledge_chunks(document_id,chunk_index,chunk_text,chunk_hash,character_count) VALUES(?,?,?,?,?)",(doc_id,index,chunk,h,len(chunk))).lastrowid
                self.connection.execute("INSERT INTO knowledge_chunks_fts(chunk_id,chunk_text) VALUES(?,?)",(str(cid),chunk))
            return doc_id

    def search_knowledge(self, terms, limit):
        rows=self.connection.execute("""SELECT c.id,c.document_id,d.source_filename,d.title,c.chunk_text,bm25(knowledge_chunks_fts) score FROM knowledge_chunks_fts f JOIN knowledge_chunks c ON c.id=CAST(f.chunk_id AS INTEGER) JOIN knowledge_documents d ON d.id=c.document_id WHERE knowledge_chunks_fts MATCH ? AND c.active=1 AND d.active=1 ORDER BY score,c.id LIMIT ?""",(terms,limit)).fetchall()
        return [KnowledgeMatch(r["id"],r["document_id"],r["source_filename"],r["title"],r["chunk_text"],float(r["score"]),i+1) for i,r in enumerate(rows)]

    def knowledge_index_fingerprint(self, version):
        import hashlib
        rows=self.connection.execute("SELECT source_key,content_hash FROM knowledge_documents WHERE active=1 ORDER BY source_key").fetchall()
        return hashlib.sha256((version+"|"+"|".join(f"{r[0]}:{r[1]}" for r in rows)).encode()).hexdigest()

    def successful_retrieval(self, conversation_id, fingerprint):
        return self.connection.execute("SELECT id FROM knowledge_retrieval_runs WHERE conversation_id=? AND query_fingerprint=? AND status='succeeded'",(conversation_id,fingerprint)).fetchone()

    def start_retrieval(self, conversation_id, analysis_id, query, fingerprint, index_fingerprint, retriever, version, limit):
        with self.connection:
            row=self.connection.execute("SELECT id FROM knowledge_retrieval_runs WHERE conversation_id=? AND query_fingerprint=?",(conversation_id,fingerprint)).fetchone()
            if row:
                self.connection.execute("UPDATE knowledge_retrieval_runs SET status='running',error_class=NULL,result_count=0,completed_at=NULL WHERE id=?",(row[0],)); return row[0]
            return self.connection.execute("INSERT INTO knowledge_retrieval_runs(conversation_id,conversation_analysis_id,query_text,query_fingerprint,knowledge_index_fingerprint,retriever,retriever_version,retrieval_limit,status) VALUES(?,?,?,?,?,?,?,?, 'running')",(conversation_id,analysis_id,query,fingerprint,index_fingerprint,retriever,version,limit)).lastrowid

    def complete_retrieval(self, run_id, matches):
        with self.connection:
            for match in matches: self.connection.execute("INSERT INTO knowledge_retrieval_results(retrieval_run_id,knowledge_chunk_id,rank,score) VALUES(?,?,?,?)",(run_id,match.chunk_id,match.rank,match.score))
            self.connection.execute("UPDATE knowledge_retrieval_runs SET status='succeeded',result_count=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",(len(matches),run_id))

    def fail_retrieval(self, run_id, error):
        with self.connection: self.connection.execute("UPDATE knowledge_retrieval_runs SET status='failed',error_class=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",(error,run_id))

    def latest_successful_conversation_analysis(self, conversation_id):
        row=self.connection.execute("""SELECT a.*,r.id run_id FROM conversation_analyses a JOIN conversation_analysis_runs r ON r.id=a.conversation_analysis_run_id WHERE a.conversation_id=? AND r.status='succeeded' ORDER BY a.id DESC LIMIT 1""",(conversation_id,)).fetchone()
        if not row: return None
        return row['id'], ConversationAnalysis(row['conversation_summary'],row['current_intent'],row['priority'],row['urgency'],tuple(json.loads(row['unresolved_requests_json'])),tuple(json.loads(row['resolved_points_json'])),tuple(json.loads(row['order_numbers_json'])),tuple(json.loads(row['relevant_dates_json'])),row['latest_sender_request'],row['confidence'],bool(row['needs_human']),row['human_reason'],row['recommended_action'])

    def get_successful_reply_draft_run(self, conversation_id: int, input_fingerprint: str) -> ReplyDraftRun | None:
        row = self.connection.execute(
            "SELECT * FROM reply_draft_runs WHERE conversation_id = ? AND input_fingerprint = ? AND status = 'succeeded'",
            (conversation_id, input_fingerprint),
        ).fetchone()
        return _reply_draft_run_from_row(row) if row else None

    def start_reply_draft_run(self, *, conversation_id: int, conversation_analysis_id: int,
                              knowledge_retrieval_run_id: int, generator: str, generator_version: str,
                              model: str, prompt_version: str, input_fingerprint: str) -> ReplyDraftRun:
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM reply_draft_runs WHERE conversation_id = ? AND input_fingerprint = ?",
                (conversation_id, input_fingerprint),
            ).fetchone()
            if row:
                self.connection.execute(
                    """UPDATE reply_draft_runs SET status='running', error_class=NULL,
                       started_at=CURRENT_TIMESTAMP, completed_at=NULL WHERE id=?""", (row["id"],),
                )
                return ReplyDraftRun(row["id"], conversation_id, conversation_analysis_id, knowledge_retrieval_run_id,
                                     generator, generator_version, model, prompt_version, input_fingerprint, "running")
            cursor = self.connection.execute(
                """INSERT INTO reply_draft_runs (
                    conversation_id, conversation_analysis_id, knowledge_retrieval_run_id, generator,
                    generator_version, model, prompt_version, input_fingerprint, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
                (conversation_id, conversation_analysis_id, knowledge_retrieval_run_id, generator, generator_version,
                 model, prompt_version, input_fingerprint),
            )
            return ReplyDraftRun(cursor.lastrowid, conversation_id, conversation_analysis_id, knowledge_retrieval_run_id,
                                 generator, generator_version, model, prompt_version, input_fingerprint, "running")

    def complete_reply_draft_run(self, run: ReplyDraftRun, *, latest_message_id: int,
                                 draft: ReplyDraft, grounding_chunk_ids: tuple[int, ...]) -> PersistedReplyDraft:
        """Persist the local draft, provenance, and success state atomically."""
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO reply_drafts (
                    draft_run_id, conversation_id, latest_message_id, draft_status, subject, body, confidence,
                    needs_review, review_reason, unsupported_claims_json, response_language
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.id, run.conversation_id, latest_message_id, draft.draft_status, draft.subject, draft.body,
                 draft.confidence, int(draft.needs_review), draft.review_reason,
                 json.dumps(list(draft.unsupported_claims), ensure_ascii=False), draft.response_language),
            )
            draft_id = cursor.lastrowid
            for chunk_id in grounding_chunk_ids:
                self.connection.execute(
                    "INSERT INTO reply_draft_grounding (reply_draft_id, knowledge_chunk_id) VALUES (?, ?)",
                    (draft_id, chunk_id),
                )
            self.connection.execute(
                "UPDATE reply_draft_runs SET status='succeeded', completed_at=CURRENT_TIMESTAMP WHERE id=?", (run.id,),
            )
            return PersistedReplyDraft(draft_id, run.id, run.conversation_id, latest_message_id, draft)

    def fail_reply_draft_run(self, run: ReplyDraftRun, error_class: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE reply_draft_runs SET status='failed', error_class=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (error_class, run.id),
            )

    def get_reply_draft_by_run_id(self, run_id: int) -> PersistedReplyDraft | None:
        row = self.connection.execute("SELECT * FROM reply_drafts WHERE draft_run_id=?", (run_id,)).fetchone()
        return _persisted_reply_draft_from_row(row) if row else None

    def get_reply_draft_run_fingerprint(self, run_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT input_fingerprint FROM reply_draft_runs WHERE id=? AND status='succeeded'", (run_id,),
        ).fetchone()
        return row["input_fingerprint"] if row else None

    def get_reply_draft_grounding(self, reply_draft_id: int) -> tuple[int, ...]:
        rows = self.connection.execute(
            "SELECT knowledge_chunk_id FROM reply_draft_grounding WHERE reply_draft_id=? ORDER BY knowledge_chunk_id",
            (reply_draft_id,),
        ).fetchall()
        return tuple(row[0] for row in rows)

    def successful_retrieval_snapshot(self, retrieval_run_id: int) -> tuple[int, int, str | None, tuple[tuple[int, str], ...]] | None:
        """Return only stable provenance facts for a successful retrieval run."""
        row = self.connection.execute(
            """SELECT conversation_id, conversation_analysis_id, knowledge_index_fingerprint
               FROM knowledge_retrieval_runs WHERE id=? AND status='succeeded'""", (retrieval_run_id,),
        ).fetchone()
        if not row:
            return None
        chunks = self.connection.execute(
            """SELECT result.knowledge_chunk_id, chunk.chunk_hash FROM knowledge_retrieval_results result
               JOIN knowledge_chunks chunk ON chunk.id=result.knowledge_chunk_id
               WHERE result.retrieval_run_id=? ORDER BY result.rank, result.knowledge_chunk_id""", (retrieval_run_id,),
        ).fetchall()
        return row["conversation_id"], row["conversation_analysis_id"], row["knowledge_index_fingerprint"], tuple(
            (chunk["knowledge_chunk_id"], chunk["chunk_hash"]) for chunk in chunks
        )

    def latest_successful_retrieval(self, conversation_id: int, analysis_id: int) -> tuple[int, list[KnowledgeMatch]] | None:
        row = self.connection.execute(
            """SELECT id FROM knowledge_retrieval_runs
               WHERE conversation_id=? AND conversation_analysis_id=? AND status='succeeded'
               ORDER BY id DESC LIMIT 1""", (conversation_id, analysis_id),
        ).fetchone()
        if not row:
            return None
        rows = self.connection.execute(
            """SELECT result.knowledge_chunk_id, chunk.document_id, document.source_filename, document.title,
                      chunk.chunk_text, result.score, result.rank
               FROM knowledge_retrieval_results result
               JOIN knowledge_chunks chunk ON chunk.id=result.knowledge_chunk_id
               JOIN knowledge_documents document ON document.id=chunk.document_id
               WHERE result.retrieval_run_id=? ORDER BY result.rank, result.knowledge_chunk_id""", (row["id"],),
        ).fetchall()
        return row["id"], [
            KnowledgeMatch(item["knowledge_chunk_id"], item["document_id"], item["source_filename"], item["title"],
                           item["chunk_text"], float(item["score"]), item["rank"])
            for item in rows
        ]

    def get_policy_decision_by_fingerprint(self, conversation_id: int, input_fingerprint: str) -> PersistedPolicyDecision | None:
        row = self.connection.execute(
            "SELECT * FROM policy_decisions WHERE conversation_id=? AND input_fingerprint=?",
            (conversation_id, input_fingerprint),
        ).fetchone()
        return _persisted_policy_decision_from_row(row) if row else None

    def create_policy_decision_and_review(
        self, *, conversation_id: int, conversation_analysis_id: int, reply_draft_id: int,
        input_fingerprint: str, decision: PolicyDecision, review_type: str | None,
    ) -> tuple[PersistedPolicyDecision, HumanReviewItem | None]:
        """Atomically persist immutable policy provenance and any initial review queue state."""
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO policy_decisions (
                    conversation_id, reply_draft_id, conversation_analysis_id, policy_version, decision,
                    reason_codes_json, primary_reason, input_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (conversation_id, reply_draft_id, conversation_analysis_id, decision.rule_version,
                 decision.decision, json.dumps(list(decision.reason_codes), separators=(",", ":")),
                 decision.primary_reason, input_fingerprint),
            )
            policy_id = cursor.lastrowid
            persisted = PersistedPolicyDecision(
                policy_id, conversation_id, reply_draft_id, conversation_analysis_id, input_fingerprint, decision,
            )
            self._record_audit_event(AuditEvent(
                "policy_decision_recorded", "policy_decision", policy_id,
                metadata={"conversation_id": conversation_id, "reply_draft_id": reply_draft_id,
                          "decision": decision.decision, "reason_codes": list(decision.reason_codes),
                          "policy_version": decision.rule_version},
            ))
            if review_type is None:
                return persisted, None
            review_cursor = self.connection.execute(
                """INSERT INTO human_review_items (
                    policy_decision_id, conversation_id, reply_draft_id, review_type, status
                ) VALUES (?, ?, ?, ?, 'pending')""",
                (policy_id, conversation_id, reply_draft_id, review_type),
            )
            review_id = review_cursor.lastrowid
            self.connection.execute(
                "INSERT INTO human_review_events (review_item_id, event_type) VALUES (?, 'created')", (review_id,),
            )
            self._record_audit_event(AuditEvent(
                "human_review_created", "human_review_item", review_id,
                metadata={"policy_decision_id": policy_id, "review_type": review_type, "status": "pending"},
            ))
            return persisted, HumanReviewItem(
                review_id, policy_id, conversation_id, reply_draft_id, review_type, "pending",
            )

    def get_review_item_for_policy_decision(self, policy_decision_id: int) -> HumanReviewItem | None:
        row = self.connection.execute(
            "SELECT * FROM human_review_items WHERE policy_decision_id=?", (policy_decision_id,),
        ).fetchone()
        return _review_item_from_row(row) if row else None

    def get_review_item(self, review_item_id: int) -> HumanReviewItem | None:
        row = self.connection.execute("SELECT * FROM human_review_items WHERE id=?", (review_item_id,)).fetchone()
        return _review_item_from_row(row) if row else None

    def list_pending_review_items(self) -> list[HumanReviewItem]:
        rows = self.connection.execute(
            "SELECT * FROM human_review_items WHERE status='pending' ORDER BY created_at ASC, id ASC",
        ).fetchall()
        return [_review_item_from_row(row) for row in rows]

    def list_review_events(self, review_item_id: int) -> list[HumanReviewEvent]:
        rows = self.connection.execute(
            "SELECT * FROM human_review_events WHERE review_item_id=? ORDER BY id ASC", (review_item_id,),
        ).fetchall()
        return [_review_event_from_row(row) for row in rows]

    def transition_review_item(self, review_item_id: int, status: str, reviewer_id: str,
                               note: str | None) -> HumanReviewItem:
        event_by_status = {
            "approved": "human_review_approved", "rejected": "human_review_rejected",
            "changes_requested": "human_review_changes_requested",
        }
        if status not in event_by_status:
            raise ValueError("Unsupported review transition")
        with self.connection:
            row = self.connection.execute("SELECT * FROM human_review_items WHERE id=?", (review_item_id,)).fetchone()
            if not row:
                raise ValueError("Review item does not exist")
            if row["status"] != "pending":
                raise ValueError("Only pending review items may transition")
            if row["review_type"] == "blocked_resolution" and status == "approved":
                raise ValueError("Blocked review items cannot be approved")
            self.connection.execute(
                """UPDATE human_review_items SET status=?, reviewer_id=?, updated_at=CURRENT_TIMESTAMP,
                   resolved_at=CURRENT_TIMESTAMP WHERE id=?""", (status, reviewer_id, review_item_id),
            )
            self.connection.execute(
                """INSERT INTO human_review_events (review_item_id, event_type, reviewer_id, note)
                   VALUES (?, ?, ?, ?)""", (review_item_id, status, reviewer_id, note),
            )
            self._record_audit_event(AuditEvent(
                event_by_status[status], "human_review_item", review_item_id,
                metadata={"policy_decision_id": row["policy_decision_id"], "review_type": row["review_type"],
                          "status": status, "reviewer_id": reviewer_id},
            ))
            updated = self.connection.execute("SELECT * FROM human_review_items WHERE id=?", (review_item_id,)).fetchone()
            return _review_item_from_row(updated)

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


def _reply_draft_run_from_row(row: sqlite3.Row) -> ReplyDraftRun:
    return ReplyDraftRun(
        row["id"], row["conversation_id"], row["conversation_analysis_id"], row["knowledge_retrieval_run_id"],
        row["generator"], row["generator_version"], row["model"], row["prompt_version"],
        row["input_fingerprint"], row["status"], row["error_class"],
    )


def _persisted_reply_draft_from_row(row: sqlite3.Row) -> PersistedReplyDraft:
    draft = ReplyDraft(
        row["draft_status"], row["subject"], row["body"], (), tuple(json.loads(row["unsupported_claims_json"])),
        float(row["confidence"]), bool(row["needs_review"]), row["review_reason"], row["response_language"],
    )
    return PersistedReplyDraft(row["id"], row["draft_run_id"], row["conversation_id"], row["latest_message_id"], draft)


def _persisted_policy_decision_from_row(row: sqlite3.Row) -> PersistedPolicyDecision:
    decision = PolicyDecision(
        row["decision"], row["policy_version"], tuple(json.loads(row["reason_codes_json"])), row["primary_reason"],
    )
    return PersistedPolicyDecision(
        row["id"], row["conversation_id"], row["reply_draft_id"], row["conversation_analysis_id"],
        row["input_fingerprint"], decision,
    )


def _review_item_from_row(row: sqlite3.Row) -> HumanReviewItem:
    return HumanReviewItem(
        row["id"], row["policy_decision_id"], row["conversation_id"], row["reply_draft_id"],
        row["review_type"], row["status"], row["reviewer_id"], row["resolved_at"],
    )


def _review_event_from_row(row: sqlite3.Row) -> HumanReviewEvent:
    return HumanReviewEvent(
        row["id"], row["review_item_id"], row["event_type"], row["reviewer_id"], row["note"], row["created_at"],
    )


def _safe_metadata(message: InboxMessage) -> Mapping[str, object]:
    """Deliberately omit body text and other unnecessary sensitive content."""
    return {
        "provider": message.provider,
        "provider_message_id": message.provider_message_id,
        "provider_thread_id": message.provider_thread_id,
        "content_hash": message.content_hash,
    }
