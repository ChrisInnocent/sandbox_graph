"""L2 D10 (task 4.2): FencedPostgresSaver writes share the lease row. Skips without real PostgreSQL."""
import pytest
import psycopg
from psycopg.rows import dict_row
from langgraph.checkpoint.base import empty_checkpoint

from sandbox_graph.persistence.repository import StaleOwner
from sandbox_graph.persistence.checkpointer import FencedPostgresSaver
from sandbox_graph.runtime.leases import LeaseService

CONFIG = {'configurable': {'thread_id': 't', 'checkpoint_ns': ''}}


def test_stale_owner_cannot_write_checkpoint_or_pending_writes(postgres):
    repo, dsn = postgres
    token = LeaseService(repo).acquire_thread('t', 'a', 10)
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        saver = FencedPostgresSaver(conn, token)
        saved = saver.put(CONFIG, empty_checkpoint(), {}, {})
        with repo.transaction() as tx:
            tx.connection.execute("UPDATE thread_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE thread_id='t'")
        with pytest.raises(StaleOwner):
            saver.put(CONFIG, empty_checkpoint(), {}, {})
        with pytest.raises(StaleOwner):
            saver.put_writes(saved, [('result', 'old')], 'task')


def test_new_owner_commits_pending_writes(postgres):
    repo, dsn = postgres
    token = LeaseService(repo).acquire_thread('t', 'a', 10)
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        saved = FencedPostgresSaver(conn, token).put(CONFIG, empty_checkpoint(), {}, {})
    with repo.transaction() as tx:
        tx.connection.execute("UPDATE thread_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE thread_id='t'")
    new = LeaseService(repo).acquire_thread('t', 'b', 30)
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        current = FencedPostgresSaver(conn, new)
        current.put_writes(saved, [('result', 'new')], 'task')
        assert current.get_tuple(saved).pending_writes[0][2] == 'new'
