"""L2 D15 (task 3.1): real PostgreSQL ledger, immutable versions, CAS, advisory lock.

Skips without SANDBOX_GRAPH_TEST_POSTGRES_DSN; a skip is never a pass.
"""
import pytest
from threading import Barrier, Thread

from sandbox_graph.persistence.repository import Conflict


def test_immutable_record_and_kind_isolation(postgres):
    repo, _ = postgres
    with repo.transaction() as tx:
        tx.put('runs', 'r', {'run_id': 'r'}, immutable=True)
    with repo.transaction() as tx:
        tx.put('other', 'r', {'run_id': 'x'})          # same key, different kind
        assert tx.get('runs', 'r')['body'] == {'run_id': 'r'}
        assert tx.get('other', 'r')['body'] == {'run_id': 'x'}
    with pytest.raises(Conflict):
        with repo.transaction() as tx:
            tx.put('runs', 'r', {'run_id': 'other'}, immutable=True)


def test_revision_cas_rejects_a_stale_writer(postgres):
    repo, _ = postgres
    with repo.transaction() as tx:
        rev = tx.put('plans', 'p', {'v': 1})
    with repo.transaction() as tx:
        tx.put('plans', 'p', {'v': 2}, expected_revision=rev)
    with pytest.raises(Conflict):
        with repo.transaction() as tx:
            tx.put('plans', 'p', {'v': 3}, expected_revision=rev)


def test_advisory_lock_serializes_a_read_modify_write(postgres):
    repo, _ = postgres
    with repo.transaction() as tx:
        tx.put('counter', 'c', {'n': 0})
    barrier, errors = Barrier(2), []

    def bump():
        try:
            barrier.wait()
            with repo.transaction() as tx:
                current = tx.get('counter', 'c')['revision']
                tx.put('counter', 'c', {'n': 1}, expected_revision=current)
        except Conflict as exc:
            errors.append(exc)

    threads = [Thread(target=bump) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(errors) == 1          # exactly one CAS winner under the advisory lock
