import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from app.config import Settings
import app.main as runtime
from app.main import process_poll_cycle
from app.models import Attachment, EmailMessage
from app.conversation_models import ContextMessage, ConversationAnalysis, ConversationContext
from app.inbox_models import Conversation
from app.knowledge_models import KnowledgeMatch

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

def conversation_analysis():
 return ConversationAnalysis.from_mapping({'conversation_summary':'summary','current_intent':'check_order_status','priority':'normal','urgency':'medium','unresolved_requests':[],'resolved_points':[],'order_numbers':[],'relevant_dates':[],'latest_sender_request':'help','confidence':.9,'needs_human':False,'human_reason':None,'recommended_action':'draft_reply'})

class DraftRepo(Repo):
 def __init__(self, *, analysis=True, matches=None):
  super().__init__(analysis); self.analysis_value=conversation_analysis(); self.matches=[] if matches is None else matches
 def latest_successful_conversation_analysis(self,id): return (7,self.analysis_value) if self.analysis else None
 def latest_successful_retrieval(self,conversation_id,analysis_id): return (19,self.matches)
 def list_messages_for_conversation(self,conversation_id): return []

class Builder:
 def __init__(self):
  conversation=Conversation(1,'gmail','thread','open','2026-08-17T10:00:00+00:00')
  self.context=ConversationContext(conversation,(ContextMessage(3,'m','s',('r',),'subject','body','time','a'*64),),3,1,1,False,'context-fingerprint')
  self.calls=[]
 def build(self,conversation,messages): self.calls.append((conversation,messages)); return self.context

class Drafting:
 def __init__(self, fail=False): self.calls=[]; self.fail=fail
 def create_draft(self,draft_input,*,conversation_analysis_id):
  self.calls.append((draft_input,conversation_analysis_id))
  if self.fail: raise RuntimeError('draft failed')
  return type('Outcome',(),{'generated':True})()

class RuntimeTests(unittest.TestCase):
 def execute_cycle(self, repo=None, convo=None, retrieval=None, drafting=None, builder=None):
  msg=EmailMessage('m','s','x','b','t',(Attachment('a','a.txt',b'x'),),'t')
  attach=S(); result=process_poll_cycle(messages=[msg],repository=repo or Repo(),message_ingestion_service=S(),inbox_analysis_service=S(),conversation_analysis_service=convo or S(),knowledge_retrieval_service=retrieval or Retrieval(),reply_draft_service=drafting,thread_context_builder=builder,attachment_processor=attach); return result,attach
 def test_success_analysis_triggers_retrieval_and_stops(self):
  r=Retrieval(); result,attach=self.execute_cycle(retrieval=r); self.assertEqual(r.calls,[(1,7,'validated-analysis')]); self.assertEqual(result,'attachment-summary'); self.assertEqual(attach.calls,1)
 def test_missing_analysis_blocks_retrieval(self):
  r=Retrieval(); draft=Drafting(); self.execute_cycle(repo=Repo(False),retrieval=r,drafting=draft,builder=Builder()); self.assertEqual(r.calls,[]); self.assertEqual(draft.calls,[])
 def test_failed_analysis_blocks_retrieval_and_keeps_attachment_path(self):
  r=Retrieval(); draft=Drafting(); _,attach=self.execute_cycle(convo=S(True),retrieval=r,drafting=draft,builder=Builder()); self.assertEqual(r.calls,[]); self.assertEqual(draft.calls,[]); self.assertEqual(attach.calls,1)
 def test_retrieval_failure_keeps_attachment_path(self):
  r=Retrieval(True); draft=Drafting(); _,attach=self.execute_cycle(retrieval=r,drafting=draft,builder=Builder()); self.assertEqual(len(r.calls),1); self.assertEqual(draft.calls,[]); self.assertEqual(attach.calls,1)
 def test_zero_result_runtime_is_terminal(self):
  r=Retrieval(zero=True); result,attach=self.execute_cycle(retrieval=r); self.assertEqual(len(r.calls),1); self.assertEqual(result,'attachment-summary'); self.assertEqual(attach.calls,1)
 def test_successful_retrieval_invokes_drafting_with_bounded_persisted_state_and_stops(self):
  match=KnowledgeMatch(11,2,'policy.md',None,'approved knowledge',.8,1); repo=DraftRepo(matches=[match]); draft=Drafting(); builder=Builder()
  result,attach=self.execute_cycle(repo=repo,retrieval=Retrieval(),drafting=draft,builder=builder)
  self.assertEqual(result,'attachment-summary'); self.assertEqual(attach.calls,1); self.assertEqual(len(draft.calls),1)
  draft_input,analysis_id=draft.calls[0]
  self.assertEqual((analysis_id,draft_input.conversation_id,draft_input.latest_message_id,draft_input.knowledge_retrieval_run_id),(7,1,3,19))
  self.assertEqual((draft_input.context_fingerprint,draft_input.allowed_grounding_chunk_ids),('context-fingerprint',frozenset({11})))
  self.assertEqual(draft_input.knowledge_matches[0].chunk_text,'approved knowledge')
 def test_zero_result_retrieval_reaches_drafting_for_local_insufficient_knowledge_path(self):
  draft=Drafting(); result,attach=self.execute_cycle(repo=DraftRepo(matches=[]),retrieval=Retrieval(),drafting=draft,builder=Builder())
  self.assertEqual(result,'attachment-summary'); self.assertEqual(attach.calls,1); self.assertEqual(len(draft.calls),1)
  self.assertEqual(draft.calls[0][0].knowledge_matches,())
 def test_drafting_failure_keeps_retrieval_state_and_attachment_path_independent(self):
  retrieval=Retrieval(); draft=Drafting(True); repo=DraftRepo(matches=[KnowledgeMatch(11,2,'policy.md',None,'approved knowledge',.8,1)])
  _,attach=self.execute_cycle(repo=repo,retrieval=retrieval,drafting=draft,builder=Builder())
  self.assertEqual(len(retrieval.calls),1); self.assertEqual(len(draft.calls),1); self.assertEqual(attach.calls,1)
 def test_run_once_delegates_to_process_poll_cycle(self):
  with tempfile.TemporaryDirectory() as directory:
   settings=Settings(.85,'review',{'x':'folder'},Path(directory)/'state.sqlite3',anthropic_api_key='test')
   email=type('Email',(),{'list_messages':lambda self: ()})()
   called=[]
   with patch.object(runtime,'build_email',return_value=email), patch.object(runtime,'ClaudeInboxAnalyzer'), patch.object(runtime,'ClaudeConversationAnalyzer'), patch.object(runtime,'ClaudeGroundedReplyGenerator'), patch.object(runtime,'SqliteStateManager'), patch.object(runtime,'build_classifier'), patch.object(runtime,'build_drive'), patch.object(runtime,'process_poll_cycle',side_effect=lambda **kwargs: called.append(kwargs) or 'summary'):
    with patch.object(runtime.ClaudeInboxAnalyzer,'from_settings',return_value=object()), patch.object(runtime.ClaudeConversationAnalyzer,'from_settings',return_value=object()), patch.object(runtime.ClaudeGroundedReplyGenerator,'from_settings',return_value=object()):
     self.assertEqual(runtime.run_once(settings),'summary')
   self.assertEqual(len(called),1)
