"""P7.4 / ORC-015,036,046,051,070: group graph closes through delivery; roles are fixed."""
import asyncio

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.factory import build_group
from sandbox_graph.services.workflows import group_definition, group_routers


class Executor:
    def __init__(self, handlers, outcomes=None):
        self.handlers = handlers
        self.outcomes = outcomes or {}
        self.visited = []

    async def execute(self, node, state):
        self.visited.append(node)
        return self.outcomes.get(node, NodeResult(execution_status='succeeded')), f'ref:{node}'


NODES = ('G0', 'E1', 'E2', 'C1', 'C2', 'C3', 'T0', 'T1', 'T2', 'T4', 'T5', 'T6', 'T8',
         'TC', 'TB', 'GB', 'GW', 'GE')
STATE = {'thread_id': 't', 'scope': 'G_A', 'run_ref': 'r', 'plan_ref': 'p', 'graph_ref': 'g',
         'activation_id': 'a', 'input_refs': {}}


def run(executor):
    graph = build_group(group_definition(), executor, routers=group_routers())
    return asyncio.run(graph.ainvoke(dict(STATE)))


def test_group_graph_uses_a_fixed_node_set_never_per_machine_agents():
    assert group_definition().nodes == NODES


def test_default_success_closes_via_gateway():
    executor = Executor({node: object() for node in NODES})
    run(executor)
    assert executor.visited[0] == 'G0' and executor.visited[-1] == 'GW'


def test_delivery_continuation_reaches_acceptance_and_delivery():
    executor = Executor({node: object() for node in NODES},
                        {'T2': NodeResult(execution_status='succeeded', continuation='T6')})
    run(executor)
    assert 'T8' in executor.visited and executor.visited[-1] == 'GE'


def test_a_failed_env_stage_routes_to_the_blocked_branch():
    executor = Executor({node: object() for node in NODES},
                        {'E1': NodeResult(execution_status='failed')})
    run(executor)
    assert 'GB' in executor.visited
