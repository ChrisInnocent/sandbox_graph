"""Real modern order → claimed worker → receipt/postcheck/settlement (memory unit)."""
import asyncio
from copy import deepcopy

import pytest

from sandbox_graph.domain.models import ErrorRecord, NodeResult
from sandbox_graph.persistence.repository import Conflict, StaleOwner
from sandbox_graph.runtime.runner import OperationService, RunnerStatus, RunnerCollection
from sandbox_graph.runtime.tasks import TaskBackend
from sandbox_graph.runtime.worker import RemedyJobHandler
from sandbox_graph.services.agent_lifecycle import LifecycleService
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from tests.orchestration.unit.test_remediation_requests import ready


def modern_job(repository, *, allowance=1, now=1000.):
    service, request, old_token = ready(repository,now=now)
    with repository.transaction() as tx:
        # Distinct precursor result refs must reach the controlled node context.
        source = tx.get('lifecycle_sources', request.lifecycle_id)
        tx.put('lifecycle_sources', request.lifecycle_id, source['body'] | {'result_refs': {'N00': 'receipt/zero'}})
        if allowance != 1:
            for row in tx.scan('lifecycle_budgets'):
                body = row['body']
                body['limits']['repair_attempts'] = allowance
                tx.put('lifecycle_budgets', row['key'], body)
    ordered = service.request_remediation(request, principal='supervisor/a', token=old_token)
    with repository.transaction() as tx:
        LifecycleSafetyService.release_work_in(tx, 'work/a', old_token)
        tx.release_thread(old_token)
    return request, ordered, TaskBackend(repository).claim('worker/new', 80)


def worker(repository, action):
    return RemedyJobHandler(repository, lambda token, activation: {'N01': action},
                            lease_seconds=30, segment_seconds=20)


def closed_operation(repository, operation='op/a'):
    with repository.transaction() as tx:
        tx.put('operations', operation, {'scope': 'work/a', 'run_id': 'run/a',
            'thread_id': 'work-thread', 'phase': 'terminal',
            'status': RunnerStatus(state='completed', authoritative=True).model_dump(mode='json')})


@pytest.mark.asyncio
async def test_last_allowed_attempt_executes_real_node_preserves_refs_and_requests_recovery(repository):
    request, order, job = modern_job(repository)
    closed_operation(repository)
    calls = []
    async def action(context):
        calls.append(context)
        return NodeResult(execution_status='succeeded', operation_refs=('op/a',))
    original = deepcopy(repository.rows[('receipts', 'receipt/a')])
    returned = await worker(repository, action)(job)
    assert returned['execution_status'] == 'succeeded'
    assert returned['output']['phase'] == 'reentering'
    assert 'resume_from' not in returned['output']
    assert len(calls) == 1
    assert calls[0].input_refs == {'fixture': 'fixture/a'}
    assert calls[0].result_refs == {'N00': 'receipt/zero'}
    assert repository.rows[('receipts', 'receipt/a')] == original
    with repository.transaction() as tx:
        assert LifecycleService.load_in(tx, request.lifecycle_id).phase == 'reentering'
        assert LifecycleBudgetService.snapshot_in(tx, request.lifecycle_id)['lifecycle']['spent']['repair_attempts'] == 1
        assert len(tx.scan('repair_outcomes')) == len(tx.scan('budget_settlements')) == 1
        assert any(r['body']['kind'] == 'repair_recovery_required' for r in tx.scan('outbox'))
        assert not [r for r in tx.scan('jobs') if r['body']['job_kind'] == 'work']
        assert not tx.get('work_execution_leases', 'work/a')
    assert repository.leases[job['thread_id']].lease_until <= repository.clock.now
    assert await worker(repository, action)(job) == returned
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_exit_zero_and_review_accept_without_action_evidence_cannot_pass(repository):
    request, order, job = modern_job(repository)
    async def action(context):
        return NodeResult(execution_status='succeeded')
    returned = await worker(repository, action)(job)
    assert returned['execution_status'] == 'blocked'
    with repository.transaction() as tx:
        outcome = tx.get('repair_outcomes', order['order_ref'])['body']
        assert outcome['postcheck_status'] == 'rejected'
        assert LifecycleService.load_in(tx, request.lifecycle_id).phase == 'blocked'
        assert tx.get('budget_settlements', 'repair-budget:repair/a')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,key,patch', [
    ('repair_authorizations', 'auth/a', {'revoked': True}),
    ('repair_authorizations', 'auth/a', {'expires_at': 1000.0}),
    ('repair_authorizations', 'auth/a', {'permissions': []}),
    ('dispatch_gates', 'run/a', {'open': False}),
    ('dispatch_gates', 'run/a', {'generation': 4}),
    ('work_instances', 'work/a', {'status': 'changed'}),
    ('work_safety_facts', 'work/a', {'sampler_status': 'running'}),
])
async def test_current_authority_gate_work_revision_and_disposition_are_rechecked(repository, kind, key, patch):
    request, order, job = modern_job(repository)
    with repository.transaction() as tx:
        tx.put(kind, key, tx.get(kind, key)['body'] | patch)
    calls = []
    async def action(context):
        calls.append(context)
        return NodeResult(execution_status='succeeded')
    with pytest.raises((Conflict, PermissionError)):
        await worker(repository, action)(job)
    assert calls == []
    with repository.transaction() as tx:
        assert tx.scan('repair_outcomes') == tx.scan('repair_executions') == []


