"""ALU-21: actual thread ownership, independent of durable task claims."""
import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.persistence.repository import Conflict, StaleOwner
from sandbox_graph.runtime.tasks import TaskBackend, JobSpec
from sandbox_graph.runtime.worker import RemedyJobHandler
from tests.orchestration.conftest import FakeClock, MemoryRepository, MemoryTransaction


def setup_job():
    repo = MemoryRepository(FakeClock())
    # Persist a pre-migration order explicitly: the legacy order API must no
    # longer mint jobs without lifecycle authority. These tests verify draining
    # already queued legacy work and its actual thread/job lease ownership.
    with repo.transaction() as tx:
        tx.put('remedy_orders', 'legacy', {
            'owner_agent_id': 'agent:work:w', 'work_ref': 'w', 'node_id': 'N09',
            'run_ref': 'r', 'plan_ref': 'p', 'graph_ref': 'g', 'input_refs': {},
            'action_ref': 'resource.cleanup', 'attempt': 1}, immutable=True)
        TaskBackend.enqueue_in(tx, JobSpec(job_id='legacy-job', job_kind='remedy',
            scope_ref='w', thread_id='legacy-thread', activation_id='legacy-activation',
            dedupe_key='legacy-job', due_at=tx.now, deadline=2000, run_id='r',
            payload={'remedy_ref': 'legacy'}))
    return repo, TaskBackend(repo).claim('worker', 90)


def make_handler(repo, action, factory=None):
    return RemedyJobHandler(repo, factory or (lambda token, activation: {'N09': action}),
                            lease_seconds=30, segment_seconds=20)


@pytest.mark.asyncio
async def test_remedy_acquires_random_thread_lease_and_releases_it(monkeypatch):
    repo, job = setup_job()
    original = MemoryTransaction.acquire_thread
    acquired = []
    def independent(tx, thread, owner, ttl):
        token = replace(original(tx, thread, owner, ttl), lease_id=str(uuid4()), owner_epoch=7)
        tx.repo.leases[thread] = token
        acquired.append(token)
        return token
    monkeypatch.setattr(MemoryTransaction, 'acquire_thread', independent)
    seen = []
    async def action(context):
        seen.append((context.owner_epoch, context.lease_id))
        return NodeResult(execution_status='succeeded')
    result = await make_handler(repo, action)(job)
    assert result['execution_status'] == 'succeeded'
    assert seen == [(7, acquired[0].lease_id)]
    assert repo.leases[job['thread_id']].lease_until <= repo.clock.now


@pytest.mark.asyncio
async def test_remedy_does_not_reuse_another_owners_active_thread():
    repo, job = setup_job()
    with repo.transaction() as tx:
        token = tx.acquire_thread(job['thread_id'], 'other-worker', 60)
    async def action(context):
        pytest.fail('must not execute without acquiring ownership')
    with pytest.raises(Conflict):
        await make_handler(repo, action)(job)
    assert repo.leases[job['thread_id']] == token


@pytest.mark.asyncio
@pytest.mark.parametrize('loss', ['thread_expired', 'claim_replaced'])
async def test_ownership_loss_cannot_publish_outcome_or_continuation(loss):
    repo, job = setup_job()
    entered = []
    async def action(context):
        entered.append(True)
        if loss == 'thread_expired':
            repo.clock.advance(31)
        else:
            with repo.transaction() as tx:
                row = tx.get('jobs', job['job_id'])
                tx.put('jobs', job['job_id'], row['body'] | {'owner_epoch': job['owner_epoch'] + 1})
        return NodeResult(execution_status='succeeded')
    with pytest.raises(StaleOwner):
        await make_handler(repo, action)(job)
    assert entered == [True]
    with repo.transaction() as tx:
        assert tx.scan('receipts') == []
        assert not [r for r in tx.scan('agent_remedies') if r['key'].endswith(':outcome')]
        assert not [r for r in tx.scan('jobs') if r['body']['job_kind'] == 'work']


@pytest.mark.asyncio
async def test_factory_failure_releases_acquired_lease():
    repo, job = setup_job()
    def factory(token, activation):
        raise ValueError('invalid configuration')
    with pytest.raises(ValueError, match='invalid configuration'):
        await make_handler(repo, None, factory)(job)
    assert repo.leases[job['thread_id']].lease_until <= repo.clock.now


@pytest.mark.asyncio
async def test_cancellation_releases_lease():
    repo, job = setup_job()
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def action(context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    task = asyncio.create_task(make_handler(repo, action)(job))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert repo.leases[job['thread_id']].lease_until <= repo.clock.now
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_renewal_failure_cancels_execution_without_publishing(monkeypatch):
    repo, job = setup_job()
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def tick(seconds):
        await entered.wait()
    def lost(tx, token, ttl):
        raise StaleOwner('renewal lost')
    monkeypatch.setattr(asyncio, 'sleep', tick)
    monkeypatch.setattr(MemoryTransaction, 'renew_thread', lost)
    async def action(context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    with pytest.raises(StaleOwner, match='renewal lost'):
        await asyncio.wait_for(make_handler(repo, action)(job), 1)
    assert stopped.is_set()
    with repo.transaction() as tx:
        assert not [r for r in tx.scan('agent_remedies') if r['key'].endswith(':outcome')]


@pytest.mark.asyncio
async def test_takeover_is_not_released_by_the_old_remedy():
    repo, job = setup_job()
    replacement = []
    async def action(context):
        repo.clock.advance(31)
        with repo.transaction() as tx:
            replacement.append(tx.acquire_thread(job['thread_id'], 'new-owner', 60))
        return NodeResult(execution_status='succeeded')
    with pytest.raises(StaleOwner):
        await make_handler(repo, action)(job)
    assert repo.leases[job['thread_id']] == replacement[0]
    assert replacement[0].lease_until > repo.clock.now


@pytest.mark.asyncio
async def test_successful_script_with_rejected_evidence_does_not_continue():
    repo, job = setup_job()
    async def action(context):
        return NodeResult(execution_status='succeeded', data_status='valid')  # no evidence
    result = await make_handler(repo, action)(job)
    assert result['execution_status'] == 'blocked'
    with repo.transaction() as tx:
        assert not [r for r in tx.scan('jobs') if r['body']['job_kind'] == 'work']
        receipt = tx.get('receipts', result['output']['receipt_ref'])
        assert receipt['body']['result']['execution_status'] == 'succeeded'
        outcome = tx.get('agent_remedies', job['payload']['remedy_ref'] + ':outcome')
        assert outcome['body']['postcheck_status'] == 'rejected'
