"""Anthropic-backed, structured Inbox Analyzer; separate from document routing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .analysis_models import CATEGORIES, PRIORITIES, RECOMMENDED_ACTIONS, URGENCIES, InboxAnalysis
from .config import Settings
from .errors import InboxAnalyzerAPIError, InboxAnalyzerAuthenticationError, InboxAnalyzerResponseError
from .inbox_models import InboxMessage

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "inbox_analysis.txt"
ANALYSIS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "category": {"type": "string", "enum": sorted(CATEGORIES)},
        "intent": {"type": "string"},
        "priority": {"type": "string", "enum": sorted(PRIORITIES)},
        "urgency": {"type": "string", "enum": sorted(URGENCIES)},
        "summary": {"type": "string"},
        "customer_name": {"type": ["string", "null"]},
        "order_numbers": {"type": "array", "items": {"type": "string"}},
        "dates": {"type": "array", "items": {"type": "string"}},
        "requirements": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "needs_human": {"type": "boolean"},
        "human_reason": {"type": ["string", "null"]},
        "recommended_action": {"type": "string", "enum": sorted(RECOMMENDED_ACTIONS)},
    },
    "required": [
        "category", "intent", "priority", "urgency", "summary", "customer_name", "order_numbers",
        "dates", "requirements", "confidence", "needs_human", "human_reason", "recommended_action",
    ],
}


class ClaudeInboxAnalyzer:
    def __init__(self, client: Any, model: str, max_body_chars: int, prompt: str | None = None) -> None:
        self._client = client
        self.model = model
        self.max_body_chars = max_body_chars
        self._prompt = prompt or PROMPT_PATH.read_text(encoding="utf-8")

    @classmethod
    def from_settings(cls, settings: Settings) -> "ClaudeInboxAnalyzer":
        if not settings.anthropic_api_key:
            raise InboxAnalyzerAuthenticationError("ANTHROPIC_API_KEY is required")
        try:
            from anthropic import Anthropic
            return cls(Anthropic(api_key=settings.anthropic_api_key), settings.inbox_analyzer_model,
                       settings.max_inbox_message_chars)
        except InboxAnalyzerAuthenticationError:
            raise
        except Exception as exc:
            raise InboxAnalyzerAuthenticationError("Unable to initialise Inbox Analyzer client") from exc

    def analyze(self, message: InboxMessage) -> InboxAnalysis:
        context = {
            "sender": message.sender,
            "recipients": list(message.recipients),
            "subject": message.subject,
            "body_text": message.body_text[:self.max_body_chars],
        }
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=800,
                system=self._prompt,
                messages=[{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
            )
        except Exception as exc:
            self._raise_api_error(exc)
            raise AssertionError("unreachable")
        content = getattr(response, "content", None) or []
        text = next((getattr(block, "text", None) for block in content if getattr(block, "text", None)), None)
        if not text:
            raise InboxAnalyzerResponseError("Inbox Analyzer returned an empty structured response")
        try:
            decoded = json.loads(text)
            if not isinstance(decoded, dict):
                raise ValueError("response is not an object")
            return InboxAnalysis.from_mapping(decoded)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InboxAnalyzerResponseError("Inbox Analyzer returned invalid structured output") from exc

    @staticmethod
    def _raise_api_error(exc: Exception) -> None:
        if type(exc).__name__ in {"AuthenticationError", "PermissionDeniedError"}:
            raise InboxAnalyzerAuthenticationError("Inbox Analyzer authentication failed") from exc
        raise InboxAnalyzerAPIError("Inbox Analyzer API request failed") from exc
