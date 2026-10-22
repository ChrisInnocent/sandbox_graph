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


def test_registered_action_orders_a_bounded_remedy():
    repo = MemoryRepository(FakeClock())
    service = NodeRemediationService(repo, max_attempts=2)
    result = _order(service, decision=_proposed(['resource.cleanup']))
    assert result['status'] == 'ordered' and result['attempt'] == 1
    with repo.transaction() as tx:
        jobs = [r['body'] for r in tx.scan('jobs') if r['body']['job_kind'] == 'remedy']
        assert len(jobs) == 1 and jobs[0]['payload']['node_id'] == 'N09'


def test_attempts_are_bounded_per_work_and_node():
    repo = MemoryRepository(FakeClock())
    service = NodeRemediationService(repo, max_attempts=2)
    assert _order(service, decision=_proposed(['resource.cleanup']))['status'] == 'ordered'
    assert _order(service, decision=_proposed(['resource.cleanup']))['status'] == 'ordered'
    third = _order(service, decision=_proposed(['resource.cleanup']))
    assert third['status'] == 'blocked' and third['reason'] == 'remediation_exhausted'
    assert service.attempts('work-1', 'N09') == 2


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


def test_supervision_orders_remedy_and_remedy_reexecutes_failed_node():
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
    assert out['remedies'][0]['status'] == 'ordered'

    with repo.transaction() as tx:
        rjob = [r['body'] for r in tx.scan('jobs') if r['body']['job_kind'] == 'remedy'][0]
    LeaseService(repo).acquire_thread(rjob['thread_id'], 'remedy', 1000)

    async def healing(context):
        return NodeResult(execution_status='succeeded')

    remedy = RemedyJobHandler(repo, lambda token, activation: {'N09': healing},
                              lease_seconds=30, segment_seconds=20)
    result = asyncio.run(remedy({'thread_id': rjob['thread_id'], 'owner_epoch': 1, 'deadline': 10 ** 9,
                                 'activation_id': 'rem-1', 'payload': {'remedy_ref': rjob['payload']['remedy_ref']}}))
    assert result['execution_status'] == 'succeeded'
    with repo.transaction() as tx:
        outcomes = [r['body'] for r in tx.scan('agent_remedies') if r['body'].get('status') == 'succeeded']
        assert outcomes and outcomes[0]['attempt'] == 1


def test_work_graph_resumes_from_the_given_node():
    """修复后再入：从 resume_from 继续，不重跑 N00..。"""
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
    _asyncio.run(graph.ainvoke(state))
    assert visited[0] == 'N05' and 'N00' not in visited and visited[-1] == 'N10'
