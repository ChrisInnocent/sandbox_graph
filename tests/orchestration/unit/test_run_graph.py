"""P7.3 / ORC-047,052,053: run graph routes independent groups and stops on failure."""
import asyncio

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.factory import build_run
from sandbox_graph.services.workflows import run_definition, run_routers


class Executor:
    def __init__(self, handlers, outcomes=None):
        self.handlers = handlers
        self.outcomes = outcomes or {}
        self.visited = []

    async def execute(self, node, state):
        self.visited.append(node)
        return self.outcomes.get(node, NodeResult(execution_status='succeeded')), f'ref:{node}'


NODES = ('R0', 'R1', 'RP', 'R2', 'R3', 'R6', 'R7')
STATE = {'thread_id': 't', 'scope': 'r', 'run_ref': 'r', 'plan_ref': 'p', 'graph_ref': 'g',
         'activation_id': 'a', 'input_refs': {}}


def run(executor):
    graph = build_run(run_definition(), executor, routers=run_routers())
    return asyncio.run(graph.ainvoke(dict(STATE)))


def test_success_path_dispatches_then_reaches_the_park_node():
    executor = Executor({node: object() for node in NODES})
    run(executor)
    assert executor.visited == ['R0', 'R1', 'RP', 'R2', 'R3', 'R7']


def test_a_failed_stage_stops_the_run_without_dispatching_later_groups():
    executor = Executor({node: object() for node in NODES},
                        {'R2': NodeResult(execution_status='failed')})
    run(executor)
    assert executor.visited == ['R0', 'R1', 'RP', 'R2']


def test_rework_continuation_routes_through_r6_before_reparking():
    executor = Executor({node: object() for node in NODES},
                        {'R3': NodeResult(execution_status='succeeded', continuation='R6')})
    run(executor)
    assert executor.visited == ['R0', 'R1', 'RP', 'R2', 'R3', 'R6', 'R7']
