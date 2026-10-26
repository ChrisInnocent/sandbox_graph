"""ALU-02/03: trusted packets, real file evidence, literal isolation canaries."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.domain.agent_lifecycle import ContextPacket
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.agent_lifecycle import LifecycleService
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle


def context_service(repository, tmp_path, **limits):
    from sandbox_graph.services.lifecycle_context import LifecycleContextService
    lifecycle, token = setup_lifecycle(repository)
    with repository.transaction() as tx:
        original = tx.get('receipts', 'receipt/a')['body']
        original['identity']['node_id'] = 'N02'
        original['result']['error'] = {'code': 'E_FAIL', 'message': 'failure SECRET_CANARY',
                                      'observed_at': 1000.0}
        tx.put('receipts', 'receipt/a', original)
        tx.put('node_reviews', 'receipt/a', {'decision': 'reject', 'x-api-token': 'UNREGISTERED_TOKEN'})
        other = deepcopy(original)
        other['identity']['node_id'] = 'N09'
        tx.put('receipts', 'receipt/b', other, immutable=True)
    records = [lifecycle.observe_failure(ref, policy_ref='policy/a', token=token)
               for ref in ('receipt/a', 'receipt/b')]
    with repository.transaction() as tx:
        for record in records:
            LifecycleBudgetService.initialize_in(tx, lifecycle_id=record.key.lifecycle_id,
                lineage_budget_ref='lineage/a', limits={
                    'repair_attempts': 3, 'diagnosis_calls': 3, 'candidate_revisions': 3,
                    'tokens': 100, 'child_jobs': 10, 'observations': 3, 'cost': 100},
                deadline=1100.0, safety_limits={'stop_calls': 3, 'query_calls': 3,
                    'cleanup_calls': 3, 'cost': 100}, safety_deadline=1150.0,
                cost_unit='test_units')
        tx.put('work_safety_facts', 'work/a', {'scope': 'work/a', 'run_ref': 'run/a',
            'plan_ref': 'plan/a', 'graph_ref': 'graph/a', 'contract_ref': 'contract/a',
            'work_revision': 0, 'schema_version': 1, 'stopped': True, 'admission_status': 'closed',
            'sampler_status': 'not_started', 'attempt_closed': True,
            'producer': 'work_runtime', 'producer_version': '1',
            'activation_id': 'activation/a', 'attempt_receipt_ref': 'closed/N10',
            'receipt_refs': {node: 'closed/' + node for node in ('N07', 'N09', 'N10')}})
        for node in ('N07', 'N09', 'N10'):
            tx.put('receipts', 'closed/' + node, {'identity': {'node_id': node,
                'scope': 'work/a', 'run_ref': 'run/a', 'plan_ref': 'plan/a',
                'graph_ref': 'graph/a', 'activation_id': 'activation/a'},
                'result': {'execution_status': 'succeeded'}}, immutable=True)
    safety = LifecycleSafetyService(repository)
    safety.acquire_work('work/a', token, purpose='diagnosis')
    proof = safety.produce_disposition('work/a', token, ttl=50)
    for record in records:
        lifecycle.bind_disposition(record.key.lifecycle_id, proof, token=token)
    store = FileArtifactStore(tmp_path / 'artifacts')
    service = LifecycleContextService(repository, store, artifact_scope='evidence',
        secret_values=('SECRET_CANARY',), **limits)
    return service, store, records, token


def grant(service, store, record, content, *, node=None, visibility='private', **identity_changes):
    artifact = store.commit([content], scope='evidence', media_type='application/json', producer='test')
    identity = dict(run_ref='run/a', scope='work/a', node_id=node or record.key.node_id,
                    graph_ref='graph/a', plan_ref='plan/a', contract_ref='contract/a')
    identity.update(identity_changes)
    handle = service.grant_evidence(record.key.lifecycle_id, artifact=artifact,
        producer_identity=identity, visibility=visibility, offline_sealed=True)
    return handle, artifact


def test_node_context_isolation(repository, tmp_path):
    service, store, (n02, n09), token = context_service(repository, tmp_path)
    h02, raw02 = grant(service, store, n02, b'{"fact":"N02_PRIVATE SECRET_CANARY","x-api-token":"TOKEN_CANARY"}')
    h09, _ = grant(service, store, n09, b'{"fact":"N09_PRIVATE"}')
    shared, _ = grant(service, store, n02, b'{"fact":"SHARED_FACT"}', node='N09', visibility='shared')
    p02 = service.build(n02.key.lifecycle_id, expected_revision=0, token=token)
    p09 = service.build(n09.key.lifecycle_id, expected_revision=0, token=token)
    assert p02.node_supervisor_id != p09.node_supervisor_id
    assert p02.evidence_refs == (h02, shared)
    assert p09.evidence_refs == (h09,)
    assert p02.graph_ref == 'graph/a' and p02.contract_ref == 'contract/a'
    assert p02.failure_summary == 'E_FAIL: failure [REDACTED]'
    assert p02.hypotheses == ()
    assert p02.tool_capabilities == ()
    payload = service.read_evidence(n02.key.lifecycle_id, h02, offset=0, length=len(b'{"fact":"N02_PRIVATE [REDACTED]","x-api-token":"[REDACTED]"}'), token=token)
    assert json.loads(payload) == {'fact': 'N02_PRIVATE [REDACTED]', 'x-api-token': '[REDACTED]'}
    assert service.read_evidence(n02.key.lifecycle_id, shared, offset=0, length=len(b'{"fact":"SHARED_FACT"}'), token=token) == b'{"fact":"SHARED_FACT"}'
    with pytest.raises(PermissionError):
        service.read_evidence(n02.key.lifecycle_id, h09, offset=0, length=10, token=token)
    for text in (p02.model_dump_json(), payload.decode()):
        assert 'N09_PRIVATE' not in text and 'SECRET_CANARY' not in text
        assert 'TOKEN_CANARY' not in text and 'UNREGISTERED_TOKEN' not in text
        assert 'parent_conversation' not in text
    assert store.path_for(raw02).read_bytes() == b'{"fact":"N02_PRIVATE SECRET_CANARY","x-api-token":"TOKEN_CANARY"}'


@pytest.mark.parametrize('changes,visibility', [
    ({'node_id': 'N09'}, 'private'), ({'scope': 'group/other'}, 'shared'),
    ({'run_ref': 'run/other'}, 'shared'), ({'plan_ref': 'plan/other'}, 'shared'),
    ({'graph_ref': 'graph/other'}, 'shared'), ({'contract_ref': 'contract/other'}, 'shared'),
    ({}, 'parent_conversation'),
])
def test_grants_reject_private_parent_and_identity_expansion(repository, tmp_path, changes, visibility):
    service, store, (record, _), _token = context_service(repository, tmp_path)
    with pytest.raises((PermissionError, ValueError)):
        grant(service, store, record, b'{"fact":"PRIVATE"}', visibility=visibility, **changes)


@pytest.mark.parametrize('uri', ['../outside', '/tmp/outside', 'evidence/../outside'])
def test_artifact_path_traversal_is_rejected(repository, tmp_path, uri):
    service, store, (record, _), _token = context_service(repository, tmp_path)
    raw = store.commit([b'ORIGINAL'], scope='evidence', media_type='text/plain', producer='test')
    with pytest.raises((PermissionError, ValueError)):
        service.grant_evidence(record.key.lifecycle_id, artifact=raw.model_copy(update={'uri': uri}),
            producer_identity=dict(run_ref='run/a', scope='work/a', node_id='N02', graph_ref='graph/a',
                plan_ref='plan/a', contract_ref='contract/a'), visibility='private')


def test_symlink_and_forged_evidence_handle_are_rejected(repository, tmp_path):
    service, store, (record, _), token = context_service(repository, tmp_path)
    handle, raw = grant(service, store, record, b'{"fact":"PUBLIC"}')
    path = store.path_for(raw)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'{"fact":"PUBLIC"}')
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        service.read_evidence(record.key.lifecycle_id, handle, offset=0, length=4, token=token)
    with pytest.raises(PermissionError):
        service.read_evidence(record.key.lifecycle_id, '../outside', offset=0, length=4, token=token)


def test_range_budget_is_persistent_and_does_not_reset_with_packet(repository, tmp_path):
    service, store, (record, _), token = context_service(repository, tmp_path, max_read_bytes=8, total_evidence_bytes=12)
    handle, _ = grant(service, store, record, b'0123456789ABCDEF')
    read = lambda offset, length: service.read_evidence(record.key.lifecycle_id, handle,
        offset=offset, length=length, token=token)
    for offset, length in [(-1, 1), (0, -1), (0, 9), (True, 1), (0, True), (17, 1), (15, 2)]:
        with pytest.raises(ValueError):
            read(offset, length)
    assert read(0, 8) == b'01234567'
    service.build(record.key.lifecycle_id, expected_revision=0, token=token)
    assert read(8, 4) == b'89AB'
    with pytest.raises(Conflict, match='narrower'):
        read(12, 1)
    assert repository.rows[('lifecycle_context_usage', record.key.lifecycle_id)]['body']['bytes_read'] == 12


def test_secret_crossing_range_boundary_is_redacted_before_slicing(repository, tmp_path):
    service, store, (record, _), token = context_service(repository, tmp_path)
    handle, _ = grant(service, store, record, b'prefix SECRET_CANARY suffix')
    assert service.read_evidence(record.key.lifecycle_id, handle, offset=7, length=5, token=token) == b'[REDA'


def test_packet_reads_current_budget_and_rejects_stale_revision(repository, tmp_path):
    service, _store, (record, _), token = context_service(repository, tmp_path)
    with repository.transaction() as tx:
        LifecycleBudgetService.reserve_in(tx, request_id='diagnosis/reservation',
            lifecycle_id=record.key.lifecycle_id, amounts={'diagnosis_calls': 1, 'tokens': 7, 'cost': 2},
            input_ref='failure/input', provider_enforces_maximum=True)
    packet = service.build(record.key.lifecycle_id, expected_revision=0, token=token)
    assert packet.budget_usage.diagnosis_calls == 1
    assert packet.budget_usage.tokens_reserved == 7
    with pytest.raises(Conflict):
        service.build(record.key.lifecycle_id, expected_revision=1, token=token)


def test_live_packet_and_reads_recheck_actual_disposition(repository, tmp_path, clock):
    service, store, (record, _), token = context_service(repository, tmp_path)
    handle, _ = grant(service, store, record, b'PUBLIC')
    packet = service.build(record.key.lifecycle_id, expected_revision=0, token=token)
    assert packet.evidence_mode == 'live' and packet.disposition_receipt_ref.startswith('disposition:')
    clock.advance(50)
    with pytest.raises(Conflict):
        service.build(record.key.lifecycle_id, expected_revision=0, token=token)
    with pytest.raises(Conflict):
        service.read_evidence(record.key.lifecycle_id, handle, offset=0, length=2, token=token)
    offline = service.build(record.key.lifecycle_id, expected_revision=0, token=token, evidence_mode='offline_sealed')
    assert offline.disposition_receipt_ref is None and offline.tool_capabilities == ()
    assert service.read_evidence(record.key.lifecycle_id, handle, offset=0, length=6,
                                 token=token, evidence_mode='offline_sealed') == b'PUBLIC'
    with pytest.raises(ValidationError):
        ContextPacket.model_validate(offline.model_dump() | {'evidence_mode': 'live'})
    with pytest.raises(ValidationError):
        ContextPacket.model_validate(offline.model_dump() | {'tool_capabilities': ('site:create',)})


def test_packet_and_source_limits_reject_without_truncating_failure(repository, tmp_path):
    service, store, (record, _), token = context_service(repository, tmp_path, max_packet_bytes=64, max_source_bytes=16)
    with pytest.raises(ValueError, match='narrower'):
        service.build(record.key.lifecycle_id, expected_revision=0, token=token)
    with pytest.raises(ValueError, match='narrower'):
        grant(service, store, record, b'A' * 17)


@pytest.mark.asyncio
async def test_successful_node_never_calls_model(repository, spy_model):
    from sandbox_graph.graphs.executor import NodeExecutor
    from sandbox_graph.runtime.agents import AgentGateway, AgentSupervisor
    async def success(_context):
        return NodeResult(execution_status='succeeded', coverage_status='unsupported')
    with repository.transaction() as tx:
        token = tx.acquire_thread('healthy/thread', 'worker', 100)
    result, receipt_ref = await NodeExecutor(repository, {'N02': success}, token).execute('N02', {
        'thread_id': 'healthy/thread', 'scope': 'work/a', 'run_ref': 'run/a',
        'plan_ref': 'plan/a', 'graph_ref': 'graph/a', 'activation_id': 'healthy/activation', 'input_refs': {}})
    supervisor = AgentSupervisor(repository, AgentGateway(repository, spy_model, registered_actions=()))
    assert await supervisor.review('agent:work:work/a', allowed_fields=()) == []
    assert result.execution_status == 'succeeded'
    assert repository.rows[('node_reviews', receipt_ref)]['body']['decision'] == 'accept'
    assert spy_model.calls == []
    assert not any(kind in {'agent_calls', 'agent_supervision', 'agent_lifecycles'}
                   for kind, _key in repository.rows)


def test_packet_does_not_export_parent_or_arbitrary_review_fields(repository, tmp_path):
    service, _store, (record, _), token = context_service(repository, tmp_path)
    with repository.transaction() as tx:
        row = tx.get('node_reviews', record.source_receipt)
        tx.put('node_reviews', record.source_receipt, row['body'] | {
            'parent_conversation': 'PARENT_PRIVATE', 'other_group_logs': 'GROUP_PRIVATE',
            'findings': ['independent failure fact']})
    packet = service.build(record.key.lifecycle_id, expected_revision=0, token=token)
    assert 'independent failure fact' in packet.model_dump_json()
    assert 'PARENT_PRIVATE' not in packet.model_dump_json()
    assert 'GROUP_PRIVATE' not in packet.model_dump_json()


def test_offline_requires_explicit_seal_and_cannot_read_unsealed_grant(repository, tmp_path):
    service, store, (record, _), token = context_service(repository, tmp_path)
    raw = store.commit([b'UNSEALED'], scope='evidence', media_type='text/plain', producer='test')
    handle = service.grant_evidence(record.key.lifecycle_id, artifact=raw,
        producer_identity=dict(run_ref='run/a', scope='work/a', node_id='N02',
            graph_ref='graph/a', plan_ref='plan/a', contract_ref='contract/a'), visibility='private')
    with pytest.raises(Conflict, match='sealed'):
        service.build(record.key.lifecycle_id, expected_revision=0, token=token, evidence_mode='offline_sealed')
    with pytest.raises(PermissionError):
        service.read_evidence(record.key.lifecycle_id, handle, offset=0, length=8,
                              token=token, evidence_mode='offline_sealed')


def test_wire_compatibility_defaults_old_packet_to_live_without_allowing_forged_offline(repository, tmp_path):
    service, store, (record, _), token = context_service(repository, tmp_path)
    grant(service, store, record, b'SEALED')
    packet = service.build(record.key.lifecycle_id, expected_revision=0, token=token)
    old_wire = packet.model_dump(mode='json')
    del old_wire['evidence_mode']
    assert ContextPacket.model_validate_json(json.dumps(old_wire)).evidence_mode == 'live'
    for patch in ({'disposition_receipt_ref': None}, {'schema_version': True},
                  {'evidence_mode': 'offline_sealed'}, {'accessible_evidence': ['fake']}):
        with pytest.raises(ValidationError):
            ContextPacket.model_validate_json(json.dumps(old_wire | patch))


@pytest.mark.parametrize('kind,patch', [
    ('identity', {'run_ref': 'run/other'}), ('identity', {'node_id': 'N09'}),
    ('identity', {'graph_ref': 'graph/other'}), ('result', {'execution_status': 'succeeded'}),
])
def test_original_failure_cannot_be_rebound_or_rewritten(repository, tmp_path, kind, patch):
    service, _store, (record, _), token = context_service(repository, tmp_path)
    with repository.transaction() as tx:
        source = tx.get('receipts', record.source_receipt)['body']
        source[kind].update(patch)
        tx.put('receipts', record.source_receipt, source)
    with pytest.raises(PermissionError):
        service.build(record.key.lifecycle_id, expected_revision=0, token=token)


def test_foreign_owner_and_null_owner_cannot_read_sealed_context(repository, tmp_path):
    service, store, (record, _), _token = context_service(repository, tmp_path)
    handle, _ = grant(service, store, record, b'SEALED')
    with repository.transaction() as tx:
        other = tx.acquire_thread('other/thread', 'other', 20)
    for token in (None, other):
        with pytest.raises((PermissionError, Conflict)):
            service.build(record.key.lifecycle_id, expected_revision=0, token=token, evidence_mode='offline_sealed')
        with pytest.raises((PermissionError, Conflict)):
            service.read_evidence(record.key.lifecycle_id, handle, offset=0, length=6,
                                  token=token, evidence_mode='offline_sealed')


def test_modern_builder_uses_trusted_packet_and_preserves_existing_supervisor_identity(repository, tmp_path):
    from sandbox_graph.graphs.executor import NodeContext
    from sandbox_graph.runtime.agents import LifecycleContextBuilder
    service, _store, (record, _), token = context_service(repository, tmp_path)
    packet = LifecycleContextBuilder(service).build(lifecycle_id=record.key.lifecycle_id,
        expected_revision=0, token=token)
    old_context = NodeContext('N02', 'agent:work:work/a', 't', 'work/a', 'run/a',
                             'plan/a', 'graph/a', 'a', 'v', {}, {}, 1, 'lease')
    assert packet.node_supervisor_id == old_context.node_supervisor_id
    with pytest.raises(TypeError):
        LifecycleContextBuilder(service).build(lifecycle_id=record.key.lifecycle_id,
            expected_revision=0, token=token, accessible_evidence=('forged',))


def test_changed_context_hash_or_new_service_cannot_reset_or_expand_read_allowance(repository, tmp_path):
    from sandbox_graph.services.lifecycle_context import LifecycleContextService
    service, store, (record, _), token = context_service(repository, tmp_path,
        max_read_bytes=4, total_evidence_bytes=4)
    handle, _ = grant(service, store, record, b'PUBLIC')
    assert service.read_evidence(record.key.lifecycle_id, handle, offset=0, length=4, token=token) == b'PUBL'
    changed = LifecycleContextService(repository, store, artifact_scope='evidence',
        secret_values=('SECRET_CANARY',), max_read_bytes=4, total_evidence_bytes=8)
    with pytest.raises(Conflict, match='policy'):
        changed.read_evidence(record.key.lifecycle_id, handle, offset=4, length=2, token=token)
    assert repository.rows[('lifecycle_context_usage', record.key.lifecycle_id)]['body']['bytes_read'] == 4


def test_one_actual_disposition_can_bind_two_lifecycles_without_reinterpreting_legacy_evidence(repository, tmp_path):
    _service, _store, (n02, n09), token = context_service(repository, tmp_path)
    safety = LifecycleSafetyService(repository)
    proof = safety.produce_disposition('work/a', token, ttl=40)
    legacy = {'lifecycle_id': n02.key.lifecycle_id, 'kind': 'resource_disposition'}
    with repository.transaction() as tx:
        tx.put('lifecycle_evidence', proof, legacy, immutable=True)
    service = LifecycleService(repository)
    service.bind_disposition(n02.key.lifecycle_id, proof, token=token)
    service.bind_disposition(n09.key.lifecycle_id, proof, token=token)
    with repository.transaction() as tx:
        assert tx.get('lifecycle_evidence', proof)['body'] == legacy
        LifecycleService._evidence_in(tx, n02, (proof,))
        LifecycleService._evidence_in(tx, n09, (proof,))
    for record in (n02, n09):
        assert repository.rows[('lifecycle_dispositions', record.key.lifecycle_id)]['body']['receipt_ref'] == proof


def test_lifecycle_evidence_pair_cannot_authorize_another_lifecycle_or_scope(repository, tmp_path):
    _service, _store, (n02, n09), token = context_service(repository, tmp_path)
    with repository.transaction() as tx:
        LifecycleService.bind_evidence_in(tx, n02, 'private-fact', kind='diagnosis')
        LifecycleService._evidence_in(tx, n02, ('private-fact',))
        with pytest.raises(PermissionError):
            LifecycleService._evidence_in(tx, n09, ('private-fact',))
        tx.put('lifecycle_evidence', 'legacy-fact', {'lifecycle_id': n02.key.lifecycle_id, 'kind': 'diagnosis'})
        LifecycleService._evidence_in(tx, n02, ('legacy-fact',))
        with pytest.raises(PermissionError):
            LifecycleService._evidence_in(tx, n09, ('legacy-fact',))
        tx.put('work_instances', 'work/other', {'work_id': 'work/other', 'run_id': 'run/a',
            'graph_ref': 'graph/a', 'plan_ref': 'plan/a', 'contract_ref': 'contract/a'})
        LifecycleSafetyService.acquire_work_in(tx, 'work/other', token, purpose='diagnosis')
        foreign = LifecycleSafetyService.produce_disposition_in(tx, 'work/other', token, ttl=40)
    with pytest.raises(Conflict):
        LifecycleService(repository).bind_disposition(n02.key.lifecycle_id, foreign, token=token)


def test_failure_effect_certainty_and_reconciliation_facts_are_preserved(repository, tmp_path):
    service, _store, (record, _), token = context_service(repository, tmp_path)
    packet = service.build(record.key.lifecycle_id, expected_revision=0, token=token)
    assert 'error_effect_certainty=none' in packet.facts
    assert 'error_retry_class=never' in packet.facts
    assert 'cleanup_status=not_needed' in packet.facts
    assert 'data_status=unverified' in packet.facts


def test_existing_sensitive_keys_and_x_api_token_are_all_redacted(repository, tmp_path):
    service, store, (record, _), token = context_service(repository, tmp_path)
    handle, _ = grant(service, store, record,
        b'{"apiSecret":"A","accessToken":"B","manifestKey":"C","secretValue":"D","x-api-token":"E"}')
    payload = service.read_evidence(record.key.lifecycle_id, handle, offset=0,
        length=len(b'{"accessToken":"[REDACTED]","apiSecret":"[REDACTED]","manifestKey":"[REDACTED]","secretValue":"[REDACTED]","x-api-token":"[REDACTED]"}'), token=token)
    assert json.loads(payload) == dict(apiSecret='[REDACTED]', accessToken='[REDACTED]',
        manifestKey='[REDACTED]', secretValue='[REDACTED]', **{'x-api-token': '[REDACTED]'})


def test_registered_secret_in_json_key_is_rejected_without_leaking_error(repository, tmp_path):
    service, store, (record, _), _token = context_service(repository, tmp_path)
    with pytest.raises(ValueError) as rejected:
        grant(service, store, record, b'{"SECRET_CANARY":"public"}')
    assert 'SECRET_CANARY' not in str(rejected.value)


def test_forged_unvalidated_artifact_digest_never_opens_outside_root(repository, tmp_path):
    service, store, (record, _), _token = context_service(repository, tmp_path)
    raw = store.commit([b'PUBLIC'], scope='evidence', media_type='text/plain', producer='test')
    # model_copy is deliberately unchecked; the service boundary must revalidate
    # instead of letting path_for open an escaped filename during hash checking.
    forged = raw.model_copy(update={'digest': '../outside', 'uri': 'evidence/../outside'})
    with pytest.raises((ValueError, PermissionError)):
        service.grant_evidence(record.key.lifecycle_id, artifact=forged,
            producer_identity=dict(run_ref='run/a', scope='work/a', node_id='N02',
                graph_ref='graph/a', plan_ref='plan/a', contract_ref='contract/a'), visibility='private')


def test_live_packet_cannot_use_new_work_version_disposition_for_old_failure(repository, tmp_path):
    service, _store, (record, _), token = context_service(repository, tmp_path)
    with repository.transaction() as tx:
        LifecycleSafetyService.release_work_in(tx, 'work/a', token)
        work = tx.get('work_instances', 'work/a')['body']
        tx.put('work_instances', 'work/a', work | {'plan_ref': 'plan/new'})
        facts = tx.get('work_safety_facts', 'work/a')['body']
        tx.put('work_safety_facts', 'work/a', facts | {'plan_ref': 'plan/new', 'work_revision': 1})
        for node in ('N07', 'N09', 'N10'):
            receipt = tx.get('receipts', 'closed/' + node)['body']
            receipt['identity']['plan_ref'] = 'plan/new'
            tx.put('receipts', 'closed/' + node, receipt)
        LifecycleSafetyService.acquire_work_in(tx, 'work/a', token, purpose='diagnosis')
        fresh = LifecycleSafetyService.produce_disposition_in(tx, 'work/a', token, ttl=40)
    LifecycleService(repository).bind_disposition(record.key.lifecycle_id, fresh, token=token)
    with pytest.raises((PermissionError, Conflict)):
        service.build(record.key.lifecycle_id, expected_revision=0, token=token)


def test_registered_numeric_secret_is_not_reintroduced_by_json_serialization(repository, tmp_path):
    from sandbox_graph.services.lifecycle_context import LifecycleContextService
    _service, store, (record, _), _token = context_service(repository, tmp_path)
    service = LifecycleContextService(repository, store, artifact_scope='evidence',
        secret_values=('SECRET_CANARY', '1234567'))
    with pytest.raises(ValueError) as rejected:
        grant(service, store, record, b'{"measurement":1234567}')
    assert '1234567' not in str(rejected.value)
