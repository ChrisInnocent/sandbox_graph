"""ALU-13 / ORC-088: real task claims, bridge and trusted context boundaries."""
import json

import pytest

from sandbox_graph.domain.agent_lifecycle import LifecycleEvent
from sandbox_graph.persistence.repository import Conflict, StaleOwner
from sandbox_graph.runtime.agent_bridge import CollectReply, LookupReply, ProviderError, Usage
from sandbox_graph.runtime.tasks import JobSpec, TaskBackend
from sandbox_graph.runtime.worker import Worker, WorkerPolicy
from sandbox_graph.services.agent_lifecycle import LifecycleService
from tests.orchestration.unit.test_agent_bridge import ControlledProvider, capabilities, receipt, scope
from tests.orchestration.unit.test_lifecycle_context import context_service, grant


def configuration(**changes):
    from sandbox_graph.services.agent_team import ModelTeamConfig
    return ModelTeamConfig(**(dict(schema_version=1, provider_id='local', model_id='model',
        model_version='v1', max_input_tokens=20, max_output_tokens=10, max_cost_microunits=40,
        max_cancel_cost_microunits=3,
        cost_unit='microunits', deadline=1090.0, max_observations=2, poll_seconds=0.001,
        max_input_bytes=16000, max_output_bytes=8000, evidence_bytes_per_grant=64) | changes))


def use_model_cost_unit(repository, supervisor=None):
    """Synthetic fixture configuration, before any model intent is created."""
    with repository.transaction() as tx:
        for row in tx.scan('repair_policies'):
            body = row['body']
            body['safety_budget']['cost_unit'] = 'microunits'
            tx.put('repair_policies',row['key'],body)
        for row in tx.scan('lifecycle_budgets'):
            body = row['body']
            body['cost_unit'] = body['original']['cost_unit'] = 'microunits'
            tx.put('lifecycle_budgets',row['key'],body)
    if supervisor is not None:
        for binding in supervisor.bindings.values():
            binding['cost_unit'] = 'microunits'


class DiagnosticProvider(ControlledProvider):
    def __init__(self, *, route='candidate_needed', mutate=None):
        super().__init__(capabilities(scopes=(scope('work/a'),)))
        self.route, self.mutate = route, mutate

    async def submit(self, env):
        await super().submit(env)
        self.current = receipt(env, state='succeeded', children_stopped=True,
            receipt_id='receipt:done', usage=Usage(schema_version=1, input_tokens=7,
                                                  output_tokens=3, cost_microunits=9))
        return self.current

    async def collect(self, handle):
        env = self.submissions[0]
        body = json.loads(env.input_json)
        binding = body['task']
        proposal = dict(schema_version=1, task_ref=binding['task_ref'], role=binding['role'],
            input_hash=env.input_hash, lifecycle_id=env.lifecycle_id,
            source_receipt=binding['source_receipt'], scope_ref=binding['scope_ref'],
            graph_ref=binding['graph_ref'], plan_ref=binding['plan_ref'],
            contract_ref=binding['contract_ref'], candidate_ref=binding['candidate_ref'],
            route=self.route, action_ref=None,
            facts=[{'text':body['packet']['facts'][1], 'evidence_ref':binding['source_receipt']}],
            hypotheses=['A registered candidate implementation is needed'])
        if self.mutate:
            self.mutate(proposal)
        return CollectReply(schema_version=1, receipt=self.current,
                            output_json=json.dumps(proposal))


def prepared(repository, tmp_path, *, config=None):
    from sandbox_graph.services.agent_team import AgentTeamService
    context, store, (record, _), token = context_service(repository, tmp_path)
    use_model_cost_unit(repository)
    grant(context, store, record, b'{"fact":"NODE_PRIVATE SECRET_CANARY"}')
    with repository.transaction() as tx:
        proof = tx.get('lifecycle_dispositions', record.key.lifecycle_id)['body']['receipt_ref']
        event = LifecycleEvent(schema_version=1, event_id='team:diagnosing', kind='disposition_confirmed',
            source='test', expected_revision=0, evidence_refs=(proof,), observed_at=tx.now)
        record = LifecycleService.advance_in(tx, record.key.lifecycle_id, event,
            expected_revision=0, token=token)
        TaskBackend.enqueue_in(tx, JobSpec(job_id='parent/job', job_kind='lifecycle',
            scope_ref='work/a', thread_id=token.thread_id, activation_id='parent/a',
            dedupe_key='parent/job', due_at=1000.0, deadline=1100.0, run_id='run/a',
            dispatch_generation=3, payload={'stage':'diagnose','lifecycle_id':record.key.lifecycle_id}))
    parent = TaskBackend(repository).claim('worker', 90)
    team = AgentTeamService(repository, context, config=config or configuration())
    return team, context, record, parent, token


def release_parent(repository, parent, token):
    from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
    with repository.transaction() as tx:
        LifecycleSafetyService.release_work_in(tx, 'work/a', token)
        tx.release_thread(token)
    TaskBackend(repository).finish(parent['job_id'], owner_epoch=parent['owner_epoch'],
        result={'execution_status':'succeeded'})


