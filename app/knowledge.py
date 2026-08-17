"""Approved-local knowledge ingestion and lexical retrieval; never executes file content."""
from __future__ import annotations
import hashlib, re
from pathlib import Path
from .knowledge_models import KnowledgeMatch, KnowledgeQuery

SUPPORTED = {".txt", ".md"}

def chunks(text: str, maximum: int, overlap: int) -> list[str]:
    text = text.replace("\r\n", "\n").strip()
    if not text: return []
    out=[]; start=0
    while start < len(text):
        end=min(start+maximum, len(text)); cut=text.rfind("\n", start, end)
        if cut <= start: cut=end
        out.append(text[start:cut].strip())
        if cut == len(text): break
        start=max(cut-overlap, start+1)
    return [x for x in out if x]

class KnowledgeIngestionService:
    def __init__(self, repository, directory: Path, maximum: int, overlap: int, index_version: str="v1"):
        self.r, self.directory, self.maximum, self.overlap, self.index_version = repository, directory.resolve(), maximum, overlap, index_version
    def ingest_file(self, path: Path) -> int | None:
        resolved=path.resolve()
        if self.directory not in resolved.parents: raise ValueError("Knowledge path is outside approved directory")
        if resolved.suffix.lower() not in SUPPORTED: return None
        text=resolved.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
        digest=hashlib.sha256(text.encode()).hexdigest()
        return self.r.upsert_knowledge(resolved.relative_to(self.directory).as_posix(), resolved.name, text, digest,
                                       chunks(text, self.maximum, self.overlap), self.index_version)
    def ingest_all(self) -> list[int]:
        if not self.directory.is_dir(): raise FileNotFoundError("Configured knowledge directory is missing")
        return [x for p in sorted(self.directory.rglob("*")) if p.is_file() for x in [self.ingest_file(p)] if x is not None]

class SqliteKnowledgeRetriever:
    def __init__(self, repository): self.r=repository
    def retrieve(self, query: KnowledgeQuery, *, limit: int) -> list[KnowledgeMatch]:
        terms=" OR ".join(re.findall(r"[A-Za-z0-9_]+", query.text.replace("_", " ")))
        if not terms or limit < 1: return []
        return self.r.search_knowledge(terms, limit)

class KnowledgeQueryBuilder:
    def build(self, analysis) -> KnowledgeQuery:
        values=[analysis.current_intent, analysis.latest_sender_request or "", *analysis.unresolved_requests, *analysis.order_numbers]
        return KnowledgeQuery(" ".join(" ".join(values).split())[:1000])