@pytest.mark.asyncio
async def test_failure_with_trusted_next_allowance_enters_backoff(repository):
    request, order, job = modern_job(repository, allowance=2)
    async def action(context):
        return NodeResult(execution_status='failed', error=ErrorRecord(code='TEMPORARY',
            retry_class='bounded', effect_certainty='none', observed_at=1000.0))
    returned = await worker(repository, action)(job)
    with repository.transaction() as tx:
        record = LifecycleService.load_in(tx, request.lifecycle_id)
        assert record.phase == 'backoff'
        assert record.next_due_at == 1001.0
        assert len(tx.scan('repair_failure_fingerprints')) == 1
    assert returned['output']['phase'] == 'backoff'


@pytest.mark.asyncio
@pytest.mark.parametrize('loss', ['thread', 'job', 'gate', 'authorization'])
async def test_loss_during_action_cannot_publish_outcome(repository, loss):
    request, order, job = modern_job(repository)
    closed_operation(repository)
    async def action(context):
        if loss == 'thread':
            repository.clock.advance(31)
        else:
            with repository.transaction() as tx:
                if loss == 'job':
                    row = tx.get('jobs', job['job_id'])
                    tx.put('jobs', job['job_id'], row['body'] | {'owner_epoch': 9})
                elif loss == 'gate':
                    tx.put('dispatch_gates', 'run/a', {'open': False, 'generation': 3})
                else:
                    row = tx.get('repair_authorizations', 'auth/a')
                    tx.put('repair_authorizations', 'auth/a', row['body'] | {'revoked': True})
        return NodeResult(execution_status='succeeded', operation_refs=('op/a',))
    with pytest.raises((StaleOwner, Conflict, PermissionError)):
        await worker(repository, action)(job)
    with repository.transaction() as tx:
        assert tx.scan('repair_outcomes') == tx.scan('budget_settlements') == []


