"""P9.1 / ORC-020,021,069: evidence-scoped context with an allowlist and byte budget."""
import pytest

from sandbox_graph.runtime.agents import AgentGateway, ContextBudget


def budget(**extra):
    data = dict(max_input_bytes=1000, max_output_bytes=1000, max_calls=3, max_tokens=100,
                deadline=10 ** 9)
    data.update(extra)
    return ContextBudget(**data)


def test_build_context_projects_only_allowed_fields():
    gateway = AgentGateway(None, None, registered_actions=())
    context = gateway.build_context(scope='g', node_id='N05', fields={'keep': 1, 'drop': 2},
                                    budget=budget(), allowed_fields={'keep'})
    assert context['fields'] == {'keep': 1}
    assert context['scope'] == 'g' and len(context['context_id']) == 64


def test_build_context_rejects_over_budget_instead_of_truncating():
    gateway = AgentGateway(None, None, registered_actions=())
    with pytest.raises(ValueError, match='byte budget'):
        gateway.build_context(scope='g', node_id='N05', fields={'keep': 'x' * 2000},
                              budget=budget(), allowed_fields={'keep'})
