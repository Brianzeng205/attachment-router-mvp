from __future__ import annotations
from typing import Protocol
from .knowledge_models import KnowledgeMatch, KnowledgeQuery

class KnowledgeRetriever(Protocol):
    def retrieve(self, query: KnowledgeQuery, *, limit: int) -> list[KnowledgeMatch]: ...