def runner(repository, team, provider):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    handler = AgentTeamJobHandler(team, provider, lease_seconds=30)
    return Worker(repository, {'agent_team':handler},
        policy=WorkerPolicy(claim_seconds=30, job_seconds=20, poll_seconds=0.01))


def test_enqueue_atomically_binds_real_parent_and_reserves_shared_limits(repository, tmp_path):
    team, _, record, parent, token = prepared(repository, tmp_path)
    ref = team.request_role(parent, token=token, role='investigator')
    assert team.request_role(parent, token=token, role='investigator') == ref
    with repository.transaction() as tx:
        task = tx.get('agent_team_tasks', ref)['body']
        assert task['contract']['parent_job_ref'] == parent['job_id']
        assert task['contract']['depth'] == 1
        assert task['contract']['lineage_budget_ref'] == record.key.lineage_budget_ref
        assert task['contract']['node_supervisor_id']
        assert tx.get('jobs', ref)['body']['job_kind'] == 'agent_team'
        budget = tx.get('lifecycle_budgets','lineage:lineage/a')['body']
        assert budget['reserved']['tokens'] == 30 and budget['reserved']['cost'] == 40
        assert budget['reserved']['diagnosis_calls'] == budget['reserved']['child_jobs'] == 1
        assert budget['reserved']['observations'] == 2
        assert 'SECRET_CANARY' not in task['envelope']['input_json']
    with pytest.raises(Conflict, match='concurrency'):
        team.request_role(parent, token=token, role='reviewer', candidate_ref='candidate/x')


@pytest.mark.asyncio
async def test_real_worker_bridge_proposal_commits_candidate_and_decision_once(repository, tmp_path):
    team, _, record, parent, token = prepared(repository, tmp_path)
    ref = team.request_role(parent, token=token, role='investigator')
    release_parent(repository, parent, token)
    provider = DiagnosticProvider()
    worker = runner(repository, team, provider)
    assert await worker.run_once()
    for _ in range(3):
        await worker.run_once()
    with repository.transaction() as tx:
        task = tx.get('agent_team_tasks',ref)['body']
        assert task['status'] == 'completed'
        assert tx.get('jobs',ref)['body']['status'] == 'complete'
        assert len(tx.scan('lifecycle_decisions')) == len(tx.scan('agent_team_proposals')) == 1
        candidate = tx.scan('candidate_needed_intents')[0]['body']
        assert candidate['source_receipt'] == record.source_receipt
        assert candidate['status'] == 'waiting_dependency'
        assert not tx.scan('repair_orders')
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['tokens'] == 10
        assert len(tx.scan('budget_settlements')) == 1
        assert tx.get('outbox','decision-ready:' + task['decision_ref'])
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutate', [
    lambda p:p['facts'][0].update(evidence_ref='receipt/other'),
    lambda p:p['facts'][0].update(text='invented fact'),
    lambda p:p.update(scope_ref='work/other'),
    lambda p:p.update(script='rm -rf /'),
    lambda p:p.update(route='registered_action', action_ref='python.exec@1'),
])
async def test_untrusted_proposal_is_blocked_before_decision_or_effect(repository, tmp_path, mutate):
    team, _, _, parent, token = prepared(repository, tmp_path)
    ref = team.request_role(parent, token=token, role='investigator')
    release_parent(repository, parent, token)
    await runner(repository, team, DiagnosticProvider(mutate=mutate)).run_once()
    with repository.transaction() as tx:
        assert tx.get('jobs',ref)['body']['status'] == 'blocked'
        assert tx.get('agent_team_tasks',ref)['body']['status'] == 'blocked'
        assert not tx.scan('lifecycle_decisions') and not tx.scan('repair_orders')
        assert len(tx.scan('budget_settlements')) == 1
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['tokens'] == 10


@pytest.mark.asyncio
async def test_unknown_acceptance_has_bounded_original_lookup_and_full_charge(repository, tmp_path):
    team, _, _, parent, token = prepared(repository, tmp_path)
    ref = team.request_role(parent, token=token, role='investigator')
    release_parent(repository, parent, token)
    provider = ControlledProvider(capabilities(scopes=(scope('work/a'),)))
    provider.lose_submit, provider.unknown_lookup = True, True
    worker = runner(repository, team, provider)
    await worker.run_once()
    for _ in range(4):
        await worker.run_once()
    assert len(provider.submissions) == 1
    assert provider.lookups == [ref]
    with repository.transaction() as tx:
        task = tx.get('agent_team_tasks',ref)['body']
        assert task['status'] == 'waiting_provider' and task['observations'] == 2
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['tokens'] == 30
        assert not tx.scan('lifecycle_decisions')


