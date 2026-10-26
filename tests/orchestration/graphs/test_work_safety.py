"""Real graph activation must share work ownership with repairs."""
import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.driver import Activation, Driver
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService


def setup_work(repository):
    with repository.transaction() as tx:
        tx.put('runs', 'r', {'graph_ref': 'g', 'active_plan_ref': 'p'})
        tx.put('work_instances', 'w', {'work_id': 'w', 'run_id': 'r', 'plan_ref': 'p',
                                     'graph_ref': 'g', 'contract_ref': 'c', 'status': 'dispatched'})
    return Activation(activation_id='a', run_id='r', thread_id='t', scope='w',
                      plan_ref='p', graph_ref='g', input_refs={}, lease_seconds=30, segment_seconds=20)


def make_driver(repository, handler):
    return Driver(repository, {f'N{i:02}': handler for i in range(11)},
                  lambda token: InMemorySaver(), build_work)


@pytest.mark.asyncio
async def test_active_repair_mutex_prevents_original_graph_from_starting(repository):
    activation = setup_work(repository)
    with repository.transaction() as tx:
        owner = tx.acquire_thread('repair', 'repair-worker', 30)
        LifecycleSafetyService.acquire_work_in(tx, 'w', owner, purpose='reconcile')
    visited = []
    async def handler(context):
        visited.append(context.node_id)
        return NodeResult(execution_status='succeeded')
    with pytest.raises(Conflict, match='work already executing'):
        await make_driver(repository, handler).start(activation)
    assert visited == []
    with repository.transaction() as tx:
        assert tx.scan('receipts') == []
        assert tx.get('activations', 'a') is None


@pytest.mark.asyncio
async def test_actual_graph_closure_produces_reusable_disposition_facts(repository):
    activation = setup_work(repository)
    async def handler(context):
        with repository.transaction() as tx:
            owner = tx.get('work_execution_leases', 'w')
            assert owner and owner['body']['owner']['thread_id'] == 't'
        return NodeResult(execution_status='succeeded', cleanup_status='complete')
    result = await make_driver(repository, handler).start(activation)
    assert len(result['result_refs']) == 11
    with repository.transaction() as tx:
        assert tx.get('work_execution_leases', 'w') is None
        facts = tx.get('work_safety_facts', 'w')['body']
        assert facts['receipt_refs'] == {n: result['result_refs'][n] for n in ('N07', 'N09', 'N10')}
        token = tx.acquire_thread('supervisor', 'supervisor', 30)
        LifecycleSafetyService.acquire_work_in(tx, 'w', token, purpose='diagnosis')
        proof = LifecycleSafetyService.produce_disposition_in(tx, 'w', token, ttl=5)
        receipt = LifecycleSafetyService.verify_disposition_in(tx, proof, 'w', token)
        assert receipt.stopped and receipt.sampler_status == 'not_started'


@pytest.mark.asyncio
async def test_cancelled_activation_releases_execution_but_does_not_invent_closure(repository):
    activation = setup_work(repository)
    entered = asyncio.Event()
    async def handler(context):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(make_driver(repository, handler).start(activation))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with repository.transaction() as tx:
        assert tx.get('work_execution_leases', 'w') is None
        facts = tx.get('work_safety_facts', 'w')
        assert not facts or facts['body']['attempt_closed'] is False
        token = tx.acquire_thread('supervisor', 'supervisor', 30)
        LifecycleSafetyService.acquire_work_in(tx, 'w', token, purpose='diagnosis')
        proof = LifecycleSafetyService.produce_disposition_in(tx, 'w', token, ttl=5)
        with pytest.raises(Conflict, match='not conclusively closed'):
            LifecycleSafetyService.verify_disposition_in(tx, proof, 'w', token)


@pytest.mark.asyncio
async def test_new_attempt_invalidates_prior_closure_before_any_action(repository):
    activation = setup_work(repository)
    async def first(context):
        return NodeResult(execution_status='succeeded')
    await make_driver(repository, first).start(activation)
    seen = []
    async def second(context):
        with repository.transaction() as tx:
            facts = tx.get('work_safety_facts', 'w')['body']
            seen.append((facts['activation_id'], facts['attempt_closed'], facts['admission_status']))
        return NodeResult(execution_status='succeeded')
    await make_driver(repository, second).start(activation.model_copy(update={'activation_id': 'a2', 'thread_id': 't2'}))
    assert seen and all(item == ('a2', False, 'open') for item in seen)


@pytest.mark.asyncio
async def test_running_sampler_cannot_be_hidden_by_successful_node_receipts(repository):
    activation = setup_work(repository)
    async def handler(context):
        if context.node_id == 'N04':
            with repository.transaction() as tx:
                tx.put('observations', 'sampler', {'scope': 'w', 'status': 'running'})
        return NodeResult(execution_status='succeeded')
    await make_driver(repository, handler).start(activation)
    with repository.transaction() as tx:
        token = tx.acquire_thread('supervisor', 'supervisor', 30)
        LifecycleSafetyService.acquire_work_in(tx, 'w', token, purpose='diagnosis')
        proof = LifecycleSafetyService.produce_disposition_in(tx, 'w', token, ttl=5)
        with pytest.raises(Conflict, match='not conclusively closed'):
            LifecycleSafetyService.verify_disposition_in(tx, proof, 'w', token)


