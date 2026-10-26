"""Explicit synthetic fixtures; memory UoW is NOT PostgreSQL validation."""
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from uuid import uuid4
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
        from sandbox_graph.services.lifecycle_disposition import DispositionVersions
        DispositionVersions.changed_in(self,kind,key,old['body'] if old else None,body)
        return revision
    def scan(self, kind): return [deepcopy(row) for (k,_),row in sorted(self.repo.rows.items()) if k == kind]
    def delete(self, kind, key):
        old = self.repo.rows.pop((kind,key),None)
        if old:
            from sandbox_graph.services.lifecycle_disposition import DispositionVersions
            DispositionVersions.changed_in(self,kind,key,old['body'],None)
    def thread_lease_until(self, token):
        self.assert_fence(token)
        return self.repo.leases[token.thread_id].lease_until
    def acquire_thread(self, thread_id, owner, ttl):
        from sandbox_graph.persistence.repository import Conflict, LeaseToken
        old=self.repo.leases.get(thread_id)
        if old and old.lease_until>self.now: raise Conflict('thread already leased')
        token=LeaseToken(thread_id, (old.owner_epoch+1) if old else 1, str(uuid4()), self.now+ttl)
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
            # get/scan copy on read; put replaces a row with a copied body and
            # lease tokens are frozen values. Copy the indexes for rollback,
            # not every historical artifact on each transaction.
            before=(self.rows.copy(),self.leases.copy())
            try: yield MemoryTransaction(self)
            except BaseException:
                self.rows,self.leases=before
                raise

@pytest.fixture
def clock(): return FakeClock()
@pytest.fixture
def repository(clock): return MemoryRepository(clock)


class SpyModel:
    """Records calls and can return over-reaching output; never mutates deterministic state."""
    def __init__(self, payload=None):
        self.payload = payload
        self.calls = []
    async def diagnose(self, context):
        self.calls.append(context)
        return self.payload, 0, 'spy:v1'


@pytest.fixture
def spy_model(): return SpyModel()


@pytest.fixture
def postgres():
    """Real PostgreSQL L2 fixture; missing DSN is explicitly skipped, never passed."""
    import os
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver
    from sandbox_graph.persistence.repository import PostgresRepository
    dsn = os.environ.get('SANDBOX_GRAPH_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('BLOCKED: SANDBOX_GRAPH_TEST_POSTGRES_DSN not provided; real PostgreSQL required')
    namespace = 'orc_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
    scoped = make_conninfo(dsn, options=f'-c search_path={namespace}')
    repository = PostgresRepository(scoped)
    repository.migrate()
    with psycopg.connect(scoped, autocommit=True, row_factory=dict_row) as conn:
        PostgresSaver(conn).setup()
    yield repository, scoped
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))
@pytest.fixture
def environments():
    return {'G_A':('host_A',),'G_B':('host_B_control','host_B_worker'),'G_C':('host_A',)}


@pytest.fixture
def lifecycle_failure_event():
    """Literal wire event; no production event builder supplies its oracle."""
    return {'schema_version': 1, 'event_id': 'event/owner-lost', 'kind': 'owner_lost',
        'source': 'worker/a', 'expected_revision': 7,
        'evidence_refs': ['receipt/historical'], 'observed_at': 1000.0}


@pytest.fixture
def lifecycle_record_wire():
    """Persisted v1 record before optional stop/operation fields existed."""
    return {'schema_version': 1, 'key': {'schema_version': 1, 'run_ref': 'run/a',
        'scope_kind': 'work', 'scope_ref': 'work/a', 'node_id': 'N01',
        'lifecycle_id': 'life/historical', 'lineage_budget_ref': 'lineage/a'},
        'graph_ref': 'graph/a', 'plan_ref': 'plan/a', 'contract_ref': 'contract/a',
        'policy_ref': 'policy/a', 'phase': 'diagnosing', 'revision': 7,
        'owner_epoch': 2, 'source_receipt': 'receipt/historical',
        'issue_fingerprint': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'deadline': 1100.0, 'attempt_refs': ['attempt/1', 'attempt/2'],
        'node_result': {'execution_status': 'failed', 'performance_status': 'fail',
            'review_status': 'rejected', 'data_status': 'unverified', 'cleanup_status': 'unknown'}}


