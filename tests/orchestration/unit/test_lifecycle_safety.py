"""Synthetic repository tests: these do not qualify PostgreSQL or adapters."""
from dataclasses import replace

import pytest

from sandbox_graph.persistence.repository import Conflict, StaleOwner
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService


def seed(repository):
    with repository.transaction() as tx:
        token = tx.acquire_thread('work-thread', 'worker', 100)
        tx.put('work_instances', 'work/a', {'work_id': 'work/a', 'run_id': 'run/a',
            'plan_ref': 'plan/a', 'graph_ref': 'graph/a', 'contract_ref': 'contract/a'})
        tx.put('work_safety_facts', 'work/a', {'scope': 'work/a', 'run_ref': 'run/a',
            'plan_ref': 'plan/a', 'graph_ref': 'graph/a', 'contract_ref': 'contract/a',
            'work_revision': 0, 'schema_version': 1, 'stopped': True, 'admission_status': 'closed',
            'sampler_status': 'not_started', 'attempt_closed': True,
            'producer': 'work_runtime', 'producer_version': '1',
            'activation_id': 'activation/a', 'attempt_receipt_ref': 'receipt/N10',
            'receipt_refs': {node: 'receipt/' + node for node in ('N07', 'N09', 'N10')}})
        for node in ('N07', 'N09', 'N10'):
            tx.put('receipts', 'receipt/' + node, {'identity': {'node_id': node,
                'scope': 'work/a', 'run_ref': 'run/a', 'plan_ref': 'plan/a',
                'graph_ref': 'graph/a', 'activation_id': 'activation/a'},
                'result': {'execution_status': 'succeeded', 'cleanup_status': 'complete'}}, immutable=True)
    service = LifecycleSafetyService(repository)
    service.acquire_work('work/a', token)
    return service, token


def operation(repository, *, phase='terminal', authoritative=True):
    with repository.transaction() as tx:
        tx.put('operations', 'original-op', {'scope': 'work/a', 'run_id': 'run/a',
            'phase': phase, 'status': {'state': 'completed' if phase == 'terminal' else 'unknown',
                'authoritative': authoritative, 'resource_refs': ['resource/a']}})
        tx.put('resources', 'resource/a', {'scope': 'work/a',
            'operation_id': 'original-op', 'status': 'closed' if phase == 'terminal' else 'unresolved'})
        tx.put('resource_leases', 'domain/a', {'scope': 'work/a', 'run_id': 'run/a',
            'thread_id': 'work-thread', 'owner_epoch': 1, 'status': 'reserved'})


def test_two_thread_leases_cannot_own_same_work(repository):
    service, token = seed(repository)
    with repository.transaction() as tx:
        other = tx.acquire_thread('remedy-thread', 'remediator', 100)
    with pytest.raises(Conflict):
        service.acquire_work('work/a', other)
    service.assert_work('work/a', token)
    with pytest.raises(StaleOwner):
        service.assert_work('work/a', other)
    with pytest.raises(StaleOwner):
        service.release_work('work/a', replace(token, lease_id='fabricated'))


def test_expired_owner_takeover_preserves_original_operation_and_quarantine(repository, clock):
    service, token = seed(repository)
    operation(repository, phase='uncertain', authoritative=False)
    clock.advance(101)
    with repository.transaction() as tx:
        other = tx.acquire_thread('remedy-thread', 'remediator', 100)
    service.acquire_work('work/a', other, purpose='reconcile')
    with pytest.raises(StaleOwner):
        service.produce_disposition('work/a', token, ttl=10)
    with repository.transaction() as tx:
        assert tx.get('resources', 'resource/a')['body']['status'] == 'unresolved'
        assert tx.get('resource_leases', 'domain/a')['body']['status'] == 'quarantine'
        assert tx.get('operations', 'original-op')['body']['phase'] == 'uncertain'
        assert tx.get('work_safety_quarantines', 'work/a')['body']['operation_refs'] == ['original-op']


