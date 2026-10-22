"""BCT-13 / RT-46 / BCD-17: operation sync/async endpoint mapping and synchronous latency.

oracle 为手写字面量表；不调用被测函数生成预期。
"""
import asyncio

import pytest

from sandbox_bench.operation_sync import await_operation, completion_plan

SYNC_TABLE = {
    ('kuasar', 'pause'): (True, 'API_COMPLETE', 'paused'),
    ('kuasar', 'resume'): (False, 'STATE_READY', 'running'),
    ('kuasar', 'delete'): (False, 'STATE_READY', 'gone'),
    ('cubesandbox', 'create'): (True, 'API_COMPLETE', 'running'),
    ('cubesandbox', 'pause'): (True, 'API_COMPLETE', 'paused'),
    ('cubesandbox', 'resume'): (True, 'API_COMPLETE', 'running'),
    ('cubesandbox', 'delete'): (True, 'API_COMPLETE', 'gone'),
    ('agentenv', 'pause'): (True, 'API_COMPLETE', 'paused'),
    ('agentenv', 'resume'): (True, 'API_COMPLETE', 'running'),
    ('agentenv', 'delete'): (True, 'API_COMPLETE', 'gone'),
    ('agentenv', 'snapshot'): (True, 'API_COMPLETE', 'published'),
}


@pytest.mark.parametrize('key,expected', list(SYNC_TABLE.items()))
def test_sync_table_is_literal(key, expected):
    product, operation = key
    plan = completion_plan(product, operation)
    assert (plan['synchronous'], plan['endpoint'], plan['ready_state']) == expected
    assert plan['verified'] is True


def test_kuasar_create_syncness_depends_on_entry():
    router = completion_plan('kuasar', 'create', topology='router')
    node = completion_plan('kuasar', 'create', topology='node_direct')
    assert router['synchronous'] is True and router['poll_required'] is False
    assert node['synchronous'] is False and node['endpoint'] == 'STATE_READY' and node['poll_required'] is True
    assert completion_plan('kuasar', 'create')['verified'] is False      # entry not given


def test_uncovered_or_pending_operations_are_unverified():
    assert completion_plan('kuasar', 'snapshot')['verified'] is False    # export pending
    assert completion_plan('cubesandbox', 'snapshot')['verified'] is False
    assert completion_plan('agentenv', 'clone')['verified'] is False     # fork pending
    assert completion_plan('kuasar', 'clone')['verified'] is False       # no native fork
    assert completion_plan('nope', 'pause')['verified'] is False


class FakeAdapter:
    product = 'kuasar'

    def __init__(self, states):
        self.states = iter(states)

    def info(self, handle, timeout):
        return {'state': next(self.states)}


async def test_async_plan_polls_to_ready_state():
    row = await await_operation(FakeAdapter(['starting', 'starting', 'running']),
                                {'sandbox_id': 's'}, completion_plan('kuasar', 'resume'),
                                api_complete_s=0.0, timeout=5, poll_interval=0)
    assert row['status'] == 'success' and row['polls'] == 3
    assert row['state_ready_s'] >= row['api_complete_s']


async def test_sync_plan_does_not_poll():
    row = await await_operation(FakeAdapter(['running']), {'sandbox_id': 's'},
                                completion_plan('kuasar', 'pause'),
                                api_complete_s=1.0, timeout=5, poll_interval=0)
    assert row['polls'] == 0 and row['state_ready_s'] == row['api_complete_s'] == 1.0


async def test_delete_poll_reaches_gone():
    row = await await_operation(FakeAdapter(['deleting', 'dead']), {'sandbox_id': 's'},
                                completion_plan('kuasar', 'delete'),
                                api_complete_s=0.0, timeout=5, poll_interval=0)
    assert row['status'] == 'success' and row['state'] == 'dead'


async def test_async_timeout_is_not_success():
    row = await await_operation(FakeAdapter(['starting'] * 3), {'sandbox_id': 's'},
                                completion_plan('kuasar', 'resume'),
                                api_complete_s=0.0, timeout=0.001, poll_interval=0.001)
    assert row['status'] == 'timeout' and row['state_ready_s'] is None


def test_measure_exposes_the_n05_completion_plan():
    from sandbox_bench.measure import operation_completion

    class A:
        product = 'kuasar'

    assert operation_completion(A(), 'pause')['poll_required'] is False
    assert operation_completion(A(), 'resume')['poll_required'] is True
    assert operation_completion(A(), 'create', topology='router')['synchronous'] is True
    assert operation_completion(A(), 'create', topology='node_direct')['synchronous'] is False
    assert operation_completion(A(), 'snapshot')['verified'] is False
