"""Persistent graph-to-candidate dispatch using the common TaskBackend."""
from __future__ import annotations
import asyncio
from contextlib import suppress

from ..domain.models import canonical_hash
from ..persistence.repository import Conflict,StaleOwner,positive_duration
from ..services.agent_lifecycle import LifecycleService,decode
from ..services.lifecycle_budgets import LifecycleBudgetService
from ..domain.evolution import ChangeProposal,TargetBinding
from .tasks import JobSpec,TaskBackend


class EvolutionCoordinator:
    def __init__(self,service,worker,*,bindings,lease_seconds,owner='candidate-worker',isolations=None):
        self.service,self.repository,self.worker=service,service.repository,worker
        self.lease_seconds=positive_duration(lease_seconds);self.owner=owner;self.bindings={};self.revision_targets={}
        from .candidate_safety import CandidateSafety
        providers={('coding',worker.config.provider_id):worker.provider} if worker else {}
        if worker and worker.reviewer is not None:
            providers[('review',worker.reviewer.config.provider_id)]=worker.reviewer.provider
        self.safety=CandidateSafety(service,providers,isolations=isolations,owner=owner+'-safety',lease_seconds=lease_seconds)
        for binding in bindings:
            proposal=binding['proposal'];target=binding['target']
            if not isinstance(proposal,ChangeProposal) or not isinstance(target,TargetBinding):
                raise ValueError('typed operator proposal and frozen target binding required')
            key=(proposal.source_key.run_ref,proposal.source_key.scope_ref,proposal.source_key.node_id)
            if key in self.bindings:raise ValueError('duplicate candidate scope binding')
            self.bindings[key]=(proposal,target)
            self.revision_targets[proposal.proposal_id]=dict(binding.get('revision_targets',{}))

    def _register_in(self,tx,proposal,target,token):
        self.service.register_in(tx,proposal,target=target,token=token)
        for revision,binding in self.revision_targets[proposal.proposal_id].items():
            self.service.bind_revision_target_in(tx,proposal.proposal_id,revision,binding,token=token)

    def supports(self,record):
        value=self.bindings.get((record.key.run_ref,record.key.scope_ref,record.key.node_id))
        return bool(value and value[0].source_key==record.key and self.worker is not None)

    def delegate_role_in(self,tx,contract,record,*,token):
        LifecycleService.assert_owner_in(tx,record.key.lifecycle_id,token)
        if contract.role not in {'coder','reviewer'} or contract.lifecycle_id!=record.key.lifecycle_id:
            raise PermissionError('candidate role scope differs')
        from ..services.agent_team import AgentTeamService
        AgentTeamService._candidate_in(tx,record,contract.candidate_ref)
        if not self.supports(record):return {'status':'waiting_dependency','reason':'candidate_scope_binding_missing'}
        proposal,target=self.bindings[(record.key.run_ref,record.key.scope_ref,record.key.node_id)]
        if contract.role=='reviewer':
            current=tx.get('candidate_workers',proposal.proposal_id)
            if self.worker.reviewer is None or not current or current['body'].get('status')!='awaiting_independent_review':
                return {'status':'waiting_dependency','reason':'frozen_candidate_and_independent_reviewer_required'}
        previous=tx.get('candidate_role_dispatches',contract.task_ref)
        if previous:return previous['body']['result']
        self._register_in(tx,proposal,target,token)
        sequence=1+max((row['body'].get('payload',{}).get('sequence',0) for row in tx.scan('jobs')
            if row['body'].get('payload',{}).get('proposal_id')==proposal.proposal_id
            and row['body'].get('job_kind')=='candidate'),default=0)
        ref=self._enqueue_in(tx,proposal,sequence=sequence)
        result={'status':'candidate_queued','proposal_id':proposal.proposal_id,'job_ref':ref}
        tx.put('candidate_role_dispatches',contract.task_ref,{'role':contract.role,'result':result},immutable=True)
        return result

    def request_in(self,tx,decision,record,*,token):
        LifecycleService.assert_owner_in(tx,record.key.lifecycle_id,token)
        if decision.get('route')!='needs_code':raise ValueError('candidate dispatch requires needs_code decision')
        binding=self.bindings.get((record.key.run_ref,record.key.scope_ref,record.key.node_id))
        if not binding or self.worker is None:
            return {'status':'waiting_dependency','reason':'candidate_scope_binding_missing'}
        proposal,target=binding
        if proposal.source_key!=record.key:raise PermissionError('candidate binding belongs to another lifecycle')
        self._register_in(tx,proposal,target,token)
        old=tx.get('candidate_dispatches',decision['decision_ref'])
        if old:
            if old['body']['proposal_id']!=proposal.proposal_id:raise Conflict('candidate decision binding changed')
            return old['body']['result']
        job=self._enqueue_in(tx,proposal,sequence=1)
        result={'status':'candidate_queued','job_ref':job,'proposal_id':proposal.proposal_id}
        tx.put('candidate_dispatches',decision['decision_ref'],{'proposal_id':proposal.proposal_id,
            'lifecycle_id':record.key.lifecycle_id,'result':result},immutable=True)
        return result

    @staticmethod
    def _enqueue_in(tx,proposal,*,sequence):
        identity='candidate-job:'+canonical_hash({'proposal':proposal.proposal_id,'sequence':sequence})
        if tx.get('jobs',identity):return identity
        life=LifecycleService.load_in(tx,proposal.source_key.lifecycle_id)
        from ..services.lifecycle_stop import LifecycleStopService
        LifecycleStopService.assert_admission_in(tx,life)
        LifecycleBudgetService.reserve_in(tx,request_id=identity,lifecycle_id=life.key.lifecycle_id,
            amounts={'child_jobs':1},input_ref=proposal.proposal_id,metadata={'sequence':sequence})
        run=tx.get('runs',life.key.run_ref)['body']
        TaskBackend.enqueue_in(tx,JobSpec(job_id=identity,job_kind='candidate',scope_ref=life.key.scope_ref,
            thread_id=identity,activation_id=identity,dedupe_key=identity,due_at=tx.now,deadline=life.deadline,
            run_id=life.key.run_ref,dispatch_generation=run['dispatch_generation'],
            payload={'proposal_id':proposal.proposal_id,'lifecycle_id':life.key.lifecycle_id,'sequence':sequence}))
        return identity

    async def __call__(self,job):
        with self.repository.transaction() as tx:
            TaskBackend.assert_claim_in(tx,job)
            replay=tx.get('candidate_job_results',job['job_id'])
            if replay:return replay['body']
            inputs,proposal,target=self.service._inputs_in(tx,job['payload']['proposal_id'])
            if (job['run_id'],job['scope_ref'],job['payload']['lifecycle_id'])!=(
                    proposal.source_key.run_ref,proposal.source_key.scope_ref,proposal.source_key.lifecycle_id):
                raise PermissionError('candidate job scope differs from frozen proposal')
            binding=self.bindings.get((job['run_id'],job['scope_ref'],proposal.source_key.node_id))
            if binding!=(proposal,target):raise PermissionError('operator candidate binding changed')
            token=tx.acquire_thread(job['thread_id'],self.owner,min(self.lease_seconds,job['deadline']-tx.now))
        pulse=None;execution=None
        try:
            LifecycleService(self.repository).claim(proposal.source_key.lifecycle_id,token=token)
            self.service.open(proposal,target=target,token=token)
            with self.repository.transaction() as tx:self._register_in(tx,proposal,target,token)
            async def heartbeat():
                while True:
                    await asyncio.sleep(self.lease_seconds/3)
                    with self.repository.transaction() as tx:
                        TaskBackend.assert_claim_in(tx,job)
                        tx.renew_thread(token,min(self.lease_seconds,job['deadline']-tx.now))
            pulse=asyncio.create_task(heartbeat())
            execution=asyncio.create_task(self.worker.tick(proposal.proposal_id,token=token))
            done,_=await asyncio.wait({execution,pulse},return_when=asyncio.FIRST_COMPLETED)
            if pulse in done:pulse.result()
            outcome=execution.result()
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx,job)
                LifecycleService.assert_owner_in(tx,proposal.source_key.lifecycle_id,token)
                result={'execution_status':'succeeded','candidate_status':outcome['status'],'candidate_result':outcome}
                if outcome['status'] in {'tests_failed','provider_unknown','provider_running','review_unknown','review_running','review_changes_requested'}:
                    try:result['next_job_ref']=self._enqueue_in(tx,proposal,sequence=job['payload']['sequence']+1)
                    except (Conflict,PermissionError) as exc:
                        result.update(execution_status='blocked',reason=str(exc))
                elif outcome['status'] in {'gate_blocked','provider_failed','provider_cancelled','review_blocked','invalid_response','no_progress'}:
                    result['execution_status']='blocked'
                LifecycleBudgetService.settle_in(tx,request_id=job['job_id'])
                tx.put('candidate_job_results',job['job_id'],result,immutable=True)
                return result
        finally:
            for task in (execution,pulse):
                if task is not None:
                    if not task.done():task.cancel()
                    with suppress(asyncio.CancelledError):await task
            with self.repository.transaction() as tx:
                with suppress(StaleOwner):tx.release_thread(token)
