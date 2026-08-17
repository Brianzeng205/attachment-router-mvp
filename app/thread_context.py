"""Deterministic, bounded construction of persisted conversation context."""

from __future__ import annotations

import hashlib
import json

from .conversation_models import ConversationContext, context_message
from .inbox_models import Conversation, InboxMessage


class ThreadContextBuilder:
    def __init__(self, max_messages: int, max_context_chars: int, version: str = "v1") -> None:
        if max_messages < 1 or max_context_chars < 1:
            raise ValueError("Thread context limits must be positive")
        self.max_messages = max_messages
        self.max_context_chars = max_context_chars
        self.version = version

    def build(self, conversation: Conversation, messages: list[InboxMessage]) -> ConversationContext:
        ordered = sorted(messages, key=lambda item: (item.received_at, item.id or 0))
        if not ordered:
            raise ValueError("A conversation requires at least one persisted message")
        selected = ordered[-self.max_messages:]
        truncated = len(selected) < len(ordered)
        # Allocate body characters from newest to oldest, then restore chronological order.
        remaining = self.max_context_chars
        bounded_bodies: dict[int, str] = {}
        for message in reversed(selected):
            if message.id is None:
                raise ValueError("Persisted messages require an internal ID")
            body, remaining, body_truncated = _take(message.body_text, remaining)
            bounded_bodies[message.id] = body
            truncated = truncated or body_truncated

        # Preserve useful newest content first; any remaining budget goes to metadata.
        bounded_reversed = []
        for message in reversed(selected):
            subject, remaining, subject_truncated = _take(message.subject, remaining)
            sender, remaining, sender_truncated = _take(message.sender, remaining)
            recipients: list[str] = []
            recipients_truncated = False
            for recipient in message.recipients:
                bounded_recipient, remaining, was_truncated = _take(recipient, remaining)
                recipients.append(bounded_recipient)
                recipients_truncated = recipients_truncated or was_truncated
            if subject_truncated or sender_truncated or recipients_truncated:
                truncated = True
            bounded = InboxMessage(**{
                **message.__dict__, "sender": sender, "recipients": tuple(recipients), "subject": subject,
            })
            bounded_reversed.append(context_message(bounded, bounded_bodies[message.id or 0]))
        included = tuple(reversed(bounded_reversed))
        fingerprint_input = {
            "conversation_id": conversation.id,
            "messages": [(item.id, item.content_hash) for item in included],
            "builder_version": self.version,
            "max_messages": self.max_messages,
            "max_context_chars": self.max_context_chars,
        }
        fingerprint = hashlib.sha256(json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return ConversationContext(conversation, included, included[-1].id, len(ordered), len(included), truncated, fingerprint)


def _take(value: str, remaining: int) -> tuple[str, int, bool]:
    bounded = value[:max(remaining, 0)]
    return bounded, remaining - len(bounded), len(bounded) < len(value)
