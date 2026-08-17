import tempfile, unittest
from pathlib import Path
from app.inbox_repository import SqliteInboxRepository
from app.knowledge import KnowledgeIngestionService, SqliteKnowledgeRetriever, chunks
from app.knowledge_models import KnowledgeQuery
from app.knowledge_retrieval_service import KnowledgeRetrievalService

class KnowledgeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.k=self.root/'knowledge'; self.k.mkdir(); self.r=SqliteInboxRepository(self.root/'state.sqlite3'); self.s=KnowledgeIngestionService(self.r,self.k,40,8)
 def tearDown(self): self.r.close(); self.tmp.cleanup()
 def test_txt_md_idempotent_and_relevant_retrieval(self):
  (self.k/'refund.txt').write_text('Refund policy: refund orders within thirty days.')
  (self.k/'shipping.md').write_text('# Shipping\nDelivery tracking and shipping delays.')
  self.s.ingest_all(); self.s.ingest_all()
  self.assertEqual(self.r.connection.execute('SELECT COUNT(*) FROM knowledge_documents').fetchone()[0],2)
  finder=SqliteKnowledgeRetriever(self.r)
  refund=finder.retrieve(KnowledgeQuery('refund policy'),limit=2)
  shipping=finder.retrieve(KnowledgeQuery('shipping delivery'),limit=1)
  self.assertEqual(refund[0].source_filename,'refund.txt'); self.assertEqual(shipping[0].source_filename,'shipping.md')
  self.assertTrue(refund[0].chunk_id and refund[0].document_id and refund[0].rank==1)
 def test_bounds_overlap_updates_and_safe_sources(self):
  file=self.k/'policy.txt'; file.write_text('abcdefghijklmnopqrstuvwxyz'*3)
  self.s.ingest_file(file); before=self.r.connection.execute('SELECT COUNT(*) FROM knowledge_chunks').fetchone()[0]
  self.assertTrue(all(len(x)<=40 for x in chunks(file.read_text(),40,8)))
  self.assertEqual(chunks('abcdefghij',6,2),['abcdef','efghij'])
  file.write_text('new refund policy only'); self.s.ingest_file(file)
  self.assertEqual(SqliteKnowledgeRetriever(self.r).retrieve(KnowledgeQuery('abcdefghijklmnopqrstuvwxyz'),limit=5),[])
  self.assertGreater(before,0)
  self.assertIsNone(self.s.ingest_file(self.k/'bad.pdf'))
  with self.assertRaises(ValueError): self.s.ingest_file(self.root/'outside.txt')
 def test_empty_and_no_match_are_safe(self):
  (self.k/'empty.txt').write_text('   '); self.s.ingest_all()
  self.assertEqual(SqliteKnowledgeRetriever(self.r).retrieve(KnowledgeQuery('nothing'),limit=3),[])
 def test_persisted_retrieval_idempotency_and_failure(self):
  (self.k/'refund.txt').write_text('Refund policy refund orders.'); self.s.ingest_all()
  class Analysis: current_intent='request_refund'; latest_sender_request='refund'; unresolved_requests=(); order_numbers=()
  service=KnowledgeRetrievalService(self.r,SqliteKnowledgeRetriever(self.r),limit=2)
  found, skipped=service.retrieve(1,99,Analysis()); self.assertFalse(skipped); self.assertTrue(found)
  again, skipped=service.retrieve(1,99,Analysis()); self.assertTrue(skipped); self.assertEqual(again,[])
  row=self.r.connection.execute("SELECT status,result_count FROM knowledge_retrieval_runs").fetchone(); self.assertEqual(tuple(row),('succeeded',1))
  self.assertEqual(self.r.connection.execute('SELECT COUNT(*) FROM knowledge_retrieval_results').fetchone()[0],1)
  class Broken:
   def retrieve(self,q,limit): raise RuntimeError('fts unavailable')
  with self.assertRaises(RuntimeError): KnowledgeRetrievalService(self.r,Broken(),retriever_version='broken').retrieve(1,100,Analysis())
  self.assertEqual(self.r.connection.execute("SELECT status FROM knowledge_retrieval_runs ORDER BY id DESC").fetchone()[0],'failed')