@pytest.mark.asyncio
async def test_outcome_crash_rolls_back_settlement_transition_and_outbox_then_replay_uses_receipt(repository, monkeypatch):
    from tests.orchestration.conftest import MemoryTransaction
    request, order, job = modern_job(repository)
    closed_operation(repository)
    calls = []
    async def action(context):
        calls.append(context)
        return NodeResult(execution_status='succeeded', operation_refs=('op/a',))
    original = MemoryTransaction.put
    def cut(tx, kind, key, body, *args, **kwargs):
        if kind == 'outbox' and body.get('kind') == 'repair_recovery_required':
            raise RuntimeError('outcome commit cut')
        return original(tx, kind, key, body, *args, **kwargs)
    monkeypatch.setattr(MemoryTransaction, 'put', cut)
    with pytest.raises(RuntimeError, match='outcome commit cut'):
        await worker(repository, action)(job)
    with repository.transaction() as tx:
        assert tx.scan('repair_outcomes') == tx.scan('budget_settlements') == []
        assert LifecycleService.load_in(tx, request.lifecycle_id).phase == 'remediating'
    monkeypatch.setattr(MemoryTransaction, 'put', original)
    assert (await worker(repository, action)(job))['execution_status'] == 'succeeded'
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('with_operation', [False, True])
async def test_started_action_without_receipt_is_never_executed_again(repository, with_operation):
    from sandbox_graph.services.repair_outcomes import RepairOutcomeService
    request, order, job = modern_job(repository)
    if with_operation:
        closed_operation(repository)
    # Durable cut immediately after marking execution started, before any receipt.
    with repository.transaction() as tx:
        token = tx.acquire_thread(job['thread_id'], 'interrupted', 30)
    LifecycleService(repository).claim(request.lifecycle_id, token=token)
    with repository.transaction() as tx:
        LifecycleSafetyService.acquire_work_in(tx, 'work/a', token)
        RepairOutcomeService.start_in(tx, job, tx.get('repair_orders', order['order_ref'])['body'], token, ttl=20)
        LifecycleSafetyService.release_work_in(tx, 'work/a', token)
        tx.release_thread(token)
    calls = []
    async def action(context):
        calls.append(context)
        return NodeResult(execution_status='succeeded')
    returned = await worker(repository, action)(job)
    assert calls == []
    assert returned['output']['postcheck_status'] == 'unknown'
    with repository.transaction() as tx:
        record = LifecycleService.load_in(tx, request.lifecycle_id)
        assert record.phase == ('reconciling' if with_operation else 'remediating')
        assert record.operation_refs == (('op/a',) if with_operation else ())
        assert record.operation_kind == ('site' if with_operation else None)
        assert len(tx.scan('budget_settlements')) == len(tx.scan('repair_isolations')) == 1
        intent = tx.get('outbox', 'repair-outcome:' + order['order_ref'])['body']
        assert intent['kind'] == 'repair_reconciliation_required'
        assert intent['operation_refs'] == (['op/a'] if with_operation else [])
    assert await worker(repository, action)(job) == returned
    assert calls == []


@pytest.mark.asyncio
async def test_actual_unknown_operation_is_quarantined_and_never_reissued(repository):
    request, order, job = modern_job(repository)
    calls = []
    async def action(context):
        calls.append(context)
        with repository.transaction() as tx:
            tx.put('operations', 'op/original', {'scope': 'work/a', 'run_id': 'run/a',
                'thread_id': context.thread_id, 'phase': 'uncertain',
                'status': RunnerStatus(state='unknown').model_dump(mode='json')})
            tx.put('resource_leases', 'domain/a', {'scope': 'work/a', 'status': 'active'})
        return NodeResult(execution_status='failed', operation_refs=('op/original',),
            cleanup_status='unknown', error=ErrorRecord(code='REMOTE_UNKNOWN',
                effect_certainty='unknown', observed_at=1000.0))
    returned = await worker(repository, action)(job)
    assert returned['output']['phase'] == 'reconciling'
    with repository.transaction() as tx:
        assert tx.get('resource_leases', 'domain/a')['body']['status'] == 'quarantine'
        record = LifecycleService.load_in(tx, request.lifecycle_id)
        assert record.operation_refs == ('op/original',)
        assert record.resume_phase == 'remediating'
        assert tx.get('repair_outcomes', order['order_ref'])['body']['outcome']['effect_certainty'] == 'unknown'
    assert await worker(repository, action)(job) == returned
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_active_work_execution_owner_blocks_repair(repository):
    request, order, job = modern_job(repository)
    with repository.transaction() as tx:
        owner = tx.acquire_thread('other-work-thread', 'other', 90)
        LifecycleSafetyService.acquire_work_in(tx, 'work/a', owner)
    async def action(context):
        pytest.fail('shared work mutex must stop the action')
    with pytest.raises(Conflict, match='work already executing'):
        await worker(repository, action)(job)
    with repository.transaction() as tx:
        assert tx.get('work_execution_leases', 'work/a')['body']['owner']['lease_id'] == owner.lease_id
        assert tx.scan('repair_executions') == tx.scan('repair_outcomes') == []


