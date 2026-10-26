"""R1–R3: original-operation draining, atomic admission and typed ownership."""
from copy import deepcopy

import pytest

from sandbox_graph.domain.agent_lifecycle import LifecycleRecord
from sandbox_graph.domain.models import RunRecord
from sandbox_graph.interfaces.service import RunService
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.planning.compiler import PlanCompiler
from sandbox_graph.services.agent_lifecycle import LifecycleService
from sandbox_graph.services.lifecycle_stop import LifecycleStopService
from sandbox_graph.services.revisions import RevisionService
from tests.orchestration.unit.test_agent_team import prepared, release_parent, runner
from tests.orchestration.unit.test_agent_bridge import ControlledProvider, capabilities, scope, receipt


def install_gate(repository):
    with repository.transaction() as tx:
        run = tx.get('runs', 'run/a')['body']
        tx.put('dispatch_gates', 'run/a', {'open': True, 'generation': run['dispatch_generation']})


def supersede(repository, *, same_graph=False):
    with repository.transaction() as tx:
        row = tx.get('runs', 'run/a')
        run = RunRecord.model_validate(row['body'] | {'root_thread_id': 'root/a', 'auth_ref': 'auth/a'})
        tx.put('runs', 'run/a', run.model_dump(mode='json'))
        revision = tx.get('runs', 'run/a')['revision']
    return RevisionService(repository).activate_revision(request_id='next-version', predecessor_run_id='run/a',
        expected_revision=revision, candidate_plan_hash='plan/next',
        graph_ref=run.graph_ref if same_graph else 'graph/next', disposition_policy='drain')


@pytest.mark.parametrize('moment', ['before_context', 'during_context'])
def test_cancel_rejects_new_role_before_reserving_or_enqueueing(repository, tmp_path, monkeypatch, moment):
    team, context, record, parent, token = prepared(repository, tmp_path)
    install_gate(repository)
    def cancel():
        RunService(repository, PlanCompiler([])).cancel('cutoff', 'run/a', 'stop')
    if moment == 'before_context':
        cancel()
    else:
        build = context.build
        def cancel_during_build(*args, **kwargs):
            packet = build(*args, **kwargs)
            cancel()
            return packet
        monkeypatch.setattr(context, 'build', cancel_during_build)
    with repository.transaction() as tx:
        budgets = deepcopy(tx.scan('lifecycle_budgets'))
    with pytest.raises(Conflict, match='stop requested'):
        team.request_role(parent, token=token, role='investigator')
    with repository.transaction() as tx:
        assert tx.scan('lifecycle_budgets') == budgets
        assert not tx.scan('agent_team_tasks')
        assert not [r for r in tx.scan('jobs') if r['body']['job_kind'] == 'agent_team']


def test_cancel_preserves_same_role_request_replay_without_spending(repository, tmp_path):
    team, _, _, parent, token = prepared(repository, tmp_path)
    install_gate(repository)
    ref = team.request_role(parent, token=token, role='investigator')
    with repository.transaction() as tx:
        budgets = deepcopy(tx.scan('lifecycle_budgets'))
    RunService(repository, PlanCompiler([])).cancel('cutoff', 'run/a', 'stop')
    assert team.request_role(parent, token=token, role='investigator') == ref
    with repository.transaction() as tx:
        assert tx.scan('lifecycle_budgets') == budgets
        assert len(tx.scan('agent_team_tasks')) == 1


async def pending_provider(repository, tmp_path):
    team, _, record, parent, token = prepared(repository, tmp_path)
    target = team.request_role(parent, token=token, role='investigator')
    release_parent(repository, parent, token)
    from sandbox_graph.runtime.agent_bridge import CancelReply
    class Provider(ControlledProvider):
        stops = 0
        async def cancel(self, handle):
            self.stops += 1
            return CancelReply(schema_version=1, request_id=target, scope=scope('work/a'), handle=handle, accepted=True)
    provider = Provider(capabilities(scopes=(scope('work/a'),)))
    provider.lose_submit = provider.unknown_lookup = True
    await runner(repository, team, provider).run_once()
    provider.unknown_lookup = False
    return record, target, provider


