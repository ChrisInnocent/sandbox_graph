"""有界失败循环：agent 只能发起注册修复动作；旧证据不可变；预算受限。"""
import asyncio

import pytest

from tests.orchestration.conftest import MemoryRepository, FakeClock
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.services.remediation import NodeRemediationService, REGISTERED_NODE_ACTIONS


def _order(service, *, node='N09', decision, work='work-1', **extra):
    return service.order(owner_agent_id='agent:work:G_A', work_ref=work, node_id=node,
                         run_ref='r', plan_ref='p', graph_ref='g', decision=decision,
                         deadline=10 ** 9, **extra)


def _proposed(actions):
    return {'status': 'proposed', 'proposal': {'suggested_actions': list(actions),
            'facts': [], 'hypotheses': [], 'affected_scope': [], 'required_decisions': []}}


def test_legacy_registered_action_requires_migration_contract():
    repo = MemoryRepository(FakeClock())
    result = _order(NodeRemediationService(repo), decision=_proposed(['resource.cleanup']))
    assert result['status'] == 'blocked' and result['reason'] == 'lifecycle_contract_required'
    assert result['recovery_entry'] == 'request_remediation'
    with repo.transaction() as tx:
        assert tx.scan('jobs') == []


def test_repeated_legacy_requests_neither_dispatch_nor_reset_allowance():
    repo = MemoryRepository(FakeClock())
    service = NodeRemediationService(repo, max_attempts=2)
    with repo.transaction() as tx:
        tx.put('node_attempts', 'work-1:N09', {'attempts': 2})
    results = [_order(service, decision=_proposed(['resource.cleanup'])) for _ in range(3)]
    assert all(r['status'] == 'blocked' for r in results)
    assert service.attempts('work-1', 'N09') == 2
    with repo.transaction() as tx:
        assert tx.scan('jobs') == []


def test_unregistered_action_and_unknown_node_are_refused():
    repo = MemoryRepository(FakeClock())
    service = NodeRemediationService(repo, max_attempts=2)
    bad = _order(service, decision=_proposed(['delete_everything']))
    assert bad['status'] == 'blocked' and bad['reason'] == 'agent_did_not_request_registered_action'
    none = _order(service, node='N05', decision=_proposed(['resource.cleanup']))
    assert none['status'] == 'blocked' and none['reason'] == 'no_registered_action_for_node'


def test_no_model_proposal_blocks_instead_of_looping():
    repo = MemoryRepository(FakeClock())
    service = NodeRemediationService(repo, max_attempts=2)
    result = _order(service, decision={'status': 'model_unavailable', 'proposal': None})
    assert result['status'] == 'blocked' and result['reason'] == 'model_unavailable'
    with repo.transaction() as tx:
        assert [r['body'] for r in tx.scan('jobs') if r['body']['job_kind'] == 'remedy'] == []


def test_known_registered_actions_are_the_only_ones():
    assert set(REGISTERED_NODE_ACTIONS) == {'N01', 'N07', 'N09'}


def test_legacy_supervision_preserves_proposal_without_unauthorized_dispatch():
    from sandbox_graph.domain.models import ErrorRecord
    from sandbox_graph.runtime.agents import AgentGateway, AgentSupervisor, ContextBudget
    from sandbox_graph.runtime.worker import RemedyJobHandler, SupervisionJobHandler

    repo = MemoryRepository(FakeClock())
    token = LeaseService(repo).acquire_thread('work-t', 'worker', 100)

    async def failing(context):
        return NodeResult(execution_status='blocked', error=ErrorRecord(code='X', observed_at=0, message='x'))

    state = {'thread_id': 'work-t', 'scope': 'work-1', 'run_ref': 'r', 'plan_ref': 'p',
             'graph_ref': 'g', 'activation_id': 'a', 'input_refs': {}}
    asyncio.run(NodeExecutor(repo, {'N09': failing}, token).execute('N09', state))

    class StubModel:
        async def diagnose(self, context):
            return ({'facts': [{'statement': 's', 'evidence_refs': ['e']}], 'hypotheses': ['h'],
                     'suggested_actions': ['resource.cleanup'], 'affected_scope': [context['scope']],
                     'required_decisions': []}, 1, 'stub:1')

    budget = ContextBudget(max_input_bytes=4096, max_output_bytes=4096, max_calls=2, max_tokens=100, deadline=10 ** 9)
    supervisor = AgentSupervisor(repo, AgentGateway(repo, StubModel(), registered_actions={'resource.cleanup'}))
    handler = SupervisionJobHandler(supervisor, allowed_fields={'scope', 'execution_status', 'reason'},
        budget=budget, accessible_evidence={'e'},
        fields_by_node=lambda node, record: {'scope': record['scope'], 'execution_status': record['execution_status'],
                                             'reason': record.get('reason')},
        remediation=NodeRemediationService(repo, max_attempts=2))
    out = asyncio.run(handler({'payload': {'owner_agent_id': 'agent:work:work-1'}, 'deadline': 10 ** 9}))
    assert out['decisions'][0]['status'] == 'proposed'
    assert out['remedies'][0]['status'] == 'blocked'
    assert out['remedies'][0]['reason'] == 'lifecycle_contract_required'
    with repo.transaction() as tx:
        assert tx.scan('jobs') == []
        assert len(tx.scan('receipts')) == 1  # Original failure remains immutable.


def test_work_graph_rejects_raw_resume_from():
    """Caller-selected numeric continuation is never an execution capability."""
    import asyncio as _asyncio
    from sandbox_graph.graphs.factory import build_work
    from sandbox_graph.runtime.leases import LeaseService as _LS
    from sandbox_graph.graphs.executor import NodeExecutor as _NE
    from sandbox_graph.domain.models import NodeResult as _NR

    repo = MemoryRepository(FakeClock())
    token = _LS(repo).acquire_thread('resume-thread', 'worker', 100)
    visited = []

    def handler(node):
        async def run(context):
            visited.append(node)
            return _NR(execution_status='succeeded')
        return run

    handlers = {n: handler(n) for n in __import__('sandbox_graph.catalog.registry', fromlist=['NODE_GROUPS']).NODE_GROUPS['work']}
    graph = build_work(_NE(repo, handlers, token))
    state = {'thread_id': 'resume-thread', 'scope': 'w', 'run_ref': 'r', 'plan_ref': 'p', 'graph_ref': 'g',
             'activation_id': 'a', 'input_refs': {}, 'resume_from': 'N05'}
    with pytest.raises(ValueError, match='resume_from'):
        _asyncio.run(graph.ainvoke(state))
    assert visited == []