def test_stale_parent_cannot_allocate_task_or_budget(repository, tmp_path, clock):
    team, _, _, parent, token = prepared(repository, tmp_path)
    clock.advance(91)
    with pytest.raises(StaleOwner):
        team.request_role(parent, token=token, role='investigator')
    with repository.transaction() as tx:
        assert not tx.scan('agent_team_tasks') and not tx.scan('budget_reservations')


@pytest.mark.asyncio
@pytest.mark.parametrize('role',['coder','reviewer'])
async def test_candidate_role_contract_is_isolated_and_blocked_without_tool_dependency(repository, tmp_path, role):
    team, _, _, parent, token = prepared(repository, tmp_path)
    with repository.transaction() as tx:
        record = LifecycleService.load_in(tx,parent['payload']['lifecycle_id'])
        tx.put('candidate_needed_intents','candidate/x',dict(candidate_ref='candidate/x',
            lifecycle_id=record.key.lifecycle_id,lineage_budget_ref=record.key.lineage_budget_ref,
            source_receipt=record.source_receipt,run_ref=record.key.run_ref,scope_ref=record.key.scope_ref,
            node_id=record.key.node_id,graph_ref=record.graph_ref,plan_ref=record.plan_ref,
            contract_ref=record.contract_ref,private_session='CODER_PRIVATE_CANARY'),immutable=True)
    ref = team.request_role(parent, token=token, role=role, candidate_ref='candidate/x')
    release_parent(repository, parent, token)
    provider = DiagnosticProvider()
    await runner(repository, team, provider).run_once()
    with repository.transaction() as tx:
        task = tx.get('agent_team_tasks',ref)['body']
        assert task['status'] == 'blocked' and 'candidate' in task['reason']
        assert task['contract']['candidate_ref'] == 'candidate/x'
        assert task['contract']['role'] == role
        assert 'CODER_PRIVATE_CANARY' not in task['envelope']['input_json']
        assert not tx.scan('agent_provider_requests')
    assert not provider.submissions


def test_recursive_role_parent_cannot_bypass_one_layer_with_payload_depth(repository, tmp_path):
    team, _, _, parent, token = prepared(repository, tmp_path)
    with repository.transaction() as tx:
        row = tx.get('jobs',parent['job_id'])
        changed = row['body'] | {'job_kind':'agent_team','payload':row['body']['payload'] | {'depth':0}}
        tx.put('jobs',parent['job_id'],changed)
    with pytest.raises(PermissionError, match='recursive'):
        team.request_role(changed, token=token, role='investigator')
    with repository.transaction() as tx:
        assert not tx.scan('agent_team_tasks')


@pytest.mark.asyncio
async def test_supervisor_unknown_branch_uses_real_persistent_team(repository, tmp_path):
    from sandbox_graph.artifacts.store import FileArtifactStore
    from sandbox_graph.services.agent_team import AgentTeamService
    from sandbox_graph.services.lifecycle_context import LifecycleContextService
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    from tests.orchestration.graphs.test_work_remediation import assembly, drain
    worker, supervisor, _, _ = assembly(repository, failure='N08')
    use_model_cost_unit(repository,supervisor)
    context = LifecycleContextService(repository, FileArtifactStore(tmp_path / 'artifacts'),
        artifact_scope='evidence', secret_values=())
    team = AgentTeamService(repository,context,config=configuration())
    supervisor.model_team = team
    provider = DiagnosticProvider()
    worker.handlers['agent_team'] = AgentTeamJobHandler(team,provider,lease_seconds=30)
    await drain(worker)
    with repository.transaction() as tx:
        assert len(tx.scan('agent_team_tasks')) == 1
        task = tx.scan('agent_team_tasks')[0]['body']
        assert task['status'] == 'completed'
        assert tx.scan('agent_lifecycles')[0]['body']['phase'] == 'waiting_decision'
        assert tx.scan('candidate_needed_intents')[0]['body']['status'] == 'waiting_dependency'
        assert tx.scan('lifecycle_blockers')[0]['body']['reason'] == 'candidate_tools_46_not_connected'
        assert not tx.scan('repair_orders')
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
async def test_configured_team_never_calls_provider_for_deterministic_repair(repository, tmp_path):
    from sandbox_graph.artifacts.store import FileArtifactStore
    from sandbox_graph.services.agent_team import AgentTeamService
    from sandbox_graph.services.lifecycle_context import LifecycleContextService
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    from tests.orchestration.graphs.test_work_remediation import assembly, drain
    worker, supervisor, _, repairs = assembly(repository)
    use_model_cost_unit(repository,supervisor)
    context = LifecycleContextService(repository,FileArtifactStore(tmp_path / 'artifacts'),
        artifact_scope='evidence',secret_values=())
    team = AgentTeamService(repository,context,config=configuration())
    supervisor.model_team = team
    provider = DiagnosticProvider()
    worker.handlers['agent_team'] = AgentTeamJobHandler(team,provider,lease_seconds=30)
    await drain(worker)
    assert len(repairs) == 1 and not provider.submissions
    with repository.transaction() as tx:
        assert not tx.scan('agent_team_tasks')