def safety_worker(repository, record, target, provider):
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService, ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    from sandbox_graph.runtime.worker import Worker, WorkerPolicy
    service = LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1, cost_unit='microunits', query_cost=1,
            stop_cost=1, cleanup_cost=1, backoff_seconds=1), authorizations={target: 'safety/auth'})
    service.authorize('safety/auth', operation_kind='provider', target_ref=target, principal='operator',
        expires_at=1140.0, permissions=('query', 'stop'))
    return Worker(repository, {'reconcile': LifecycleReconciliationHandler(service, agent_provider=provider, lease_seconds=30)},
        policy=WorkerPolicy(claim_seconds=30, job_seconds=20, poll_seconds=0.01))


def stop_status(repository, life):
    with repository.transaction() as tx:
        requests = [r for r in tx.scan('lifecycle_stop_requests') if r['body']['lifecycle_id'] == life]
        assert len(requests) == 1
        return tx.get('lifecycle_stop_status', requests[0]['key'])['body']


@pytest.mark.asyncio
@pytest.mark.parametrize('parent_kind,parent_scope', [('run', 'run/a'), ('group', 'group/a')])
async def test_parent_inventory_includes_child_provider_and_excludes_foreign_run(repository, tmp_path, parent_kind, parent_scope):
    record, target, _ = await pending_provider(repository, tmp_path)
    parent = record.model_dump()
    parent['key'].update(scope_kind=parent_kind, scope_ref=parent_scope, lifecycle_id='parent/life')
    parent = LifecycleRecord.model_validate(parent)
    with repository.transaction() as tx:
        foreign = record.model_dump(mode='json')
        foreign['key'].update(run_ref='other/run', scope_ref='other/work', lifecycle_id='other/life')
        tx.put('work_instances', 'other/work', {'run_id': 'other/run', 'group_id': 'group/a', 'work_id': 'other/work'})
        tx.put('agent_lifecycles', 'other/life', foreign)
        remote = deepcopy(tx.get('agent_provider_requests', target)['body'])
        remote['envelope'].update(request_id='other/request', lifecycle_id='other/life')
        remote['envelope']['scope']['ref'] = 'other/work'
        tx.put('agent_provider_requests', 'other/request', remote)
        found = LifecycleStopService.inventory_in(tx, parent)['providers']
        assert [r['key'] for r in found] == [target]


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['natural', 'deadline', 'explicit_cancel'])
async def test_drain_observes_original_provider_until_completion_or_cutoff(repository, tmp_path, clock, finish):
    from tests.orchestration.graphs.test_work_remediation import drain
    record, target, provider = await pending_provider(repository, tmp_path)
    runtime = safety_worker(repository, record, target, provider)
    successor = supersede(repository)
    await drain(runtime)
    with repository.transaction() as tx:
        current = LifecycleService.load_in(tx, record.key.lifecycle_id)
        assert current.stop_reason is None
        assert current.phase not in {'cancelling', 'cancelled', 'superseded'}
        assert not tx.get('agent_provider_requests', target)['body']['cancel_accepted']
    assert provider.stops == 0
    assert stop_status(repository, record.key.lifecycle_id)['status'] == 'waiting_dependency'
    if finish == 'natural':
        provider.current = receipt(provider.submissions[0], state='succeeded', children_stopped=True, receipt_id='natural-completion')
        clock.advance(1)
    elif finish == 'deadline':
        clock.advance(provider.submissions[0].deadline - clock.now)
    else:
        RunService(repository, PlanCompiler([])).cancel('override-drain', 'run/a', 'stop now')
        clock.advance(1)
    await drain(runtime)
    assert len(provider.submissions) == 1
    if finish == 'natural':
        assert provider.stops == 0
        with repository.transaction() as tx:
            assert LifecycleService.load_in(tx, record.key.lifecycle_id).phase == 'superseded'
            assert tx.get('runs', 'run/a')['body']['successor_run_id'] == successor.run_id
            assert not [r for r in tx.scan('outbox') if r['body']['kind'] == 'repair_recovery_required']
        assert stop_status(repository, record.key.lifecycle_id)['status'] == 'closed'
    else:
        assert provider.stops == 1
        with repository.transaction() as tx:
            current = LifecycleService.load_in(tx, record.key.lifecycle_id)
            assert current.stop_reason == ('deadline' if finish == 'deadline' else 'explicit_cancel')
            assert current.phase not in {'cancelled', 'superseded'}


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['natural', 'deadline', 'explicit_cancel'])
async def test_drain_site_queries_original_then_cleans_without_business_recovery(repository, clock, finish):
    from tests.orchestration.unit.test_lifecycle_reconciliation import uncertain, install, Runner
    from tests.orchestration.graphs.test_work_remediation import drain
    from sandbox_graph.runtime.runner import RunnerStatus
    life, order, _ = await uncertain(repository, resources=True)
    gateway = Runner(RunnerStatus(state='running', authoritative=True))
    _, runtime = install(repository, life, order, gateway)
    supersede(repository)
    await drain(runtime)
    assert [c[0] for c in gateway.calls] == ['query']
    assert stop_status(repository, life)['status'] == 'waiting_dependency'
    with repository.transaction() as tx:
        assert LifecycleService.load_in(tx, life).stop_reason is None
        original_outcome = deepcopy(tx.get('repair_outcomes', order))
    if finish == 'natural':
        gateway.status = RunnerStatus(state='completed', authoritative=True)
        clock.advance(1)
    elif finish == 'deadline':
        clock.advance(100)
    else:
        RunService(repository, PlanCompiler([])).cancel('override-drain', 'run/a', 'stop now')
        clock.advance(1)
    await drain(runtime)
    with repository.transaction() as tx:
        assert tx.get('repair_outcomes', order) == original_outcome
        assert not [r for r in tx.scan('outbox') if r['body']['kind'] in {
            'repair_recovery_required', 'repair_retry_required'}]
        if finish == 'natural':
            assert LifecycleService.load_in(tx, life).phase == 'superseded'
            assert tx.get('resources', 'resource/a')['body']['status'] == 'closed'
            assert not tx.scan('resource_leases')
        else:
            assert LifecycleService.load_in(tx, life).stop_reason == (
                'deadline' if finish == 'deadline' else 'explicit_cancel')
    assert [c[0] for c in gateway.calls] == (['query', 'query', 'cleanup']
        if finish == 'natural' else ['query', 'query', 'stop'])


