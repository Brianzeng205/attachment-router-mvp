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
from app.decision_policy import DefaultDecisionPolicy
from app.policy_models import PolicyDecision
from app.reply_draft_models import PersistedReplyDraft, ReplyDraft

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
 def get_reply_draft_run_fingerprint(self,run_id): return 'draft-fingerprint'

class Builder:
 def __init__(self):
  conversation=Conversation(1,'gmail','thread','open','2026-08-17T10:00:00+00:00')
  self.context=ConversationContext(conversation,(ContextMessage(3,'m','s',('r',),'subject','body','time','a'*64),),3,1,1,False,'context-fingerprint')
  self.calls=[]
 def build(self,conversation,messages): self.calls.append((conversation,messages)); return self.context

class Drafting:
 def __init__(self, fail=False, reply=None):
  self.calls=[]; self.fail=fail; self.reply=reply or ReplyDraft('drafted','Re: Help','Validated local reply',(11,),(),.9,False,None,'en')
 def create_draft(self,draft_input,*,conversation_analysis_id):
  self.calls.append((draft_input,conversation_analysis_id))
  if self.fail: raise RuntimeError('draft failed')
  return type('Outcome',(),{'generated':True,'draft':PersistedReplyDraft(31,41,1,3,self.reply)})()

class Policy:
 def __init__(self, fail=False): self.calls=[]; self.fail=fail; self.actual=DefaultDecisionPolicy()
 def evaluate(self,**kwargs):
  self.calls.append(kwargs)
  if self.fail: raise RuntimeError('policy failed')
  return self.actual.evaluate(**kwargs)

class Review:
 def __init__(self, fail=False): self.calls=[]; self.fail=fail; self.rows={}; self.transition_calls=[]
 def record_decision(self,**kwargs):
  self.calls.append(kwargs)
  if self.fail: raise RuntimeError('review persistence failed')
  decision=kwargs['decision']; review_type={'ready_for_review':'standard_review','human_review_required':'required_review','blocked':'blocked_resolution'}.get(decision.decision)
  key=(kwargs['reply_draft_id'],decision.rule_version,decision.decision)
  if key not in self.rows:
   item=None if review_type is None else type('Item',(),{'review_type':review_type,'status':'pending'})()
   self.rows[key]=type('Result',(),{'policy_decision':decision,'review_item':item})()
  return self.rows[key]
 def approve(self,*args): self.transition_calls.append(('approve',args))
 def reject(self,*args): self.transition_calls.append(('reject',args))
 def request_changes(self,*args): self.transition_calls.append(('request_changes',args))

