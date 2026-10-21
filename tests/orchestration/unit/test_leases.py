"""D18.3 / ORC-033,035,036: atomic resource domains, verified closure, scope-bound quarantine."""
import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.leases import LeaseService


def test_resource_domains_are_claimed_atomically_without_partial_locks(repository):
    service = LeaseService(repository)
    token = service.acquire_thread('t', 'owner', 100)
    service.acquire_all(('a', 'b'), scope='s', token=token)
    with pytest.raises(Conflict):
        service.acquire_all(('b', 'c'), scope='other', token=token)
    assert ('resource_leases', 'c') not in repository.rows   # the failed batch left no lock


def test_release_requires_verified_closure_and_terminal_operations(repository):
    service = LeaseService(repository)
    token = service.acquire_thread('t', 'owner', 100)
    service.acquire_all(('a',), scope='s', token=token)
    with pytest.raises(Conflict):
        service.release(('a',), scope='s', token=token, closed=False)
    repository.rows[('operations', 'op1')] = {'key': 'op1', 'revision': 0,
                                              'body': {'scope': 's', 'phase': 'running', 'status': {'state': 'running'}}}
    with pytest.raises(Conflict):
        service.release(('a',), scope='s', token=token, closed=True, operation_refs=('op1',))
    repository.rows[('operations', 'op1')]['body'] = {'scope': 's', 'phase': 'terminal',
                                                      'status': {'state': 'completed'}}
    service.release(('a',), scope='s', token=token, closed=True, operation_refs=('op1',))
    assert ('resource_leases', 'a') not in repository.rows


def test_quarantine_is_scope_bound_and_survives_lease_expiry(repository):
    service = LeaseService(repository)
    token = service.acquire_thread('t', 'owner', 100)
    service.acquire_all(('a',), scope='s', token=token)
    with pytest.raises(Conflict):
        service.quarantine(('a',), scope='other', reason='r', token=token)
    service.quarantine(('a',), scope='s', reason='unknown external effect', token=token)
    repository.clock.advance(10_000)                          # thread lease expires
    assert repository.rows[('resource_leases', 'a')]['body']['status'] == 'quarantine'
