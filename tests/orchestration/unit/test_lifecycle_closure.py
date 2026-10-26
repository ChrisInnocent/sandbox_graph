"""ALU-20: attempt delivery cannot hide live repair/candidate work."""
import pytest

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.closure import ClosureService
from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle


def lifecycle(repository, phase):
    service, token = setup_lifecycle(repository)
    record = service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    record = record.model_copy(update={'phase': phase})
    with repository.transaction() as tx:
        tx.put('agent_lifecycles', record.key.lifecycle_id, record.model_dump(mode='json'))
    return record


def archive(repository, *, partial=False):
    return ClosureService(repository).seal_archive('run/a', expected_revision=0,
        analysis_ref='a', report_analysis_ref='a', excel_analysis_ref='a', inflight=(),
        artifact_refs=(), scopes={'work/a': (NodeResult(execution_status='succeeded',
            scope_disposition='blocked_closed' if partial else 'fulfilled',
            data_status='insufficient' if partial else 'valid', cleanup_status='complete'), 'review/a')})


@pytest.mark.parametrize('phase', ['draining', 'diagnosing', 'remediation_pending',
    'remediating', 'backoff', 'reconciling', 'reentering', 'candidate_building',
    'candidate_verifying', 'promotion_pending', 'verified_waiting_worker',
    'waiting_decision', 'waiting_dependency', 'cancelling', 'blocked'])
def test_every_nonterminal_lifecycle_prevents_archive_even_with_delivered_attempt(repository, phase):
    lifecycle(repository, phase)
    with pytest.raises(Conflict, match='lifecycle'):
        archive(repository)
    with repository.transaction() as tx:
        assert tx.scan('archives') == []


@pytest.mark.parametrize('kind', ['remedy', 'supervision', 'candidate_build',
                                 'candidate_verify', 'promotion', 'agent_team', 'lifecycle'])
def test_running_lifecycle_jobs_prevent_archive_after_terminal_phase(repository, kind):
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('jobs', 'child', {'run_id': 'run/a', 'job_kind': kind, 'status': 'running'})
    with pytest.raises(Conflict, match='in-flight job'):
        archive(repository)


def test_unknown_repair_marker_without_domains_is_not_clean_archive(repository):
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('repair_isolations', 'unknown', {'work_ref': 'work/a', 'operation_refs': [],
                                             'reason': 'operation_binding_missing'})
    with pytest.raises(Conflict, match='isolation'):
        archive(repository)


def test_explicit_partial_close_keeps_isolation_and_failure_visible(repository):
    record = lifecycle(repository, 'blocked_closed')
    with repository.transaction() as tx:
        tx.put('agent_lifecycles', record.key.lifecycle_id,
               record.model_copy(update={'isolation_refs': ('unknown',)}).model_dump(mode='json'))
        tx.put('repair_isolations', 'unknown', {'work_ref': 'work/a', 'operation_refs': [],
                                             'reason': 'operation_binding_missing'})
    result = archive(repository, partial=True)
    assert result['status'] == 'partial'
    assert result['lifecycle_status'][record.key.lifecycle_id] == 'blocked_closed'
    assert result['repair_isolation_refs'] == ['unknown']
    with repository.transaction() as tx:
        assert tx.get('repair_isolations', 'unknown') is not None


def test_fulfilled_lifecycle_allows_complete_archive(repository):
    record = lifecycle(repository, 'fulfilled')
    result = archive(repository)
    assert result['status'] == 'complete'
    assert result['lifecycle_status'] == {record.key.lifecycle_id: 'fulfilled'}


def test_lifecycle_in_another_run_does_not_block_this_archive(repository):
    record = lifecycle(repository, 'diagnosing')
    with repository.transaction() as tx:
        other = record.model_copy(update={'key': record.key.model_copy(update={'run_ref': 'other'})})
        tx.put('agent_lifecycles', record.key.lifecycle_id, other.model_dump(mode='json'))
    assert archive(repository)['status'] == 'complete'


