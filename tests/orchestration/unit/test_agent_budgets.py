"""P9.3 / ORC-023,043-045,069: bounded model budgets; missing usage is unknown, not invented."""
import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.agents import AgentGateway, ContextBudget, DiagnosisProposal, Fact


def budget(**extra):
    data = dict(max_input_bytes=1000, max_output_bytes=1000, max_calls=1, max_tokens=10,
                deadline=10 ** 9)
    data.update(extra)
    return ContextBudget(**data)


def output():
    return DiagnosisProposal(facts=(Fact(statement='s', evidence_refs=('e1',)),), hypotheses=(),
                             suggested_actions=(), affected_scope=('g',), required_decisions=()
                             ).model_dump(mode='json')


class FakeModel:
    def __init__(self, payload, usage=5):
        self.payload = payload
        self.usage = usage
        self.calls = 0

    async def diagnose(self, context):
        self.calls += 1
        return self.payload, self.usage, 'model:v1'


def context(gateway, **overrides):
    return gateway.build_context(scope='g', node_id='N05', fields={},
                                 budget=budget(**overrides), allowed_fields=set())


async def test_diagnose_requires_resources_disposed_first(repository):
    gateway = AgentGateway(repository, FakeModel(output()), registered_actions=())
    with pytest.raises(Conflict, match='dispose'):
        await gateway.diagnose(context(gateway), accessible_evidence={'e1'}, resources_disposed=False)


async def test_call_budget_is_exhausted_and_missing_usage_is_unknown(repository):
    model = FakeModel(output(), usage=None)
    gateway = AgentGateway(repository, model, registered_actions=())
    ctx = context(gateway)
    await gateway.diagnose(ctx, accessible_evidence={'e1'}, resources_disposed=True)
    call = next(row['body'] for (kind, _), row in repository.rows.items() if kind == 'agent_calls')
    assert call['usage_status'] == 'unknown' and call['usage'] is None
    with pytest.raises(Conflict, match='budget'):
        await gateway.diagnose(ctx, accessible_evidence={'e1'}, resources_disposed=True)
    assert model.calls == 1


async def test_token_budget_overflow_is_rejected(repository):
    gateway = AgentGateway(repository, FakeModel(output(), usage=50), registered_actions=())
    with pytest.raises(Conflict, match='token budget'):
        await gateway.diagnose(context(gateway), accessible_evidence={'e1'}, resources_disposed=True)
