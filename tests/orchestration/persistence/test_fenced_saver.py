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


@pytest.mark.parametrize('first',['writer','takeover'])
@pytest.mark.parametrize('kind',['checkpoint','pending_writes'])
@pytest.mark.parametrize('asynchronous',[False,True])
def test_actual_saver_and_takeover_serialize_on_same_lease_row(postgres,monkeypatch,first,kind,asynchronous):
    """Both lock orders, actual saver SQL, independently observed blocking PID.

    Expiry is moved by SQL solely to arrange the race; it never grants the old
    owner a new lease. The second connection must wait for the same row lock.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    from sandbox_graph.persistence import checkpointer
    repo,dsn=postgres;old=LeaseService(repo).acquire_thread('t','old',60)
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as connection:
        saved=FencedPostgresSaver(connection,old).put(CONFIG,empty_checkpoint(),{}, {})
    entered=Event();release=Event();started=Event();pids={}
    original=checkpointer.assert_fence_cursor
    def held_fence(cursor,token):
        original(cursor,token)
        if first=='writer':
            # The expiry update and the following checkpoint remain in the
            # same transaction while the takeover waits for this lease row.
            cursor.execute("UPDATE thread_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE thread_id='t'")
            entered.set()
            assert release.wait(15),'controller did not release writer barrier'
    monkeypatch.setattr(checkpointer,'assert_fence_cursor',held_fence)
    def writer():
        with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as connection:
            pids['writer']=connection.info.backend_pid;started.set()
            saver=FencedPostgresSaver(connection,old)
            if kind=='checkpoint':
                cp=empty_checkpoint()
                return asyncio.run(saver.aput(CONFIG,cp,{},{})) if asynchronous else saver.put(CONFIG,cp,{}, {})
            args=(saved,[('result','original-effect')],'racing-writer')
            return asyncio.run(saver.aput_writes(*args)) if asynchronous else saver.put_writes(*args)
    def takeover():
        with repo.transaction() as tx:
            pids['takeover']=tx.connection.info.backend_pid;started.set()
            new=tx.acquire_thread('t','new',60)
            if first=='takeover':
                entered.set()
                assert release.wait(15),'controller did not release takeover barrier'
            return new
    if first=='takeover':
        with repo.transaction() as tx:
            tx.connection.execute("UPDATE thread_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE thread_id='t'")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(writer if first=='writer' else takeover)
        try:
            assert entered.wait(10),'first actor did not acquire the lease row'
            started.clear();b=pool.submit(takeover if first=='writer' else writer)
            assert started.wait(10),'second actor did not connect'
            second='takeover' if first=='writer' else 'writer'
            with psycopg.connect(dsn,autocommit=True) as observer:
                end=time.monotonic()+10
                while time.monotonic()<end:
                    blockers=observer.execute('SELECT pg_blocking_pids(%s)',(pids[second],)).fetchone()[0]
                    if pids[first] in blockers:break
                    if b.done():raise AssertionError('second actor bypassed the held lease row')
                    time.sleep(.02)
                else:raise AssertionError('actual database lock wait was not observed')
        finally:release.set()
        a.result(timeout=10)
        if first=='takeover':
            with pytest.raises(StaleOwner):b.result(timeout=10)
        else:b.result(timeout=10)
    with repo.transaction() as tx:
        expected=int(first=='writer')
        if kind=='checkpoint':
            assert tx.connection.execute("SELECT count(*) AS n FROM checkpoints WHERE thread_id='t'").fetchone()['n']==1+expected
        else:
            assert tx.connection.execute("SELECT count(*) AS n FROM checkpoint_writes WHERE thread_id='t' AND task_id='racing-writer'").fetchone()['n']==expected
        assert tx.connection.execute("SELECT owner_epoch FROM thread_leases WHERE thread_id='t'").fetchone()['owner_epoch']==old.owner_epoch+1