@pytest.mark.parametrize('state,children_stopped', [('running', False), ('succeeded', False)])
def test_live_provider_children_prevent_archive_even_after_parent_lifecycle_terminal(repository, state, children_stopped):
    from sandbox_graph.runtime.agent_bridge import RequestRecord
    from tests.orchestration.unit.test_agent_bridge import envelope, receipt, scope
    record = lifecycle(repository, 'fulfilled')
    env = envelope(lifecycle_id=record.key.lifecycle_id, scope=scope('work/a'))
    response = receipt(env, state=state, children_stopped=children_stopped)
    request = RequestRecord(schema_version=1, provider_id='local', envelope=env,
                            state='terminal' if state=='succeeded' else 'submitted', receipt=response)
    with repository.transaction() as tx:
        tx.put('agent_provider_requests', env.request_id, request.model_dump(mode='json'))
    with pytest.raises(Conflict, match='provider'):
        archive(repository)


def test_sampler_and_shared_work_owner_prevent_archive(repository):
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('observations', 'sampling', {'scope': 'work/a', 'status': 'running'})
    with pytest.raises(Conflict, match='observation'):
        archive(repository)
    with repository.transaction() as tx:
        tx.delete('observations', 'sampling')
        tx.put('work_execution_leases', 'work/a', {'identity': {'scope': 'work/a'}, 'owner': {}})
    with pytest.raises(Conflict, match='work execution'):
        archive(repository)


def unknown_provider(repository, lifecycle_id, *, kind='group', ref='G_A'):
    from sandbox_graph.runtime.agent_bridge import ProviderScope, RequestRecord
    from tests.orchestration.unit.test_agent_bridge import envelope
    env = envelope(lifecycle_id=lifecycle_id,
                   scope=ProviderScope(schema_version=1, kind=kind, ref=ref))
    request = RequestRecord(schema_version=1, provider_id='local', envelope=env,
                            state='submitted_unknown')
    with repository.transaction() as tx:
        tx.put('agent_provider_requests', env.request_id, request.model_dump(mode='json'))


@pytest.mark.parametrize('kind', ['cleanup', 'reconcile'])
@pytest.mark.parametrize('status', ['pending', 'running', 'draining'])
@pytest.mark.parametrize('run_id', ['run/a', 'run/b'])
def test_active_safety_jobs_block_only_their_run(repository, kind, status, run_id):
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('jobs', 'active-safety-job', {'run_id': run_id, 'scope_ref': 'work/' + run_id.rsplit('/', 1)[1],
            'job_kind': kind, 'status': status})
    if run_id == 'run/a':
        with pytest.raises(Conflict, match='in-flight job'):
            archive(repository)
        with repository.transaction() as tx:
            assert tx.scan('archives') == []
    else:
        assert archive(repository)['status'] == 'complete'


@pytest.mark.parametrize('kind', ['analysis', 'report', 'graph:analysis', 'graph:report'])
def test_enclosing_publication_job_does_not_block_archive(repository, kind):
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('jobs', 'publication', {'run_id': 'run/a', 'job_kind': kind, 'status': 'running'})
    assert archive(repository)['status'] == 'complete'


@pytest.mark.parametrize('blocker,match,provider_kind', [('observations', 'observation', None),
    ('repair_isolations', 'isolation', None), ('work_execution_leases', 'work execution', None),
    ('agent_provider_requests', 'provider', 'group'),
    ('agent_provider_requests', 'provider', 'work'),
    ('agent_provider_requests', 'provider', 'node'),
    ('agent_provider_requests', 'provider', 'candidate')])
def test_operation_owned_scope_is_checked_before_archive(repository, blocker, match, provider_kind):
    lifecycle(repository, 'fulfilled')
    scope = 'group/operation'
    with repository.transaction() as tx:
        tx.put('operations', 'op/group', {'run_id': 'run/a', 'scope': scope, 'phase': 'terminal'})
        if blocker == 'observations':
            tx.put(blocker, 'live-group-sampler', {'scope': scope, 'status': 'running'})
        elif blocker == 'repair_isolations':
            tx.put(blocker, 'unknown-group', {'work_ref': scope, 'operation_refs': ['op/group']})
        elif blocker == 'work_execution_leases':
            tx.put(blocker, scope, {'identity': {'scope': scope}, 'owner': {}})
    if blocker == 'agent_provider_requests':
        unknown_provider(repository, 'missing-lifecycle', kind=provider_kind, ref=scope)
    with pytest.raises(Conflict, match=match):
        archive(repository)


