"""One bounded coding/verification tick; retries retain provider and edit IDs.

The coding provider returns CAS edits, never host commands. Only frozen trees
reach the separately registered trusted verifier. A successful tick still needs
independent review and explicit publication through PromotionService.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from ..domain.evolution import CandidateRevision
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from ..services.agent_lifecycle import decode
from ..services.lifecycle_budgets import LifecycleBudgetService
from ..services.gates import GateService
from .agent_bridge import AgentBridge,ProviderScope,SubmitEnvelope,UsageCeiling


@dataclass(frozen=True)
class CodingConfig:
    provider_id: str
    model_id: str
    model_version: str
    usage_ceiling: UsageCeiling
    graph_prefix: str
    max_observations: int
    max_context_bytes: int

    def __post_init__(self):
        for value in (self.provider_id,self.model_id,self.model_version,self.graph_prefix):
            if not isinstance(value,str) or not value or any(c.isspace() for c in value):
                raise ValueError('explicit coding identity required')
        if type(self.max_observations) is not int or not 1<=self.max_observations<=100:
            raise ValueError('bounded coding observation limit required')
        if type(self.max_context_bytes) is not int or not 1<=self.max_context_bytes<=1048576:
            raise ValueError('bounded coding context required')
        if not isinstance(self.usage_ceiling,UsageCeiling):raise ValueError('typed provider ceiling required')

    def wire(self):return dict(self.__dict__)|{'usage_ceiling':self.usage_ceiling.model_dump(mode='json')}


class _CodingProvider:
    def __init__(self,worker,proposal_id,request_id,token):
        self.worker,self.proposal_id,self.request_id,self.token=worker,proposal_id,request_id,token

    def check(self, *, observation=False):
        service=self.worker.service
        with service.repository.transaction() as tx:
            _,proposal,_=service._inputs_in(tx,self.proposal_id)
            service.authorize_in(tx,proposal,self.token)
            if tx.get('candidate_progress',self.proposal_id)['body']['status']=='cancelled':
                raise PermissionError('candidate cancelled')
            row=tx.get('coding_requests',self.request_id)
            if not row:raise PermissionError('coding request has no budgeted intent')
            if observation:
                count=row['body']['observations']
                if count>=self.worker.config.max_observations:raise Conflict('coding observation budget exhausted')
                reservation='coding-observation:'+self.request_id+':'+str(count)
                LifecycleBudgetService.reserve_in(tx,request_id=reservation,lifecycle_id=proposal.source_key.lifecycle_id,
                    amounts={'observations':1},input_ref=row['body']['envelope']['input_hash'])
                LifecycleBudgetService.settle_in(tx,request_id=reservation)
                tx.put('coding_requests',self.request_id,row['body']|{'observations':count+1},expected_revision=row['revision'])
            return row['body']

    async def capabilities(self):
        self.check();return await self.worker.provider.capabilities()
    async def submit(self,envelope):
        row=self.check()
        if row['envelope']!=envelope.model_dump(mode='json'):raise PermissionError('coding envelope changed')
        return await self.worker.provider.submit(envelope)
    async def lookup(self,request_id):
        if request_id!=self.request_id:raise PermissionError('coding lookup identity changed')
        self.check(observation=True);return await self.worker.provider.lookup(request_id)
    async def status(self,handle):
        self.check(observation=True);return await self.worker.provider.status(handle)
    async def collect(self,handle):
        self.check();return await self.worker.provider.collect(handle)
    async def cancel(self,handle):
        raise PermissionError('candidate cancel requires dedicated safety intent')


class CandidateWorker:
    def __init__(self,service,provider,*,config,verify,reviewer=None):
        self.service,self.repository,self.provider=service,service.repository,provider
        self.config,self.verify=config,verify
        self.reviewer=reviewer

    def _context(self,proposal,revision,previous):
        folder=self.service.workspace._folder(proposal.proposal_id)/'work'
        files={};used=0
        for prefix in proposal.allowed_paths:
            root=folder/prefix
            for path in sorted(root.rglob('*')) if root.is_dir() else [root]:
                if not path.exists() or path.is_dir():continue
                if path.is_symlink() or folder not in path.resolve().parents:raise ValueError('coding input escapes candidate')
                name=path.relative_to(folder).as_posix()
                if name in files:continue
                if len(files)>=256:raise ValueError('coding context file count exceeds limit')
                remaining=self.config.max_context_bytes-used
                if path.stat().st_size>remaining:raise ValueError('coding input exceeds context limit')
                with path.open('rb') as stream:raw=stream.read(remaining+1)
                if len(raw)>remaining:raise ValueError('coding input exceeds context limit')
                used+=len(raw)
                files[name]={'sha256':hashlib.sha256(raw).hexdigest(),'content':raw.decode('utf-8')}
        body={'schema_version':1,'role':'coder','proposal_id':proposal.proposal_id,'revision':revision,
            'facts':proposal.facts,'target_behavior':proposal.target_behavior,'allowed_paths':proposal.allowed_paths,
            'acceptance_refs':proposal.acceptance_refs,'files':files,'previous_gate':previous,
            'response_contract':{'schema_version':1,'edits':[{'path':'relative path','before_sha256':'exact current hash or null for create','content':'new UTF-8 text or null for delete'}]}}
        raw=json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if len(raw.encode())>self.config.max_context_bytes:raise ValueError('coding context exceeds byte limit')
        return raw

    async def tick(self,proposal_id,*,token):
        result=await self._tick(proposal_id,token=token)
        if result['status']!='awaiting_independent_review' or self.reviewer is None:return result
        with self.repository.transaction() as tx:
            candidate=decode(CandidateRevision,tx.get('candidate_revisions',result['candidate_ref'])['body'])
        reviewed=await self.reviewer.review(candidate,result['manifest_ref'],token=token)
        if reviewed['status']=='review_changes_requested':
            with self.repository.transaction() as tx:
                _,proposal,_=self.service._inputs_in(tx,proposal_id)
                self.service.authorize_in(tx,proposal,token)
                row=tx.get('candidate_workers',proposal_id)
                proof=tx.get('candidate_reviews',reviewed['review_ref'])['body']
                from ..domain.agent_lifecycle import TransitionGuards
                from ..services.agent_lifecycle import LifecycleService
                guards=TransitionGuards(schema_version=1,proof_valid=True)
                life=LifecycleService.load_in(tx,proposal.source_key.lifecycle_id)
                if life.phase=='promotion_pending':
                    self.service._event_in(tx,proposal,'proof_invalid',reviewed['review_ref'],token,guards)
                budget=all(value['spent']['candidate_revisions']+value['reserved']['candidate_revisions']<value['limits']['candidate_revisions']
                    for value in LifecycleBudgetService.snapshot_in(tx,life.key.lifecycle_id).values())
                self.service._event_in(tx,proposal,'tests_fail' if budget else 'budget_exhausted',reviewed['review_ref'],token,
                    guards.model_copy(update={'budget_available':budget,'effects_in_flight':False}))
                tx.put('candidate_workers',proposal_id,row['body']|{'status':'tests_failed',
                    'gate':row['body']['gate']|{'independent_review':proof['findings']}},expected_revision=row['revision'])
        return reviewed

    async def _tick(self,proposal_id,*,token):
        with self.repository.transaction() as tx:
            _,proposal,target=self.service._inputs_in(tx,proposal_id)
            self.service.authorize_in(tx,proposal,token,new_work=False)
            progress=tx.get('candidate_progress',proposal_id)['body']
            if progress['status']=='cancelled':return {'status':'cancelled','proposal_id':proposal_id}
            self.service.authorize_in(tx,proposal,token)
            previous=tx.get('candidate_workers',proposal_id)
            if previous and previous['body']['config']!=self.config.wire():raise Conflict('coding configuration changed')
            old=previous['body'] if previous else {}
            if old.get('status') in {'invalid_response','no_progress'}:return old['result']
            if old.get('status')=='awaiting_independent_review':
                candidate=decode(CandidateRevision,tx.get('candidate_revisions',old['result']['candidate_ref'])['body'])
                checked=GateService.evaluate_in(tx,old['result']['manifest_ref'],candidate)
                if checked['status']=='pass':return old['result']
                raise Conflict('candidate gate proof no longer passes')
            revision=old.get('revision',1)+(1 if old.get('status')=='tests_failed' else 0)
            request_id='coding:'+proposal_id+':'+str(revision)
            existing=tx.get('coding_requests',request_id)
            if not existing:self.service.revision_target_in(tx,proposal_id,revision)
        if existing:
            envelope=decode(SubmitEnvelope,existing['body']['envelope'])
        else:
            raw=self._context(proposal,revision,old.get('gate'))
            envelope=SubmitEnvelope(schema_version=1,request_id=request_id,
                lifecycle_id=proposal.source_key.lifecycle_id,
                scope=ProviderScope(schema_version=1,kind='candidate',ref=proposal_id),
                input_json=raw,input_hash=hashlib.sha256(raw.encode()).hexdigest(),model_id=self.config.model_id,
                model_version=self.config.model_version,usage_ceiling=self.config.usage_ceiling,
                deadline=float(min(self.service.status(proposal_id).get('deadline',float('inf')),
                    self._deadline(proposal,token))))
            with self.repository.transaction() as tx:
                self.service.authorize_in(tx,proposal,token)
                # Reject the next coding call before spending tokens if no revision can follow.
                snapshot=LifecycleBudgetService.snapshot_in(tx,proposal.source_key.lifecycle_id)
                for bound in snapshot.values():
                    if bound['spent']['candidate_revisions']+bound['reserved']['candidate_revisions']>=bound['limits']['candidate_revisions']:
                        raise Conflict('candidate revision budget exhausted')
                ceiling=self.config.usage_ceiling
                LifecycleBudgetService.reserve_in(tx,request_id=request_id,lifecycle_id=proposal.source_key.lifecycle_id,
                    amounts={'diagnosis_calls':1,'tokens':ceiling.input_tokens+ceiling.output_tokens,
                        'cost':ceiling.cost_microunits},input_ref=envelope.input_hash,provider_enforces_maximum=True)
                tx.put('coding_requests',request_id,{'envelope':envelope.model_dump(mode='json'),'observations':0},immutable=True)
                tx.put('candidate_workers',proposal_id,{'config':self.config.wire(),'revision':revision,
                    'status':'coding','gate':old.get('gate')},expected_revision=previous['revision'] if previous else -1)
        wrapper=_CodingProvider(self,proposal_id,request_id,token)
        bridge=AgentBridge(self.repository,wrapper,provider_id=self.config.provider_id,scope=envelope.scope,
            commit_guard=lambda tx: self.service.authorize_in(tx,proposal,token))
        remote=await bridge.submit(envelope)
        if not remote.terminal:
            return {'status':'provider_unknown' if remote.receipt is None else 'provider_running',
                'request_id':request_id,'proposal_id':proposal_id}
        if remote.receipt.state!='succeeded':
            with self.repository.transaction() as tx:
                self.service.authorize_in(tx,proposal,token,new_work=False)
                usage=remote.receipt.usage
                LifecycleBudgetService.settle_in(tx,request_id=request_id,
                    actual_tokens=usage.input_tokens+usage.output_tokens if usage else None,
                    actual_cost=usage.cost_microunits if usage else None)
            return {'status':'provider_'+remote.receipt.state,'request_id':request_id}
        output=await bridge.collect(request_id)
        usage=output.receipt.usage
        with self.repository.transaction() as tx:
            self.service.authorize_in(tx,proposal,token)
            LifecycleBudgetService.settle_in(tx,request_id=request_id,
                actual_tokens=usage.input_tokens+usage.output_tokens if usage else None,
                actual_cost=usage.cost_microunits if usage else None)
        edit_id='edit:'+request_id
        with self.repository.transaction() as tx:operation=tx.get('candidate_operations',edit_id)
        try:
            from ..environment.declarations import _unique_json_object
            body=json.loads(output.output_json,object_pairs_hook=_unique_json_object)
            if (not isinstance(body,dict) or set(body)!={'schema_version','edits'}
                    or type(body['schema_version']) is not int or body['schema_version']!=1):
                raise ValueError('coding provider must return only versioned structured edits')
            if not operation:self.service.workspace.validate_edits(proposal_id,body['edits'])
        except (ValueError,TypeError,PermissionError) as exc:
            result={'status':'no_progress' if isinstance(exc,ValueError) and 'no progress' in str(exc) else 'invalid_response',
                'request_id':request_id,'output_sha256':hashlib.sha256(output.output_json.encode()).hexdigest(),
                'reason':'structured_edits_invalid_or_unchanged'}
            with self.repository.transaction() as tx:
                self.service.authorize_in(tx,proposal,token)
                row=tx.get('candidate_workers',proposal_id)
                tx.put('candidate_workers',proposal_id,row['body']|{'status':result['status'],'result':result})
                tx.put('candidate_iteration_results',request_id,{'result':result},immutable=True)
            return result
        if operation and operation['body']['status']!='completed':
            candidate=self.service.reconcile_revision(edit_id,token=token)
        else:
            with self.repository.transaction() as tx:
                _,graph=self.service.revision_target_in(tx,proposal_id,revision)
            candidate=self.service.edit_revision(proposal_id,request_id=edit_id,edits=body['edits'],
                graph_ref=graph,token=token)
        gate=await self.verify(candidate,token)
        with self.repository.transaction() as tx:
            self.service.authorize_in(tx,proposal,token)
            # Never trust the callback's status string. Resolve its stored attestation.
            checked=GateService.evaluate_in(tx,gate['manifest_ref'],candidate)
            self.service.gate_observed_in(tx,candidate,gate['manifest_ref'],token)
            status={'pass':'awaiting_independent_review','fail':'tests_failed','blocked':'gate_blocked'}[checked['status']]
            result={'status':status,'candidate_ref':candidate.candidate_ref,'manifest_ref':gate['manifest_ref']}
            row=tx.get('candidate_workers',proposal_id)
            tx.put('candidate_workers',proposal_id,row['body']|{'status':status,'result':result,'gate':checked},expected_revision=row['revision'])
            tx.put('candidate_iteration_results',request_id,{'candidate_ref':candidate.candidate_ref,
                'gate':checked,'result':result},immutable=True)
        return result

    def _deadline(self,proposal,token):
        with self.repository.transaction() as tx:
            life=self.service.authorize_in(tx,proposal,token)
            return life.deadline