class RuntimeTests(unittest.TestCase):
 def execute_cycle(self, repo=None, convo=None, retrieval=None, drafting=None, builder=None, policy=None, review=None):
  msg=EmailMessage('m','s','x','b','t',(Attachment('a','a.txt',b'x'),),'t')
  attach=S(); result=process_poll_cycle(messages=[msg],repository=repo or Repo(),message_ingestion_service=S(),inbox_analysis_service=S(),conversation_analysis_service=convo or S(),knowledge_retrieval_service=retrieval or Retrieval(),reply_draft_service=drafting,thread_context_builder=builder,decision_policy=policy,review_queue_service=review,attachment_processor=attach); return result,attach
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
 def test_policy_decision_flows_create_only_expected_pending_local_review_state(self):
  cases=(
   (ReplyDraft('drafted','Re','Safe reply',(11,),(),.9,False,None,'en'),'ready_for_review','standard_review'),
   (ReplyDraft('insufficient_knowledge',None,'Insufficient confirmed information',(),(),1,True,'insufficient_knowledge','en'),'human_review_required','required_review'),
   (ReplyDraft('drafted','Re','Unsafe reply',(11,),('unsupported claim',),.9,False,None,'en'),'blocked','blocked_resolution'),
   (ReplyDraft('not_applicable',None,'No reply is applicable',(),(),.9,False,None,'en'),'no_action',None),
  )
  for reply,expected,review_type in cases:
   with self.subTest(decision=expected):
    repo=DraftRepo(matches=[KnowledgeMatch(11,2,'policy.md',None,'approved knowledge',.8,1)]); drafting=Drafting(reply=reply); policy=Policy(); review=Review()
    result,attach=self.execute_cycle(repo=repo,retrieval=Retrieval(),drafting=drafting,builder=Builder(),policy=policy,review=review)
    self.assertEqual(result,'attachment-summary'); self.assertEqual(attach.calls,1); self.assertEqual(len(policy.calls),1); self.assertEqual(len(review.calls),1)
    self.assertIs(policy.calls[0]['conversation_analysis'],repo.analysis_value); self.assertIs(policy.calls[0]['reply_draft'],reply)
    self.assertEqual(review.calls[0]['decision'].decision,expected)
    outcome=next(iter(review.rows.values())); self.assertEqual(outcome.review_item.review_type if outcome.review_item else None,review_type)
    if outcome.review_item: self.assertEqual(outcome.review_item.status,'pending')
    self.assertEqual(review.transition_calls,[])
 def test_ai_draft_reply_recommendation_cannot_override_human_review_policy(self):
  repo=DraftRepo(matches=[]); reply=ReplyDraft('insufficient_knowledge',None,'Need confirmed information',(),(),1,True,'insufficient_knowledge','en'); policy=Policy(); review=Review()
  self.execute_cycle(repo=repo,retrieval=Retrieval(),drafting=Drafting(reply=reply),builder=Builder(),policy=policy,review=review)
  self.assertEqual(repo.analysis_value.recommended_action,'draft_reply'); self.assertEqual(review.calls[0]['decision'].decision,'human_review_required')
 def test_upstream_failures_never_reach_policy_or_review(self):
  cases=((S(True),Retrieval(),Drafting()),(S(),Retrieval(True),Drafting()),(S(),Retrieval(),Drafting(True)))
  for convo,retrieval,drafting in cases:
   with self.subTest(failure=(convo.fail,retrieval.fail,drafting.fail)):
    policy=Policy(); review=Review(); self.execute_cycle(repo=DraftRepo(matches=[]),convo=convo,retrieval=retrieval,drafting=drafting,builder=Builder(),policy=policy,review=review)
    self.assertEqual(policy.calls,[]); self.assertEqual(review.calls,[])
 def test_policy_or_review_failure_preserves_local_draft_and_attachment_independence(self):
  for policy,review in ((Policy(True),Review()),(Policy(),Review(True))):
   with self.subTest(policy_failure=policy.fail,review_failure=review.fail):
    drafting=Drafting(); _,attach=self.execute_cycle(repo=DraftRepo(matches=[]),retrieval=Retrieval(),drafting=drafting,builder=Builder(),policy=policy,review=review)
    self.assertEqual(len(drafting.calls),1); self.assertEqual(attach.calls,1)
    self.assertEqual(len(policy.calls),1)
    self.assertEqual(len(review.calls),0 if policy.fail else 1)
 def test_repeated_runtime_relies_on_review_service_idempotency_and_never_auto_resolves(self):
  repo=DraftRepo(matches=[]); drafting=Drafting(); policy=Policy(); review=Review()
  for _ in range(2): self.execute_cycle(repo=repo,retrieval=Retrieval(),drafting=drafting,builder=Builder(),policy=policy,review=review)
  self.assertEqual(len(review.calls),2); self.assertEqual(len(review.rows),1); self.assertEqual(next(iter(review.rows.values())).review_item.status,'pending'); self.assertEqual(review.transition_calls,[])
 def test_run_once_delegates_to_process_poll_cycle(self):
  with tempfile.TemporaryDirectory() as directory:
   settings=Settings(.85,'review',{'x':'folder'},Path(directory)/'state.sqlite3',anthropic_api_key='test')
   email=type('Email',(),{'list_messages':lambda self: ()})()
   called=[]
   with patch.object(runtime,'build_email',return_value=email), patch.object(runtime,'ClaudeInboxAnalyzer'), patch.object(runtime,'ClaudeConversationAnalyzer'), patch.object(runtime,'ClaudeGroundedReplyGenerator'), patch.object(runtime,'SqliteStateManager'), patch.object(runtime,'build_classifier'), patch.object(runtime,'build_drive'), patch.object(runtime,'process_poll_cycle',side_effect=lambda **kwargs: called.append(kwargs) or 'summary'):
    with patch.object(runtime.ClaudeInboxAnalyzer,'from_settings',return_value=object()), patch.object(runtime.ClaudeConversationAnalyzer,'from_settings',return_value=object()), patch.object(runtime.ClaudeGroundedReplyGenerator,'from_settings',return_value=object()):
     self.assertEqual(runtime.run_once(settings),'summary')
   self.assertEqual(len(called),1)