def test_registered_group_scope_is_checked_without_caller_summary(repository):
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('groups', 'run/a:G_A', {'run_id': 'run/a', 'group_id': 'G_A'})
        tx.put('observations', 'live-group-sampler', {'scope': 'G_A', 'status': 'running'})
    with pytest.raises(Conflict, match='observation'):
        archive(repository)


@pytest.mark.parametrize('operation_run,operation_scope', [
    ('run/a', 'G_A'), ('run/b', 'G_A'), (None, 'G_A'), ('run/b', 'G_B')])
def test_shared_group_sampler_uses_operation_run_before_scope_label(repository, operation_run, operation_scope):
    from sandbox_graph.runtime.observation import ObservationService
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        for run_id in ('run/a', 'run/b'):
            tx.put('groups', run_id + ':G_A', {'run_id': run_id, 'group_id': 'G_A'})
        if operation_run is not None:
            tx.put('operations', 'op/sampler', {'run_id': operation_run,
                'scope': operation_scope, 'phase': 'running'})
    started = ObservationService(repository).start('sampler/shared-group', 'G_A',
        operation_id='op/sampler', clock_epoch='epoch/a', deadline=1100)
    if operation_run == 'run/b' and operation_scope == 'G_A':
        assert archive(repository)['status'] == 'complete'
    else:
        with pytest.raises(Conflict, match='observation'):
            archive(repository)
        with repository.transaction() as tx:
            assert tx.scan('archives') == []
    with repository.transaction() as tx:
        assert tx.get('observations', 'sampler/shared-group')['body'] == started


@pytest.mark.parametrize('lease_run,scope,blocked', [('run/a', 'G_A', True),
    ('run/b', 'G_A', False), (None, 'G_A', True),
    ('run/b', 'work/a', True), ('run/a', 'work/b', True)])
def test_shared_group_resource_reservation_uses_run_ownership(repository, lease_run, scope, blocked):
    from sandbox_graph.runtime.leases import LeaseService
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('work_instances', 'work/b', {'work_id': 'work/b', 'run_id': 'run/b', 'group_id': 'G_A'})
        for run_id in ('run/a', 'run/b'):
            tx.put('groups', run_id + ':G_A', {'run_id': run_id, 'group_id': 'G_A'})
    service = LeaseService(repository)
    token = service.acquire_thread('reservation-thread', 'reservation-owner', 100)
    service.acquire_all(('host/shared-group',), scope=scope, token=token, owner_run_id=lease_run)
    with repository.transaction() as tx:
        reserved = tx.get('resource_leases', 'host/shared-group')['body']
    if blocked:
        with pytest.raises(Conflict, match='resource reservation'):
            archive(repository)
        with repository.transaction() as tx:
            assert tx.scan('archives') == []
    else:
        assert archive(repository)['status'] == 'complete'
    with repository.transaction() as tx:
        assert tx.get('resource_leases', 'host/shared-group')['body'] == reserved


def test_foreign_operation_does_not_claim_scope_for_this_run(repository):
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('operations', 'op/foreign', {'run_id': 'run/b', 'scope': 'group/foreign', 'phase': 'terminal'})
        tx.put('groups', 'run/b:foreign', {'run_id': 'run/b', 'group_id': 'group/foreign'})
        tx.put('observations', 'foreign-sampler', {'scope': 'group/foreign', 'status': 'running'})
    unknown_provider(repository, 'missing-lifecycle', ref='group/foreign')
    assert archive(repository)['status'] == 'complete'


@pytest.mark.parametrize('provider_run', ['run/a', 'run/b'])
def test_provider_lifecycle_run_overrides_shared_group_label(repository, provider_run):
    record = lifecycle(repository, 'fulfilled')
    provider_lifecycle = record.model_copy(update={'key': record.key.model_copy(update={
        'run_ref': provider_run, 'scope_kind': 'group', 'scope_ref': 'G_A',
        'lifecycle_id': 'life/group'})})
    with repository.transaction() as tx:
        current_group = record.model_copy(update={'key': record.key.model_copy(update={
            'scope_kind': 'group', 'scope_ref': 'G_A'})})
        tx.put('agent_lifecycles', record.key.lifecycle_id, current_group.model_dump(mode='json'))
        tx.put('agent_lifecycles', 'life/group', provider_lifecycle.model_dump(mode='json'))
        for run_id in ('run/a', 'run/b'):
            tx.put('groups', run_id + ':G_A', {'run_id': run_id, 'group_id': 'G_A'})
    unknown_provider(repository, 'life/group')
    if provider_run == 'run/a':
        with pytest.raises(Conflict, match='provider'):
            archive(repository)
    else:
        assert archive(repository)['status'] == 'complete'


