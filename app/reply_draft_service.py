"""Persistence lifecycle and idempotency for local grounded reply drafts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from .inbox_models import AuditEvent
from .reply_draft_generator import ReplyDraftGenerator
from .reply_draft_input import ReplyDraftInput
from .reply_draft_models import PersistedReplyDraft, ReplyDraft


@dataclass(frozen=True)
class ReplyDraftOutcome:
    generated: bool
    skipped: bool
    failed: bool
    draft: PersistedReplyDraft | None = None


class ReplyDraftService:
    """Owns draft input-state idempotency; it never sends or creates provider drafts."""

    def __init__(self, repository, generator: ReplyDraftGenerator, *, generator_name: str, generator_version: str,
                 model: str, prompt_version: str) -> None:
        self._repository = repository
        self._generator = generator
        self._generator_name = generator_name
        self._generator_version = generator_version
        self._model = model
        self._prompt_version = prompt_version

    def create_draft(self, draft_input: ReplyDraftInput, *, conversation_analysis_id: int) -> ReplyDraftOutcome:
        snapshot = self._repository.successful_retrieval_snapshot(draft_input.knowledge_retrieval_run_id)
        fingerprint = self.input_fingerprint(draft_input, conversation_analysis_id, snapshot)
        existing = self._repository.get_successful_reply_draft_run(draft_input.conversation_id, fingerprint)
        if existing:
            draft = self._repository.get_reply_draft_by_run_id(existing.id)
            if draft:
                grounding = self._repository.get_reply_draft_grounding(draft.id)
                return ReplyDraftOutcome(False, True, False, replace(draft, draft=replace(draft.draft, grounding_chunk_ids=grounding)))

        run = self._repository.start_reply_draft_run(
            conversation_id=draft_input.conversation_id, conversation_analysis_id=conversation_analysis_id,
            knowledge_retrieval_run_id=draft_input.knowledge_retrieval_run_id, generator=self._generator_name,
            generator_version=self._generator_version, model=self._model, prompt_version=self._prompt_version,
            input_fingerprint=fingerprint,
        )
        self._audit("reply_draft_started", run.id, {"conversation_id": draft_input.conversation_id,
                                                       "generator": self._generator_name,
                                                       "generator_version": self._generator_version})
        try:
            self._validate_retrieval_provenance(draft_input, conversation_analysis_id, snapshot)
            draft = self._zero_result_draft() if not draft_input.knowledge_matches else self._generator.generate(draft_input)
            self._validate_draft_provenance(draft, draft_input)
            persisted = self._repository.complete_reply_draft_run(
                run, latest_message_id=draft_input.latest_message_id, draft=draft,
                grounding_chunk_ids=draft.grounding_chunk_ids,
            )
        except Exception as exc:
            self._repository.fail_reply_draft_run(run, type(exc).__name__)
            self._audit("reply_draft_failed", run.id, {"conversation_id": draft_input.conversation_id,
                                                          "failure_class": type(exc).__name__})
            return ReplyDraftOutcome(False, False, True)
        self._audit("reply_draft_succeeded", run.id, {
            "conversation_id": draft_input.conversation_id, "reply_draft_id": persisted.id,
            "draft_status": draft.draft_status, "confidence": draft.confidence, "needs_review": draft.needs_review,
            "grounding_count": len(draft.grounding_chunk_ids), "generator": self._generator_name,
            "generator_version": self._generator_version,
        })
        return ReplyDraftOutcome(True, False, False, persisted)

    def input_fingerprint(self, draft_input: ReplyDraftInput, conversation_analysis_id: int, snapshot=None) -> str:
        """Hash input state only; generated text and model output are deliberately absent."""
        index_fingerprint = snapshot[2] if snapshot else None
        state = {
            "conversation_id": draft_input.conversation_id,
            "conversation_analysis_id": conversation_analysis_id,
            "context_fingerprint": draft_input.context_fingerprint,
            "knowledge_retrieval_run_id": draft_input.knowledge_retrieval_run_id,
            "knowledge_index_fingerprint": index_fingerprint,
            "knowledge_chunks": [(item.chunk_id, item.chunk_hash) for item in draft_input.knowledge_matches],
            "generator": self._generator_name, "generator_version": self._generator_version,
            "model": self._model, "prompt_version": self._prompt_version,
        }
        return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _validate_retrieval_provenance(self, draft_input: ReplyDraftInput, analysis_id: int, snapshot) -> None:
        if not snapshot:
            raise ValueError("Knowledge retrieval run is not successful")
        conversation_id, snapshot_analysis_id, _, persisted_chunks = snapshot
        expected_chunks = tuple((item.chunk_id, item.chunk_hash) for item in draft_input.knowledge_matches)
        if conversation_id != draft_input.conversation_id or snapshot_analysis_id != analysis_id:
            raise ValueError("Knowledge retrieval run does not match the draft input")
        if persisted_chunks != expected_chunks:
            raise ValueError("Knowledge retrieval chunks do not match the draft input")
        if draft_input.allowed_grounding_chunk_ids != frozenset(item.chunk_id for item in draft_input.knowledge_matches):
            raise ValueError("Allowed grounding IDs do not match retrieval input")

    @staticmethod
    def _validate_draft_provenance(draft: ReplyDraft, draft_input: ReplyDraftInput) -> None:
        if not set(draft.grounding_chunk_ids).issubset(draft_input.allowed_grounding_chunk_ids):
            raise ValueError("Draft grounding does not belong to current retrieval")

    @staticmethod
    def _zero_result_draft() -> ReplyDraft:
        return ReplyDraft(
            "insufficient_knowledge", None,
            "Thank you for your message. We do not have sufficient confirmed information to provide a complete answer.",
            (), (), 1.0, True, "insufficient_knowledge", "en",
        )

    def _audit(self, event_type: str, run_id: int, metadata: dict[str, object]) -> None:
        self._repository.record_audit_event(AuditEvent(event_type, "reply_draft_run", run_id, metadata=metadata))
