"""Validated models for bounded conversation context and analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .analysis_models import PRIORITIES, RECOMMENDED_ACTIONS, URGENCIES
from .inbox_models import Conversation, InboxMessage

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class ContextMessage:
    id: int
    provider_message_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    body_text: str
    received_at: str
    content_hash: str


@dataclass(frozen=True)
class ConversationContext:
    conversation: Conversation
    messages: tuple[ContextMessage, ...]
    latest_message_id: int
    total_message_count: int
    included_message_count: int
    truncated: bool
    context_fingerprint: str


@dataclass(frozen=True)
class ConversationAnalysis:
    conversation_summary: str
    current_intent: str
    priority: str
    urgency: str
    unresolved_requests: tuple[str, ...]
    resolved_points: tuple[str, ...]
    order_numbers: tuple[str, ...]
    relevant_dates: tuple[str, ...]
    latest_sender_request: str | None
    confidence: float
    needs_human: bool
    human_reason: str | None
    recommended_action: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ConversationAnalysis":
        required = (
            "conversation_summary", "current_intent", "priority", "urgency", "unresolved_requests",
            "resolved_points", "order_numbers", "relevant_dates", "latest_sender_request", "confidence",
            "needs_human", "human_reason", "recommended_action",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"Missing conversation analysis fields: {', '.join(missing)}")
        summary = _text(value["conversation_summary"], "conversation_summary", 1_500)
        intent = _identifier(value["current_intent"], "current_intent")
        priority = _enum(value["priority"], PRIORITIES, "priority")
        urgency = _enum(value["urgency"], URGENCIES, "urgency")
        unresolved = _text_list(value["unresolved_requests"], "unresolved_requests", 20, 300)
        resolved = _text_list(value["resolved_points"], "resolved_points", 20, 300)
        orders = _text_list(value["order_numbers"], "order_numbers", 30, 200)
        dates = _text_list(value["relevant_dates"], "relevant_dates", 30, 200)
        latest = _optional_text(value["latest_sender_request"], "latest_sender_request", 500)
        if isinstance(value["confidence"], bool):
            raise ValueError("confidence must be numeric")
        try:
            confidence = float(value["confidence"])
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be numeric") from exc
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not isinstance(value["needs_human"], bool):
            raise ValueError("needs_human must be boolean")
        human_reason = _optional_text(value["human_reason"], "human_reason", 120)
        if human_reason is not None and not _IDENTIFIER.fullmatch(human_reason):
            raise ValueError("human_reason must be a normalized identifier or null")
        action = _enum(value["recommended_action"], RECOMMENDED_ACTIONS, "recommended_action")
        return cls(summary, intent, priority, urgency, unresolved, resolved, orders, dates, latest,
                   confidence, value["needs_human"], human_reason, action)


@dataclass(frozen=True)
class ConversationAnalysisRun:
    id: int
    conversation_id: int
    analyzer: str
    analyzer_version: str
    model: str
    prompt_version: str
    context_fingerprint: str
    status: str
    error_class: str | None = None


def context_message(message: InboxMessage, body_text: str) -> ContextMessage:
    if message.id is None:
        raise ValueError("Persisted messages require an internal ID")
    return ContextMessage(message.id, message.provider_message_id, message.sender, message.recipients,
                          message.subject, body_text, message.received_at, message.content_hash)


def _enum(value: object, allowed: frozenset[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} must be an approved value")
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase snake_case identifier")
    return value


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