@pytest.mark.parametrize('kind,ref,blocked', [('run', 'run/a', True),
    ('run', 'run/b', False), ('work', 'work/a', True), ('work', 'work/b', False),
    ('group', 'G_A', True), ('node', 'N01', False)])
def test_orphan_provider_uses_scope_kind_and_ledger_ownership(repository, kind, ref, blocked):
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('work_instances', 'work/b', {'work_id': 'work/b', 'run_id': 'run/b'})
        for run_id in ('run/a', 'run/b'):
            tx.put('groups', run_id + ':G_A', {'run_id': run_id, 'group_id': 'G_A'})
    unknown_provider(repository, 'missing-lifecycle', kind=kind, ref=ref)
    if blocked:
        with pytest.raises(Conflict, match='provider'):
            archive(repository)
    else:
        assert archive(repository)['status'] == 'complete'


def test_partial_isolation_uses_bound_lifecycle_not_unrelated_fulfilled_issue(repository):
    record = lifecycle(repository, 'blocked_closed')
    bound = record.model_copy(update={'isolation_refs': ('unknown',)})
    fulfilled = record.model_copy(update={'phase': 'fulfilled', 'key': record.key.model_copy(
        update={'lifecycle_id': 'life/fulfilled'}), 'isolation_refs': ()})
    with repository.transaction() as tx:
        tx.put('agent_lifecycles', record.key.lifecycle_id, bound.model_dump(mode='json'))
        tx.put('agent_lifecycles', 'life/fulfilled', fulfilled.model_dump(mode='json'))
        tx.put('repair_isolations', 'unknown', {'work_ref': 'work/a', 'operation_refs': []})
    result = archive(repository, partial=True)
    assert result['status'] == 'partial'
    assert result['repair_isolation_refs'] == ['unknown']
    assert result['lifecycle_status'] == {record.key.lifecycle_id: 'blocked_closed',
                                         'life/fulfilled': 'fulfilled'}
    with repository.transaction() as tx:
        assert tx.get('repair_isolations', 'unknown') is not None
        assert tx.get('agent_lifecycles', record.key.lifecycle_id)['body'] == bound.model_dump(mode='json')
        assert tx.get('agent_lifecycles', 'life/fulfilled')['body'] == fulfilled.model_dump(mode='json')


@pytest.mark.parametrize('binding,phase,scope', [((), 'blocked_closed', 'work/a'),
    (('different-marker',), 'blocked_closed', 'work/a'),
    (('unknown',), 'fulfilled', 'work/a'), (('unknown',), 'blocked_closed', 'work/b'),
    (('unknown',), 'blocked', 'work/a')])
def test_partial_isolation_requires_matching_blocked_closed_binding(repository, binding, phase, scope):
    record = lifecycle(repository, phase)
    with repository.transaction() as tx:
        tx.put('agent_lifecycles', record.key.lifecycle_id, record.model_copy(update={
            'key': record.key.model_copy(update={'scope_ref': scope}),
            'isolation_refs': binding}).model_dump(mode='json'))
        tx.put('repair_isolations', 'unknown', {'work_ref': 'work/a', 'operation_refs': []})
    with pytest.raises(Conflict, match='lifecycle|isolation'):
        archive(repository, partial=True)


def test_legacy_ledger_stopped_still_prevents_archive(repository):
    from sandbox_graph.runtime.observation import ObservationService
    lifecycle(repository,'fulfilled')
    service=ObservationService(repository)
    service.start('legacy','work/a',operation_id='unknown',clock_epoch='old',deadline=1100)
    service.stop('legacy',last_seq=0)
    with pytest.raises(Conflict,match='observation'):
        archive(repository)
