"""Application-owned, validated local reply-draft model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


DRAFT_STATUSES = frozenset({"drafted", "insufficient_knowledge", "not_applicable"})


@dataclass(frozen=True)
class ReplyDraftRun:
    id: int
    conversation_id: int
    conversation_analysis_id: int
    knowledge_retrieval_run_id: int
    generator: str
    generator_version: str
    model: str
    prompt_version: str
    input_fingerprint: str
    status: str
    error_class: str | None = None


@dataclass(frozen=True)
class PersistedReplyDraft:
    id: int
    draft_run_id: int
    conversation_id: int
    latest_message_id: int
    draft: "ReplyDraft"


@dataclass(frozen=True)
class ReplyDraft:
    draft_status: str
    subject: str | None
    body: str
    grounding_chunk_ids: tuple[int, ...]
    unsupported_claims: tuple[str, ...]
    confidence: float
    needs_review: bool
    review_reason: str | None
    response_language: str

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], allowed_grounding_chunk_ids: set[int], maximum_body_chars: int = 4_000
    ) -> "ReplyDraft":
        required = (
            "draft_status", "subject", "body", "grounding_chunk_ids", "unsupported_claims",
            "confidence", "needs_review", "review_reason", "response_language",
        )
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"Missing draft fields: {', '.join(missing)}")
        if value["draft_status"] not in DRAFT_STATUSES:
            raise ValueError("Invalid draft_status")
        body = _text(value["body"], "body", maximum_body_chars)
        subject = _optional_text(value["subject"], "subject", 300)
        grounding_chunk_ids = _grounding_ids(value["grounding_chunk_ids"], allowed_grounding_chunk_ids)
        unsupported_claims = _text_list(value["unsupported_claims"], "unsupported_claims", 30, 400)
        confidence = _confidence(value["confidence"])
        if not isinstance(value["needs_review"], bool):
            raise ValueError("needs_review must be boolean")
        review_reason = _optional_text(value["review_reason"], "review_reason", 300)
        language = _text(value["response_language"], "response_language", 80)
        return cls(
            value["draft_status"], subject, body, grounding_chunk_ids, unsupported_claims, confidence,
            value["needs_review"], review_reason, language,
        )


def _text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def _optional_text(value: object, name: str, maximum: int) -> str | None:
    return None if value is None else _text(value, name, maximum)


def _text_list(value: object, name: str, maximum_items: int, maximum_length: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{name} must be a list with at most {maximum_items} items")
    return tuple(_text(item, name, maximum_length) for item in value)


def _grounding_ids(value: object, allowed: set[int]) -> tuple[int, ...]:
    if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, int) or item not in allowed for item in value):
        raise ValueError("Invalid grounding IDs")
    if len(value) != len(set(value)):
        raise ValueError("Duplicate grounding IDs")
    return tuple(value)


def _confidence(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("confidence must be numeric")
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be numeric") from exc
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    return confidence