def test_receipt_produced_from_ledger_reverified_and_scope_bound(repository, clock):
    service, token = seed(repository)
    operation(repository)
    ref = service.produce_disposition('work/a', token, ttl=10)
    receipt = service.verify_disposition(ref, 'work/a', token)
    assert receipt.work_ref == 'work/a'
    assert receipt.operation_refs == ('original-op',)
    assert receipt.owned_resources == ('resource/a',)
    assert receipt.residual_resources == ()
    assert receipt.expires_at == 1010.0
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/b', token)
    clock.advance(10)
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/a', token)


@pytest.mark.parametrize('kind,key,patch', [
    ('operations', 'original-op', {'phase': 'uncertain'}),
    ('resources', 'resource/a', {'status': 'active'}),
    ('resource_leases', 'domain/a', {'status': 'quarantine'}),
    ('work_safety_facts', 'work/a', {'attempt_closed': False}),
    ('work_instances', 'work/a', {'plan_ref': 'plan/b'}),
])
def test_changed_authoritative_fact_invalidates_unexpired_receipt(repository, kind, key, patch):
    service, token = seed(repository)
    operation(repository)
    ref = service.produce_disposition('work/a', token, ttl=10)
    with repository.transaction() as tx:
        tx.put(kind, key, tx.get(kind, key)['body'] | patch)
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/a', token)


@pytest.mark.parametrize('missing', ['stopped', 'sampler_status', 'attempt_closed', 'attempt_receipt_ref', 'receipt_refs'])
def test_missing_safety_facts_never_authorize_site_effect(repository, missing):
    service, token = seed(repository)
    with repository.transaction() as tx:
        body = tx.get('work_safety_facts', 'work/a')['body']
        body.pop(missing)
        tx.put('work_safety_facts', 'work/a', body)
    ref = service.produce_disposition('work/a', token, ttl=10)
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/a', token)


def test_unknown_operation_allows_frozen_diagnosis_only_and_keeps_original_refs(repository):
    service, token = seed(repository)
    operation(repository, phase='uncertain', authoritative=False)
    with repository.transaction() as tx:
        tx.put('frozen_lifecycle_evidence', 'sealed/a', {'scope': 'work/a', 'run_ref': 'run/a',
            'frozen': True, 'artifact_refs': ['raw/a'], 'content_hash': 'a' * 64}, immutable=True)
    ref = service.produce_disposition('work/a', token, ttl=10)
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/a', token)
    receipt = service.verify_disposition(ref, 'work/a', token, require_closed=False)
    assert receipt.operation_refs == ('original-op',)
    assert receipt.residual_resources == ('resource/a',)
    assert receipt.isolation_domains == ('domain/a',)
    assert service.offline_diagnostic_capabilities(ref, 'work/a', token, evidence_refs=('sealed/a',)) == ('read_frozen_evidence',)
    with pytest.raises(Conflict):
        service.offline_diagnostic_capabilities(ref, 'work/a', token, evidence_refs=('missing',))


def test_receipt_cannot_be_replayed_under_successor_or_forged_producer(repository, clock):
    service, token = seed(repository)
    ref = service.produce_disposition('work/a', token, ttl=500)
    with repository.transaction() as tx:
        row = tx.get('resource_disposition_receipts', ref)
        tx.put('resource_disposition_receipts', 'forged', row['body'] | {'producer': {'thread_id': 'fake'}})
    with pytest.raises(Conflict):
        service.verify_disposition('forged', 'work/a', token)
    clock.advance(101)
    with repository.transaction() as tx:
        other = tx.acquire_thread('work-thread', 'worker2', 100)
    service.acquire_work('work/a', other)
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/a', other)


def test_no_disposed_boolean_api_and_no_invented_operation_for_unknown_resources(repository):
    service, token = seed(repository)
    with pytest.raises(TypeError):
        service.produce_disposition('work/a', token, ttl=10, resources_disposed=True)
    with repository.transaction() as tx:
        tx.put('resources', 'orphan', {'scope': 'work/a', 'status': 'unknown'})
    ref = service.produce_disposition('work/a', token, ttl=10)
    proof = service.verify_disposition(ref, 'work/a', token, require_closed=False)
    assert proof.operation_refs == ()
    assert proof.residual_resources == ('orphan',)
    with repository.transaction() as tx:
        assert tx.get('work_safety_quarantines', 'work/a')['body']['residual_resources'] == ['orphan']
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/a', token)


