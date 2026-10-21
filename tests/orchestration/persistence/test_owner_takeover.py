"""L2 D10 (task 4.3): owner takeover stops new dispatch and isolates unknown domains.

Skips without real PostgreSQL.
"""
import pytest

from sandbox_graph.persistence.repository import Conflict, StaleOwner
from sandbox_graph.runtime.leases import LeaseService


def test_new_owner_cannot_grab_a_quarantined_domain_and_stale_token_is_fenced(postgres):
    repo, _ = postgres
    service = LeaseService(repo)
    old = service.acquire_thread('t', 'old', 1)
    service.acquire_all(('d',), scope='s1', token=old)
    service.quarantine(('d',), scope='s1', reason='unknown external effect', token=old)
    with repo.transaction() as tx:
        tx.connection.execute("UPDATE thread_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE thread_id='t'")
    new = service.acquire_thread('t', 'new', 30)
    with pytest.raises(Conflict):
        service.acquire_all(('d',), scope='s2', token=new)      # not released by lease expiry
    with pytest.raises(StaleOwner):
        with repo.transaction() as tx:
            tx.assert_fence(old)                               # the stale owner cannot write
    with repo.transaction() as tx:
        assert tx.get('resource_leases', 'd')['body']['status'] == 'quarantine'
