"""Trusted one-layer team contracts, atomic lineage reservations and proposals.

Only a claimed lifecycle supervisor may enqueue a role. Provider output is data;
no session, filesystem, executable tool or candidate gate is exposed by this API.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ..catalog.repair_actions import standard_repair_catalog
from ..domain.agent_lifecycle import ContextPacket, LifecycleModel, RepairPolicy, Ref
from ..domain.models import canonical_hash
from ..persistence.repository import Conflict
from ..runtime.agent_bridge import SubmitEnvelope, UsageCeiling, RequestRecord
from ..runtime.tasks import JobSpec, TaskBackend
from .agent_lifecycle import LifecycleService, decode
from .lifecycle_budgets import LifecycleBudgetService
from .lifecycle_safety import LifecycleSafetyService

Positive = Annotated[int, Field(strict=True, gt=0, le=10**12)]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8192)]
Role = Literal['investigator', 'coder', 'reviewer']
ROLE_PACKET = {'investigator':'diagnosis', 'coder':'candidate_build', 'reviewer':'candidate_verify'}
ACTIVE = frozenset({'queued', 'running', 'waiting_provider'})


class ProviderHandoffPending(PermissionError):
    """原角色尚未完成 TaskBackend 收尾，安全所有权暂不可接管。"""


class ModelTeamConfig(LifecycleModel):
    provider_id: Ref
    model_id: Ref
    model_version: Ref
    max_input_tokens: Positive
    max_output_tokens: Positive
    max_cost_microunits: Positive
    max_cancel_cost_microunits: Positive
    cost_unit: Literal['microunits']
    deadline: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
    max_observations: Positive
    poll_seconds: Annotated[float, Field(strict=True, gt=0, le=300, allow_inf_nan=False)]
    max_input_bytes: Annotated[int, Field(strict=True, gt=0, le=1048576)]
    max_output_bytes: Annotated[int, Field(strict=True, gt=0, le=1048576)]
    evidence_bytes_per_grant: Annotated[int, Field(strict=True, gt=0, le=1048576)]


class RoleContract(LifecycleModel):
    task_ref: Ref
    parent_job_ref: Ref
    parent_role: Literal['supervisor']
    role: Role
    depth: Positive
    max_depth: Positive
    max_concurrency: Positive
    lifecycle_id: Ref
    lineage_budget_ref: Ref
    node_supervisor_id: Ref
    source_receipt: Ref
    run_ref: Ref
    scope_ref: Ref
    node_id: Ref
    graph_ref: Ref
    plan_ref: Ref
    contract_ref: Ref
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    candidate_ref: Ref | None
    provider_id: Ref
    model_id: Ref
    model_version: Ref
    deadline: Annotated[float, Field(strict=True, gt=0)]


class ProposalFact(LifecycleModel):
    # Nested facts use the enclosing proposal version; do not invent provenance.
    schema_version: Literal[1] = 1
    text: Text
    evidence_ref: Ref


class TeamProposal(LifecycleModel):
    task_ref: Ref
    role: Role
    input_hash: Annotated[str, StringConstraints(strict=True, pattern=r'^[a-f0-9]{64}$')]
    lifecycle_id: Ref
    source_receipt: Ref
    scope_ref: Ref
    graph_ref: Ref
    plan_ref: Ref
    contract_ref: Ref
    candidate_ref: Ref | None
    route: Literal['registered_action', 'candidate_needed', 'needs_input', 'no_safe_route']
    action_ref: Ref | None
    facts: Annotated[tuple[ProposalFact, ...], Field(min_length=1, max_length=64)]
    hypotheses: Annotated[tuple[Text, ...], Field(max_length=32)]

    @model_validator(mode='after')
    def action_binding(self):
        if (self.route == 'registered_action') != (self.action_ref is not None):
            raise ValueError('only registered_action carries an action reference')
        return self


class AgentTeamService:
    def __init__(self, repository, context_service, *, config: ModelTeamConfig, repair_catalog=None):
        if not isinstance(config, ModelTeamConfig):
            raise ValueError('explicit ModelTeamConfig required')
        self.repository, self.context = repository, context_service
        self.repair_catalog = repair_catalog or standard_repair_catalog()
        self.config = ModelTeamConfig.model_validate_json(config.model_dump_json())

    @staticmethod
    def _parent_in(tx, parent, token, role='investigator'):
        parent = TaskBackend.assert_claim_in(tx, parent)
        if parent['job_kind'] != 'lifecycle' or parent['payload'].get('stage') != 'diagnose':
            raise PermissionError('only lifecycle supervisor may dispatch; recursive roles prohibited')
        if parent['thread_id'] != token.thread_id:
            raise PermissionError('role parent must own the actual thread')
        lifecycle_id = parent['payload']['lifecycle_id']
        LifecycleService.assert_owner_in(tx, lifecycle_id, token)
        record = LifecycleService.load_in(tx, lifecycle_id)
        from .lifecycle_rollout import LifecycleRolloutService
        LifecycleRolloutService.assert_enabled_in(tx,record,'model' if role=='investigator' else 'candidate')
        if (parent['run_id'], parent['scope_ref']) != (record.key.run_ref, record.key.scope_ref):
            raise PermissionError('role parent differs from lifecycle scope')
        LifecycleSafetyService.assert_work_in(tx, record.key.scope_ref, token)
        allowed={'investigator':{'diagnosing'},'coder':{'diagnosing','candidate_building'},
            'reviewer':{'diagnosing','candidate_verifying','promotion_pending'}}
        if record.phase not in allowed.get(role,set()) or record.is_terminal:
            raise Conflict('role dispatch lifecycle phase differs')
        return record, parent

    def _limits_in(self, tx, record, role):
        policy = decode(RepairPolicy, tx.get('repair_policies', record.policy_ref)['body'])
        snapshot = LifecycleBudgetService.snapshot_in(tx, record.key.lifecycle_id)
        if snapshot['lifecycle']['lineage_budget_ref'] != record.key.lineage_budget_ref:
            raise PermissionError('role lineage budget mismatch')
        cfg = self.config
        deadline = min(cfg.deadline, record.deadline, policy.deadline,
                       snapshot['lifecycle']['deadline'], snapshot['lineage']['deadline'])
        if tx.now >= deadline:
            raise Conflict('model diagnosis deadline elapsed')
        if (cfg.max_observations > policy.max_observations
                or cfg.max_input_tokens + cfg.max_output_tokens > policy.token_limit
                or cfg.cost_unit != snapshot['lifecycle']['cost_unit']
                or cfg.cost_unit != snapshot['lineage']['cost_unit']
                or cfg.cost_unit != policy.safety_budget.cost_unit):
            raise PermissionError('model configuration exceeds registered policy or cost unit')
        additions = {'child_jobs':1}
        if role == 'investigator':
            additions.update(diagnosis_calls=1,tokens=cfg.max_input_tokens + cfg.max_output_tokens,
                             observations=cfg.max_observations)
        policy_caps = {'child_jobs':policy.child_job_limit,'diagnosis_calls':policy.max_diagnosis_calls,
                       'tokens':policy.token_limit,'observations':policy.max_observations}
        for bound in snapshot.values():
            for counter, amount in additions.items():
                if bound['reserved'][counter] + bound['spent'][counter] + amount > policy_caps[counter]:
                    raise Conflict('registered policy budget exhausted: ' + counter)
        active = [r['body'] for r in tx.scan('agent_team_tasks')
                  if r['body']['contract']['lineage_budget_ref'] == record.key.lineage_budget_ref
                  and r['body']['status'] in ACTIVE]
        concurrency = min([policy.max_concurrency] + [b['contract']['max_concurrency'] for b in active])
        if len(active) >= concurrency:
            raise Conflict('lineage team concurrency exhausted')
        if 1 > policy.max_depth:
            raise Conflict('team depth exhausted')
        return policy, deadline

    @staticmethod
    def _candidate_in(tx, record, candidate_ref):
        if candidate_ref is None:
            return
        row = tx.get('candidate_needed_intents', candidate_ref)
        expected = {'candidate_ref':candidate_ref,'lifecycle_id':record.key.lifecycle_id,
            'source_receipt':record.source_receipt,'lineage_budget_ref':record.key.lineage_budget_ref,
            'run_ref':record.key.run_ref,'scope_ref':record.key.scope_ref,'node_id':record.key.node_id,
            'graph_ref':record.graph_ref,'plan_ref':record.plan_ref,'contract_ref':record.contract_ref}
        if not row or any(row['body'].get(k) != v for k,v in expected.items()):
            raise PermissionError('candidate intent missing or differs from original lifecycle/version')

    def query(self, task_ref):
        """Trusted internal read model, excluding private evidence/input/session.

        Service access is owned by the application's authentication boundary.
        The request_id is also the immutable Bridge identity for later safety
        consumers; this method performs no provider network calls.
        """
        with self.repository.transaction() as tx:
            row = tx.get('agent_team_tasks', task_ref)
            if not row:
                raise KeyError(task_ref)
            task = row['body']
            return {'contract':task['contract'],'request_id':task_ref,
                'input_hash':task['envelope']['input_hash'],'status':task['status'],
                'observations':task['observations'],'reason':task.get('reason'),
                'proposal_ref':task.get('proposal_ref'),'decision_ref':task.get('decision_ref'),
                'result':task.get('result')}

    def request_role(self, parent_job, *, token, role: Role, candidate_ref=None):
        if role not in ROLE_PACKET:
            raise ValueError('registered team role required')
        if role != 'investigator' and not candidate_ref:
            raise ValueError('coder/reviewer requires exact candidate reference')
        if role == 'investigator' and candidate_ref is not None:
            raise ValueError('investigator cannot inherit a candidate session')
        command = {'parent_job_ref':parent_job['job_id'], 'role':role,
                   'candidate_ref':candidate_ref, 'config':self.config.model_dump(mode='json')}
        ref = 'agent-role:' + canonical_hash({k:command[k] for k in ('parent_job_ref','role','candidate_ref')})
        with self.repository.transaction() as tx:
            record, parent = self._parent_in(tx, parent_job, token,role)
            old = tx.get('agent_team_tasks', ref)
            if old:
                if old['body']['command'] != command:
                    raise Conflict('role request configuration conflict')
                return ref
            from .lifecycle_stop import LifecycleStopService
            LifecycleStopService.assert_admission_in(tx, record)
            policy, deadline = self._limits_in(tx, record, role)
            self._candidate_in(tx, record, candidate_ref)
        packet = self.context.build(record.key.lifecycle_id, expected_revision=record.revision,
            token=token, task_role=ROLE_PACKET[role])
        # The trusted context service reads sanitized grants only. There is no
        # caller field bag or path input, and no coder-private conversation.
        evidence = {}
        for handle in packet.evidence_refs:
            with self.repository.transaction() as tx:
                grant = tx.get('lifecycle_context_grants', handle)['body']
                length = min(grant['derived']['size'], self.config.evidence_bytes_per_grant,
                             self.context.max_read_bytes)
            if length:
                evidence[handle] = self.context.read_evidence(record.key.lifecycle_id, handle,
                    offset=0, length=length, token=token, expected_revision=record.revision).decode('utf-8')
        contract = RoleContract(schema_version=1, task_ref=ref, parent_job_ref=parent_job['job_id'],
            parent_role='supervisor', role=role, depth=1, max_depth=policy.max_depth,
            max_concurrency=policy.max_concurrency, lifecycle_id=record.key.lifecycle_id,
            lineage_budget_ref=record.key.lineage_budget_ref, node_supervisor_id=packet.node_supervisor_id,
            source_receipt=record.source_receipt, run_ref=record.key.run_ref,
            scope_ref=record.key.scope_ref, node_id=record.key.node_id,
            graph_ref=record.graph_ref, plan_ref=record.plan_ref, contract_ref=record.contract_ref,
            expected_revision=record.revision, candidate_ref=candidate_ref,
            provider_id=self.config.provider_id, model_id=self.config.model_id,
            model_version=self.config.model_version, deadline=min(deadline, parent['deadline']))
        body = {'schema_version':1, 'task':contract.model_dump(mode='json'),
                'packet':packet.model_dump(mode='json'), 'evidence':evidence,
                'output_schema':TeamProposal.model_json_schema()}
        input_json = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',',':'), allow_nan=False)
        if len(input_json.encode('utf-8')) > self.config.max_input_bytes:
            raise ValueError('model input exceeds explicit byte limit; narrower evidence required')
        envelope = SubmitEnvelope(schema_version=1, request_id=ref, lifecycle_id=record.key.lifecycle_id,
            scope={'schema_version':1,'kind':record.key.scope_kind,'ref':record.key.scope_ref},
            input_hash=hashlib.sha256(input_json.encode()).hexdigest(), input_json=input_json,
            model_id=self.config.model_id, model_version=self.config.model_version,
            usage_ceiling=UsageCeiling(schema_version=1, input_tokens=self.config.max_input_tokens,
                output_tokens=self.config.max_output_tokens, cost_microunits=self.config.max_cost_microunits),
            deadline=float(contract.deadline))
        with self.repository.transaction() as tx:
            current, current_parent = self._parent_in(tx, parent_job, token,role)
            if current != record:
                raise Conflict('lifecycle changed while building role context')
            old = tx.get('agent_team_tasks', ref)
            if old:
                if old['body']['command'] != command:
                    raise Conflict('role request configuration conflict')
                return ref
            LifecycleStopService.assert_admission_in(tx, current)
            _, current_deadline = self._limits_in(tx, current, role)
            if contract.deadline > min(current_deadline,current_parent['deadline']):
                raise Conflict('parent or authority deadline shortened while building role context')
            self._candidate_in(tx, current, candidate_ref)
            amounts = {'child_jobs':1}
            if role == 'investigator':
                amounts.update(diagnosis_calls=1, tokens=self.config.max_input_tokens + self.config.max_output_tokens,
                               cost=self.config.max_cost_microunits, observations=self.config.max_observations)
            LifecycleBudgetService.reserve_in(tx, request_id=ref, lifecycle_id=record.key.lifecycle_id,
                amounts=amounts, input_ref=envelope.input_hash,
                metadata={'contract':contract.model_dump(mode='json')},
                provider_enforces_maximum=True if role == 'investigator' else None)
            tx.put('agent_team_tasks', ref, {'command':command, 'contract':contract.model_dump(mode='json'),
                'packet':packet.model_dump(mode='json'), 'envelope':envelope.model_dump(mode='json'),
                'status':'queued', 'observations':0}, immutable=True)
            TaskBackend.enqueue_in(tx, JobSpec(job_id=ref, job_kind='agent_team', scope_ref=record.key.scope_ref,
                thread_id=ref, activation_id=ref, dedupe_key=ref, due_at=tx.now,
                deadline=float(contract.deadline), run_id=record.key.run_ref,
                dispatch_generation=tx.get('runs',record.key.run_ref)['body']['dispatch_generation'],
                max_attempts=self.config.max_observations + 1,
                payload={'task_ref':ref, 'lifecycle_id':record.key.lifecycle_id,
                         'plan_ref':record.plan_ref, 'graph_ref':record.graph_ref}))
        return ref

    @staticmethod
    def assert_task_in(tx, job, token):
        job = TaskBackend.assert_claim_in(tx, job)
        if job['job_kind'] != 'agent_team' or token.thread_id != job['thread_id']:
            raise PermissionError('role token must own the actual role job thread')
        row = tx.get('agent_team_tasks', job['job_id'])
        if not row or job['payload'].get('task_ref') != job['job_id']:
            raise Conflict('role task missing')
        task = row['body']
        contract = decode(RoleContract, task['contract'])
        if (job['run_id'], job['scope_ref'], job['thread_id'], job['payload'].get('lifecycle_id')) != (
                contract.run_ref, contract.scope_ref, contract.task_ref, contract.lifecycle_id):
            raise PermissionError('role job differs from frozen contract')
        LifecycleService.assert_owner_in(tx, contract.lifecycle_id, token)
        LifecycleSafetyService.assert_work_in(tx, contract.scope_ref, token)
        record = LifecycleService.load_in(tx, contract.lifecycle_id)
        if (record.source_receipt, record.key.lineage_budget_ref, record.revision,
                record.graph_ref, record.plan_ref, record.contract_ref) != (
                contract.source_receipt, contract.lineage_budget_ref, contract.expected_revision,
                contract.graph_ref, contract.plan_ref, contract.contract_ref):
            raise Conflict('role lifecycle identity or revision changed')
        return row, contract, record

    @classmethod
    def assert_model_network_in(cls, tx, job, token, *, consume_observation=False):
        row, contract, record = cls.assert_task_in(tx,job,token)
        from .lifecycle_rollout import LifecycleRolloutService
        LifecycleRolloutService.assert_enabled_in(tx,record,'model')
        from .lifecycle_stop import LifecycleStopService
        LifecycleStopService.assert_admission_in(tx,record)
        task = row['body']
        if contract.role != 'investigator' or task['status'] not in {'queued','running'}:
            raise PermissionError('only active investigator role may call the provider')
        config = decode(ModelTeamConfig,task['command']['config'])
        envelope = decode(SubmitEnvelope,task['envelope'])
        actual = TaskBackend.assert_claim_in(tx,job)
        expected_ceiling = UsageCeiling(schema_version=1,input_tokens=config.max_input_tokens,
            output_tokens=config.max_output_tokens,cost_microunits=config.max_cost_microunits)
        payload = json.loads(envelope.input_json)
        if (envelope.request_id != contract.task_ref or envelope.lifecycle_id != contract.lifecycle_id
                or envelope.scope.kind != record.key.scope_kind or envelope.scope.ref != contract.scope_ref
                or envelope.model_id != contract.model_id or envelope.model_version != contract.model_version
                or envelope.deadline != contract.deadline or envelope.deadline > actual['deadline']
                or envelope.usage_ceiling != expected_ceiling
                or payload.get('task') != contract.model_dump(mode='json')
                or payload.get('packet') != task['packet']):
            raise PermissionError('provider envelope differs from frozen role/model limits')
        expected_amounts = {'child_jobs':1,'diagnosis_calls':1,
            'tokens':config.max_input_tokens + config.max_output_tokens,
            'cost':config.max_cost_microunits,'observations':config.max_observations}
        reservation = tx.get('budget_reservations',contract.task_ref)
        binding = reservation['body'] if reservation else {}
        if (binding.get('lifecycle_id') != contract.lifecycle_id
                or binding.get('lineage_budget_ref') != contract.lineage_budget_ref
                or binding.get('input_ref') != envelope.input_hash
                or binding.get('amounts') != expected_amounts
                or binding.get('metadata') != {'contract':contract.model_dump(mode='json')}
                or binding.get('safety') is not False
                or binding.get('provider_enforces_maximum') is not True
                or tx.get('budget_settlements',contract.task_ref)):
            raise PermissionError('active frozen model budget reservation required')
        snapshot = LifecycleBudgetService.snapshot_in(tx,contract.lifecycle_id)
        for bound in snapshot.values():
            if bound['cost_unit'] != config.cost_unit or any(
                    bound['reserved'][counter] < amount for counter,amount in expected_amounts.items()):
                raise PermissionError('reserved model budget or cost unit changed')
        if consume_observation:
            if task['observations'] >= config.max_observations:
                raise Conflict('provider observation budget exhausted')
            tx.put('agent_team_tasks',contract.task_ref,task | {
                'status':'running','observations':task['observations'] + 1},expected_revision=row['revision'])
        return row, contract, record

    @classmethod
    def assert_cancel_network_in(cls, tx, job, token):
        row, contract, record = cls.assert_model_network_in(tx,job,token)
        cancel = tx.get('agent_team_cancellations',contract.task_ref)
        intent = cancel['body'] if cancel else {}
        config = decode(ModelTeamConfig,row['body']['command']['config'])
        reservation = tx.get('budget_reservations',intent.get('reservation_ref',''))
        binding = reservation['body'] if reservation else {}
        metadata = {'request_id':contract.task_ref,'provider_id':contract.provider_id,
                    'handle':intent.get('handle'),'input_hash':row['body']['envelope']['input_hash']}
        expected = {'stop_calls':1,'cost':config.max_cancel_cost_microunits}
        if (intent.get('request_id') != contract.task_ref or intent.get('provider_id') != contract.provider_id
                or intent.get('state') != 'prepared' or intent.get('network_started') is not False
                or binding.get('lifecycle_id') != contract.lifecycle_id
                or binding.get('lineage_budget_ref') != contract.lineage_budget_ref
                or binding.get('input_ref') != metadata['input_hash'] or binding.get('metadata') != metadata
                or binding.get('amounts') != expected or binding.get('safety') is not True
                or binding.get('operation_kind') != 'stop'
                or tx.get('budget_settlements',intent.get('reservation_ref',''))):
            raise PermissionError('unspent original-ID cancellation safety intent required')
        snapshot = LifecycleBudgetService.snapshot_in(tx,contract.lifecycle_id)
        if any(bound['cost_unit'] != config.cost_unit or any(
                bound['safety_reserved'][counter] < amount for counter,amount in expected.items())
                for bound in snapshot.values()):
            raise PermissionError('cancellation safety reservation or cost unit changed')
        return cancel

    @staticmethod
    def settle_cancel_in(tx, task_ref):
        row = tx.get('agent_team_cancellations',task_ref)
        if not row:
            return
        intent = row['body']
        if not tx.get('budget_settlements',intent['reservation_ref']):
            # v1 cancel reports no authenticated cost usage. A rejection or lost
            # reply is not free; consume the entire explicit safety maximum.
            LifecycleBudgetService.settle_in(tx,request_id=intent['reservation_ref'])
        if intent['state'] == 'prepared':
            tx.put('agent_team_cancellations',task_ref,intent | {'state':'unknown'},
                   expected_revision=row['revision'])

    def validate_proposal_in(self, tx, task, proposal):
        contract = decode(RoleContract, task['contract'])
        for name in ('task_ref','role','lifecycle_id','source_receipt','scope_ref',
                     'graph_ref','plan_ref','contract_ref','candidate_ref'):
            if getattr(proposal, name) != getattr(contract, name):
                raise PermissionError('proposal differs from frozen role identity')
        if proposal.input_hash != task['envelope']['input_hash']:
            raise PermissionError('proposal input digest mismatch')
        packet = decode(ContextPacket, task['packet'])
        evidence = json.loads(task['envelope']['input_json'])['evidence']
        for fact in proposal.facts:
            if fact.evidence_ref == contract.source_receipt:
                if fact.text not in (*packet.facts, packet.failure_summary):
                    raise PermissionError('fact not verified by original source failure')
            elif fact.evidence_ref not in packet.evidence_refs or fact.evidence_ref not in evidence:
                raise PermissionError('fact evidence not readable in frozen packet')
            elif fact.text not in evidence[fact.evidence_ref]:
                raise PermissionError('fact text not verified by readable evidence')
        if proposal.route == 'registered_action':
            policy = decode(RepairPolicy, tx.get('repair_policies',
                LifecycleService.load_in(tx, contract.lifecycle_id).policy_ref)['body'])
            if proposal.action_ref not in policy.actions:
                raise PermissionError('proposal action not allowed by registered policy')
            action_id, sep, version = proposal.action_ref.rpartition('@')
            if not sep:
                raise PermissionError('versioned registered action required')
            self.repair_catalog.resolve(action_id, version, contract.node_id).require_available()
        return contract

    @staticmethod
    def safety_target_in(tx, task_ref):
        """窄安全合同：只证明已停普通角色的原请求，不授予模型/提案权限。"""
        row=tx.get('agent_team_tasks',task_ref)
        original_job=tx.get('jobs',task_ref)
        remote_row=tx.get('agent_provider_requests',task_ref)
        if not row or not original_job or not remote_row:
            raise Conflict('original_provider_task_job_request_missing')
        task=row['body']; contract=decode(RoleContract,task['contract'])
        envelope=decode(SubmitEnvelope,task['envelope'])
        remote=decode(RequestRecord,remote_row['body'])
        record=LifecycleService.load_in(tx,contract.lifecycle_id)
        original=original_job['body']
        reservation=tx.get('budget_reservations',task_ref)
        binding=reservation['body'] if reservation else {}
        if (contract.role!='investigator' or task['status'] not in {'waiting_provider','reconciled'}
                or original['job_kind']!='agent_team' or original['job_id']!=contract.task_ref
                or original['payload'].get('task_ref')!=task_ref
                or original['payload'].get('lifecycle_id')!=contract.lifecycle_id
                or (original['run_id'],original['scope_ref'],original['thread_id'])!=(contract.run_ref,contract.scope_ref,task_ref)
                or remote.envelope!=envelope or remote.envelope.request_id!=task_ref
                or remote.provider_id!=contract.provider_id or envelope.request_id!=task_ref
                or envelope.lifecycle_id!=contract.lifecycle_id
                or envelope.scope.ref!=contract.scope_ref or envelope.scope.kind!=record.key.scope_kind
                or (record.key.run_ref,record.key.scope_ref,record.key.lineage_budget_ref,record.source_receipt,
                    record.plan_ref,record.graph_ref,record.contract_ref)!=(contract.run_ref,contract.scope_ref,
                    contract.lineage_budget_ref,contract.source_receipt,contract.plan_ref,contract.graph_ref,contract.contract_ref)
                or binding.get('lifecycle_id')!=contract.lifecycle_id
                or binding.get('lineage_budget_ref')!=contract.lineage_budget_ref
                or binding.get('input_ref')!=envelope.input_hash
                or binding.get('metadata')!={'contract':task['contract']}
                or binding.get('safety') is not False or binding.get('provider_enforces_maximum') is not True
                or not tx.get('budget_settlements',task_ref)):
            raise PermissionError('waiting_original_provider_safety_binding_required')
        if original['status'] in {'pending','running','draining'}:
            raise ProviderHandoffPending('original_provider_job_not_finished')
        return task,contract,record,remote

    @staticmethod
    def reconciliation_intent_in(tx, task_ref, lifecycle_id):
        tx.put('outbox','provider-reconciliation:' + task_ref,{
            'kind':'provider_reconciliation_required','lifecycle_id':lifecycle_id,
            'task_ref':task_ref,'operation_refs':[task_ref]},immutable=True)

    @staticmethod
    def settle_observed_in(tx, request_id):
        """Charge authenticated terminal usage, otherwise the full maximum."""
        old = tx.get('budget_settlements', request_id)
        if old:
            return old['body']
        row = tx.get('agent_provider_requests', request_id)
        remote = decode(RequestRecord,row['body']) if row else None
        usage = remote.receipt.usage if remote and remote.terminal else None
        return LifecycleBudgetService.settle_in(tx,request_id=request_id,
            actual_tokens=usage.input_tokens + usage.output_tokens if usage else None,
            actual_cost=usage.cost_microunits if usage else None)

    @staticmethod
    def job_ended_in(tx, job):
        if job['job_kind'] != 'agent_team':
            return
        row = tx.get('agent_team_tasks', job['job_id'])
        if not row:
            return
        AgentTeamService.settle_cancel_in(tx,job['job_id'])
        if row['body']['status'] not in {'queued','running'}:
            return
        task = row['body']
        # A timeout/cancellation cannot establish nonexecution. Keep an unknown
        # remote task occupying its lineage slot; only original-ID reconciliation
        # can establish stopped children. Never allocate a replacement identity.
        provider = tx.get('agent_provider_requests', job['job_id'])
        unknown = bool(provider and not decode(RequestRecord, provider['body']).terminal)
        result = {'execution_status':'blocked','error':{'code':'MODEL_JOB_ENDED',
                  'reason':'provider_acceptance_or_children_unknown' if unknown else 'role_job_stopped'}}
        AgentTeamService.settle_observed_in(tx,job['job_id'])
        tx.put('agent_team_tasks', job['job_id'], task | {
            'status':'waiting_provider' if unknown else 'blocked',
            'reason':result['error']['reason'], 'result':result}, expected_revision=row['revision'])
        tx.put('lifecycle_blockers',task['contract']['lifecycle_id'], {
            'status':'waiting_dependency' if unknown else 'blocked',
            'reason':result['error']['reason'], 'task_ref':job['job_id'],
            'lifecycle_id':task['contract']['lifecycle_id'],
            'request_id':job['job_id']})
        if unknown:
            AgentTeamService.reconciliation_intent_in(tx,job['job_id'],task['contract']['lifecycle_id'])