@pytest.mark.asyncio
async def test_current_owner_produces_new_disposition_and_thread_epoch_is_not_job_epoch(repository, monkeypatch):
    from dataclasses import replace
    from tests.orchestration.conftest import MemoryTransaction
    request, order, job = modern_job(repository)
    closed_operation(repository)
    acquire = MemoryTransaction.acquire_thread
    def independent(tx, thread, owner, ttl):
        token = replace(acquire(tx, thread, owner, ttl), owner_epoch=13)
        tx.repo.leases[thread] = token
        return token
    monkeypatch.setattr(MemoryTransaction, 'acquire_thread', independent)
    seen = []
    async def action(context):
        seen.append(context.owner_epoch)
        return NodeResult(execution_status='succeeded', operation_refs=('op/a',))
    await worker(repository, action)(job)
    assert seen == [13]
    with repository.transaction() as tx:
        producer = tx.get('lifecycle_owners', request.lifecycle_id)['body']
        assert producer['owner_epoch'] == 13
        current = tx.get('lifecycle_dispositions', request.lifecycle_id)['body']['receipt_ref']
        assert current != tx.get('repair_orders', order['order_ref'])['body']['disposition_ref']
        assert tx.get('resource_disposition_receipts', current)['body']['producer'] == producer


@pytest.mark.asyncio
async def test_same_strategy_stops_at_trusted_no_progress_limit_with_budget_remaining(repository):
    request, order, job = modern_job(repository, allowance=2)
    with repository.transaction() as tx:
        frozen = tx.get('repair_orders', order['order_ref'])['body']
        record = LifecycleService.load_in(tx, request.lifecycle_id)
        tx.put('repair_failure_fingerprints', 'previous/order', {
            'lineage_budget_ref': 'lineage/a', 'action_ref': 'case.reconcile@1',
            'input_hash': frozen['input_hash'], 'failure_fingerprint': record.issue_fingerprint})
    async def action(context):
        return NodeResult(execution_status='failed', error=ErrorRecord(code='NO_CHANGE', observed_at=1000.0))
    returned = await worker(repository, action)(job)
    assert returned['output']['phase'] == 'blocked'
    assert returned['output']['reason'] == 'no_progress'
    with repository.transaction() as tx:
        assert LifecycleBudgetService.snapshot_in(tx, request.lifecycle_id)['lifecycle']['spent']['repair_attempts'] == 1


def different_node_job(repository, node):
    from sandbox_graph.domain.agent_lifecycle import RepairRequest
    action, permission = {
        'N07': ('evidence.seal@1', 'evidence.seal_owned'),
        'N09': ('resource.cleanup@1', 'resource.cleanup_owned'),
    }[node]
    service, request, token = ready(repository)
    with repository.transaction() as tx:
        for kind, key in (('receipts', 'receipt/a'), ('lifecycle_sources', request.lifecycle_id)):
            row = tx.get(kind, key)
            row['body']['identity']['node_id'] = node
            tx.put(kind, key, row['body'])
        row = tx.get('agent_lifecycles', request.lifecycle_id)
        row['body']['key']['node_id'] = node
        tx.put('agent_lifecycles', request.lifecycle_id, row['body'])
        row = tx.get('repair_policies', 'policy/a')
        row['body']['actions'] = [action]
        tx.put('repair_policies', 'policy/a', row['body'])
        row = tx.get('repair_authorizations', 'auth/a')
        tx.put('repair_authorizations', 'auth/a', row['body'] | {'actions': [action], 'permissions': [permission]})
        row = tx.get('lifecycle_decisions', request.proposal_ref)
        tx.put('lifecycle_decisions', request.proposal_ref, row['body'] | {'action_ref': action})
    # Updating the fixture policy/identity invalidated the precursor proof;
    # produce a new real safety-service proof before ordering this action.
    proof = LifecycleSafetyService(repository).produce_disposition('work/a', token, ttl=50)
    LifecycleService(repository).bind_disposition(request.lifecycle_id, proof, token=token)
    request = RepairRequest.model_validate(request.model_copy(update={'action_ref': action}))
    ordered = service.request_remediation(request, principal='supervisor/a', token=token)
    with repository.transaction() as tx:
        LifecycleSafetyService.release_work_in(tx, 'work/a', token)
        tx.release_thread(token)
    return request, ordered, TaskBackend(repository).claim('worker/new', 80)


