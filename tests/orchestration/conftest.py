"""Explicit synthetic fixtures; memory UoW is NOT PostgreSQL validation."""
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
import pytest

class FakeClock:
    def __init__(self): self.now = 1000.0
    def advance(self, amount): self.now += amount

class MemoryTransaction:
    def __init__(self, repository): self.repo = repository
    @property
    def now(self): return self.repo.clock.now
    def get(self, kind, key): return deepcopy(self.repo.rows.get((kind,key)))
    def put(self, kind, key, body, expected_revision=None, immutable=False):
        from sandbox_graph.persistence.repository import Conflict
        old = self.repo.rows.get((kind,key))
        if immutable and old:
            if old['body'] != body: raise Conflict('immutable content conflict')
            return old['revision']
        if expected_revision is not None and (old['revision'] if old else -1) != expected_revision:
            raise Conflict('revision conflict')
        revision = (old['revision']+1) if old else 0
        self.repo.rows[(kind,key)] = {'key':key,'revision':revision,'body':deepcopy(body)}
        return revision
    def scan(self, kind): return [deepcopy(row) for (k,_),row in sorted(self.repo.rows.items()) if k == kind]
    def delete(self, kind, key): self.repo.rows.pop((kind,key),None)
    def acquire_thread(self, thread_id, owner, ttl):
        from sandbox_graph.persistence.repository import Conflict, LeaseToken
        old=self.repo.leases.get(thread_id)
        if old and old.lease_until>self.now: raise Conflict('thread already leased')
        token=LeaseToken(thread_id, (old.owner_epoch+1) if old else 1, owner, self.now+ttl)
        self.repo.leases[thread_id]=token
        return token
    def assert_fence(self, token):
        from sandbox_graph.persistence.repository import StaleOwner
        old=self.repo.leases.get(token.thread_id)
        if old is None or (old.owner_epoch,old.lease_id)!=(token.owner_epoch,token.lease_id) or old.lease_until<=self.now:
            raise StaleOwner('stale owner')
    def renew_thread(self, token, ttl):
        from dataclasses import replace
        self.assert_fence(token)
        renewed=replace(token,lease_until=self.now+ttl)
        self.repo.leases[token.thread_id]=renewed
        return renewed
    def release_thread(self,token):
        from dataclasses import replace
        self.assert_fence(token)
        self.repo.leases[token.thread_id]=replace(token,lease_until=self.now)

class MemoryRepository:
    is_test_fixture=True
    def __init__(self,clock):
        self.clock=clock; self.rows={}; self.leases={}; self.lock=RLock()
    @contextmanager
    def transaction(self):
        with self.lock:
            before=deepcopy((self.rows,self.leases))
            try: yield MemoryTransaction(self)
            except BaseException:
                self.rows,self.leases=before
                raise

@pytest.fixture
def clock(): return FakeClock()
@pytest.fixture
def repository(clock): return MemoryRepository(clock)
@pytest.fixture
def environments():
    return {'G_A':('host_A',),'G_B':('host_B_control','host_B_worker'),'G_C':('host_A',)}