@pytest.fixture
def historical_record_bytes():
    """Original wire bytes intentionally retain spacing, order and Unicode."""
    return {
        ('remedy_orders', 'legacy/order'): b'{ "attempt":2, "node_id":"N01", "work_ref":"work/a", "status":"succeeded" }\n',
        ('agent_reviews', 'legacy/review'): '{ "status":"proposed", "proposal":{"suggested_actions":["case.reconcile"], "facts":["旧证据"]} }\n'.encode(),
    }


@pytest.fixture
def lifecycle_budget_config():
    """Synthetic finite bounds, independently written rather than policy-derived."""
    return {'lifecycle_id': 'life/historical', 'lineage_budget_ref': 'lineage/a',
        'limits': {'repair_attempts': 2, 'diagnosis_calls': 3, 'candidate_revisions': 2,
            'tokens': 100, 'child_jobs': 3, 'observations': 4, 'cost': 1000},
        'deadline': 1100.0,
        'safety_limits': {'stop_calls': 2, 'query_calls': 3, 'cleanup_calls': 2, 'cost': 50},
        'safety_deadline': 1200.0, 'cost_unit': 'microUSD'}


@pytest.fixture
def historical_budget_repository(repository, lifecycle_budget_config):
    """Seed already spent counters without asking the budget service to compute them."""
    config = {k: deepcopy(v) for k, v in lifecycle_budget_config.items()
              if k not in {'lifecycle_id', 'lineage_budget_ref'}}
    for kind, ref in [('lifecycle', 'life/historical'), ('lineage', 'lineage/a')]:
        body = {'kind': kind, 'ref': ref, 'lineage_budget_ref': 'lineage/a',
            'original': deepcopy(config), **deepcopy(config),
            'reserved': {'repair_attempts': 0, 'diagnosis_calls': 0, 'candidate_revisions': 0,
                'tokens': 0, 'child_jobs': 0, 'observations': 0, 'cost': 0},
            'spent': {'repair_attempts': 2, 'diagnosis_calls': 1, 'candidate_revisions': 1,
                'tokens': 70, 'child_jobs': 1, 'observations': 2, 'cost': 700},
            'safety_reserved': {'stop_calls': 0, 'query_calls': 0, 'cleanup_calls': 0, 'cost': 0},
            'safety_spent': {'stop_calls': 1, 'query_calls': 1, 'cleanup_calls': 0, 'cost': 10}}
        with repository.transaction() as tx:
            tx.put('lifecycle_budgets', kind + ':' + ref, body, immutable=True)
    return repository


@pytest.fixture
def lifecycle_dependency_graph():
    """Hand-declared dependency chain for preparation/revalidation tests."""
    return {'N01': (), 'N02': ('N01',), 'N03': ('N02',), 'N05': ('N03',), 'N09': ('N01',)}


@pytest.fixture
def effect_ledger():
    """Independent append-only observations for controlled action fixtures."""
    return []


@pytest.fixture
def lifecycle_model():
    """Explicit legacy proposal only; calls are inspectable and grant no trust."""
    return SpyModel({'facts': [{'statement': 'fixture absent',
        'evidence_refs': ['receipt/historical']}], 'hypotheses': [],
        'suggested_actions': ['case.reconcile'], 'affected_scope': ['work/a'],
        'required_decisions': []})


@pytest.fixture
def controlled_repair_action(effect_ledger):
    """Test-only effect sink; no host provider or environment acceptance claim."""
    async def action(context):
        from sandbox_graph.domain.models import NodeResult
        effect_ledger.append({'node_id': context.node_id, 'scope': context.scope})
        return NodeResult(execution_status='succeeded')
    return action