def node_worker(repository, node, action):
    return RemedyJobHandler(repository, lambda token, activation: {node: action},
                            lease_seconds=30, segment_seconds=20)


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['complete', 'missing_collection', 'wrong_scope', 'gaps', 'interrupted', 'wrong_artifacts'])
async def test_n07_requires_owned_complete_collection_instead_of_output_claim(repository, evidence):
    request, order, job = different_node_job(repository, 'N07')
    closed_operation(repository)
    if evidence == 'wrong_scope':
        with repository.transaction() as tx:
            row = tx.get('operations', 'op/a')
            tx.put('operations', 'op/a', row['body'] | {'scope': 'work/other'})
    if evidence != 'missing_collection':
        with repository.transaction() as tx:
            tx.put('collections', 'op/a:1', RunnerCollection(last_seq=1,
                artifact_refs=('raw/other',) if evidence == 'wrong_artifacts' else ('raw/original',),
                interrupted=evidence == 'interrupted',
                missing_sequences=(1,) if evidence == 'gaps' else ()).model_dump(mode='json'), immutable=True)
            if evidence != 'gaps':
                tx.put('runner_events', 'op/a:00000000000000000001', {'seq': 1, 'raw': 'original'}, immutable=True)
    async def action(context):
        return NodeResult(execution_status='succeeded', data_status='valid',
            artifact_refs=('raw/original',), operation_refs=('op/a',),
            output={'complete_collection': True})
    returned = await node_worker(repository, 'N07', action)(job)
    assert returned['execution_status'] == ('succeeded' if evidence == 'complete' else 'blocked')
    assert returned['output']['phase'] == ('reentering' if evidence == 'complete' else 'blocked')


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['complete', 'missing_owned_ref', 'active_residual', 'sampler_running'])
async def test_n09_requires_current_closed_disposition_with_exact_owned_inventory(repository, evidence):
    request, order, job = different_node_job(repository, 'N09')
    closed_operation(repository)
    with repository.transaction() as tx:
        tx.put('resources', 'resource/a', {'scope': 'work/a', 'operation_id': 'op/a', 'status': 'closed'})
    async def action(context):
        with repository.transaction() as tx:
            if evidence == 'active_residual':
                tx.put('resources', 'resource/a', {'scope': 'work/a', 'operation_id': 'op/a', 'status': 'active'})
            if evidence == 'sampler_running':
                tx.put('observations', 'sampler/a', {'scope': 'work/a', 'status': 'running'})
        return NodeResult(execution_status='succeeded', cleanup_status='complete',
            operation_refs=('op/a',), resource_refs=() if evidence == 'missing_owned_ref' else ('resource/a',))
    returned = await node_worker(repository, 'N09', action)(job)
    assert returned['execution_status'] == ('succeeded' if evidence == 'complete' else 'blocked')
    assert returned['output']['phase'] == ('reentering' if evidence == 'complete' else
                                          'blocked' if evidence == 'missing_owned_ref' else 'reconciling')


