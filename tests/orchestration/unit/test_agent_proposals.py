"""P9.2 / ORC-022,023,046,057: proposals cannot expand scope, act unregistered, or cite nothing."""
import pytest

from sandbox_graph.runtime.agents import AgentGateway, DiagnosisProposal, Fact


def proposal(**extra):
    data = dict(facts=(Fact(statement='observed', evidence_refs=('e1',)),), hypotheses=(),
                suggested_actions=('act.a',), affected_scope=('g',), required_decisions=())
    data.update(extra)
    return DiagnosisProposal(**data)


def test_validate_proposal_rejects_scope_action_and_evidence_violations():
    gateway = AgentGateway(None, None, registered_actions=('act.a',))
    gateway.validate_proposal(proposal(), scope='g', accessible_evidence={'e1'})
    with pytest.raises(PermissionError, match='scope'):
        gateway.validate_proposal(proposal(affected_scope=('other',)), scope='g',
                                  accessible_evidence={'e1'})
    with pytest.raises(ValueError, match='unregistered action'):
        gateway.validate_proposal(proposal(suggested_actions=('bad',)), scope='g',
                                  accessible_evidence={'e1'})
    bad_fact = (Fact(statement='s', evidence_refs=('missing',)),)
    with pytest.raises(ValueError, match='evidence'):
        gateway.validate_proposal(proposal(facts=bad_fact), scope='g', accessible_evidence={'e1'})
    empty_fact = (Fact(statement='s', evidence_refs=()),)
    with pytest.raises(ValueError, match='evidence'):
        gateway.validate_proposal(proposal(facts=empty_fact), scope='g', accessible_evidence={'e1'})