@pytest.mark.asyncio
async def test_success_crash_before_local_commit_replays_without_recharge_or_remote_submit(repository, tmp_path, clock, monkeypatch):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    class ProcessCut(BaseException):
        pass
    team, _, _, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    provider = DiagnosticProvider()
    worker = runner(repository,team,provider)
    handler = worker.handlers['agent_team']
    original = handler._commit
    def cut(*args):
        raise ProcessCut()
    monkeypatch.setattr(handler,'_commit',cut)
    with pytest.raises(ProcessCut):
        await worker.run_once()
    with repository.transaction() as tx:
        assert tx.get('agent_provider_requests',ref)['body']['result']
        assert not tx.scan('budget_settlements') and not tx.scan('lifecycle_decisions')
    clock.advance(31)
    monkeypatch.setattr(handler,'_commit',original)
    await worker.run_once()
    with repository.transaction() as tx:
        assert tx.get('agent_team_tasks',ref)['body']['status'] == 'completed'
        assert len(tx.scan('budget_settlements')) == len(tx.scan('lifecycle_decisions')) == 1
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['tokens'] == 10
        assert tx.get('agent_team_tasks',ref)['body']['observations'] == 1
    assert len(provider.submissions) == 1


@pytest.mark.asyncio
async def test_stale_job_after_capabilities_cannot_submit_provider_intent(repository,tmp_path):
    team, _, _, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    class StaleDuringCapabilities(DiagnosticProvider):
        async def capabilities(self):
            with repository.transaction() as tx:
                row = tx.get('jobs',ref)
                tx.put('jobs',ref,row['body'] | {'owner_epoch':row['body']['owner_epoch'] + 1})
            return await super().capabilities()
    provider = StaleDuringCapabilities()
    with pytest.raises(StaleOwner):
        await runner(repository,team,provider).run_once()
    assert not provider.submissions
    with repository.transaction() as tx:
        assert not tx.scan('lifecycle_decisions') and not tx.scan('budget_settlements')


def test_atomic_enqueue_failure_rolls_back_both_budget_and_role_contract(repository,tmp_path,monkeypatch):
    from tests.orchestration.conftest import MemoryTransaction
    team, _, _, parent, token = prepared(repository,tmp_path)
    original = MemoryTransaction.put
    def cut(tx,kind,key,body,*args,**kwargs):
        if kind == 'outbox' and body.get('kind') == 'job_ready':
            raise RuntimeError('queue cut')
        return original(tx,kind,key,body,*args,**kwargs)
    monkeypatch.setattr(MemoryTransaction,'put',cut)
    with pytest.raises(RuntimeError,match='queue cut'):
        team.request_role(parent,token=token,role='investigator')
    with repository.transaction() as tx:
        assert not tx.scan('agent_team_tasks') and not tx.scan('budget_reservations')
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['reserved']['child_jobs'] == 0


@pytest.mark.asyncio
async def test_known_handle_cancel_acceptance_does_not_release_unstopped_children(repository,tmp_path):
    team, _, _, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    class UnstoppedProvider(ControlledProvider):
        async def cancel(self,handle):
            from sandbox_graph.runtime.agent_bridge import CancelReply
            return CancelReply(schema_version=1,request_id=ref,scope=scope('work/a'),
                               handle=handle,accepted=True)
    provider = UnstoppedProvider(capabilities(scopes=(scope('work/a'),)))
    await runner(repository,team,provider).run_once()
    with repository.transaction() as tx:
        remote = tx.get('agent_provider_requests',ref)['body']
        assert remote['cancel_accepted'] and remote['state'] != 'terminal'
        assert tx.get('agent_team_tasks',ref)['body']['status'] == 'waiting_provider'
        assert not tx.scan('lifecycle_decisions')


def test_query_reports_frozen_result_identity_without_context_or_session(repository,tmp_path):
    team, _, _, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    result = team.query(ref)
    assert result['contract']['task_ref'] == ref
    assert result['request_id'] == ref and result['status'] == 'queued'
    assert 'packet' not in result and 'envelope' not in result
    assert 'NODE_PRIVATE' not in json.dumps(result)


def test_candidate_contract_cannot_bind_other_lifecycle_or_unregistered_candidate(repository,tmp_path):
    team, _, _, parent, token = prepared(repository,tmp_path)
    with pytest.raises(PermissionError,match='candidate'):
        team.request_role(parent,token=token,role='coder',candidate_ref='candidate/missing')
    with repository.transaction() as tx:
        assert not tx.scan('agent_team_tasks') and not tx.scan('budget_reservations')


