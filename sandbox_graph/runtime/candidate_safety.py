"""Persistent bounded cancellation of original candidate providers and isolations."""
import asyncio
from contextlib import suppress
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict,StaleOwner
from ..services.agent_lifecycle import LifecycleService,decode
from ..services.lifecycle_budgets import LifecycleBudgetService
from .agent_bridge import AgentBridge,RequestRecord,ProviderError
from .tasks import JobSpec,TaskBackend,business_job_kind


class _SafetyProvider:
    def __init__(self,handler,job,token,provider,request_id):
        self.handler,self.job,self.token,self.provider,self.request_id=handler,job,token,provider,request_id
    def check(self,kind=None):
        with self.handler.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx,self.job);tx.assert_fence(self.token)
            tx.renew_thread(self.token,min(self.handler.lease_seconds,self.job['deadline']-tx.now))
            life=self.job['payload']['lifecycle_id'];LifecycleService.assert_owner_in(tx,life,self.token)
            if not CandidateSafety.valid_in(tx,self.job):raise PermissionError('original candidate safety binding changed')
            if kind:
                row=tx.get('candidate_safety_calls',self.job['job_id']);sequence=(row['body']['sequence'] if row else 0)+1
                ref=self.job['job_id']+':'+str(sequence)
                LifecycleBudgetService.reserve_in(tx,request_id=ref,lifecycle_id=life,safety=True,operation_kind=kind,
                    amounts={('stop_calls' if kind=='stop' else 'query_calls'):1},input_ref=self.request_id)
                LifecycleBudgetService.settle_in(tx,request_id=ref)
                tx.put('candidate_safety_calls',self.job['job_id'],{'sequence':sequence})
    async def capabilities(self):self.check();return await asyncio.wait_for(self.provider.capabilities(),min(10,self.handler.lease_seconds/3))
    async def lookup(self,request_id):
        if request_id!=self.request_id:raise PermissionError('original provider request required')
        self.check('query');return await asyncio.wait_for(self.provider.lookup(request_id),min(10,self.handler.lease_seconds/3))
    async def status(self,handle):self.check('query');return await asyncio.wait_for(self.provider.status(handle),min(10,self.handler.lease_seconds/3))
    async def cancel(self,handle):self.check('stop');return await asyncio.wait_for(self.provider.cancel(handle),min(10,self.handler.lease_seconds/3))
    async def submit(self,*args):raise PermissionError('safety cannot submit replacement work')
    async def collect(self,*args):raise PermissionError('safety cannot collect coding instructions')


