"""Mocked end-to-end work-node orchestration producing all four metrics.

Real LangGraph work graph + real N00–N10 handlers + real BenchSession and analysis;
only the product adapter (Scenario) and the host sampler are mocked. No real product,
no real procfs, no PostgreSQL.
"""
import asyncio
import json
import threading
import time

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sandbox_bench.bridge import BenchGateway, work_handlers
from sandbox_bench.contracts import CaseContract
from sandbox_bench.session import BenchSession
from sandbox_graph.domain.models import RunRecord, ScriptRef
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.runtime.runner import OperationService
from sandbox_graph.services.revisions import RevisionService
from tests.benchmark.unit.test_contract_models import sample
from tests.benchmark.unit.test_session import Scenario
from tests.orchestration.conftest import FakeClock, MemoryRepository

CLOCK_DOMAIN = 'mock:boot'


class SlowScenario(Scenario):
    """Mock product boundary; a real create would poll a real sandbox."""

    async def execute(self, index):
        await asyncio.sleep(0.02)
        return await super().execute(index)


class FakeSampler:
    """Mock host sampler: quota-free cgroup-style CPU/memory at real monotonic times."""

    def __init__(self, clock_domain):
        self.clock_domain = clock_domain
        self.safe_to_submit = True
        self._lock = threading.Lock()
        self._seq = 0
        self._pending = []
        # Declared inventory: the authoritative identity -> class map for attribution.
        self.inventory = [{'identity': {'environment': 'env', 'node': 'node', 'boot_id': 'boot',
                                        'pid': 4242, 'start_ticks': 1},
                           'roles': ['work'], 'components': ['vmm'],
                           'sandbox_id': 's1', 'generation': 'g1'}]

    def sample_once(self):
        with self._lock:
            self._seq += 1
            seq = self._seq
        now = time.monotonic()
        row = {'schema_version': 'resource_sample.v1', 'kind': 'resource_sample', 'seq': seq,
               'sample_id': f'fake:{seq}', 'clock_domain': self.clock_domain,
               'started_s': now, 'ended_s': now, 't_s': now,
               'processes': [{'identity': {'environment': 'env', 'node': 'node', 'boot_id': 'boot',
                                           'pid': 4242, 'start_ticks': 1},
                              'pss_bytes': 1_000_000 + 10_000 * seq, 'cpu_seconds': 0.05 * seq,
                              'roles': ['work'], 'components': ['vmm'],
                              'sandbox_id': 's1', 'generation': 'g1'}],
               'inventory_complete': True, 'snapshot_cohort_ids': ['s1'],
               'sampler_wall_seconds': 0.0, 'sampler_cpu_seconds': 0.0,
               'poll_requests': 0, 'renewal_requests': 0,
               'inventory_version': 1, 'inventory_source_ref': 'fake'}
        self._pending.append(row)
        return row

    def reconcile(self, sink):
        acked = 0
        for row in list(self._pending):
            if sink(row) is False:
                raise ConnectionError('sink rejected sample')
            self._pending.remove(row)
            acked += 1
        return {'status': 'reconciled', 'acknowledged': acked, 'pending': 0}

    def close(self):
        pass


async def _steady_check(sampler, resources):
    return {'status': 'completed'}


def _case_with_resources():
    data = sample()
    data.update(work_id='mock.work', group_id='mock.group', plan_ref='plan1', graph_ref='graph1')
    data['resources'] = {'interval_s': 0.005, 'window_s': 1.0, 'max_wait_s': 2.0,
                         'max_gap_s': 5.0, 'min_samples': 2, 'inventory_ref': 'fixture.'}
    return CaseContract.model_validate(data)


