"""L2 D05 (task 3.2): visit/receipt idempotency and conflict. Skips without real PostgreSQL."""
import pytest

from sandbox_graph.persistence.repository import Conflict

RECEIPT = {'thread_id': 't', 'node_id': 'N05', 'visit_id': 'v1', 'input_hash': 'a' * 64,
           'result': {'execution_status': 'succeeded'}}
KEY = 't:N05:v1:' + 'a' * 64


def test_receipt_replay_is_idempotent_and_conflict_is_rejected(postgres):
    repo, _ = postgres
    with repo.transaction() as tx:
        tx.put('receipts', KEY, RECEIPT, immutable=True)
    with repo.transaction() as tx:
        tx.put('receipts', KEY, RECEIPT, immutable=True)      # same input -> reuse, no new effect
        assert tx.get('receipts', KEY)['body'] == RECEIPT
    with pytest.raises(Conflict):
        with repo.transaction() as tx:
            tx.put('receipts', KEY, RECEIPT | {'result': {'execution_status': 'failed'}}, immutable=True)


def test_a_new_loop_visit_gets_a_new_identity(postgres):
    repo, _ = postgres
    with repo.transaction() as tx:
        tx.put('receipts', 't:N05:v1:h', {'visit_id': 'v1'}, immutable=True)
        tx.put('receipts', 't:N05:v2:h', {'visit_id': 'v2'}, immutable=True)
        assert tx.get('receipts', 't:N05:v1:h')['body']['visit_id'] == 'v1'
        assert tx.get('receipts', 't:N05:v2:h')['body']['visit_id'] == 'v2'