@pytest.mark.asyncio
async def test_model_registered_action_still_uses_original_authorization_and_remediation(repository,tmp_path):
    from sandbox_graph.artifacts.store import FileArtifactStore
    from sandbox_graph.services.agent_team import AgentTeamService
    from sandbox_graph.services.lifecycle_context import LifecycleContextService
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    from tests.orchestration.graphs.test_work_remediation import assembly, drain
    worker, supervisor, _, repairs = assembly(repository,failure_code='UNKNOWN_NEW_CLASS')
    use_model_cost_unit(repository,supervisor)
    context = LifecycleContextService(repository,FileArtifactStore(tmp_path / 'artifacts'),
        artifact_scope='evidence',secret_values=())
    team = AgentTeamService(repository,context,config=configuration())
    supervisor.model_team = team
    provider = DiagnosticProvider(route='registered_action',
        mutate=lambda p:p.update(action_ref='case.reconcile@1'))
    worker.handlers['agent_team'] = AgentTeamJobHandler(team,provider,lease_seconds=30)
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase'] == 'fulfilled'
        assert len(tx.scan('repair_orders')) == len(tx.scan('lifecycle_decisions')) == 1
        assert tx.scan('repair_orders')[0]['body']['action_ref'] == 'case.reconcile@1'
        assert tx.scan('lifecycle_decision_consumptions')[0]['body']['result']['status'] == 'ordered'
    assert len(repairs) == len(provider.submissions) == 1


def test_role_limits_cannot_exceed_policy_when_budget_ledger_is_more_permissive(repository,tmp_path):
    from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
    team, _, record, parent, token = prepared(repository,tmp_path)
    with repository.transaction() as tx:
        LifecycleBudgetService.reserve_in(tx,request_id='already/children',
            lifecycle_id=record.key.lifecycle_id,amounts={'child_jobs':3},input_ref='fixture/prior')
    with pytest.raises(Conflict,match='policy.*child_jobs'):
        team.request_role(parent,token=token,role='investigator')
    with repository.transaction() as tx:
        assert not tx.scan('agent_team_tasks')
        assert len(tx.scan('budget_reservations')) == 1


@pytest.mark.asyncio
async def test_missing_provider_capability_blocks_once_without_remote_submit(repository,tmp_path):
    team, _, _, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    provider = ControlledProvider(capabilities(scopes=(scope('work/a'),),usage_limits=False))
    worker = runner(repository,team,provider)
    await worker.run_once()
    for _ in range(3):
        await worker.run_once()
    with repository.transaction() as tx:
        task = tx.get('agent_team_tasks',ref)['body']
        assert task['status'] == 'blocked' and 'capability_missing:usage_limits' in task['reason']
        assert not tx.scan('agent_provider_requests') and len(tx.scan('agent_team_tasks')) == 1
    assert not provider.submissions


@pytest.mark.asyncio
async def test_v1_negative_lookup_does_not_refund_or_change_request_identity(repository,tmp_path):
    team, _, _, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    class NegativeProvider(ControlledProvider):
        async def lookup(self,request_id):
            self.lookups.append(request_id)
            return LookupReply(schema_version=1,acceptance='not_accepted',receipt=None)
    provider = NegativeProvider(capabilities(scopes=(scope('work/a'),)))
    provider.lose_submit = True
    await runner(repository,team,provider).run_once()
    with repository.transaction() as tx:
        assert tx.get('agent_provider_requests',ref)['body']['state'] == 'submitted_unknown'
        totals = tx.get('lifecycle_budgets','lineage:lineage/a')['body']
        assert totals['spent']['tokens'] == 30 and totals['spent']['cost'] == 40
        assert tx.get('agent_team_tasks',ref)['body']['status'] == 'waiting_provider'
    assert provider.lookups == [ref] and len(provider.submissions) == 1


def test_provider_cost_unit_requires_microunits_without_implicit_conversion():
    with pytest.raises(ValueError,match='microunits'):
        configuration(cost_unit='usd')


@pytest.mark.asyncio
async def test_stale_job_after_remote_success_cannot_publish_decision_or_charge(repository,tmp_path):
    team, _, _, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    class StaleDuringCollect(DiagnosticProvider):
        async def collect(self,handle):
            result = await super().collect(handle)
            with repository.transaction() as tx:
                row = tx.get('jobs',ref)
                tx.put('jobs',ref,row['body'] | {'owner_epoch':row['body']['owner_epoch'] + 1})
            return result
    provider = StaleDuringCollect()
    with pytest.raises(StaleOwner):
        await runner(repository,team,provider).run_once()
    with repository.transaction() as tx:
        assert tx.get('agent_provider_requests',ref)['body']['result']
        assert not tx.scan('lifecycle_decisions') and not tx.scan('budget_settlements')
        assert not tx.scan('agent_team_proposals')
    assert len(provider.submissions) == 1


def test_caller_cannot_forge_parent_kind_around_actual_role_relationship(repository,tmp_path):
    team, _, _, parent, token = prepared(repository,tmp_path)
    with repository.transaction() as tx:
        row = tx.get('jobs',parent['job_id'])
        tx.put('jobs',row['key'],row['body'] | {'job_kind':'agent_team'})
    with pytest.raises(PermissionError,match='recursive'):
        team.request_role(parent,token=token,role='investigator')
    with repository.transaction() as tx:
        assert not tx.scan('agent_team_tasks')


