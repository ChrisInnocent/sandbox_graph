"""PostgreSQL unit of work. No production in-memory or SQLite fallback.

Initial implementation serializes control-plane transactions with one database
advisory lock. External work never runs inside this lock. This trades throughput
for clear atomicity; shard the lock only after real concurrency qualification.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4
import psycopg
import math
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

class Conflict(ValueError): pass
class StaleOwner(Conflict): pass

def positive_duration(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
        raise ValueError('finite positive numeric duration required')
    return value

@dataclass(frozen=True)
class LeaseToken:
    thread_id: str
    owner_epoch: int
    lease_id: str
    lease_until: float

class Repository(Protocol):
    def transaction(self): ...

MIGRATION = '''
CREATE TABLE IF NOT EXISTS orchestration_records (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision >= 0),
    body JSONB NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS thread_leases (
    thread_id TEXT PRIMARY KEY,
    owner_epoch BIGINT NOT NULL CHECK (owner_epoch > 0),
    lease_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS orchestration_kind_idx ON orchestration_records(kind);
'''

class PostgresRepository:
    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError('PostgreSQL DSN is required; no fallback backend')
        self.dsn = dsn

    def migrate(self):
        """Provision every engine table: native ledger plus LangGraph checkpointer tables.

        issue #4：仅建 `orchestration_records`/`thread_leases` 会让驱动在首个 saver
        查询时报 `UndefinedTable`。checkpointer 表必须在这里一并创建（幂等）。
        """
        from langgraph.checkpoint.postgres import PostgresSaver
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            with conn.transaction():
                conn.execute(MIGRATION)
            PostgresSaver(conn).setup()

    @contextmanager
    def transaction(self):
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            with conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock(%s)', (724690451,))
                yield UnitOfWork(conn)

class UnitOfWork:
    def __init__(self, connection): self.connection = connection
    @property
    def now(self):
        return float(self.connection.execute('SELECT extract(epoch FROM clock_timestamp()) AS now').fetchone()['now'])

    def get(self, kind: str, key: str):
        return self.connection.execute('SELECT key, revision, body FROM orchestration_records WHERE kind=%s AND key=%s FOR UPDATE', (kind,key)).fetchone()

    def scan(self, kind: str):
        return self.connection.execute('SELECT key, revision, body FROM orchestration_records WHERE kind=%s ORDER BY key FOR UPDATE', (kind,)).fetchall()

    def put(self, kind: str, key: str, body: dict, expected_revision: int | None = None, immutable: bool = False):
        old = self.get(kind,key)
        if immutable and old:
            if old['body'] != body: raise Conflict('immutable content conflict')
            return old['revision']
        actual = old['revision'] if old else -1
        if expected_revision is not None and actual != expected_revision:
            raise Conflict('revision conflict')
        revision = actual + 1
        self.connection.execute('INSERT INTO orchestration_records(kind,key,revision,body) VALUES (%s,%s,%s,%s) ON CONFLICT(kind,key) DO UPDATE SET revision=EXCLUDED.revision,body=EXCLUDED.body', (kind,key,revision,Jsonb(body)))
        return revision

    def delete(self, kind: str, key: str):
        self.connection.execute('DELETE FROM orchestration_records WHERE kind=%s AND key=%s', (kind,key))

    def acquire_thread(self, thread_id: str, owner: str, ttl: float) -> LeaseToken:
        positive_duration(ttl)
        row=self.connection.execute('SELECT owner_epoch,lease_until > clock_timestamp() AS active FROM thread_leases WHERE thread_id=%s FOR UPDATE',(thread_id,)).fetchone()
        if row and row['active']: raise Conflict('thread already leased')
        epoch=(row['owner_epoch']+1) if row else 1
        lease_id=str(uuid4())
        result=self.connection.execute('INSERT INTO thread_leases(thread_id,owner_epoch,lease_id,owner_id,lease_until) VALUES (%s,%s,%s,%s,clock_timestamp()+(%s * interval \'1 second\')) ON CONFLICT(thread_id) DO UPDATE SET owner_epoch=EXCLUDED.owner_epoch,lease_id=EXCLUDED.lease_id,owner_id=EXCLUDED.owner_id,lease_until=EXCLUDED.lease_until RETURNING extract(epoch FROM lease_until) AS until', (thread_id,epoch,lease_id,owner,ttl)).fetchone()
        return LeaseToken(thread_id,epoch,lease_id,float(result['until']))

    def assert_fence(self, token: LeaseToken):
        assert_fence_cursor(self.connection, token)

    def renew_thread(self, token: LeaseToken, ttl: float) -> LeaseToken:
        positive_duration(ttl)
        self.assert_fence(token)
        result=self.connection.execute('UPDATE thread_leases SET lease_until=clock_timestamp()+(%s * interval \'1 second\') WHERE thread_id=%s RETURNING extract(epoch FROM lease_until) AS until',(ttl,token.thread_id)).fetchone()
        return LeaseToken(token.thread_id,token.owner_epoch,token.lease_id,float(result['until']))

    def release_thread(self,token: LeaseToken):
        self.assert_fence(token)
        self.connection.execute('UPDATE thread_leases SET lease_until=clock_timestamp() WHERE thread_id=%s',(token.thread_id,))


def assert_fence_cursor(cursor, token: LeaseToken):
    """Must be called inside the SAME transaction as all fenced writes."""
    row=cursor.execute('SELECT owner_epoch,lease_id,lease_until > clock_timestamp() AS active FROM thread_leases WHERE thread_id=%s FOR UPDATE',(token.thread_id,)).fetchone()
    if row is None or row['owner_epoch'] != token.owner_epoch or row['lease_id'] != token.lease_id or not row['active']:
        raise StaleOwner('stale or expired thread owner')