@pytest.mark.asyncio
async def test_worker_completion_revision_keeps_actual_attempt_facts_usable(repository):
    from sandbox_graph.runtime.tasks import TaskBackend, JobSpec
    from sandbox_graph.runtime.worker import Worker, WorkerPolicy, GraphJobHandler
    setup_work(repository)
    with repository.transaction() as tx:
        tx.put('work_instances', 'w', tx.get('work_instances', 'w')['body'] | {'group_id': 'group'})
    async def handler(context):
        return NodeResult(execution_status='succeeded', cleanup_status='complete')
    TaskBackend(repository).enqueue(JobSpec(job_id='j', job_kind='work', scope_ref='w',
        thread_id='t', activation_id='a', dedupe_key='j', due_at=1000, deadline=1100,
        run_id='r', payload={'plan_ref': 'p', 'graph_ref': 'g'}))
    graph_handler = GraphJobHandler(repository, build_work, lambda token: InMemorySaver(),
        lambda token, activation: {f'N{i:02}': handler for i in range(11)},
        lease_seconds=30, segment_seconds=20)
    worker = Worker(repository, {'work': graph_handler},
                    policy=WorkerPolicy(claim_seconds=30, job_seconds=20, poll_seconds=1))
    await worker.run_once()
    worker.pump_outbox()
    with repository.transaction() as tx:
        work = tx.get('work_instances', 'w')
        assert work['body']['status'] == 'blocked'  # No delivery manifest; attempt still ended.
        token = tx.acquire_thread('supervisor', 'supervisor', 30)
        LifecycleSafetyService.acquire_work_in(tx, 'w', token, purpose='diagnosis')
        proof = LifecycleSafetyService.produce_disposition_in(tx, 'w', token, ttl=5)
        assert LifecycleSafetyService.verify_disposition_in(tx, proof, 'w', token).stopped


@pytest.mark.asyncio
async def test_unknown_repair_without_resource_domain_blocks_original_graph(repository):
    activation = setup_work(repository)
    with repository.transaction() as tx:
        tx.put('repair_isolations', 'repair/unknown', {'work_ref': 'w',
            'order_ref': 'repair/unknown', 'operation_refs': [], 'reason': 'operation_binding_missing'})
    seen = []
    async def handler(context):
        seen.append(context.node_id)
        return NodeResult(execution_status='succeeded')
    with pytest.raises(Conflict, match='unresolved repair isolation'):
        await make_driver(repository, handler).start(activation)
    assert seen == []


@pytest.mark.asyncio
async def test_unknown_repair_cannot_gain_closed_proof_from_old_completed_attempt(repository):
    activation = setup_work(repository)
    async def handler(context):
        return NodeResult(execution_status='succeeded', cleanup_status='complete')
    await make_driver(repository, handler).start(activation)
    with repository.transaction() as tx:
        tx.put('repair_isolations', 'repair/unknown', {'work_ref': 'w',
            'order_ref': 'repair/unknown', 'operation_refs': [], 'reason': 'operation_binding_missing'})
        token = tx.acquire_thread('supervisor', 'supervisor', 30)
        LifecycleSafetyService.acquire_work_in(tx, 'w', token, purpose='diagnosis')
        proof = LifecycleSafetyService.produce_disposition_in(tx, 'w', token, ttl=5)
        with pytest.raises(Conflict, match='not conclusively closed'):
            LifecycleSafetyService.verify_disposition_in(tx, proof, 'w', token)
        assert not LifecycleSafetyService.verify_disposition_in(tx, proof, 'w', token, require_closed=False).stopped


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['cleanup', 'reconcile'])
async def test_recovery_jobs_share_original_work_mutex(repository, kind):
    from sandbox_graph.runtime.worker import RecoveryJobHandler
    from sandbox_graph.runtime.runner import RunnerStatus
    setup_work(repository)
    with repository.transaction() as tx:
        owner = tx.acquire_thread('original', 'worker', 30)
        LifecycleSafetyService.acquire_work_in(tx, 'w', owner)
        tx.put('operations', 'op', {'run_id': 'r', 'scope': 'w', 'phase': 'terminal',
            'status': {'state': 'completed', 'authoritative': True}})
    calls = []
    class Gateway:
        async def reconcile(self, *args):
            calls.append(args)
            return RunnerStatus(state='completed', authoritative=True)
    handler = RecoveryJobHandler(repository, Gateway(), lease_seconds=30)
    with pytest.raises(Conflict, match='work already executing'):
        await handler({'thread_id': 'recovery', 'scope_ref': 'w', 'run_id': 'r',
                       'job_kind': kind, 'payload': {}})
    assert calls == []
    with repository.transaction() as tx:
        tx.assert_fence(owner)
