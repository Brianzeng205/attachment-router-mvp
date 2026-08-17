"""Strict, application-owned schema for Inbox Analyzer output."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Mapping

CATEGORIES = frozenset({
    "customer_support", "order_support", "billing", "sales", "scheduling",
    "account", "feedback", "spam", "other",
})
PRIORITIES = frozenset({"low", "normal", "high", "critical"})
URGENCIES = frozenset({"low", "medium", "high", "immediate"})
RECOMMENDED_ACTIONS = frozenset({"no_action", "draft_reply", "request_information", "human_review"})
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class InboxAnalysis:
    category: str
    intent: str
    priority: str
    urgency: str
    summary: str
    customer_name: str | None
    order_numbers: tuple[str, ...]
    dates: tuple[str, ...]
    requirements: tuple[str, ...]
    confidence: float
    needs_human: bool
    human_reason: str | None
    recommended_action: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "InboxAnalysis":
        required = (
            "category", "intent", "priority", "urgency", "summary", "customer_name",
            "order_numbers", "dates", "requirements", "confidence", "needs_human",
            "human_reason", "recommended_action",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"Missing inbox analysis fields: {', '.join(missing)}")
        category = _enum(value["category"], CATEGORIES, "category")
        intent = _identifier(value["intent"], "intent")
        priority = _enum(value["priority"], PRIORITIES, "priority")
        urgency = _enum(value["urgency"], URGENCIES, "urgency")
        summary = _text(value["summary"], "summary", 1_000)
        customer_name = _optional_text(value["customer_name"], "customer_name", 200)
        order_numbers = _text_list(value["order_numbers"], "order_numbers", 20, 200)
        dates = _text_list(value["dates"], "dates", 20, 200)
        requirements = _text_list(value["requirements"], "requirements", 20, 300)
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
        return cls(category, intent, priority, urgency, summary, customer_name, order_numbers, dates,
                   requirements, confidence, value["needs_human"], human_reason, action)

    def as_mapping(self) -> dict[str, object]:
        return asdict(self)


def _enum(value: object, allowed: frozenset[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
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
    if value is None:
        return None
    return _text(value, name, maximum)


def _text_list(value: object, name: str, maximum_items: int, maximum_length: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{name} must be a list with at most {maximum_items} items")
    return tuple(_text(item, name, maximum_length) for item in value)
