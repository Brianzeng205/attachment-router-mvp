"""Bounded, application-owned input passed to a reply-draft generator."""

from __future__ import annotations

from dataclasses import dataclass

from .conversation_models import ConversationAnalysis, ConversationContext
from .knowledge_models import KnowledgeMatch


@dataclass(frozen=True)
class ReplyDraftMessage:
    id: int
    sender: str
    recipients: tuple[str, ...]
    subject: str
    body_text: str
    received_at: str


@dataclass(frozen=True)
class ReplyDraftKnowledge:
    chunk_id: int
    chunk_hash: str
    source_filename: str
    title: str | None
    chunk_text: str
    rank: int


@dataclass(frozen=True)
class ReplyDraftInput:
    conversation_id: int
    latest_message_id: int
    messages: tuple[ReplyDraftMessage, ...]
    conversation_analysis: ConversationAnalysis
    knowledge_retrieval_run_id: int
    knowledge_matches: tuple[ReplyDraftKnowledge, ...]
    allowed_grounding_chunk_ids: frozenset[int]
    context_truncated: bool
    context_fingerprint: str

    @classmethod
    def from_context(
        cls,
        context: ConversationContext,
        analysis: ConversationAnalysis,
        knowledge_retrieval_run_id: int,
        matches: list[KnowledgeMatch] | tuple[KnowledgeMatch, ...],
        *,
        max_conversation_chars: int = 24_000,
        max_knowledge_chars: int = 6_000,
    ) -> "ReplyDraftInput":
        if context.conversation.id < 1 or context.latest_message_id < 1 or knowledge_retrieval_run_id < 1:
            raise ValueError("Reply draft input requires persisted identifiers")
        if max_conversation_chars < 1 or max_knowledge_chars < 1:
            raise ValueError("Reply draft input limits must be positive")
        remaining_context = max_conversation_chars
        messages: list[ReplyDraftMessage] = []
        for item in context.messages:
            body, remaining_context = _take(item.body_text, remaining_context)
            subject, remaining_context = _take(item.subject, remaining_context)
            sender, remaining_context = _take(item.sender, remaining_context)
            recipients: list[str] = []
            for recipient in item.recipients:
                bounded, remaining_context = _take(recipient, remaining_context)
                recipients.append(bounded)
            messages.append(ReplyDraftMessage(item.id, sender, tuple(recipients), subject, body, item.received_at))
        remaining_knowledge = max_knowledge_chars
        knowledge: list[ReplyDraftKnowledge] = []
        for match in sorted(matches, key=lambda item: (item.rank, item.chunk_id)):
            chunk_text, remaining_knowledge = _take(match.chunk_text, remaining_knowledge)
            source, remaining_knowledge = _take(match.source_filename, remaining_knowledge)
            title = None
            if match.title is not None:
                title, remaining_knowledge = _take(match.title, remaining_knowledge)
            knowledge.append(ReplyDraftKnowledge(
                match.chunk_id, _hash(match.chunk_text), source, title, chunk_text, match.rank,
            ))
        allowed = frozenset(item.chunk_id for item in knowledge)
        return cls(
            context.conversation.id, context.latest_message_id, tuple(messages), analysis, knowledge_retrieval_run_id,
            tuple(knowledge), allowed, context.truncated or remaining_context < 0 or remaining_knowledge < 0,
            context.context_fingerprint,
        )


def _take(value: str, remaining: int) -> tuple[str, int]:
    bounded = value[:max(remaining, 0)]
    return bounded, remaining - len(bounded)


def _hash(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
