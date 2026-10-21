"""L2 tests require real PostgreSQL. Missing DSN is explicitly skipped, not passed."""
import os
from threading import Barrier, Thread
from uuid import uuid4
import pytest
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from sandbox_graph.persistence.repository import PostgresRepository, StaleOwner
from sandbox_graph.persistence.checkpointer import FencedPostgresSaver
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.runtime.tasks import TaskBackend, JobSpec

@pytest.fixture
def postgres():
    dsn=os.environ.get('SANDBOX_GRAPH_TEST_POSTGRES_DSN')
    if not dsn: pytest.skip('BLOCKED: SANDBOX_GRAPH_TEST_POSTGRES_DSN not provided; real PostgreSQL required')
    namespace='orc_'+uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
    scoped=make_conninfo(dsn,options=f'-c search_path={namespace}')
    repository=PostgresRepository(scoped); repository.migrate()
    with psycopg.connect(scoped,autocommit=True,row_factory=dict_row) as conn: PostgresSaver(conn).setup()
    yield repository,scoped
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))


def test_two_real_connections_claim_only_once(postgres):
    repo,_=postgres; q=TaskBackend(repo)
    with repo.transaction() as tx: now=tx.now
    q.enqueue(JobSpec(job_id='j',job_kind='work',scope_ref='s',thread_id='t',activation_id='a',dedupe_key='a',due_at=now,deadline=now+60))
    barrier=Barrier(2); results=[]
    def claim(owner): barrier.wait(); results.append(q.claim(owner,ttl=10))
    threads=[Thread(target=claim,args=(name,)) for name in ('a','b')]
    for t in threads: t.start()
    for t in threads: t.join(timeout=10)
    assert sum(x is not None for x in results)==1


def test_pending_writes_and_checkpoint_reject_stale_owner(postgres):
    repo,dsn=postgres; lease=LeaseService(repo); old=lease.acquire_thread('t','a',10)
    config={'configurable':{'thread_id':'t','checkpoint_ns':''}}
    cp=empty_checkpoint()
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        saver=FencedPostgresSaver(conn,old)
        saved=saver.put(config,cp,{}, {})
        with repo.transaction() as tx:
            tx.connection.execute("UPDATE thread_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE thread_id='t'")
        new=lease.acquire_thread('t','b',10)
        with pytest.raises(StaleOwner): saver.put(config,empty_checkpoint(),{}, {})
        with pytest.raises(StaleOwner): saver.put_writes(saved,[('result','old')],'task')
        with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as new_conn:
            current=FencedPostgresSaver(new_conn,new)
            current.put_writes(saved,[('result','new')],'task')
            found=current.get_tuple(saved)
            assert found.pending_writes[0][2]=='new'


def test_saver_rejects_cross_thread_and_destructive_worker_calls(postgres):
    repo,dsn=postgres; token=LeaseService(repo).acquire_thread('t','a',10)
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        saver=FencedPostgresSaver(conn,token)
        with pytest.raises(PermissionError): saver.put({'configurable':{'thread_id':'other','checkpoint_ns':''}},empty_checkpoint(),{}, {})
        with pytest.raises(PermissionError): saver.delete_thread('t')
