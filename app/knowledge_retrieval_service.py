from __future__ import annotations
import hashlib
from .knowledge import KnowledgeQueryBuilder

class KnowledgeRetrievalService:
    """Passive retrieval only; results are never interpreted as commands or actions."""
    def __init__(self, repository, retriever, query_builder=None, *, limit=5, retriever_name="sqlite_fts", retriever_version="v1", index_version="v1"):
        self.repository,self.retriever,self.builder,self.limit=repository,retriever,query_builder or KnowledgeQueryBuilder(),limit; self.retriever_name,self.retriever_version,self.index_version=retriever_name,retriever_version,index_version
    def retrieve(self, conversation_id, analysis_id, analysis):
        query=self.builder.build(analysis)
        index=self.repository.knowledge_index_fingerprint(self.index_version)
        fingerprint=hashlib.sha256(f"{analysis_id}:{query.text}:{index}:{self.retriever_name}:{self.retriever_version}:{self.limit}".encode()).hexdigest()
        if self.repository.successful_retrieval(conversation_id,fingerprint): return [], True
        run=self.repository.start_retrieval(conversation_id,analysis_id,query.text,fingerprint,index,self.retriever_name,self.retriever_version,self.limit)
        self.repository.record_audit_event(__import__('app.inbox_models',fromlist=['AuditEvent']).AuditEvent('knowledge_retrieval_started','knowledge_retrieval_run',run,metadata={'conversation_id':conversation_id,'retriever':self.retriever_name}))
        try:
            matches=self.retriever.retrieve(query,limit=self.limit); self.repository.complete_retrieval(run,matches)
        except Exception as exc:
            self.repository.fail_retrieval(run,type(exc).__name__); self.repository.record_audit_event(__import__('app.inbox_models',fromlist=['AuditEvent']).AuditEvent('knowledge_retrieval_failed','knowledge_retrieval_run',run,metadata={'failure_class':type(exc).__name__})); raise
        self.repository.record_audit_event(__import__('app.inbox_models',fromlist=['AuditEvent']).AuditEvent('knowledge_retrieval_succeeded','knowledge_retrieval_run',run,metadata={'result_count':len(matches),'index':index[:12]}))
        return matches, False
