"""ALU-07/08: no dispatch intent can escape its fenced budget transaction."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from sandbox_graph.domain.agent_lifecycle import LifecycleEvent, RepairRequest
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.persistence.repository import Conflict, StaleOwner
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from sandbox_graph.services.remediation import NodeRemediationService
from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle


def ready(repository, *, now=1000.):
    lifecycle, token = setup_lifecycle(repository, now=now)
    record = lifecycle.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    safety = LifecycleSafetyService(repository)
    safety.acquire_work('work/a', token)
    with repository.transaction() as tx:
        for node in ('N07', 'N09', 'N10'):
            tx.put('receipts', f'closed/{node}', {'identity': {'thread_id': 'work-thread',
                'scope': 'work/a', 'run_ref': 'run/a', 'plan_ref': 'plan/a', 'graph_ref': 'graph/a',
                'activation_id': 'activation/a', 'node_id': node},
                'result': NodeResult(execution_status='succeeded', cleanup_status='complete').model_dump(mode='json')}, immutable=True)
        tx.put('work_safety_facts', 'work/a', {'schema_version': 1, 'producer': 'work_runtime',
            'producer_version': '1', 'scope': 'work/a', 'run_ref': 'run/a', 'plan_ref': 'plan/a',
            'graph_ref': 'graph/a', 'contract_ref': 'contract/a', 'work_revision': 0,
            'stopped': True, 'admission_status': 'closed', 'sampler_status': 'not_started',
            'attempt_closed': True, 'activation_id': 'activation/a', 'attempt_receipt_ref': 'closed/N10',
            'receipt_refs': {n: f'closed/{n}' for n in ('N07', 'N09', 'N10')}})
        tx.put('dispatch_gates', 'run/a', {'open': True, 'generation': 3})
        tx.put('repair_authorizations', 'auth/a', {'principal': 'supervisor/a',
            'run_ref': 'run/a', 'scope_ref': 'work/a', 'plan_ref': 'plan/a',
            'graph_ref': 'graph/a', 'contract_ref': 'contract/a', 'work_revision': 0,
            'actions': ['case.reconcile@1'], 'permissions': ['operation.read_owned'],
            'expires_at': (now+100), 'revoked': False})
        LifecycleBudgetService.initialize_in(tx, lifecycle_id=record.key.lifecycle_id,
            lineage_budget_ref='lineage/a', limits={'repair_attempts': 1, 'diagnosis_calls': 2,
            'candidate_revisions': 2, 'tokens': 100, 'child_jobs': 2, 'observations': 2, 'cost': 100},
            deadline=(now+100), safety_limits={'stop_calls': 2, 'query_calls': 2, 'cleanup_calls': 2, 'cost': 10},
            safety_deadline=(now+150), cost_unit='test_units')
    proof = safety.produce_disposition('work/a', token, ttl=50)
    lifecycle.bind_disposition(record.key.lifecycle_id, proof, token=token)
    record = lifecycle.advance(record.key.lifecycle_id, LifecycleEvent(schema_version=1,
        event_id='closed/1', kind='disposition_confirmed', source='safety', expected_revision=0,
        evidence_refs=(proof,), observed_at=now), expected_revision=0, token=token)
    decision = lifecycle.record_decision(record.key.lifecycle_id, request_id='decision/a',
        route='known_action', action_ref='case.reconcile@1', expected_revision=record.revision,
        evidence_refs=('receipt/a',), token=token)
    service = NodeRemediationService(repository)
    service.register_input(record.key.lifecycle_id, 'input/a', {'schema_version': 1,
        'source_receipt': 'receipt/a', 'evidence_refs': ['receipt/a'],
        'input_refs': {'fixture': 'fixture/a'}}, token=token)
    request = RepairRequest(schema_version=1, request_id='repair/a', lifecycle_id=record.key.lifecycle_id,
        source_receipt='receipt/a', expected_revision=record.revision, action_ref='case.reconcile@1',
        input_manifest_ref='input/a', proposal_ref=decision['decision_ref'], authorization_ref='auth/a')
    return service, request, token


def test_atomic_request_replay_uses_current_generation_and_frozen_inputs(repository):
    service, request, token = ready(repository)
    first = service.request_remediation(request, principal='supervisor/a', token=token)
    assert service.request_remediation(request, principal='supervisor/a', token=token) == first
    with repository.transaction() as tx:
        jobs = tx.scan('jobs')
        assert len(jobs) == 1
        assert jobs[0]['body']['dispatch_generation'] == 3
        order = tx.get('repair_orders', first['order_ref'])['body']
        assert order['attempt'] == 1
        assert order['input_refs'] == {'fixture': 'fixture/a'}
        assert order['source_receipt'] == 'receipt/a'
        assert LifecycleBudgetService.snapshot_in(tx, request.lifecycle_id)['lifecycle']['reserved']['repair_attempts'] == 1
    with pytest.raises(Conflict):
        service.request_remediation(request.model_copy(update={'input_manifest_ref': 'input/b'}), principal='supervisor/a', token=token)


def test_competing_requests_cannot_spend_last_allowance_twice(repository):
    service, request, token = ready(repository)
    def issue(index):
        try:
            return service.request_remediation(request.model_copy(update={'request_id': f'repair/{index}'}),
                                               principal='supervisor/a', token=token)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        result = list(pool.map(issue, range(12)))
    assert sum(value is not None for value in result) == 1
    with repository.transaction() as tx:
        assert len(tx.scan('repair_orders')) == len(tx.scan('repair_requests')) == len(tx.scan('jobs')) == 1


@pytest.mark.parametrize('kind,key,patch', [
    ('dispatch_gates', 'run/a', {'open': False}),
    ('repair_authorizations', 'auth/a', {'revoked': True}),
    ('repair_authorizations', 'auth/a', {'scope_ref': 'work/other'}),
    ('repair_authorizations', 'auth/a', {'permissions': []}),
    ('repair_authorizations', 'auth/a', {'expires_at': 1000.0}),
    ('work_instances', 'work/a', {'status': 'changed'}),
    ('work_safety_facts', 'work/a', {'sampler_status': 'running'}),
])
def test_permission_gate_revision_and_disposition_refusals_leave_no_partial_order(repository, kind, key, patch):
    service, request, token = ready(repository)
    with repository.transaction() as tx:
        tx.put(kind, key, tx.get(kind, key)['body'] | patch)
    with pytest.raises((Conflict, PermissionError)):
        service.request_remediation(request, principal='supervisor/a', token=token)
    with repository.transaction() as tx:
        assert tx.scan('repair_orders') == tx.scan('jobs') == tx.scan('budget_reservations') == []


def test_stale_owner_cannot_replay_request_as_permission(repository, clock):
    service, request, token = ready(repository)
    service.request_remediation(request, principal='supervisor/a', token=token)
    clock.advance(101)
    with pytest.raises(StaleOwner):
        service.request_remediation(request, principal='supervisor/a', token=token)


def test_cut_after_order_rolls_back_budget_attempt_job_and_request(repository, monkeypatch):
    from tests.orchestration.conftest import MemoryTransaction
    service, request, token = ready(repository)
    original = MemoryTransaction.put
    def crash(tx, kind, key, body, *args, **kwargs):
        if kind == 'outbox' and body.get('kind') == 'job_ready':
            raise RuntimeError('cut after job write')
        return original(tx, kind, key, body, *args, **kwargs)
    monkeypatch.setattr(MemoryTransaction, 'put', crash)
    with pytest.raises(RuntimeError):
        service.request_remediation(request, principal='supervisor/a', token=token)
    with repository.transaction() as tx:
        assert tx.scan('repair_orders') == tx.scan('repair_requests') == tx.scan('jobs') == []
        assert LifecycleBudgetService.snapshot_in(tx, request.lifecycle_id)['lifecycle']['reserved']['repair_attempts'] == 0
    monkeypatch.setattr(MemoryTransaction, 'put', original)
    assert service.request_remediation(request, principal='supervisor/a', token=token)['attempt'] == 1


def test_legacy_proposal_cannot_bypass_lifecycle_authorization_or_budget(repository):
    service = NodeRemediationService(repository)
    response = service.order(owner_agent_id='agent:work:w', work_ref='w', node_id='N09',
        run_ref='r', plan_ref='p', graph_ref='g', deadline=1100,
        decision={'status': 'proposed', 'proposal': {'suggested_actions': ['resource.cleanup']}})
    assert response['status'] == 'blocked'
    assert response['reason'] == 'lifecycle_contract_required'
    with repository.transaction() as tx:
        assert tx.scan('jobs') == tx.scan('remedy_orders') == tx.scan('node_attempts') == []


def test_acceptance_seed_uses_supplied_real_clock_epoch(repository,clock):
    clock.now=2000000000.
    service,request,token=ready(repository,now=clock.now)
    assert service.request_remediation(request,principal='supervisor/a',token=token)['attempt']==1