@pytest.mark.asyncio
async def test_parent_stop_waits_without_adopting_child_provider(repository, tmp_path):
    record, target, _ = await pending_provider(repository, tmp_path)
    parent = record.model_dump()
    parent['key'].update(scope_kind='run', scope_ref='run/a', lifecycle_id='parent/life')
    parent.update(operation_kind=None, operation_refs=(), isolation_refs=())
    parent = LifecycleRecord.model_validate(parent)
    with repository.transaction() as tx:
        tx.put('agent_lifecycles', 'parent/life', parent.model_dump(mode='json'))
        LifecycleStopService.request_in(tx, request_id='stop-parent', run_ref='run/a',
            plan_ref=parent.plan_ref, graph_ref=parent.graph_ref, reason='stop', policy='cancel')
        intent = next(r for r in tx.scan('lifecycle_stop_requests') if r['body']['lifecycle_id'] == 'parent/life')
        budgets = deepcopy(tx.scan('lifecycle_budgets'))
        assert not LifecycleStopService.consume_in(tx, intent['key'], {'request_ref': intent['key']})
        assert LifecycleService.load_in(tx, 'parent/life').operation_refs == ()
        assert tx.scan('lifecycle_budgets') == budgets
        assert target in tx.get('lifecycle_stop_status', intent['key'])['body']['remaining']['providers']
        assert not tx.scan('lifecycle_stop_proofs')