def claim_role(repository, record):
    from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
    child = TaskBackend(repository).claim('child-worker',30)
    with repository.transaction() as tx:
        token = tx.acquire_thread(child['thread_id'],'child',30)
    LifecycleService(repository).claim(record.key.lifecycle_id,token=token)
    with repository.transaction() as tx:
        LifecycleSafetyService.acquire_work_in(tx,child['scope_ref'],token,purpose='diagnosis')
    return child, token


def candidate_fixture(repository,record):
    with repository.transaction() as tx:
        tx.put('candidate_needed_intents','candidate/x',dict(candidate_ref='candidate/x',
            lifecycle_id=record.key.lifecycle_id,lineage_budget_ref=record.key.lineage_budget_ref,
            source_receipt=record.source_receipt,run_ref=record.key.run_ref,scope_ref=record.key.scope_ref,
            node_id=record.key.node_id,graph_ref=record.graph_ref,plan_ref=record.plan_ref,
            contract_ref=record.contract_ref),immutable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('role',['coder','reviewer'])
async def test_round1_r1_public_observe_rejects_candidate_role_before_any_provider_call(repository,tmp_path,role):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    team, _, record, parent, token = prepared(repository,tmp_path)
    candidate_fixture(repository,record)
    ref = team.request_role(parent,token=token,role=role,candidate_ref='candidate/x')
    release_parent(repository,parent,token)
    child, child_token = claim_role(repository,record)
    class CountingProvider(DiagnosticProvider):
        calls = 0
        async def capabilities(self):
            self.calls += 1
            return await super().capabilities()
    provider = CountingProvider()
    with pytest.raises((PermissionError,Conflict),match='role|investigator|candidate'):
        await AgentTeamJobHandler(team,provider,lease_seconds=30).observe(child,token=child_token)
    with repository.transaction() as tx:
        assert tx.get('budget_reservations',ref)['body']['amounts'] == {'child_jobs':1}
        assert not tx.scan('agent_provider_requests')
    assert provider.calls == 0 and not provider.submissions


@pytest.mark.asyncio
async def test_round1_r2_parent_token_cannot_combine_with_valid_child_claim(repository,tmp_path):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    team, _, _, parent, token = prepared(repository,tmp_path)
    team.request_role(parent,token=token,role='investigator')
    child = TaskBackend(repository).claim('child-worker',30)
    provider = DiagnosticProvider()
    with pytest.raises((PermissionError,Conflict),match='thread'):
        await AgentTeamJobHandler(team,provider,lease_seconds=30).observe(child,token=token)
    assert not provider.submissions
    with repository.transaction() as tx:
        assert not tx.scan('agent_provider_requests')


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid',['missing_reservation','settled_reservation','blocked_status','wrong_ceiling'])
async def test_round1_r1_network_requires_active_frozen_model_reservation(repository,tmp_path,invalid):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
    team, _, record, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    child, child_token = claim_role(repository,record)
    with repository.transaction() as tx:
        if invalid == 'missing_reservation':
            tx.delete('budget_reservations',ref)
        elif invalid == 'settled_reservation':
            LifecycleBudgetService.settle_in(tx,request_id=ref)
        else:
            row = tx.get('agent_team_tasks',ref)
            body = row['body']
            if invalid == 'blocked_status':
                body['status'] = 'blocked'
            else:
                body['envelope']['usage_ceiling']['input_tokens'] = 21
            tx.put('agent_team_tasks',ref,body)
    provider = DiagnosticProvider()
    with pytest.raises((PermissionError,Conflict)):
        await AgentTeamJobHandler(team,provider,lease_seconds=30).observe(child,token=child_token)
    assert not provider.submissions
    with repository.transaction() as tx:
        assert not tx.scan('agent_provider_requests')


def test_round1_r3_child_deadline_uses_actual_parent_not_caller_copy(repository,tmp_path):
    team, _, _, parent, token = prepared(repository,tmp_path)
    with repository.transaction() as tx:
        row = tx.get('jobs',parent['job_id'])
        tx.put('jobs',row['key'],row['body'] | {'deadline':1005.0})
    ref = team.request_role(parent,token=token,role='investigator')
    with repository.transaction() as tx:
        task = tx.get('agent_team_tasks',ref)['body']
        assert task['contract']['deadline'] == task['envelope']['deadline'] == 1005.0
        assert tx.get('jobs',ref)['body']['deadline'] == 1005.0


def test_round1_r3_parent_deadline_shortened_during_context_build_rejects_stale_contract(repository,tmp_path,monkeypatch):
    team, context, _, parent, token = prepared(repository,tmp_path)
    original = context.build
    def build(*args,**kwargs):
        packet = original(*args,**kwargs)
        with repository.transaction() as tx:
            row = tx.get('jobs',parent['job_id'])
            tx.put('jobs',row['key'],row['body'] | {'deadline':1005.0})
        return packet
    monkeypatch.setattr(context,'build',build)
    with pytest.raises(Conflict,match='deadline'):
        team.request_role(parent,token=token,role='investigator')
    with repository.transaction() as tx:
        assert not tx.scan('agent_team_tasks') and not tx.scan('budget_reservations')


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['rejected','unknown'])
async def test_round1_r4_cancel_replay_is_one_durable_safety_charged_attempt(repository,tmp_path,outcome):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    from sandbox_graph.runtime.agent_bridge import CancelReply
    team, _, record, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    child, child_token = claim_role(repository,record)
    class CancelProvider(ControlledProvider):
        calls = 0
        async def cancel(self,handle):
            self.calls += 1
            if outcome == 'unknown':
                raise ProviderError('transport_unknown')
            return CancelReply(schema_version=1,request_id=ref,scope=scope('work/a'),
                               handle=handle,accepted=False)
    provider = CancelProvider(capabilities(scopes=(scope('work/a'),)))
    handler = AgentTeamJobHandler(team,provider,lease_seconds=30)
    await handler.observe(child,token=child_token)
    for _ in range(7):
        try:
            await handler.cancel(child,token=child_token)
        except ProviderError:
            pass
    assert provider.calls == 1
    with repository.transaction() as tx:
        task = tx.get('agent_team_tasks',ref)['body']
        assert task['observations'] == 1
        intent = tx.get('agent_team_cancellations',ref)['body']
        assert intent['request_id'] == ref and intent['state'] == outcome
        totals = tx.get('lifecycle_budgets','lineage:lineage/a')['body']
        assert totals['safety_spent']['stop_calls'] == 1 and totals['safety_spent']['cost'] == 3
        assert totals['safety_reserved']['stop_calls'] == totals['safety_reserved']['cost'] == 0
        assert not tx.get('agent_provider_requests',ref)['body']['cancel_accepted']


