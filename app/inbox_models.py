"""Normalized persistence models for the future inbox agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class InboxMessage:
    provider: str
    provider_message_id: str
    provider_thread_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    body_text: str
    received_at: str
    ingestion_state: str
    content_hash: str
    id: int | None = None
    conversation_id: int | None = None


@dataclass(frozen=True)
class Conversation:
    id: int
    provider: str
    provider_thread_id: str
    status: str
    latest_message_at: str


@dataclass(frozen=True)
class AuditEvent:
    event_type: str
    entity_type: str
    entity_id: int
    actor: str = "system"
    correlation_id: str | None = None
    metadata: Mapping[str, object] | None = None
    id: int | None = None


@dataclass(frozen=True)
class AnalysisRun:
    id: int
    message_id: int
    analyzer: str
    model: str
    prompt_version: str
    input_fingerprint: str
    status: str
    error_class: str | None = None
