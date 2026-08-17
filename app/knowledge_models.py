from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class KnowledgeQuery:
    text: str

@dataclass(frozen=True)
class KnowledgeMatch:
    chunk_id: int; document_id: int; source_filename: str; title: str | None; chunk_text: str; score: float; rank: int