@pytest.mark.asyncio
async def test_thread_renewal_loss_cancels_blocking_action_and_releases_own_work_lease(repository, monkeypatch):
    from tests.orchestration.conftest import MemoryTransaction
    request, order, job = modern_job(repository)
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def tick(seconds):
        await entered.wait()
    def lost(tx, token, ttl):
        raise StaleOwner('thread renewal lost')
    monkeypatch.setattr(asyncio, 'sleep', tick)
    monkeypatch.setattr(MemoryTransaction, 'renew_thread', lost)
    async def action(context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    with pytest.raises(StaleOwner, match='thread renewal lost'):
        await asyncio.wait_for(worker(repository, action)(job), 1)
    assert stopped.is_set()
    with repository.transaction() as tx:
        assert tx.scan('repair_outcomes') == tx.scan('budget_settlements') == []
        assert not tx.get('work_execution_leases', 'work/a')


@pytest.mark.asyncio
async def test_missing_operation_binding_holds_phase_without_fabricating_owner_loss(repository):
    request, order, job = modern_job(repository)
    async def action(context):
        raise ConnectionError('lost external reply with no durable operation handle')
    returned = await worker(repository, action)(job)
    assert returned['output']['reason'] == 'operation_binding_missing'
    with repository.transaction() as tx:
        record = LifecycleService.load_in(tx, request.lifecycle_id)
        assert record.phase == 'remediating'
        assert [e.kind for e in record.events] == ['disposition_confirmed', 'known_action', 'claimed']
        assert len(tx.scan('repair_outcomes')) == len(tx.scan('budget_settlements')) == 1


@pytest.mark.asyncio
async def test_new_no_progress_facts_after_order_prohibit_claimed_action(repository):
    request, order, job = modern_job(repository, allowance=2)
    with repository.transaction() as tx:
        frozen = tx.get('repair_orders', order['order_ref'])['body']
        record = LifecycleService.load_in(tx, request.lifecycle_id)
        for index in range(2):
            tx.put('repair_failure_fingerprints', f'concurrent/{index}', {
                'lineage_budget_ref': 'lineage/a', 'action_ref': 'case.reconcile@1',
                'input_hash': frozen['input_hash'], 'failure_fingerprint': record.issue_fingerprint})
    async def action(context):
        pytest.fail('no-progress proof changed before claim')
    with pytest.raises(Conflict, match='progress'):
        await worker(repository, action)(job)
    with repository.transaction() as tx:
        assert tx.scan('repair_executions') == tx.scan('repair_outcomes') == []


@pytest.mark.asyncio
async def test_real_worker_claims_modern_job_runs_action_and_commits_task_completion(repository):
    from sandbox_graph.runtime.worker import Worker, WorkerPolicy
    request, order, claimed = modern_job(repository)
    closed_operation(repository)
    with repository.transaction() as tx:
        row = tx.get('jobs', claimed['job_id'])
        tx.put('jobs', claimed['job_id'], row['body'] | {'status': 'pending', 'lease_until': 0})
    effects = []
    async def action(context):
        effects.append(context.node_id)
        return NodeResult(execution_status='succeeded', operation_refs=('op/a',))
    runner = Worker(repository, {'remedy': worker(repository, action)},
        policy=WorkerPolicy(claim_seconds=50, job_seconds=60, poll_seconds=0.1))
    assert await runner.run_once() is True
    with repository.transaction() as tx:
        job = tx.get('jobs', claimed['job_id'])['body']
        assert job['status'] == 'complete'
        assert job['result']['output']['phase'] == 'reentering'
        assert tx.get('outbox', 'job-complete:' + claimed['job_id'])
        assert len(tx.scan('repair_outcomes')) == 1
    assert effects == ['N01']


@pytest.mark.asyncio
async def test_real_failure_new_evidence_second_and_last_attempt_can_succeed(repository, clock):
    from sandbox_graph.domain.agent_lifecycle import LifecycleEvent, RepairRequest
    from sandbox_graph.services.remediation import NodeRemediationService
    request, order, first_job = modern_job(repository, allowance=2)
    effects = []
    async def failure(context):
        effects.append('failed')
        return NodeResult(execution_status='failed', error=ErrorRecord(code='TEMPORARY',
            retry_class='bounded', effect_certainty='none', observed_at=1000.0))
    failed = await worker(repository, failure)(first_job)
    assert failed['output']['phase'] == 'backoff'
    TaskBackend(repository).finish(first_job['job_id'], owner_epoch=first_job['owner_epoch'],
                                   result=failed, status='blocked')
    clock.advance(1)
    with repository.transaction() as tx:
        token = tx.acquire_thread('work-thread', 'supervisor/new', 50)
    lifecycle = LifecycleService(repository)
    lifecycle.claim(request.lifecycle_id, token=token)
    safety = LifecycleSafetyService(repository)
    safety.acquire_work('work/a', token)
    proof = safety.produce_disposition('work/a', token, ttl=30)
    lifecycle.bind_disposition(request.lifecycle_id, proof, token=token)
    with repository.transaction() as tx:
        before = LifecycleService.load_in(tx, request.lifecycle_id)
    record = lifecycle.advance(request.lifecycle_id, LifecycleEvent(schema_version=1,
        event_id='backoff/due', kind='timer_due', source='trusted_timer',
        expected_revision=before.revision, evidence_refs=(proof,), observed_at=1001.0),
        expected_revision=before.revision, token=token)
    decision = lifecycle.record_decision(request.lifecycle_id, request_id='decision/second',
        expected_revision=record.revision, route='known_action', action_ref='case.reconcile@1',
        evidence_refs=(failed['output']['receipt_ref'],), token=token)
    service = NodeRemediationService(repository)
    service.register_input(request.lifecycle_id, 'input/second', {'schema_version': 1,
        'source_receipt': 'receipt/a', 'evidence_refs': ['receipt/a', failed['output']['receipt_ref']],
        'input_refs': {'fixture': 'fixture/a'}}, token=token)
    second_request = RepairRequest(schema_version=1, request_id='repair/second',
        lifecycle_id=request.lifecycle_id, source_receipt='receipt/a', expected_revision=record.revision,
        action_ref='case.reconcile@1', input_manifest_ref='input/second',
        proposal_ref=decision['decision_ref'], authorization_ref='auth/a')
    second = service.request_remediation(second_request, principal='supervisor/a', token=token)
    safety.release_work('work/a', token)
    with repository.transaction() as tx:
        tx.release_thread(token)
    closed_operation(repository)
    second_job = TaskBackend(repository).claim('worker/second', 50)
    async def success(context):
        effects.append('succeeded')
        return NodeResult(execution_status='succeeded', operation_refs=('op/a',))
    result = await worker(repository, success)(second_job)
    assert result['execution_status'] == 'succeeded'
    assert result['output']['attempt'] == 2
    assert result['output']['phase'] == 'reentering'
    assert effects == ['failed', 'succeeded']
    with repository.transaction() as tx:
        assert LifecycleBudgetService.snapshot_in(tx, request.lifecycle_id)['lifecycle']['spent']['repair_attempts'] == 2
        assert len(tx.scan('repair_outcomes')) == len(tx.scan('budget_settlements')) == 2
        assert [e.kind for e in LifecycleService.load_in(tx, request.lifecycle_id).events] == [
            'disposition_confirmed', 'known_action', 'claimed', 'postcheck_fail_with_budget',
            'timer_due', 'known_action', 'claimed', 'postcheck_pass']


@pytest.mark.asyncio
async def test_case_reconcile_cannot_accept_a_new_operation_as_the_original_operation(repository):
    request, order, job = modern_job(repository)
    closed_operation(repository, 'op/original')
    async def action(context):
        closed_operation(repository, 'op/replacement')
        return NodeResult(execution_status='succeeded', operation_refs=('op/replacement',))
    result = await worker(repository, action)(job)
    assert result['execution_status'] == 'blocked'
    assert result['output']['postcheck_status'] == 'rejected'


@pytest.mark.asyncio
async def test_another_unresolved_repair_isolation_blocks_live_action_even_without_resource_domains(repository):
    request, order, job = modern_job(repository)
    with repository.transaction() as tx:
        tx.put('repair_isolations', 'prior/uncertain', {'order_ref': 'prior/order',
            'work_ref': 'work/a', 'operation_refs': [], 'reason': 'operation_binding_missing'}, immutable=True)
    calls = []
    async def action(context):
        calls.append(context)
        return NodeResult(execution_status='succeeded')
    with pytest.raises(Conflict, match='isolation'):
        await worker(repository, action)(job)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['intent', 'submission_started'])