def test_round1_r4_cancel_cost_requires_explicit_configuration():
    from sandbox_graph.services.agent_team import ModelTeamConfig
    body = configuration().model_dump(mode='json')
    body.pop('max_cancel_cost_microunits',None)
    with pytest.raises(ValueError,match='max_cancel_cost_microunits'):
        ModelTeamConfig.model_validate(body)


@pytest.mark.asyncio
async def test_round1_r1_candidate_role_cannot_publish_unbudgeted_model_decision(repository,tmp_path):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    from sandbox_graph.services.agent_lifecycle import decode
    from sandbox_graph.runtime.agent_bridge import SubmitEnvelope
    team, _, record, parent, token = prepared(repository,tmp_path)
    candidate_fixture(repository,record)
    ref = team.request_role(parent,token=token,role='coder',candidate_ref='candidate/x')
    release_parent(repository,parent,token)
    child, child_token = claim_role(repository,record)
    provider = DiagnosticProvider()
    with repository.transaction() as tx:
        env = decode(SubmitEnvelope,tx.get('agent_team_tasks',ref)['body']['envelope'])
    provider.submissions = [env]
    provider.current = receipt(env,state='succeeded',children_stopped=True,receipt_id='forged/zero',
        usage=Usage(schema_version=1,input_tokens=0,output_tokens=0,cost_microunits=0))
    output = await provider.collect('task:1')
    with pytest.raises((PermissionError,Conflict),match='investigator|role|model'):
        AgentTeamJobHandler(team,provider,lease_seconds=30)._commit(child,child_token,output)
    with repository.transaction() as tx:
        assert not tx.scan('lifecycle_decisions') and not tx.scan('agent_team_proposals')


@pytest.mark.asyncio
async def test_round1_r4_cancel_without_safety_capacity_has_no_remote_call_or_intent(repository,tmp_path):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
    team, _, record, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    child, child_token = claim_role(repository,record)
    provider = ControlledProvider(capabilities(scopes=(scope('work/a'),)))
    handler = AgentTeamJobHandler(team,provider,lease_seconds=30)
    await handler.observe(child,token=child_token)
    with repository.transaction() as tx:
        LifecycleBudgetService.reserve_in(tx,request_id='already/stops',lifecycle_id=record.key.lifecycle_id,
            amounts={'stop_calls':2,'cost':1},input_ref='test/prior',safety=True,operation_kind='stop')
    with pytest.raises((PermissionError,Conflict),match='safety'):
        await handler.cancel(child,token=child_token)
    with repository.transaction() as tx:
        assert not tx.get('agent_team_cancellations',ref)
        assert not tx.get('agent_provider_requests',ref)['body']['cancel_accepted']
        assert len(tx.scan('budget_reservations')) == 2


@pytest.mark.asyncio
async def test_round1_r4_direct_bridge_cancel_cannot_skip_persistent_safety_intent(repository,tmp_path):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    team, _, record, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    child, child_token = claim_role(repository,record)
    class CountingProvider(ControlledProvider):
        cancels = 0
        async def cancel(self,handle):
            self.cancels += 1
            return await super().cancel(handle)
    provider = CountingProvider(capabilities(scopes=(scope('work/a'),)))
    handler = AgentTeamJobHandler(team,provider,lease_seconds=30)
    await handler.observe(child,token=child_token)
    with pytest.raises(PermissionError,match='safety intent'):
        await handler._bridge(child,child_token).cancel(ref)
    assert provider.cancels == 0