class CandidateSafety:
    MAX_TICKS=16
    def __init__(self,service,providers,*,isolations=None,owner='candidate-safety',lease_seconds=30):
        self.service=service;self.repository=service.repository;self.providers=dict(providers)
        self.isolations=dict(isolations or {});self.owner=owner;self.lease_seconds=lease_seconds

    @staticmethod
    def valid_in(tx,job):
        if business_job_kind(job.get('job_kind',''))!='candidate_cleanup':return False
        row=tx.get('candidate_safety_jobs',job.get('job_id',''))
        return bool(row and all(row['body'].get(k)==job.get(k) for k in ('run_id','scope_ref','thread_id','deadline','payload')))

    @classmethod
    def enqueue_in(cls,tx,proposal_id,*,sequence=0):
        proposal=tx.get('change_proposals',proposal_id)['body']['proposal'];life=proposal['source_key']['lifecycle_id']
        progress=tx.get('candidate_progress',proposal_id)
        if not progress or progress['body']['status'] not in {'cancelled','cancelling'}:raise PermissionError('original candidate cancel required')
        budget=LifecycleBudgetService.snapshot_in(tx,life)
        deadline=min(row['safety_deadline'] for row in budget.values())
        identity='candidate-cleanup:'+canonical_hash({'proposal_id':proposal_id,'sequence':sequence})
        if tx.get('jobs',identity):return identity
        due=tx.now+(min(10,2**min(sequence,4)) if sequence else 0)
        if sequence>=cls.MAX_TICKS or due>=deadline:raise Conflict('candidate safety observation budget exhausted')
        spec=JobSpec(job_id=identity,job_kind='candidate_cleanup',run_id=proposal['source_key']['run_ref'],
            scope_ref=proposal['source_key']['scope_ref'],thread_id=identity,activation_id=identity,dedupe_key=identity,
            due_at=due,deadline=deadline,payload={'proposal_id':proposal_id,'lifecycle_id':life,'sequence':sequence})
        tx.put('candidate_safety_jobs',identity,spec.model_dump(mode='json'),immutable=True)
        TaskBackend.enqueue_in(tx,spec);return identity

    async def __call__(self,job):
        token=None
        try:
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx,job)
                if not self.valid_in(tx,job):raise PermissionError('unbound candidate cleanup')
                token=tx.acquire_thread(job['thread_id'],self.owner,min(self.lease_seconds,job['deadline']-tx.now))
            LifecycleService(self.repository).claim(job['payload']['lifecycle_id'],token=token)
            with self.repository.transaction() as tx:
                requests=[row for row in tx.scan('agent_provider_requests') if row['body']['envelope']['lifecycle_id']==job['payload']['lifecycle_id']
                    and row['body']['envelope']['scope']=={'schema_version':1,'kind':'candidate','ref':job['payload']['proposal_id']}]
                isolations=[row for row in tx.scan('candidate_isolations') if row['body'].get('proposal_id')==job['payload']['proposal_id']]
                # A pre-bridge crash has a budget reservation but no submission
                # intent. The new lifecycle owner fences the old caller before
                # proving this negative in the same transaction.
                for kind in ('coding_requests','candidate_review_requests'):
                    for pending in tx.scan(kind):
                        envelope=pending['body'].get('envelope',{})
                        if (envelope.get('lifecycle_id')!=job['payload']['lifecycle_id'] or envelope.get('scope')!=
                                {'schema_version':1,'kind':'candidate','ref':job['payload']['proposal_id']}):continue
                        if tx.get('agent_provider_requests',pending['key']):continue
                        LifecycleService.assert_owner_in(tx,job['payload']['lifecycle_id'],token)
                        proof={'request_id':pending['key'],'input_hash':envelope['input_hash'],
                            'lifecycle_id':job['payload']['lifecycle_id'],'proposal_id':job['payload']['proposal_id'],
                            'producer':'candidate_safety.v1','state':'not_submitted'}
                        tx.put('candidate_unsubmitted_proofs',pending['key'],proof,immutable=True)
                        LifecycleBudgetService.settle_in(tx,request_id=pending['key'],proven_not_submitted=True)

            unresolved=[]
            for row in requests:
                record=decode(RequestRecord,row['body'])
                if not record.terminal:
                    role='review' if row['key'].startswith('candidate-review:') else 'coding'
                    provider=self.providers.get((role,record.provider_id),self.providers.get(record.provider_id))
                    if provider is None:unresolved.append(row['key']);continue
                    wrapper=_SafetyProvider(self,job,token,provider,row['key'])
                    bridge=AgentBridge(self.repository,wrapper,provider_id=record.provider_id,scope=record.envelope.scope,
                        commit_guard=lambda tx:LifecycleService.assert_owner_in(tx,job['payload']['lifecycle_id'],token))
                    try:
                        if record.receipt is None:record=await bridge.lookup(row['key'])
                        if record.receipt is not None and not record.terminal:
                            await bridge.cancel(row['key']);record=await bridge.status(row['key'])
                    except (ProviderError,Conflict,PermissionError,TimeoutError):pass
                if not record.terminal:unresolved.append(row['key']);continue
                with self.repository.transaction() as tx:
                    LifecycleService.assert_owner_in(tx,job['payload']['lifecycle_id'],token)
                    usage=record.receipt.usage
                    LifecycleBudgetService.settle_in(tx,request_id=row['key'],
                        actual_tokens=usage.input_tokens+usage.output_tokens if usage else None,
                        actual_cost=usage.cost_microunits if usage else None)
            for row in isolations:
                with self.repository.transaction() as tx:
                    proof=tx.get('candidate_disposition_proofs',row['key'])
                    if (proof and proof['body'].get('isolation_ref')==row['key']
                            and proof['body'].get('isolation_hash')==canonical_hash(row['body'])
                            and proof['body'].get('children_stopped') is True):continue
                backend=self.isolations.get(row['body']['backend_id'])
                if backend is None:unresolved.append(row['key']);continue
                guard=_SafetyProvider(self,job,token,backend,row['body']['request_id'])
                guard.check('stop')
                try:result=await asyncio.wait_for(backend.cancel(row['body']['request_id'],timeout_s=5),6)
                except (TimeoutError,OSError):unresolved.append(row['key']);continue
                guard.check()
                if result.get('cleanup')!='complete':unresolved.append(row['key']);continue
                with self.repository.transaction() as tx:
                    TaskBackend.assert_claim_in(tx,job);tx.assert_fence(token)
                    current=tx.get('candidate_isolations',row['key'])
                    if current!=row:raise Conflict('candidate isolation changed during cleanup')
                    tx.put('candidate_disposition_proofs',row['key'],{'isolation_ref':row['key'],
                        'isolation_hash':canonical_hash(row['body']),'children_stopped':True,'observation':result,
                        'producer':'candidate_safety.v1','job_ref':job['job_id']},immutable=True)
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx,job);LifecycleService.assert_owner_in(tx,job['payload']['lifecycle_id'],token)
                progress=tx.get('candidate_progress',job['payload']['proposal_id'])
                active=progress['body'].get('active_operation')
                mutation=tx.get('candidate_operations',active) if active else None
            if active:
                if not mutation:unresolved.append(active)
                else:
                    disposition=self.service.workspace.observe_edits(job['payload']['proposal_id'],mutation['body']['command']['edits'])
                    if disposition['status']!='closed':unresolved.append(active)
                    else:
                        with self.repository.transaction() as tx:
                            TaskBackend.assert_claim_in(tx,job);LifecycleService.assert_owner_in(tx,job['payload']['lifecycle_id'],token)
                            if tx.get('candidate_operations',active)!=mutation:raise Conflict('original edit changed during cancellation')
                            tx.put('candidate_operations',active,mutation['body']|{'status':'cancelled','disposition':disposition})
                            LifecycleBudgetService.settle_in(tx,request_id=mutation['body']['reservation'])
                            progress=tx.get('candidate_progress',job['payload']['proposal_id'])
                            tx.put('candidate_progress',progress['key'],progress['body']|{'active_operation':None})
            result={'execution_status':'blocked' if unresolved else 'succeeded','unresolved':unresolved}
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx,job);tx.assert_fence(token)
                progress=tx.get('candidate_progress',job['payload']['proposal_id'])
                tx.put('candidate_safety_results',job['job_id'],result,immutable=True)
                if not unresolved:tx.put('candidate_progress',progress['key'],progress['body']|{'cleanup_status':'complete'})
                else:self.enqueue_in(tx,progress['key'],sequence=job['payload']['sequence']+1)
            return result
        except (Conflict,PermissionError) as exc:
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx,job)
                try:self.enqueue_in(tx,job['payload']['proposal_id'],sequence=job['payload']['sequence']+1)
                except (Conflict,PermissionError):pass
            return {'execution_status':'blocked','reason':str(exc),'proposal_id':job['payload']['proposal_id']}
        finally:
            if token is not None:
                with self.repository.transaction() as tx:
                    with suppress(StaleOwner):tx.release_thread(token)
