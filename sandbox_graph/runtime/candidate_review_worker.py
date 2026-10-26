"""Bounded independent model review over immutable candidate and gate evidence."""
import hashlib
import json
from pathlib import Path
from ..domain.evolution import ReviewRecord,ReviewFinding,TARGET_FIELDS
from ..domain.models import canonical_hash
from ..services.agent_lifecycle import decode
from ..services.candidate_reviews import CandidateReviewService
from ..services.gates import GateService
from ..services.lifecycle_budgets import LifecycleBudgetService
from .agent_bridge import AgentBridge,ProviderScope,SubmitEnvelope


class _ReviewProvider:
    """Revalidate identity at every external boundary, including after capabilities."""
    def __init__(self,worker,proposal,request_id,token):
        self.worker,self.proposal,self.request_id,self.token=worker,proposal,request_id,token
    def check(self):
        with self.worker.repository.transaction() as tx:
            self.worker._authorize(tx,self.proposal,self.token)
            if tx.get('candidate_progress',self.proposal.proposal_id)['body']['status']=='cancelled':
                raise PermissionError('candidate cancelled')
            row=tx.get('candidate_review_requests',self.request_id)
            if not row:raise PermissionError('review has no budgeted request')
            return row['body']
    async def capabilities(self):self.check();return await self.worker.provider.capabilities()
    async def submit(self,envelope):
        if self.check()['envelope']!=envelope.model_dump(mode='json'):raise PermissionError('review envelope changed')
        return await self.worker.provider.submit(envelope)
    async def lookup(self,request_id):
        if request_id!=self.request_id:raise PermissionError('review request identity changed')
        self.check();return await self.worker.provider.lookup(request_id)
    async def status(self,handle):self.check();return await self.worker.provider.status(handle)
    async def collect(self,handle):self.check();return await self.worker.provider.collect(handle)
    async def cancel(self,handle):raise PermissionError('review cancel requires a safety intent')


