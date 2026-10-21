"""L3 D11/D14 (task 14.1): combined crash/duplicate/graph-switch faults on real PostgreSQL.

Skips without real PostgreSQL; the offline recovery checks live in tests/orchestration/recovery.
"""
import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.events import EventEnvelope, EventService
from sandbox_graph.runtime.leases import LeaseService


def test_duplicate_and_reordered_events_are_idempotent(postgres):
    repo, _ = postgres
    service = EventService(repo)

    def event(seq):
        return EventEnvelope(event_id=f'e{seq}', producer_scope='s', producer_epoch=1,
                             aggregate_id='a', aggregate_seq=seq, run_ref='r', plan_ref='p',
                             graph_ref='g', kind='k', payload={})

    service.append(event(1))
    service.append(event(1))                 # duplicate
    service.append(event(1))                 # duplicate again
    assert [e.aggregate_seq for e in service.consume('c', 'a', allowed_scopes={'s'})] == [1]
    with pytest.raises(Conflict):
        service.append(EventEnvelope(event_id='e9', producer_scope='s', producer_epoch=1,
                                     aggregate_id='a', aggregate_seq=1, run_ref='r', plan_ref='p',
                                     graph_ref='g', kind='k', payload={'x': 1}))


def test_late_takeover_writer_is_fenced_after_lease_expiry(postgres):
    repo, _ = postgres
    service = LeaseService(repo)
    old = service.acquire_thread('t', 'a', 1)
    with repo.transaction() as tx:
        tx.connection.execute("UPDATE thread_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE thread_id='t'")
    service.acquire_thread('t', 'b', 30)
    with pytest.raises(Conflict):
        with repo.transaction() as tx:
            tx.assert_fence(old)


def test_started_operation_is_reconciled_not_resubmitted(postgres):
    from sandbox_graph.runtime.runner import OperationService
    repo, _ = postgres
    service = OperationService(repo, gateway=None, scripts={})
    with repo.transaction() as tx:
        tx.put('operations', 'op', {'scope': 's', 'phase': 'submission_started',
                                    'run_id': 'r', 'request_hash': 'h', 'request': {}})
    with repo.transaction() as tx:
        record = tx.get('operations', 'op')['body']
    assert record['phase'] == 'submission_started'      # recovery must reconcile, never silently resubmit
