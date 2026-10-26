"""Finite persistent role execution through the application-owned AgentBridge."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json

from ..domain.models import canonical_hash
from ..persistence.repository import Conflict, StaleOwner, positive_duration
from ..services.agent_lifecycle import LifecycleService, decode
from ..services.agent_team import AgentTeamService, ModelTeamConfig, TeamProposal
from ..domain.agent_lifecycle import RepairPolicy
from ..services.lifecycle_budgets import LifecycleBudgetService
from ..services.lifecycle_safety import LifecycleSafetyService
from .agent_bridge import AgentBridge, ProviderError, RequestRecord, SubmitEnvelope
from .tasks import TaskBackend


class _FencedProvider:
    """Fence immediately before each external operation, including submit.

    AgentBridge owns durable remote receipts; those observational receipts do not
    authorize decisions. The result commit also checks all actual owner fences.
    """
    def __init__(self, service, provider, job, token, *, purpose='model'):
        self.service, self.provider, self.job, self.token = service, provider, job, token
        self.purpose = purpose

    def check(self, *, observe=False):
        with self.service.repository.transaction() as tx:
            return self.service.assert_model_network_in(tx, self.job, self.token,
                consume_observation=observe)

    async def capabilities(self):
        if self.purpose == 'cancel':
            with self.service.repository.transaction() as tx:
                self.service.assert_cancel_network_in(tx,self.job,self.token)
        else:
            self.check()
        return await self.provider.capabilities()

    async def submit(self, envelope):
        row, _, _ = self.check(observe=True)
        if envelope != decode(SubmitEnvelope, row['body']['envelope']):
            raise PermissionError('provider submit differs from frozen role envelope')
        return await self.provider.submit(envelope)

    async def lookup(self, request_id):
        self.check(observe=True)
        if request_id != self.job['job_id']:
            raise PermissionError('role lookup must retain original request identity')
        return await self.provider.lookup(request_id)

    async def status(self, handle):
        self.check(observe=True)
        return await self.provider.status(handle)

    async def cancel(self, handle):
        with self.service.repository.transaction() as tx:
            row = self.service.assert_cancel_network_in(tx,self.job,self.token)
            if handle != row['body']['handle']:
                raise PermissionError('cancel handle differs from original safety intent')
            tx.put('agent_team_cancellations',self.job['job_id'],row['body'] | {
                'state':'unknown','network_started':True},expected_revision=row['revision'])
        return await self.provider.cancel(handle)

    async def collect(self, handle):
        self.check()
        return await self.provider.collect(handle)


class AgentTeamJobHandler:
    def __init__(self, service: AgentTeamService, provider, *, lease_seconds, owner='agent-team',evolution=None):
        self.service, self.repository, self.provider = service, service.repository, provider
        self.lease_seconds, self.owner = positive_duration(lease_seconds), owner
        self.evolution=evolution

    def _bridge(self, job, token, *, purpose='model'):
        with self.repository.transaction() as tx:
            row, contract, _ = self.service.assert_model_network_in(tx, job, token)
            envelope = decode(SubmitEnvelope, row['body']['envelope'])
        return AgentBridge(self.repository, _FencedProvider(self.service,self.provider,job,token,purpose=purpose),
                           provider_id=contract.provider_id, scope=envelope.scope)

    def _observation(self, job, token):
        with self.repository.transaction() as tx:
            row, contract, _ = self.service.assert_model_network_in(tx,job,token)
            config = decode(ModelTeamConfig,row['body']['command']['config'])
            return row['body']['observations'] < config.max_observations and tx.now < contract.deadline

    async def observe(self, job, *, token):
        """Bounded original-ID observation under the actual caller job/owner."""
        bridge = self._bridge(job, token)
        with self.repository.transaction() as tx:
            row, _, _ = self.service.assert_task_in(tx, job, token)
            envelope = decode(SubmitEnvelope, row['body']['envelope'])
            old = tx.get('agent_provider_requests', job['job_id'])
            if old and decode(RequestRecord, old['body']).terminal:
                return decode(RequestRecord, old['body'])
        if not self._observation(job, token):
            return None
        # Existing unknown Bridge intents lookup the SAME ID, never resubmit.
        return await bridge.submit(envelope)

    async def cancel(self, job, *, token):
        """One durable, safety-budgeted cancellation of the original handle.

        Rejected and unknown attempts replay their original facts locally. No
        same-job retry creates another remote call or another safety allowance.
        """
        with self.repository.transaction() as tx:
            row, contract, record = self.service.assert_model_network_in(tx,job,token)
            task = row['body']
            old = tx.get('agent_provider_requests',job['job_id'])
            if not old:
                raise Conflict('provider intent missing; no remote cancel target')
            remote = decode(RequestRecord,old['body'])
            envelope = decode(SubmitEnvelope,task['envelope'])
            if remote.provider_id != contract.provider_id or remote.envelope != envelope:
                raise PermissionError('cancel provider request differs from frozen role')
            if remote.receipt is None:
                raise ProviderError('acceptance_unknown')
            if tx.get('agent_team_cancellations',job['job_id']):
                self.service.settle_cancel_in(tx,job['job_id'])
                return remote
            if remote.terminal or remote.cancel_accepted:
                return remote
            config = decode(ModelTeamConfig,task['command']['config'])
            snapshot = LifecycleBudgetService.snapshot_in(tx,contract.lifecycle_id)
            policy = decode(RepairPolicy,tx.get('repair_policies',record.policy_ref)['body'])
            for bound in snapshot.values():
                if (bound['cost_unit'] != config.cost_unit
                        or policy.safety_budget.cost_unit != config.cost_unit
                        or bound['safety_reserved']['stop_calls'] + bound['safety_spent']['stop_calls'] + 1
                           > policy.safety_budget.max_stop_calls
                        or bound['safety_reserved']['cost'] + bound['safety_spent']['cost']
                           + config.max_cancel_cost_microunits > policy.safety_budget.cost_limit):
                    raise PermissionError('cancellation exceeds policy safety budget or cost unit')
            reservation_ref = 'agent-cancel:' + canonical_hash({'request_id':job['job_id'],
                'handle':remote.receipt.handle,'provider_id':contract.provider_id,'input_hash':envelope.input_hash})
            metadata = {'request_id':job['job_id'],'provider_id':contract.provider_id,
                        'handle':remote.receipt.handle,'input_hash':envelope.input_hash}
            LifecycleBudgetService.reserve_in(tx,request_id=reservation_ref,lifecycle_id=contract.lifecycle_id,
                amounts={'stop_calls':1,'cost':config.max_cancel_cost_microunits},input_ref=envelope.input_hash,
                metadata=metadata,safety=True,operation_kind='stop')
            tx.put('agent_team_cancellations',job['job_id'],metadata | {
                'reservation_ref':reservation_ref,'state':'prepared','network_started':False},immutable=True)
        try:
            result = await self._bridge(job,token,purpose='cancel').cancel(job['job_id'])
        except ProviderError:
            with self.repository.transaction() as tx:
                self.service.assert_task_in(tx,job,token)
                self.service.settle_cancel_in(tx,job['job_id'])
            raise
        with self.repository.transaction() as tx:
            self.service.assert_task_in(tx,job,token)
            row = tx.get('agent_team_cancellations',job['job_id'])
            tx.put('agent_team_cancellations',job['job_id'],row['body'] | {
                'state':'accepted' if result.cancel_accepted else 'rejected'},expected_revision=row['revision'])
            self.service.settle_cancel_in(tx,job['job_id'])
        return result

    def _blocked(self, job, token, reason, *, waiting=False):
        with self.repository.transaction() as tx:
            row, contract, _ = self.service.assert_task_in(tx, job, token)
            result = {'execution_status':'blocked','task_ref':job['job_id'],
                      'error':{'code':'MODEL_DIAGNOSIS_BLOCKED','reason':reason}}
            # Usage receipts are independent of proposal validity. Only a
            # verified terminal receipt can lower the unknown maximum charge.
            self.service.settle_observed_in(tx,job['job_id'])
            tx.put('agent_team_tasks',job['job_id'],row['body'] | {
                'status':'waiting_provider' if waiting else 'blocked','reason':reason,
                'result':result},expected_revision=row['revision'])
            tx.put('lifecycle_blockers',contract.lifecycle_id,{
                'status':'waiting_dependency' if waiting else 'blocked','reason':reason,
                'task_ref':job['job_id'],'request_id':job['job_id'],
                'lifecycle_id':contract.lifecycle_id})
            if waiting:
                self.service.reconciliation_intent_in(tx,job['job_id'],contract.lifecycle_id)
            return result

    def _commit(self, job, token, output):
        with self.repository.transaction() as tx:
            row, contract, _ = self.service.assert_task_in(tx, job, token)
            task = row['body']
            if task.get('result') and task['status'] == 'completed':
                return task['result']
            self.service.assert_model_network_in(tx,job,token)
            config = decode(ModelTeamConfig, task['command']['config'])
            if len(output.output_json.encode('utf-8')) > config.max_output_bytes:
                raise ValueError('model output exceeds explicit byte limit')
            # Strict JSON also rejects extra fields, fabricated guards, scripts,
            # results, budgets, delegation requests and candidate gate approvals.
            proposal = TeamProposal.model_validate_json(output.output_json)
            self.service.validate_proposal_in(tx, task, proposal)
            proposal_ref = 'team-proposal:' + canonical_hash(proposal.model_dump(mode='json'))
            tx.put('agent_team_proposals',proposal_ref,{
                'task_ref':job['job_id'],'input_hash':task['envelope']['input_hash'],
                'proposal':proposal.model_dump(mode='json')},immutable=True)
            route = {'registered_action':'known_action','candidate_needed':'needs_code',
                     'needs_input':'needs_input','no_safe_route':'no_safe_route'}[proposal.route]
            if proposal.route == 'candidate_needed':
                candidate_ref = 'candidate-needed:' + canonical_hash({
                    'task_ref':job['job_id'],'proposal_ref':proposal_ref})
                intent = {'schema_version':1, 'candidate_ref':candidate_ref,
                    'lifecycle_id':contract.lifecycle_id,'lineage_budget_ref':contract.lineage_budget_ref,
                    'source_receipt':contract.source_receipt,'scope_ref':contract.scope_ref,
                    'run_ref':contract.run_ref,'node_id':contract.node_id,
                    'graph_ref':contract.graph_ref,'plan_ref':contract.plan_ref,
                    'contract_ref':contract.contract_ref,'proposal_ref':proposal_ref,
                    'input_hash':task['envelope']['input_hash'],'task_ref':job['job_id'],
                    'status':'waiting_dependency','reason':'candidate_tools_46_not_connected'}
                tx.put('candidate_needed_intents',candidate_ref,intent,immutable=True)
                tx.put('outbox',candidate_ref,{'kind':'candidate_needed',**intent},immutable=True)
            # The source failure remains authoritative. Grant facts have already
            # been verified against the exact sanitized bytes sent in this task.
            decision = LifecycleService.record_decision_in(tx, contract.lifecycle_id,
                request_id='model-decision:' + job['job_id'],
                expected_revision=contract.expected_revision, route=route,
                evidence_refs=(contract.source_receipt,), token=token,
                action_ref=proposal.action_ref, proposal_ref=proposal_ref)
            usage = output.receipt.usage
            LifecycleBudgetService.settle_in(tx,request_id=job['job_id'],
                actual_tokens=usage.input_tokens + usage.output_tokens if usage else None,
                actual_cost=usage.cost_microunits if usage else None)
            result = {'execution_status':'succeeded','task_ref':job['job_id'],
                      'proposal_ref':proposal_ref,'decision_ref':decision['decision_ref']}
            tx.put('agent_team_tasks',job['job_id'],task | {'status':'completed',
                'proposal_ref':proposal_ref,'decision_ref':decision['decision_ref'],
                'result':result},expected_revision=row['revision'])
            return result

    async def __call__(self, job):
        with self.repository.transaction() as tx:
            job = TaskBackend.assert_claim_in(tx,job)
            task = tx.get('agent_team_tasks',job['job_id'])['body']
            if task.get('result'):
                return task['result']
            token = tx.acquire_thread(job['thread_id'],self.owner,
                min(self.lease_seconds,job['deadline']-tx.now))
        owns_work = False
        try:
            LifecycleService(self.repository).claim(task['contract']['lifecycle_id'],token=token)
            with self.repository.transaction() as tx:
                TaskBackend.assert_claim_in(tx,job)
                LifecycleSafetyService.acquire_work_in(tx,job['scope_ref'],token,purpose='diagnosis')
                owns_work = True
                row, contract, record = self.service.assert_task_in(tx,job,token)
                proof = LifecycleSafetyService.produce_disposition_in(tx,job['scope_ref'],token,
                    ttl=min(self.lease_seconds,job['deadline']-tx.now))
                LifecycleSafetyService.verify_disposition_in(tx,proof,job['scope_ref'],token)
                tx.put('lifecycle_dispositions',contract.lifecycle_id,{'receipt_ref':proof})
                LifecycleService.bind_evidence_in(tx,record,proof,kind='resource_disposition')
            if contract.role in {'coder','reviewer'}:
                if self.evolution is None:
                    return self._blocked(job,token,'candidate_scope_backend_missing: '+contract.role)
                with self.repository.transaction() as tx:
                    row,contract,record=self.service.assert_task_in(tx,job,token)
                    result=self.evolution.delegate_role_in(tx,contract,record,token=token)
                    status='completed' if result['status']=='candidate_queued' else 'blocked'
                    outcome={'execution_status':'succeeded' if status=='completed' else 'blocked',
                        'task_ref':job['job_id'],'candidate_result':result}
                    LifecycleBudgetService.settle_in(tx,request_id=job['job_id'])
                    tx.put('agent_team_tasks',job['job_id'],row['body']|{'status':status,'result':outcome,
                        'reason':result.get('reason')},expected_revision=row['revision'])
                    return outcome
            while True:
                remote = await self.observe(job,token=token)
                if remote is None:
                    break
                if remote.terminal:
                    if remote.receipt.state != 'succeeded':
                        return self._blocked(job,token,'provider_terminal_' + remote.receipt.state)
                    output = await self._bridge(job,token).collect(job['job_id'])
                    return self._commit(job,token,output)
                config = decode(ModelTeamConfig,task['command']['config'])
                await asyncio.sleep(config.poll_seconds)
            # Do not turn cancellation acknowledgement into terminal evidence.
            with self.repository.transaction() as tx:
                self.service.assert_task_in(tx,job,token)
                old = tx.get('agent_provider_requests',job['job_id'])
                known = old and decode(RequestRecord,old['body']).receipt is not None
            if known:
                with suppress(ProviderError):
                    await self.cancel(job,token=token)
            return self._blocked(job,token,'provider_observation_budget_exhausted; original_request_only',waiting=True)
        except (Conflict, PermissionError, ValueError) as exc:
            if isinstance(exc,StaleOwner):
                raise
            # Stable codes only; validation exceptions can echo hostile output.
            code = exc.code if isinstance(exc,ProviderError) else type(exc).__name__
            with self.repository.transaction() as tx:
                self.service.assert_task_in(tx,job,token)
                old = tx.get('agent_provider_requests',job['job_id'])
                unknown = old and not decode(RequestRecord,old['body']).terminal
            return self._blocked(job,token,'provider_or_proposal_rejected:' + code,waiting=bool(unknown))
        finally:
            with self.repository.transaction() as tx:
                if owns_work:
                    with suppress(StaleOwner):
                        LifecycleSafetyService.release_work_in(tx,job['scope_ref'],token)
                with suppress(StaleOwner):
                    tx.release_thread(token)
