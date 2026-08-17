"""Anthropic-backed conversation analyzer, separate from single-message analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .analysis_models import PRIORITIES, RECOMMENDED_ACTIONS, URGENCIES
from .config import Settings
from .conversation_models import ConversationAnalysis, ConversationContext
from .errors import ConversationAnalyzerAPIError, ConversationAnalyzerAuthenticationError, ConversationAnalyzerResponseError
from .retry import RetryPolicy, is_transient_provider_error, policy_from_settings

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "conversation_analysis.txt"
CONVERSATION_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "conversation_summary": {"type": "string"}, "current_intent": {"type": "string"},
        "priority": {"type": "string", "enum": sorted(PRIORITIES)},
        "urgency": {"type": "string", "enum": sorted(URGENCIES)},
        "unresolved_requests": {"type": "array", "items": {"type": "string"}},
        "resolved_points": {"type": "array", "items": {"type": "string"}},
        "order_numbers": {"type": "array", "items": {"type": "string"}},
        "relevant_dates": {"type": "array", "items": {"type": "string"}},
        "latest_sender_request": {"type": ["string", "null"]}, "confidence": {"type": "number"},
        "needs_human": {"type": "boolean"}, "human_reason": {"type": ["string", "null"]},
        "recommended_action": {"type": "string", "enum": sorted(RECOMMENDED_ACTIONS)},
    },
    "required": ["conversation_summary", "current_intent", "priority", "urgency", "unresolved_requests",
                 "resolved_points", "order_numbers", "relevant_dates", "latest_sender_request", "confidence",
                 "needs_human", "human_reason", "recommended_action"],
}


class ClaudeConversationAnalyzer:
    def __init__(
        self, client: Any, model: str, prompt: str | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._client, self.model = client, model
        self._prompt = prompt or PROMPT_PATH.read_text(encoding="utf-8")
        self._retry_policy = retry_policy or RetryPolicy()

    @classmethod
    def from_settings(cls, settings: Settings) -> "ClaudeConversationAnalyzer":
        if not settings.anthropic_api_key:
            raise ConversationAnalyzerAuthenticationError("ANTHROPIC_API_KEY is required")
        try:
            from anthropic import Anthropic
            return cls(
                Anthropic(
                    api_key=settings.anthropic_api_key,
                    timeout=settings.provider_request_timeout_seconds,
                    max_retries=0,
                ),
                settings.conversation_analyzer_model, retry_policy=policy_from_settings(settings),
            )
        except Exception as exc:
            raise ConversationAnalyzerAuthenticationError("Unable to initialise conversation analyzer client") from exc

    def analyze(self, context: ConversationContext) -> ConversationAnalysis:
        payload = {
            "conversation_id": context.conversation.id,
            "total_message_count": context.total_message_count, "included_message_count": context.included_message_count,
            "context_truncated": context.truncated,
            "messages": [{"id": item.id, "sender": item.sender, "recipients": list(item.recipients),
                          "subject": item.subject, "body_text": item.body_text, "received_at": item.received_at}
                         for item in context.messages],
        }
        try:
            response = self._retry_policy.execute(
                lambda: self._client.messages.create(
                    model=self.model, max_tokens=900, system=self._prompt,
                    messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    output_config={"format": {"type": "json_schema", "schema": CONVERSATION_SCHEMA}},
                ),
                retry_if=is_transient_provider_error,
                provider="claude", operation_name="conversation_analysis",
            )
        except Exception as exc:
            raise ConversationAnalyzerAPIError("Conversation Analyzer API request failed") from exc
        content = getattr(response, "content", None) or []
        text = next((getattr(block, "text", None) for block in content if getattr(block, "text", None)), None)
        if not text:
            raise ConversationAnalyzerResponseError("Conversation Analyzer returned an empty structured response")
        try:
            decoded = json.loads(text)
            if not isinstance(decoded, dict):
                raise ValueError("response is not an object")
            return ConversationAnalysis.from_mapping(decoded)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ConversationAnalyzerResponseError("Conversation Analyzer returned invalid structured output") from exc