async def test_receipt_referencing_statusless_original_operation_commits_unknown_atomically(repository, phase):
    request, order, job = modern_job(repository)
    calls = []
    async def action(context):
        calls.append(context.node_id)
        with repository.transaction() as tx:
            tx.put('operations', 'op/statusless', {'scope': 'work/a', 'run_id': 'run/a',
                'thread_id': context.thread_id, 'phase': phase})
        return NodeResult(execution_status='failed', operation_refs=('op/statusless',),
            error=ErrorRecord(code='REMOTE_INCOMPLETE', effect_certainty='unknown', observed_at=1000.0))
    returned = await worker(repository, action)(job)
    assert returned['execution_status'] == 'blocked'
    assert returned['output']['phase'] == 'reconciling'
    assert returned['output']['postcheck_status'] == 'unknown'
    with repository.transaction() as tx:
        assert len(tx.scan('repair_outcomes')) == len(tx.scan('budget_settlements')) == len(tx.scan('repair_isolations')) == 1
        assert LifecycleService.load_in(tx, request.lifecycle_id).operation_refs == ('op/statusless',)
        intent = tx.get('outbox', 'repair-outcome:' + order['order_ref'])['body']
        assert intent['kind'] == 'repair_reconciliation_required'
        assert intent['operation_refs'] == ['op/statusless']
    assert await worker(repository, action)(job) == returned
    assert calls == ['N01']


