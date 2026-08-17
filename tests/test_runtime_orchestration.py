import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from app.config import Settings
import app.main as runtime
from app.main import process_poll_cycle
from app.models import Attachment, EmailMessage

class S:
 def __init__(self, fail=False): self.calls=0; self.fail=fail
 def ingest_all(self,x): self.calls+=1
 def analyze_all(self,x):
  self.calls+=1
  if self.fail: raise RuntimeError('failed')
 def process_all(self): self.calls+=1; return 'attachment-summary'

class Repo:
 def __init__(self, analysis=True): self.analysis=analysis; self.conversation=type('C',(),{'id':1})()
 def list_conversations(self): return [self.conversation]
 def latest_successful_conversation_analysis(self,id): return (7,'validated-analysis') if self.analysis else None

class Retrieval:
 def __init__(self, fail=False, zero=False): self.calls=[]; self.fail=fail; self.zero=zero
 def retrieve(self,*args):
  self.calls.append(args)
  if self.fail: raise RuntimeError('retrieval failed')

class RuntimeTests(unittest.TestCase):
 def execute_cycle(self, repo=None, convo=None, retrieval=None):
  msg=EmailMessage('m','s','x','b','t',(Attachment('a','a.txt',b'x'),),'t')
  attach=S(); result=process_poll_cycle(messages=[msg],repository=repo or Repo(),message_ingestion_service=S(),inbox_analysis_service=S(),conversation_analysis_service=convo or S(),knowledge_retrieval_service=retrieval or Retrieval(),attachment_processor=attach); return result,attach
 def test_success_analysis_triggers_retrieval_and_stops(self):
  r=Retrieval(); result,attach=self.execute_cycle(retrieval=r); self.assertEqual(r.calls,[(1,7,'validated-analysis')]); self.assertEqual(result,'attachment-summary'); self.assertEqual(attach.calls,1)
 def test_missing_analysis_blocks_retrieval(self):
  r=Retrieval(); self.execute_cycle(repo=Repo(False),retrieval=r); self.assertEqual(r.calls,[])
 def test_failed_analysis_blocks_retrieval_and_keeps_attachment_path(self):
  r=Retrieval(); _,attach=self.execute_cycle(convo=S(True),retrieval=r); self.assertEqual(r.calls,[]); self.assertEqual(attach.calls,1)
 def test_retrieval_failure_keeps_attachment_path(self):
  r=Retrieval(True); _,attach=self.execute_cycle(retrieval=r); self.assertEqual(len(r.calls),1); self.assertEqual(attach.calls,1)
 def test_zero_result_runtime_is_terminal(self):
  r=Retrieval(zero=True); result,attach=self.execute_cycle(retrieval=r); self.assertEqual(len(r.calls),1); self.assertEqual(result,'attachment-summary'); self.assertEqual(attach.calls,1)
 def test_run_once_delegates_to_process_poll_cycle(self):
  with tempfile.TemporaryDirectory() as directory:
   settings=Settings(.85,'review',{'x':'folder'},Path(directory)/'state.sqlite3',anthropic_api_key='test')
   email=type('Email',(),{'list_messages':lambda self: ()})()
   called=[]
   with patch.object(runtime,'build_email',return_value=email), patch.object(runtime,'ClaudeInboxAnalyzer'), patch.object(runtime,'ClaudeConversationAnalyzer'), patch.object(runtime,'SqliteStateManager'), patch.object(runtime,'build_classifier'), patch.object(runtime,'build_drive'), patch.object(runtime,'process_poll_cycle',side_effect=lambda **kwargs: called.append(kwargs) or 'summary'):
    with patch.object(runtime.ClaudeInboxAnalyzer,'from_settings',return_value=object()), patch.object(runtime.ClaudeConversationAnalyzer,'from_settings',return_value=object()):
     self.assertEqual(runtime.run_once(settings),'summary')
   self.assertEqual(len(called),1)
