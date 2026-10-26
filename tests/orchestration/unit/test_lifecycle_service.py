"""Durable lifecycle and decision cut points, using explicit synthetic facts."""
from copy import deepcopy

import pytest

from sandbox_graph.domain.agent_lifecycle import LifecycleEvent, RepairPolicy
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.persistence.repository import Conflict, StaleOwner
from sandbox_graph.services.agent_lifecycle import LifecycleService


def setup_lifecycle(repository, *, result=None, review='accept', now=1000.):
    policy = RepairPolicy(schema_version=1, policy_version='v1',
        actions=('case.reconcile@1',), max_repair_attempts=2,
        max_diagnosis_calls=2, max_candidate_revisions=2, token_limit=100,
        token_unit='tokens', child_job_limit=3, max_depth=1, max_concurrency=1,
        deadline=(now+100), clock='wall',
        backoff={'schema_version': 1, 'seconds': 1.0, 'unit': 'seconds'},
        max_same_failure=2, max_observations=2,
        safety_budget={'schema_version': 1, 'max_stop_calls': 2,
            'max_query_calls': 2, 'max_cleanup_calls': 2, 'cost_limit': 10.0,
            'cost_unit': 'test_units', 'deadline': (now+150), 'clock': 'wall'},
        postcheck_ref='postcheck/v1')
    with repository.transaction() as tx:
        token = tx.acquire_thread('work-thread', 'worker', 100)
        tx.put('runs', 'run/a', {'run_id': 'run/a', 'graph_ref': 'graph/a',
            'active_plan_ref': 'plan/a', 'contract_ref': 'contract/a',
            'lineage_id': 'lineage/a', 'dispatch_generation': 3})
        tx.put('work_instances', 'work/a', {'work_id': 'work/a', 'run_id': 'run/a',
            'graph_ref': 'graph/a', 'plan_ref': 'plan/a', 'contract_ref': 'contract/a',
            'group_id': 'group/a', 'status': 'blocked'})
        tx.put('receipts', 'receipt/a', {'identity': {'thread_id': 'work-thread',
            'run_ref': 'run/a', 'scope': 'work/a', 'node_id': 'N01',
            'plan_ref': 'plan/a', 'graph_ref': 'graph/a',
            'activation_id': 'activation/a', 'input_refs': {'fixture': 'fixture/a'}},
            'result': (result or NodeResult(execution_status='failed')).model_dump(mode='json')}, immutable=True)
        tx.put('node_reviews', 'receipt/a', {'decision': review}, immutable=True)
    service = LifecycleService(repository)
    service.register_policy('policy/a', policy)
    return service, token


def test_same_failure_has_one_lifecycle_and_preserves_receipt(repository):
    service, token = setup_lifecycle(repository)
    before = deepcopy(repository.rows[('receipts', 'receipt/a')])
    first = service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    second = service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    assert first == second
    assert first.phase == 'draining'
    assert first.key.lineage_budget_ref == 'lineage/a'
    assert len([1 for kind, _ in repository.rows if kind == 'agent_lifecycles']) == 1
    assert repository.rows[('receipts', 'receipt/a')] == before


@pytest.mark.parametrize('result,review,accepted', [
    (NodeResult(execution_status='succeeded', performance_status='fail'), 'accept', False),
    (NodeResult(execution_status='succeeded', review_status='rejected'), 'accept', True),
    (NodeResult(execution_status='succeeded'), 'reject', True),
])
def test_observation_is_driven_by_quality_and_execution_not_performance(repository, result, review, accepted):
    service, token = setup_lifecycle(repository, result=result, review=review)
    if accepted:
        assert service.observe_failure('receipt/a', policy_ref='policy/a', token=token).phase == 'draining'
    else:
        with pytest.raises(Conflict, match='failure|quality'):
            service.observe_failure('receipt/a', policy_ref='policy/a', token=token)


def test_scope_and_expired_fence_cannot_create_lifecycle(repository, clock):
    service, token = setup_lifecycle(repository)
    with repository.transaction() as tx:
        stranger = tx.acquire_thread('other-thread', 'other-worker', 100)
    with pytest.raises(PermissionError):
        service.observe_failure('receipt/a', policy_ref='policy/a', token=stranger)
    clock.advance(101)
    with pytest.raises(StaleOwner):
        service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    assert not [1 for kind, _ in repository.rows if kind == 'agent_lifecycles']


