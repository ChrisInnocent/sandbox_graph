"""ST-02/03/13: literal ownership and closure oracle, no product environment."""
import pytest

from sandbox_graph.services.lifecycle_inventory import LifecycleInventory
from tests.orchestration.unit.test_agent_bridge import envelope, scope as provider_scope
from sandbox_graph.runtime.agent_bridge import RequestRecord


def topology(tx):
    for run in ('a', 'b'):
        tx.put('groups', run + ':shared', {'run_id': run, 'group_id': 'shared'})
        tx.put('work_instances', 'work/' + run, {'run_id': run, 'group_id': 'shared', 'work_id': 'work/' + run})
        tx.put('agent_lifecycles', 'life/' + run, {'key': {'run_ref': run, 'scope_kind': 'work',
            'scope_ref': 'work/' + run, 'lifecycle_id': 'life/' + run}})


@pytest.mark.parametrize('kind,scope', [('run', 'a'), ('group', 'shared'), ('work', 'work/a'), ('node', 'work/a')])
@pytest.mark.parametrize('binding,want', [('owned', 'owned'), ('foreign', None), ('conflict', 'ambiguous'), ('legacy', 'ambiguous')])
def test_typed_ownership_matrix(repository, kind, scope, binding, want):
    with repository.transaction() as tx:
        topology(tx)
        op_run = 'b' if binding == 'foreign' else 'a'
        op_scope = 'work/' + op_run
        tx.put('operations', 'op', {'run_id': op_run, 'scope': op_scope, 'phase': 'submission_started'})
        body = {'scope': op_scope, 'operation_id': 'op', 'status': 'active'}
        if binding == 'conflict': body['run_id'] = 'b'
        if binding == 'legacy': body = {'scope': scope, 'status': 'active'}
        tx.put('resources', 'resource', body)
        snapshot = LifecycleInventory.collect_in(tx, run_ref='a', scope_kind=kind, scope_ref=scope)
        items = [i for i in snapshot.items if i.kind == 'resources']
        if want is None:
            assert items == []
        else:
            # A globally identified work is stronger than a bare shared label.
            if binding == 'legacy' and scope == 'work/a': want = 'owned'
            assert len(items) == 1 and items[0].ownership == want
            assert not items[0].closed


@pytest.mark.parametrize('parent,scope', [('run', 'a'), ('group', 'shared')])
def test_parent_includes_child_provider(repository, parent, scope):
    with repository.transaction() as tx:
        topology(tx)
        for run in ('a', 'b'):
            env = envelope(request_id='provider/' + run, lifecycle_id='life/' + run,
                scope=provider_scope('work/' + run))
            request = RequestRecord(schema_version=1, provider_id='local', envelope=env, state='submitted_unknown')
            tx.put('agent_provider_requests', env.request_id, request.model_dump(mode='json'))
        snapshot = LifecycleInventory.collect_in(tx, run_ref='a', scope_kind=parent, scope_ref=scope)
        assert [i.key for i in snapshot.items if i.kind == 'agent_provider_requests'] == ['provider/a']


def test_unlinked_owned_resource_and_live_work_lease_are_not_dropped(repository):
    with repository.transaction() as tx:
        topology(tx)
        tx.put('resources', 'unlinked', {'scope': 'work/a', 'status': 'active'})
        tx.put('work_execution_leases', 'work/a', {'identity': {'run_ref': 'a', 'scope': 'work/a'}})
        snapshot = LifecycleInventory.collect_in(tx, run_ref='a', scope_kind='run', scope_ref='a')
        assert {(i.kind, i.key) for i in snapshot.blockers} == {('resources', 'unlinked'), ('work_execution_leases', 'work/a')}


def test_closed_rows_and_hashes_are_evidence_not_discarded(repository):
    with repository.transaction() as tx:
        topology(tx)
        tx.put('resources', 'r', {'scope': 'work/a', 'status': 'closed'})
        before = LifecycleInventory.collect_in(tx, run_ref='a', scope_kind='run', scope_ref='a')
        assert before.blockers == ()
        tx.put('resources', 'r', {'scope': 'work/a', 'status': 'active'})
        after = LifecycleInventory.collect_in(tx, run_ref='a', scope_kind='run', scope_ref='a')
        assert before.digest != after.digest
        assert after.blockers[0].key == 'r'


def test_stop_and_archive_both_reject_conflicting_resource_identity(repository):
    from tests.orchestration.unit.test_lifecycle_closure import lifecycle, archive
    from sandbox_graph.services.lifecycle_stop import LifecycleStopService
    from sandbox_graph.persistence.repository import Conflict
    record = lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('operations', 'op', {'run_id': 'run/a', 'scope': 'work/a', 'phase': 'terminal'})
        tx.put('resources', 'r', {'run_id': 'other/run', 'scope': 'work/a', 'operation_id': 'op', 'status': 'active'})
        inventory = LifecycleStopService.inventory_in(tx, record)
        assert inventory['ambiguous']
    with pytest.raises(Conflict, match='ambiguous'):
        archive(repository)


def test_archive_rejects_owned_resource_without_operation_or_lease(repository):
    from tests.orchestration.unit.test_lifecycle_closure import lifecycle, archive
    from sandbox_graph.persistence.repository import Conflict
    lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        tx.put('resources', 'orphan', {'scope': 'work/a', 'status': 'active'})
    with pytest.raises(Conflict, match='resource'):
        archive(repository)


@pytest.mark.parametrize('kind', ['jobs', 'agent_provider_requests'])
def test_explicit_foreign_run_cannot_hide_an_owned_work_reference(repository, kind):
    with repository.transaction() as tx:
        topology(tx)
        if kind == 'jobs':
            body = {'run_id': 'b', 'scope_ref': 'work/a', 'job_kind': 'cleanup', 'status': 'pending'}
        else:
            env = envelope(lifecycle_id='life/b', scope=provider_scope('work/a'))
            body = RequestRecord(schema_version=1, provider_id='local', envelope=env, state='submitted_unknown').model_dump(mode='json')
        tx.put(kind, 'conflicting', body)
        snapshot = LifecycleInventory.collect_in(tx, run_ref='a', scope_kind='run', scope_ref='a')
        assert len(snapshot.blockers) == 1
        assert snapshot.blockers[0].ownership == 'ambiguous'