@pytest.mark.asyncio
async def test_outer_worker_timeout_commits_unknown_handoff_before_releasing_live_owner(repository):
    from sandbox_graph.runtime.worker import Worker, WorkerPolicy
    request, order, claimed = modern_job(repository)
    with repository.transaction() as tx:
        row = tx.get('jobs', claimed['job_id'])
        tx.put('jobs', claimed['job_id'], row['body'] | {'status': 'pending', 'lease_until': 0})
    entered, stopped = asyncio.Event(), asyncio.Event()
    calls = []
    async def action(context):
        calls.append(context.node_id)
        with repository.transaction() as tx:
            tx.put('operations', 'op/original-timeout', {'scope': 'work/a', 'run_id': 'run/a',
                'thread_id': context.thread_id, 'phase': 'submission_started'})
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    runner = Worker(repository, {'remedy': worker(repository, action)},
        policy=WorkerPolicy(claim_seconds=50, job_seconds=0.02, poll_seconds=0.1))
    assert await asyncio.wait_for(runner.run_once(), 1) is True
    assert entered.is_set() and stopped.is_set()
    with repository.transaction() as tx:
        job = tx.get('jobs', claimed['job_id'])['body']
        assert job['status'] == 'failed'
        assert job['result']['error']['type'] == 'TimeoutError'
        assert len(tx.scan('repair_outcomes')) == len(tx.scan('budget_settlements')) == len(tx.scan('repair_isolations')) == 1
        outcome = tx.get('repair_outcomes', order['order_ref'])['body']
        assert outcome['outcome']['effect_certainty'] == 'unknown'
        assert outcome['postcheck_status'] == 'unknown'
        record = LifecycleService.load_in(tx, request.lifecycle_id)
        assert record.phase == 'reconciling'
        assert record.operation_refs == ('op/original-timeout',)
        intent = tx.get('outbox', 'repair-outcome:' + order['order_ref'])['body']
        assert intent['kind'] == 'repair_reconciliation_required'
        assert intent['operation_refs'] == ['op/original-timeout']
        assert not tx.get('work_execution_leases', 'work/a')
    assert repository.leases[claimed['thread_id']].lease_until <= repository.clock.now
    assert TaskBackend(repository).claim('next-worker', 50) is None
    assert calls == ['N01']


@pytest.mark.asyncio
@pytest.mark.parametrize('loss', [None, 'job', 'thread', 'lifecycle', 'work', 'other_failure'])
async def test_interruption_fact_handoff_requires_all_current_owners_and_never_accepts_success(repository, loss):
    request, order, job = modern_job(repository)
    closed_operation(repository)
    entered = asyncio.Event()
    async def action(context):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(worker(repository, action)(job))
    await asyncio.wait_for(entered.wait(), 1)
    replacement = None
    with repository.transaction() as tx:
        if loss is None:
            # Revoked ordinary authority cannot erase facts about an action
            # already started. The handoff must remain unknown, never accepted.
            row = tx.get('repair_authorizations', 'auth/a')
            tx.put('repair_authorizations', 'auth/a', row['body'] | {'revoked': True})
            tx.put('dispatch_gates', 'run/a', {'open': False, 'generation': 3})
        elif loss == 'job':
            row = tx.get('jobs', job['job_id'])
            tx.put('jobs', job['job_id'], row['body'] | {'owner_epoch': job['owner_epoch'] + 1})
        elif loss == 'other_failure':
            row = tx.get('jobs', job['job_id'])
            tx.put('jobs', job['job_id'], row['body'] | {'status': 'failed',
                'result': {'execution_status': 'failed', 'error': {'code': 'EXECUTION_FAILED', 'type': 'ValueError'}}})
        elif loss == 'thread':
            old = repository.leases[job['thread_id']]
            tx.release_thread(old)
            replacement = tx.acquire_thread(job['thread_id'], 'replacement', 50)
        elif loss == 'lifecycle':
            row = tx.get('lifecycle_owners', request.lifecycle_id)
            tx.put('lifecycle_owners', request.lifecycle_id, row['body'] | {'lease_id': 'replacement'})
        else:
            row = tx.get('work_execution_leases', 'work/a')
            tx.put('work_execution_leases', 'work/a', row['body'] | {'owner': row['body']['owner'] | {'lease_id': 'replacement'}})
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with repository.transaction() as tx:
        assert len(tx.scan('repair_outcomes')) == len(tx.scan('budget_settlements')) == (1 if loss is None else 0)
        if loss is None:
            assert tx.get('repair_outcomes', order['order_ref'])['body']['postcheck_status'] == 'unknown'
            assert tx.get('outbox', 'repair-outcome:' + order['order_ref'])['body']['kind'] == 'repair_reconciliation_required'
    if replacement:
        assert repository.leases[job['thread_id']] == replacement