@pytest.mark.asyncio
async def test_round1_r1_direct_bridge_observations_share_persistent_round_limit(repository,tmp_path):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    team, _, record, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    child, child_token = claim_role(repository,record)
    class CountingProvider(ControlledProvider):
        status_calls = 0
        async def status(self,handle):
            self.status_calls += 1
            return await super().status(handle)
    provider = CountingProvider(capabilities(scopes=(scope('work/a'),)))
    handler = AgentTeamJobHandler(team,provider,lease_seconds=30)
    await handler.observe(child,token=child_token)
    bridge = handler._bridge(child,child_token)
    await bridge.status(ref)
    with pytest.raises(Conflict,match='observation budget'):
        await bridge.status(ref)
    assert provider.status_calls == 1
    assert team.query(ref)['observations'] == 2


@pytest.mark.asyncio
async def test_round1_r4_completion_watchdog_charges_interrupted_cancel_after_model_commit(repository,tmp_path):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    class ProcessCut(BaseException):
        pass
    team, _, record, parent, token = prepared(repository,tmp_path)
    ref = team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    child, child_token = claim_role(repository,record)
    class InterruptedCancelProvider(DiagnosticProvider):
        async def submit(self,env):
            await super().submit(env)
            self.current = self.saved
            return self.saved
        async def cancel(self,handle):
            raise ProcessCut()
    provider = InterruptedCancelProvider()
    handler = AgentTeamJobHandler(team,provider,lease_seconds=30)
    await handler.observe(child,token=child_token)
    with pytest.raises(ProcessCut):
        await handler.cancel(child,token=child_token)
    provider.current = receipt(provider.submissions[0],state='succeeded',children_stopped=True,
        receipt_id='receipt:later_done',usage=Usage(schema_version=1,input_tokens=7,
                                                  output_tokens=3,cost_microunits=9))
    assert (await handler.observe(child,token=child_token)).terminal
    output = await handler._bridge(child,child_token).collect(ref)
    result = handler._commit(child,child_token,output)
    TaskBackend(repository).finish(ref,owner_epoch=child['owner_epoch'],result=result)
    runner(repository,team,provider).pump_outbox()
    with repository.transaction() as tx:
        assert tx.get('agent_team_tasks',ref)['body']['status'] == 'completed'
        totals = tx.get('lifecycle_budgets','lineage:lineage/a')['body']
        assert totals['safety_spent']['stop_calls'] == 1 and totals['safety_spent']['cost'] == 3
        assert totals['safety_reserved']['cost'] == 0

@pytest.mark.asyncio
@pytest.mark.parametrize('role',['coder','reviewer'])
async def test_candidate_roles_delegate_to_registered_evolution_without_diagnostic_provider_session(repository,tmp_path,role):
    from sandbox_graph.runtime.agent_team import AgentTeamJobHandler
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    team,_,record,parent,token=prepared(repository,tmp_path)
    candidate_fixture(repository,record)
    ref=team.request_role(parent,token=token,role=role,candidate_ref='candidate/x')
    release_parent(repository,parent,token)
    class RegisteredEvolution:
        calls=[]
        def delegate_role_in(self,tx,contract,record,*,token):
            LifecycleService.assert_owner_in(tx,record.key.lifecycle_id,token)
            self.calls.append((contract.role,contract.candidate_ref))
            return {'status':'candidate_queued','job_ref':'controlled-candidate-job'}
    evolution=RegisteredEvolution();provider=DiagnosticProvider()
    worker=Worker(repository,{'agent_team':AgentTeamJobHandler(team,provider,lease_seconds=30,evolution=evolution)},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=10,poll_seconds=.1))
    assert await worker.run_once()
    with repository.transaction() as tx:
        task=tx.get('agent_team_tasks',ref)['body']
        assert task['status']=='completed' and task['result']['candidate_result']['status']=='candidate_queued'
    assert evolution.calls==[(role,'candidate/x')] and not provider.submissions


def test_reviewer_dispatch_accepts_frozen_candidate_phase_and_still_rejects_investigator(repository,tmp_path):
    team,_,record,parent,token=prepared(repository,tmp_path)
    candidate_fixture(repository,record)
    with repository.transaction() as tx:
        tx.put('agent_lifecycles',record.key.lifecycle_id,record.model_copy(update={'phase':'candidate_verifying'}).model_dump(mode='json'))
    ref=team.request_role(parent,token=token,role='reviewer',candidate_ref='candidate/x')
    with repository.transaction() as tx:assert tx.get('agent_team_tasks',ref)['body']['contract']['role']=='reviewer'
    with pytest.raises(Conflict,match='phase'):
        team.request_role(parent,token=token,role='investigator')
