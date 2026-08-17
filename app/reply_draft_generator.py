"""Provider-neutral boundary for grounded local reply drafting."""

from __future__ import annotations

from typing import Protocol

from .reply_draft_input import ReplyDraftInput
from .reply_draft_models import ReplyDraft


class ReplyDraftGenerator(Protocol):
    def generate(self, draft_input: ReplyDraftInput) -> ReplyDraft: ...