def test_event_cas_and_replay_are_append_only(repository):
    service, token = setup_lifecycle(repository)
    record = service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    event = LifecycleEvent(schema_version=1, event_id='event/1', kind='owner_lost',
        source='worker', expected_revision=0, evidence_refs=('receipt/a',), observed_at=1000.0)
    updated = service.advance(record.key.lifecycle_id, event, expected_revision=0, token=token)
    assert updated.revision == 1
    assert updated.phase == 'draining'
    assert service.advance(record.key.lifecycle_id, event, expected_revision=0, token=token) == updated
    with pytest.raises(Conflict):
        service.advance(record.key.lifecycle_id, event.model_copy(update={'event_id': 'event/2'}), expected_revision=0, token=token)
    assert len([1 for kind, _ in repository.rows if kind == 'lifecycle_events']) == 1


def test_decision_and_outbox_commit_together_and_consume_once(repository, monkeypatch):
    from tests.orchestration.conftest import MemoryTransaction
    service, token = setup_lifecycle(repository)
    record = service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    original = MemoryTransaction.put
    def crash(tx, kind, key, body, *args, **kwargs):
        if kind == 'outbox' and body.get('kind') == 'decision_ready':
            raise RuntimeError('cut after decision')
        return original(tx, kind, key, body, *args, **kwargs)
    monkeypatch.setattr(MemoryTransaction, 'put', crash)
    with pytest.raises(RuntimeError):
        service.record_decision(record.key.lifecycle_id, request_id='decision/1',
            expected_revision=0, route='needs_input', evidence_refs=('receipt/a',), token=token)
    assert not [1 for kind, _ in repository.rows if kind == 'lifecycle_decisions']
    monkeypatch.setattr(MemoryTransaction, 'put', original)
    decision = service.record_decision(record.key.lifecycle_id, request_id='decision/1',
        expected_revision=0, route='needs_input', evidence_refs=('receipt/a',), token=token)
    assert any(kind == 'outbox' and row['body'].get('decision_ref') == decision['decision_ref']
               for (kind, _), row in repository.rows.items())
    calls = []
    def handler(tx, decision):
        calls.append(decision['decision_ref'])
        tx.put('test_dispatch', decision['decision_ref'], {'job': 'job/a'}, immutable=True)
        return {'job_ref': 'job/a'}
    assert service.consume_decision(decision['decision_ref'], token=token, dispatch_in=handler) == {'job_ref': 'job/a'}
    assert service.consume_decision(decision['decision_ref'], token=token, dispatch_in=handler) == {'job_ref': 'job/a'}
    assert calls == [decision['decision_ref']]
    with pytest.raises(Conflict):
        service.record_decision(record.key.lifecycle_id, request_id='decision/1',
            expected_revision=0, route='no_safe_route', evidence_refs=('receipt/a',), token=token)


def test_failed_consumer_does_not_lose_decision(repository):
    service, token = setup_lifecycle(repository)
    record = service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    d = service.record_decision(record.key.lifecycle_id, request_id='decision/1',
        expected_revision=0, route='needs_input', evidence_refs=('receipt/a',), token=token)
    def crash(tx, decision):
        tx.put('test_dispatch', 'job/a', {'pending': True})
        raise RuntimeError('cut during dispatch')
    with pytest.raises(RuntimeError):
        service.consume_decision(d['decision_ref'], token=token, dispatch_in=crash)
    assert ('test_dispatch', 'job/a') not in repository.rows
    assert ('lifecycle_decision_consumptions', d['decision_ref']) not in repository.rows
    assert ('lifecycle_decisions', d['decision_ref']) in repository.rows


def test_decision_requires_scope_owned_evidence(repository):
    service, token = setup_lifecycle(repository)
    record = service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    with pytest.raises(PermissionError):
        service.record_decision(record.key.lifecycle_id, request_id='decision/1',
            expected_revision=0, route='needs_input', evidence_refs=('receipt/other',), token=token)
