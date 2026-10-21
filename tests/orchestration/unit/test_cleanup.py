"""P6.4 / ORC-024,028,035,045: cleanup is ownership-bound and confirms separately."""
import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.runtime.runner import CleanupResult, OperationService


class FakeGateway:
    def __init__(self, result):
        self.result = result

    async def cleanup(self, scope, resource_ids):
        return self.result


def resource(scope):
    return {'scope': scope, 'operation_id': 'o', 'status': 'active'}


def service(repository, result):
    return OperationService(repository, FakeGateway(result), {})


async def test_cleanup_rejects_a_foreign_resource_and_records_closure(repository):
    token = LeaseService(repository).acquire_thread('t', 'owner', 100)
    with repository.transaction() as tx:
        tx.put('resources', 'r1', resource('g'))
        tx.put('resources', 'foreign', resource('other'))
    with pytest.raises(PermissionError):
        await service(repository, CleanupResult(status='complete', resource_ids=('foreign',))).cleanup(
            'g', ('foreign',), token)
    await service(repository, CleanupResult(status='complete', resource_ids=('r1',))).cleanup(
        'g', ('r1',), token)
    assert repository.rows[('resources', 'r1')]['body']['status'] == 'closed'


async def test_cleanup_result_must_match_the_requested_resource_set(repository):
    token = LeaseService(repository).acquire_thread('t', 'owner', 100)
    with repository.transaction() as tx:
        tx.put('resources', 'r1', resource('g'))
    with pytest.raises(Conflict):
        await service(repository, CleanupResult(status='complete', resource_ids=('other',))).cleanup(
            'g', ('r1',), token)
