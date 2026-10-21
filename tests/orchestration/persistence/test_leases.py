"""L2 D10 (task 4.1): real double-connection resource-domain atomicity + quarantine.

Skips without real PostgreSQL.
"""
import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.leases import LeaseService


def test_resource_domains_are_claimed_atomically_across_connections(postgres):
    repo, _ = postgres
    service = LeaseService(repo)
    first = service.acquire_thread('t', 'owner', 30)
    service.acquire_all(('a',), scope='s1', token=first)
    second = service.acquire_thread('t2', 'owner2', 30)
    with pytest.raises(Conflict):
        service.acquire_all(('a',), scope='s2', token=second)
    with repo.transaction() as tx:
        assert tx.get('resource_leases', 'a')['body']['scope'] == 's1'   # no partial takeover


def test_quarantine_survives_lease_expiry(postgres):
    repo, _ = postgres
    service = LeaseService(repo)
    token = service.acquire_thread('t', 'owner', 1)
    service.acquire_all(('a',), scope='s', token=token)
    service.quarantine(('a',), scope='s', reason='unknown effect', token=token)
    with repo.transaction() as tx:
        tx.connection.execute("UPDATE thread_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE thread_id='t'")
    with repo.transaction() as tx:
        assert tx.get('resource_leases', 'a')['body']['status'] == 'quarantine'   # not auto-released
