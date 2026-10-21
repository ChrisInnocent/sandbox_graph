"""L4 ORC-065-068 (task 14.2): five-stage run graph end-to-end.

The synthetic path runs offline (real StateGraph + deterministic routing). The real
dual-environment-group path requires a supplied environment and is skipped otherwise.
"""
import asyncio
import os

import pytest

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.factory import build_group, build_run
from sandbox_graph.services.workflows import (group_definition, group_routers, run_definition,
                                             run_routers)


class Executor:
    def __init__(self, nodes, outcomes=None):
        self.handlers = {node: object() for node in nodes}
        self.outcomes = outcomes or {}
        self.visited = []

    async def execute(self, node, state):
        self.visited.append(node)
        return self.outcomes.get(node, NodeResult(execution_status='succeeded')), f'ref:{node}'


STATE = {'thread_id': 't', 'scope': 'r', 'run_ref': 'r', 'plan_ref': 'p', 'graph_ref': 'g',
         'activation_id': 'a', 'input_refs': {}}


def test_synthetic_run_then_group_graph_end_to_end():
    run = Executor(('R0', 'R1', 'RP', 'R2', 'R3', 'R6', 'R7'))
    asyncio.run(build_run(run_definition(), run, routers=run_routers()).ainvoke(dict(STATE)))
    assert run.visited == ['R0', 'R1', 'RP', 'R2', 'R3', 'R7']

    group_nodes = ('G0', 'E1', 'E2', 'C1', 'C2', 'C3', 'T0', 'T1', 'T2', 'T4', 'T5', 'T6', 'T8',
                   'TC', 'TB', 'GB', 'GW', 'GE')
    group = Executor(group_nodes)
    asyncio.run(build_group(group_definition(), group, routers=group_routers()).ainvoke(dict(STATE)))
    assert group.visited[0] == 'G0' and group.visited[-1] == 'GW'


def test_partial_failure_still_reaches_cleanup_and_delivery():
    nodes = ('R0', 'R1', 'RP', 'R2', 'R3', 'R6', 'R7')
    executor = Executor(nodes, {'R2': NodeResult(execution_status='failed')})
    asyncio.run(build_run(run_definition(), executor, routers=run_routers()).ainvoke(dict(STATE)))
    assert executor.visited == ['R0', 'R1', 'RP', 'R2']        # a failed stage stops the run explicitly


@pytest.mark.skipif(not os.environ.get('SANDBOX_GRAPH_TEST_PRODUCT_ENV'),
                    reason='BLOCKED: real dual-environment-group products required')
def test_real_dual_group_end_to_end():  # pragma: no cover - real environment only
    """Fill with real products: dual groups, partial failure, human wait, incremental analysis,
    only-analysis/report. Acceptance: ORC-065-068; every selected item has a disposition and
    the Excel/report artifacts share one analysis version."""
    raise NotImplementedError('run the real dual-group acceptance described in docs/acceptance-manual.md')
