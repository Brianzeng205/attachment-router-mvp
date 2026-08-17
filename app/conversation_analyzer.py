"""Provider-neutral boundary for ordered conversation analysis."""

from __future__ import annotations

from typing import Protocol

from .conversation_models import ConversationAnalysis, ConversationContext


class ConversationAnalyzer(Protocol):
    def analyze(self, context: ConversationContext) -> ConversationAnalysis: ...