def _config(case):
    return {'product': case.product, 'api_url': 'http://localhost:1', 'data_url': 'http://localhost:1',
            'source': {'kind': case.source.kind, 'id': case.source.id},
            'create_body': {'timeout': case.ttl_s,
                            'metadata': {'kuasar-sandbox.resource': '{"capacity":{"cpu":2,"memory":"2GiB"}}'}},
            'source_evidence': {'source_id': case.source.id, 'source_digest': case.source.digest,
                                'environment_ref': case.environment_ref, 'cache_profile': case.cache_profile,
                                'effective_spec': case.spec.model_dump(), 'evidence_ref': 'synthetic-source'},
            'environment_evidence': {'ref': case.environment_ref, 'product_version': case.product_version,
                                     'evidence_kind': 'synthetic'}}


@pytest.mark.asyncio
async def test_mocked_work_graph_collects_latency_throughput_cpu_memory(tmp_path):
    repo = MemoryRepository(FakeClock())
    case = _case_with_resources()
    script = ScriptRef(script_id='benchmark.step', version='1', digest='a' * 64)
    session = BenchSession(case, _config(case), tmp_path / 'session', scenario=SlowScenario(),
                           sampler=FakeSampler(CLOCK_DOMAIN), steady_check=_steady_check,
                           clock_domain=CLOCK_DOMAIN)
    gateway = BenchGateway({case.work_id: session}, script)
    operations = OperationService(repo, gateway, {('benchmark.step', '1'): script})
    RevisionService(repo).create(RunRecord(run_id='r', root_thread_id='t', graph_ref='g',
                                           active_plan_ref='p', contract_ref=case.contract_ref,
                                           auth_ref='a', lineage_id='l'))
    token = LeaseService(repo).acquire_thread('t', 'owner', 10_000)
    handlers = work_handlers(operations, token, session, run_id='r', scope=case.work_id,
                             dispatch_generation=1, deadline=time.monotonic() + 10_000)
    executor = NodeExecutor(repo, handlers, token)
    graph = build_work(executor, checkpointer=InMemorySaver())
    state = {'thread_id': 't', 'scope': case.work_id, 'run_ref': 'r', 'plan_ref': 'p',
             'graph_ref': 'g', 'activation_id': 'act', 'input_refs': {'work': case.work_id}}
    out = await graph.ainvoke(state, config={'configurable': {'thread_id': 't'}})

    for node in (f'N{i:02}' for i in range(11)):
        assert out['results'][node]['execution_status'] == 'succeeded', (node, out['results'][node])

    analysis = json.loads((session.folder / 'analysis.json').read_text())
    # Latency: per-request percentiles from the mocked create completion.
    latency = analysis['latency']['repeats'][str(case.repeat_id)]['latency_s']
    assert latency['p50'] > 0 and latency['p95'] >= latency['p50']
    # Throughput: 1-second buckets over the natural-drain window.
    assert analysis['throughput']['overall_per_s'] > 0
    # CPU: exact seconds plus a (here, sampled) peak; distinct fields.
    assert analysis['resources']['cpu']['sampled_peak_cores'] is not None
    # Memory: PSS steady mean over a fully covered window.
    assert analysis['resources']['pss']['mean'] is not None
    assert analysis['resources']['pss']['mean'] > 0
    # The sampler really ran across the create window (baseline + loop).
    assert len(list((session.folder / 'resource_samples').glob('*.json'))) >= 2
    # N04 sampling spans N05 (the load window) and N07 has stopped it.
    assert session._sampler_closed is True
    sample_times = sorted(json.loads(p.read_text())['t_s']
                          for p in (session.folder / 'resource_samples').glob('*.json'))
    load = json.loads((session.folder / 'load.json').read_text())
    assert sample_times[0] <= load['started_at'] <= load['finished_at'] <= sample_times[-1]
    # Every measured unit is attributed to the declared component; nothing is ownerless.
    attribution = analysis['resources']['attribution']
    assert attribution['status'] == 'valid' and attribution['unattributed_rows'] == 0
    assert attribution['expected_components'] == ['vmm']
    assert set(analysis['resources']['by_component']) == {'vmm'}
    # Batch offline recompute over the sealed evidence is byte-stable in attribution.
    from sandbox_bench.analysis import recompute_case
    assert recompute_case(session.folder / 'evidence')['resources']['attribution'] == attribution