class CandidateReviewWorker:
    def __init__(self,service,provider,*,config,reviewer_identity,coder_identity):
        if reviewer_identity==coder_identity:raise PermissionError('independent reviewer identity required')
        self.service=service;self.repository=service.repository;self.provider=provider;self.config=config
        self.reviewer_identity=reviewer_identity;self.coder_identity=coder_identity

    def _authorize(self,tx,proposal,token):
        life=self.service.authorize_in(tx,proposal,token)
        row=tx.get('candidate_reviewers',self.reviewer_identity);registered=row['body'] if row else {}
        expected={'provider_id':self.config.provider_id,'model_id':self.config.model_id,
            'model_version':self.config.model_version,'version':self.config.model_version,'kind':'model'}
        if (registered.get('revoked') is not False or registered.get('expires_at',0)<=tx.now
                or any(registered.get(key)!=value for key,value in expected.items())):
            raise PermissionError('independent reviewer/provider registration differs or expired')
        return life

    async def review(self,candidate,manifest_ref,*,token):
        request_id='candidate-review:'+canonical_hash({'candidate':candidate.candidate_hash,'manifest':manifest_ref,
            'reviewer':self.reviewer_identity,'config':self.config.wire()})
        with self.repository.transaction() as tx:
            _,proposal,_=self.service._inputs_in(tx,candidate.proposal_id)
            life=self._authorize(tx,proposal,token)
            old=tx.get('candidate_review_results',request_id)
            if old:
                if old['body']['status']=='review_approved':CandidateReviewService.approved_in(tx,request_id,candidate)
                return old['body']
            checked=GateService.evaluate_in(tx,manifest_ref,candidate)
            if checked['status']!='pass':raise PermissionError('review requires current passing trusted gates')
            manifest=tx.get('gate_manifests',manifest_ref)['body']
            existing=tx.get('candidate_review_requests',request_id)
            deadline=min(life.deadline,manifest['expires_at'])
        if existing:envelope=decode(SubmitEnvelope,existing['body']['envelope'])
        else:
            frozen={key:value for key,value in candidate.model_dump(mode='json').items() if key not in {'schema_version','candidate_ref','proposal_id','budget_ref',*TARGET_FIELDS}}
            self.service.workspace.verify(frozen)
            files={};total=0
            for name in candidate.changed_paths:
                path=Path(candidate.workspace_ref)/name
                if path.exists():
                    if not path.is_file() or path.is_symlink():raise ValueError('review file is not a frozen regular file')
                    total+=path.stat().st_size
                    if total>self.config.max_context_bytes:raise ValueError('review context exceeds byte budget')
                    files[name]=path.read_text()
                else:files[name]=None
            context={'schema_version':1,'role':'reviewer','candidate_ref':candidate.candidate_ref,
                'candidate_hash':candidate.candidate_hash,'changed_files':files,'target_behavior':proposal.target_behavior,
                'gate':checked,'response_contract':{'schema_version':1,'result':'approved|changes_requested|blocked',
                    'findings':[{'finding_ref':'unique reference','severity':'info|warning|blocking',
                        'summary':'evidence-based observation','evidence_refs':[candidate.candidate_ref]}]}}
            raw=json.dumps(context,sort_keys=True,separators=(',',':'),ensure_ascii=False)
            if len(raw.encode())>self.config.max_context_bytes:raise ValueError('review context exceeds byte budget')
            envelope=SubmitEnvelope(schema_version=1,request_id=request_id,lifecycle_id=life.key.lifecycle_id,
                scope=ProviderScope(schema_version=1,kind='candidate',ref=proposal.proposal_id),input_json=raw,
                input_hash=hashlib.sha256(raw.encode()).hexdigest(),model_id=self.config.model_id,
                model_version=self.config.model_version,usage_ceiling=self.config.usage_ceiling,deadline=deadline)
            with self.repository.transaction() as tx:
                self._authorize(tx,proposal,token)
                ceiling=self.config.usage_ceiling
                LifecycleBudgetService.reserve_in(tx,request_id=request_id,lifecycle_id=life.key.lifecycle_id,
                    amounts={'diagnosis_calls':1,'tokens':ceiling.input_tokens+ceiling.output_tokens,'cost':ceiling.cost_microunits},
                    input_ref=envelope.input_hash,provider_enforces_maximum=True)
                CandidateReviewService.assign_in(tx,candidate.candidate_ref,review_ref=request_id,
                    coder_identity=self.coder_identity,reviewer_identity=self.reviewer_identity,
                    coder_context='coding:'+candidate.proposal_id,reviewer_context=request_id,
                    gate_manifest_ref=manifest_ref,token=token)
                tx.put('candidate_review_requests',request_id,{'envelope':envelope.model_dump(mode='json'),'observations':0},immutable=True)
        with self.repository.transaction() as tx:
            self._authorize(tx,proposal,token)
            row=tx.get('candidate_review_requests',request_id);count=row['body']['observations']
            if count>=self.config.max_observations:raise PermissionError('review observation budget exhausted')
            observation=request_id+':observation:'+str(count)
            LifecycleBudgetService.reserve_in(tx,request_id=observation,lifecycle_id=life.key.lifecycle_id,
                amounts={'observations':1},input_ref=envelope.input_hash)
            LifecycleBudgetService.settle_in(tx,request_id=observation)
            tx.put('candidate_review_requests',request_id,row['body']|{'observations':count+1},expected_revision=row['revision'])
        bridge=AgentBridge(self.repository,_ReviewProvider(self,proposal,request_id,token),provider_id=self.config.provider_id,scope=envelope.scope,
            commit_guard=lambda tx:self._authorize(tx,proposal,token))
        reply=await bridge.submit(envelope)
        if not reply.terminal:return {'status':'review_unknown' if reply.receipt is None else 'review_running','request_id':request_id}
        if reply.receipt.state!='succeeded':
            with self.repository.transaction() as tx:
                self._authorize(tx,proposal,token);LifecycleBudgetService.settle_in(tx,request_id=request_id)
            return {'status':'review_blocked','request_id':request_id}
        output=await bridge.collect(request_id);usage=output.receipt.usage
        with self.repository.transaction() as tx:
            self._authorize(tx,proposal,token)
            LifecycleBudgetService.settle_in(tx,request_id=request_id,
                actual_tokens=usage.input_tokens+usage.output_tokens if usage else None,actual_cost=usage.cost_microunits if usage else None)
            from ..environment.declarations import _unique_json_object
            body=json.loads(output.output_json,object_pairs_hook=_unique_json_object)
            if not isinstance(body,dict) or set(body)!={'schema_version','result','findings'} or body['schema_version']!=1:
                raise ValueError('review provider returned an invalid contract')
            findings=tuple(decode(ReviewFinding,dict(item,schema_version=1)) for item in body['findings'])
            allowed={candidate.candidate_ref,manifest_ref,*proposal.failure_evidence_refs,*proposal.acceptance_refs}
            if any(not set(item.evidence_refs)<=allowed for item in findings):raise ValueError('review cites inaccessible evidence')
            review=ReviewRecord(schema_version=1,review_ref=request_id,candidate_ref=candidate.candidate_ref,
                candidate_hash=candidate.candidate_hash,gate_manifest_ref=manifest_ref,gate_manifest_hash=checked['manifest_hash'],
                policy_hash=manifest['policy_hash'],coder_identity=self.coder_identity,reviewer_identity=self.reviewer_identity,
                reviewer_kind='model',producer_version=self.config.model_version,result=body['result'],findings=findings,
                evidence_refs=(candidate.candidate_ref,manifest_ref),observed_at=float(tx.now),expires_at=float(deadline),clock='wall',
                **{name:getattr(candidate,name) for name in TARGET_FIELDS})
            CandidateReviewService.takeover_in(tx,request_id,token=token)
            CandidateReviewService.record_in(tx,review,authenticated_identity=self.reviewer_identity,token=token)
            status='review_changes_requested' if review.result=='approved' and any(x.severity=='blocking' for x in findings) else 'review_'+review.result
            result={'status':status,'review_ref':request_id,'candidate_ref':candidate.candidate_ref,'manifest_ref':manifest_ref}
            tx.put('candidate_review_results',request_id,result,immutable=True)
            return result
