"""Provider-neutral Inbox Analyzer boundary."""

from __future__ import annotations

from typing import Protocol

from .analysis_models import InboxAnalysis
from .inbox_models import InboxMessage


class InboxAnalyzer(Protocol):
    def analyze(self, message: InboxMessage) -> InboxAnalysis: ...
