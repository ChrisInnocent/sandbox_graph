"""BCT-10-TOPO / BLOCKED / SYNC-ROUTER / ASYNC-NODE / COMPARABILITY。

oracle 为手写字面量决策表，不调用被测函数生成期望值。独立 marker：endpoint。
"""
import pytest
from pydantic import ValidationError

from sandbox_bench.endpoint import (EndpointResolution, endpoint_comparable,
                                    endpoint_requires_poll, resolve_endpoint)
from sandbox_bench.measure import create_sync
from sandbox_bench.scenarios import ScenarioBlocked
from tests.benchmark.scenarios.test_scenarios import case

pytestmark = pytest.mark.endpoint


def probe(present):
    return lambda topology: {"topology": topology, "management_plane_present": present}


# --- BCT-10-TOPO：拓扑 × 产品 → 端点/同步（字面副本，不调用被测函数） -------------

TOPO_TABLE = [
    # product, topology, management_present, expected_endpoint, synchronous, requires_poll, status
    ('kuasar', 'router', True, 'api_complete', True, False, 'resolved'),
    ('kuasar', 'node_direct', None, 'api_complete', False, True, 'resolved'),
    ('cubesandbox', 'router', True, 'api_complete', True, False, 'resolved'),
    ('cubesandbox', 'node_direct', None, 'api_complete', True, False, 'resolved'),
    ('agentenv', 'router', True, 'api_complete', True, False, 'resolved'),
    ('agentenv', 'node_direct', None, 'api_complete', True, False, 'resolved'),
    ('kuasar', 'co_located_cli', None, 'guest_ready', False, False, 'resolved'),
]


@pytest.mark.parametrize('product,topology,present,expected,synchronous,polls,status', TOPO_TABLE)
def test_topo_decision_table(product, topology, present, expected, synchronous, polls, status):
    res = resolve_endpoint(product=product, product_version='sha', topology=topology,
                           probe=probe(present), evidence_ref='evidence')
    assert (res.endpoint_kind, res.synchronous, endpoint_requires_poll(res), res.status) == (
        expected, synchronous, polls, status)


def test_topo_router_without_management_evidence_is_unverified():
    res = resolve_endpoint(product='kuasar', product_version='sha', topology='router',
                           probe=probe(None), evidence_ref='evidence')
    assert res.status == 'unverified' and res.management_plane_present is None


def test_topo_missing_product_version_is_unverified():
    res = resolve_endpoint(product='kuasar', product_version='', topology='node_direct',
                           probe=None, evidence_ref='evidence')
    assert res.status == 'unverified'


def test_topo_uncovered_pair_is_blocked():
    with pytest.raises(ScenarioBlocked):
        resolve_endpoint(product='cubesandbox', product_version='sha', topology='co_located_cli',
                         probe=None, evidence_ref='evidence')
    with pytest.raises(ScenarioBlocked):
        resolve_endpoint(product='kuasar', product_version='sha', topology='data_plane',
                         probe=None, evidence_ref='evidence')


# --- BCT-10-BLOCKED：无管理面角色，不得静默回退 ---------------------------------

def test_blocked_router_without_management_plane_is_not_downgraded():
    res = resolve_endpoint(product='kuasar', product_version='sha', topology='router',
                           probe=probe(False), evidence_ref='evidence')
    assert res.status == 'blocked' and res.topology == 'router'
    assert res.management_plane_present is False


# --- BCT-10-COMPARABILITY：跨端点不得并入同列 -----------------------------------

def blackbox(kind):
    return EndpointResolution('kuasar', 'node_direct', kind, 'poll_state', False, None, 'e', 'resolved')


def test_comparability_rejects_whitebox_and_mismatched_endpoints():
    assert endpoint_comparable(blackbox('state_ready'), blackbox('state_ready'))
    assert not endpoint_comparable(blackbox('state_ready'), blackbox('api_complete'))
    white = EndpointResolution('kuasar', 'co_located_cli', 'guest_ready', 'ready_fd', False, False, 'e', 'resolved')
    assert not endpoint_comparable(blackbox('state_ready'), white)
    assert not endpoint_comparable(white, white)


# --- BCT-10-SYNC-ROUTER / ASYNC-NODE：create_sync 端点语义 -----------------------

class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeAdapter:
    def __init__(self, clock, product='kuasar'):
        self.clock = clock
        self.product = product
        self.created = 0
        self.lookups = 0

    def create(self, run_id, timeout):
        self.created += 1
        self.clock.sleep(0.1)
        return {'sandbox_id': 's1', 'private': {}, 'api_headers_s': 0.05, 'api_complete_s': 0.1}

    def info(self, handle, timeout):
        self.lookups += 1
        return {'state': 'running' if self.lookups >= 2 else 'starting'}


def synchronous_kuasar():
    return EndpointResolution('kuasar', 'router', 'api_complete', 'none', True, True, 'e', 'resolved')


def async_kuasar():
    return EndpointResolution('kuasar', 'node_direct', 'api_complete', 'poll_state', False, None, 'e', 'resolved')


def test_sync_router_does_not_add_a_second_readiness_delay():
    clock = FakeClock()
    adapter = FakeAdapter(clock)
    row, _ = create_sync(adapter, 'r', 5, .1, clock=clock, sleep=clock.sleep,
                         completion='STATE_READY', endpoint=synchronous_kuasar())
    assert row['status'] == 'success'
    assert adapter.lookups == 0
    assert row['state_ready_s'] == row['api_complete_s'] == 0.1


def test_async_node_polls_and_separates_api_from_state_ready():
    clock = FakeClock()
    adapter = FakeAdapter(clock)
    row, _ = create_sync(adapter, 'r', 5, .1, clock=clock, sleep=clock.sleep,
                         completion='STATE_READY', endpoint=async_kuasar())
    assert row['status'] == 'success'
    assert adapter.lookups >= 1
    assert row['api_complete_s'] == 0.1 and row['state_ready_s'] > row['api_complete_s']


def test_create_sync_rejects_whitebox_guest_ready_endpoint():
    clock = FakeClock()
    adapter = FakeAdapter(clock)
    white = EndpointResolution('kuasar', 'co_located_cli', 'guest_ready', 'ready_fd', False, False, 'e', 'resolved')
    with pytest.raises(ValueError):
        create_sync(adapter, 'r', 5, .1, clock=clock, sleep=clock.sleep, endpoint=white)


# --- 契约合法性：拓扑与就绪信号必须自洽 -----------------------------------------

def test_contract_accepts_remote_sync_and_rejects_ready_fd():
    ok = case('B01.TEMPLATE', topology='node_direct', endpoint_kind='api_complete', readiness_signal='poll_state')
    assert ok.topology == 'node_direct'
    with pytest.raises(ValidationError):
        case('B01.TEMPLATE', topology='node_direct', endpoint_kind='guest_ready', readiness_signal='ready_fd')


def test_contract_requires_ready_fd_only_for_co_located_cli():
    ok = case('B01.TEMPLATE', topology='co_located_cli', endpoint_kind='guest_ready', readiness_signal='ready_fd')
    assert ok.readiness_signal == 'ready_fd'
    with pytest.raises(ValidationError):
        case('B01.TEMPLATE', topology='co_located_cli', endpoint_kind='api_complete', readiness_signal='none')
    with pytest.raises(ValidationError):
        case('B01.TEMPLATE', endpoint_kind='guest_ready', readiness_signal='none')
