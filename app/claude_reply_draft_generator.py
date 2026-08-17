"""Anthropic-backed grounded local reply generator, separate from analyzers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ReplyDraftGeneratorAPIError, ReplyDraftGeneratorAuthenticationError, ReplyDraftGeneratorResponseError
from .reply_draft_input import ReplyDraftInput
from .reply_draft_models import DRAFT_STATUSES, ReplyDraft
from .retry import RetryPolicy, is_transient_provider_error, policy_from_settings


PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "grounded_reply.txt"
REPLY_DRAFT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "draft_status": {"type": "string", "enum": sorted(DRAFT_STATUSES)},
        "subject": {"type": ["string", "null"]},
        "body": {"type": "string"},
        "grounding_chunk_ids": {"type": "array", "items": {"type": "integer"}},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "needs_review": {"type": "boolean"},
        "review_reason": {"type": ["string", "null"]},
        "response_language": {"type": "string"},
    },
    "required": [
        "draft_status", "subject", "body", "grounding_chunk_ids", "unsupported_claims", "confidence",
        "needs_review", "review_reason", "response_language",
    ],
}


class ClaudeGroundedReplyGenerator:
    def __init__(
        self, client: Any, model: str, prompt: str | None = None, *, maximum_body_chars: int = 4_000,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if maximum_body_chars < 1:
            raise ValueError("maximum_body_chars must be positive")
        self._client = client
        self.model = model
        self._prompt = prompt or PROMPT_PATH.read_text(encoding="utf-8")
        self._maximum_body_chars = maximum_body_chars
        self._retry_policy = retry_policy or RetryPolicy()

    @classmethod
    def from_settings(cls, settings: Settings) -> "ClaudeGroundedReplyGenerator":
        if not settings.anthropic_api_key:
            raise ReplyDraftGeneratorAuthenticationError("ANTHROPIC_API_KEY is required")
        try:
            from anthropic import Anthropic
            return cls(
                Anthropic(
                    api_key=settings.anthropic_api_key,
                    timeout=settings.provider_request_timeout_seconds,
                    max_retries=0,
                ),
                settings.reply_draft_generator_model, retry_policy=policy_from_settings(settings),
            )
        except Exception as exc:
            raise ReplyDraftGeneratorAuthenticationError("Unable to initialise grounded reply generator client") from exc

    def generate(self, draft_input: ReplyDraftInput) -> ReplyDraft:
        try:
            response = self._retry_policy.execute(
                lambda: self._client.messages.create(
                    model=self.model,
                    max_tokens=1_200,
                    system=self._prompt,
                    messages=[{"role": "user", "content": json.dumps(_payload(draft_input), ensure_ascii=False)}],
                    output_config={"format": {"type": "json_schema", "schema": REPLY_DRAFT_SCHEMA}},
                ),
                retry_if=is_transient_provider_error,
                provider="claude", operation_name="grounded_reply",
            )
        except Exception as exc:
            raise ReplyDraftGeneratorAPIError("Grounded reply generator API request failed") from exc
        content = getattr(response, "content", None) or []
        text = next((getattr(block, "text", None) for block in content if getattr(block, "text", None)), None)
        if not text:
            raise ReplyDraftGeneratorResponseError("Grounded reply generator returned an empty structured response")
        try:
            decoded = json.loads(text)
            if not isinstance(decoded, dict):
                raise ValueError("response is not an object")
            return ReplyDraft.from_mapping(
                decoded, set(draft_input.allowed_grounding_chunk_ids), self._maximum_body_chars,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ReplyDraftGeneratorResponseError("Grounded reply generator returned invalid structured output") from exc


def _payload(draft_input: ReplyDraftInput) -> dict[str, object]:
    analysis = draft_input.conversation_analysis
    return {
        "conversation_id": draft_input.conversation_id,
        "latest_message_id": draft_input.latest_message_id,
        "context_truncated": draft_input.context_truncated,
        "conversation_data_untrusted": {
            "messages": [
                {
                    "id": item.id, "sender": item.sender, "recipients": list(item.recipients),
                    "subject": item.subject, "body_text": item.body_text, "received_at": item.received_at,
                }
                for item in draft_input.messages
            ],
            "analysis": {
                "conversation_summary": analysis.conversation_summary,
                "current_intent": analysis.current_intent,
                "unresolved_requests": list(analysis.unresolved_requests),
                "latest_sender_request": analysis.latest_sender_request,
                "needs_human": analysis.needs_human,
            },
        },
        "retrieved_knowledge_reference_data": [
            {
                "chunk_id": item.chunk_id, "source_filename": item.source_filename, "title": item.title,
                "chunk_text": item.chunk_text, "rank": item.rank,
            }
            for item in draft_input.knowledge_matches
        ],
        "allowed_grounding_chunk_ids": sorted(draft_input.allowed_grounding_chunk_ids),
    }