@pytest.mark.parametrize('missing', ['work_lease_valid', 'effect_known', 'disposition_closed', 'successor_bound', None])
def test_version_drained_requires_fenced_closed_disposition_and_successor(missing):
    from tests.orchestration.unit.test_lifecycle_state import record, event, guards
    from sandbox_graph.domain.agent_lifecycle import transition_lifecycle, InvalidLifecycleTransition
    original = record()
    evt = event('version_drained', source='trusted_run_control')
    if missing:
        with pytest.raises(InvalidLifecycleTransition):
            transition_lifecycle(original, evt, guards(**{missing: False}))
    else:
        result = transition_lifecycle(original, evt, guards())
        assert result.phase == 'superseded'
        assert result.stop_reason is None
        assert result.node_result == original.node_result


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['reservation', 'network'])
async def test_drain_guards_direct_stop_at_reservation_and_io_boundary(repository, boundary):
    from tests.orchestration.unit.test_lifecycle_reconciliation import uncertain, install, Runner
    from sandbox_graph.runtime.tasks import TaskBackend
    from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
    life, order, _ = await uncertain(repository)
    gateway = Runner()
    service, runtime = install(repository, life, order, gateway)
    runtime.pump_outbox()
    job = TaskBackend(repository).claim('boundary-check', 30)
    assert job['job_kind'] == 'reconcile'
    with repository.transaction() as tx:
        token = tx.acquire_thread(job['thread_id'], 'boundary-check', 30)
    LifecycleService(repository).claim(life, token=token)
    with repository.transaction() as tx:
        LifecycleSafetyService.acquire_work_in(tx, 'work/a', token, purpose='reconcile')
        if boundary == 'network':
            call_ref, fresh = service.prepare_call_in(tx, job, token, 'stop', 'op/original')
            assert fresh
    supersede(repository)
    with repository.transaction() as tx:
        budgets = deepcopy(tx.scan('lifecycle_budgets'))
    with pytest.raises(Conflict, match='drain_waits_for_original_deadline'):
        with repository.transaction() as tx:
            if boundary == 'reservation':
                service.prepare_call_in(tx, job, token, 'stop', 'op/original')
            else:
                service.network_in(tx, job, token, call_ref, start=True)
    with repository.transaction() as tx:
        assert tx.scan('lifecycle_budgets') == budgets
        assert not any(r['body']['network_started'] for r in tx.scan('reconciliation_calls'))
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('proof', ['same_graph', 'missing', 'wrong_predecessor', 'wrong_policy'])
async def test_drain_closure_requires_the_original_version_activation(repository, tmp_path, clock, proof):
    from tests.orchestration.graphs.test_work_remediation import drain
    record, target, provider = await pending_provider(repository, tmp_path)
    runtime = safety_worker(repository, record, target, provider)
    supersede(repository, same_graph=proof == 'same_graph')
    await drain(runtime)
    with repository.transaction() as tx:
        activation = tx.get('revision_activations', 'next-version')['body']
        if proof == 'missing':
            tx.delete('revision_activations', 'next-version')
        elif proof == 'wrong_predecessor':
            tx.put('revision_activations', 'next-version', activation | {
                'predecessor': activation['predecessor'] | {'run_ref': 'another/run'}})
        elif proof == 'wrong_policy':
            tx.put('revision_activations', 'next-version', activation | {'policy': 'cancel'})
    provider.current = receipt(provider.submissions[0], state='succeeded', children_stopped=True,
        receipt_id='original-finished')
    clock.advance(1)
    await drain(runtime)
    status = stop_status(repository, record.key.lifecycle_id)
    with repository.transaction() as tx:
        current = LifecycleService.load_in(tx, record.key.lifecycle_id)
        if proof == 'same_graph':
            assert status['status'] == 'closed'
            assert current.phase == 'superseded'
        else:
            assert status['status'] == 'waiting_dependency'
            assert 'successor' in status['reason']
            assert current.phase != 'superseded'
            assert not [r for r in tx.scan('lifecycle_stop_proofs')
                if r['body']['lifecycle_id'] == record.key.lifecycle_id]
    assert provider.stops == 0
    assert len(provider.submissions) == 1


@pytest.mark.parametrize('condition', ['inflight', 'explicit_cancel'])
def test_version_drained_cannot_override_live_effects_or_explicit_cancel(condition):
    from tests.orchestration.unit.test_lifecycle_state import record, event, guards
    from sandbox_graph.domain.agent_lifecycle import transition_lifecycle, InvalidLifecycleTransition
    original = record('cancelling', stop_reason='explicit_cancel', terminal_intent='cancelled') if condition == 'explicit_cancel' else record()
    with pytest.raises(InvalidLifecycleTransition):
        transition_lifecycle(original, event('version_drained'), guards(effects_in_flight=condition == 'inflight'))