def test_actual_running_sampler_overrides_closed_caller_facts(repository):
    service, token = seed(repository)
    with repository.transaction() as tx:
        tx.put('observations', 'sampler/a', {'scope': 'work/a', 'status': 'running'})
    ref = service.produce_disposition('work/a', token, ttl=10)
    assert service.verify_disposition(ref, 'work/a', token, require_closed=False).sampler_status == 'running'
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/a', token)


@pytest.mark.parametrize('patch', [
    {'schema_version': True}, {'producer_version': 'untrusted'}, {'work_revision': 1},
    {'activation_id': 'other-activation'}, {'attempt_receipt_ref': 'missing'},
])
def test_facts_cannot_replace_actual_identity_bound_closure(repository, patch):
    service, token = seed(repository)
    with repository.transaction() as tx:
        tx.put('work_safety_facts', 'work/a', tx.get('work_safety_facts', 'work/a')['body'] | patch)
    ref = service.produce_disposition('work/a', token, ttl=10)
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/a', token)


def test_operation_reported_resource_without_ownership_record_remains_unknown(repository):
    service, token = seed(repository)
    operation(repository)
    with repository.transaction() as tx:
        tx.delete('resources', 'resource/a')
    ref = service.produce_disposition('work/a', token, ttl=10)
    assert service.verify_disposition(ref, 'work/a', token, require_closed=False).residual_resources == ('resource/a',)
    with pytest.raises(Conflict):
        service.verify_disposition(ref, 'work/a', token)


def test_live_mutex_survives_work_status_update_but_proof_does_not(repository):
    service, token = seed(repository)
    with repository.transaction() as tx:
        tx.put('work_instances', 'work/a', tx.get('work_instances', 'work/a')['body'] | {'status': 'failed'})
    service.assert_work('work/a', token)
    stale_ref = service.produce_disposition('work/a', token, ttl=10)
    with pytest.raises(Conflict):
        service.verify_disposition(stale_ref, 'work/a', token)
    with repository.transaction() as tx:
        facts = tx.get('work_safety_facts', 'work/a')['body']
        tx.put('work_safety_facts', 'work/a', facts | {'work_revision': 1})
    good_ref = service.produce_disposition('work/a', token, ttl=10)
    assert service.verify_disposition(good_ref, 'work/a', token).stopped


def test_release_does_not_release_resource_reservations(repository):
    service, token = seed(repository)
    operation(repository, phase='uncertain', authoritative=False)
    service.release_work('work/a', token)
    with repository.transaction() as tx:
        assert tx.get('work_execution_leases', 'work/a') is None
        assert tx.get('resource_leases', 'domain/a')['body']['status'] == 'quarantine'
        assert tx.get('resources', 'resource/a')['body']['status'] == 'unresolved'


def test_expired_owner_quarantines_reserved_domains_even_with_closed_resources(repository, clock):
    service, token = seed(repository)
    operation(repository)
    clock.advance(101)
    with repository.transaction() as tx:
        successor = tx.acquire_thread('cleanup-thread', 'cleaner', 100)
    service.acquire_work('work/a', successor, purpose='cleanup')
    with repository.transaction() as tx:
        assert tx.get('resource_leases', 'domain/a')['body']['status'] == 'quarantine'
        assert tx.get('resources', 'resource/a')['body']['status'] == 'closed'


def test_boolean_epoch_cannot_impersonate_real_integer_epoch(repository):
    service, token = seed(repository)
    with pytest.raises(StaleOwner):
        service.assert_work('work/a', replace(token, owner_epoch=True))


def test_legacy_ledger_stop_cannot_make_a_closed_disposition(repository):
    from sandbox_graph.runtime.observation import ObservationService
    service, token = seed(repository)
    observations = ObservationService(repository)
    observations.start('legacy','work/a',operation_id='old-op',clock_epoch='old',deadline=1100)
    observations.stop('legacy',last_seq=0)
    proof = service.produce_disposition('work/a',token,ttl=10)
    receipt = service.verify_disposition(proof,'work/a',token,require_closed=False)
    assert receipt.sampler_status == 'unknown'
    with pytest.raises(Conflict):
        service.verify_disposition(proof,'work/a',token)
