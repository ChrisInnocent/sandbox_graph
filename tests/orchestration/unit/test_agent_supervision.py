"""WF-04 看护闭环：owner agent 消费看护记录，有模型出提案、无模型显式记缺口。"""
import asyncio

import pytest

from tests.orchestration.conftest import MemoryRepository, FakeClock
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.runtime.agents import AgentGateway, AgentSupervisor, ContextBudget
from sandbox_graph.runtime.leases import LeaseService


def budget():
    return ContextBudget(max_input_bytes=4000, max_output_bytes=4000, max_calls=3,
                         max_tokens=100, deadline=10 ** 9)


def _failed_node(repo):
    token = LeaseService(repo).acquire_thread('t', 'worker', 100)

    async def handler(context):
        return NodeResult(execution_status='failed', output={})

    executor = NodeExecutor(repo, {'N05': handler}, token)
    state = {'thread_id': 't', 'scope': 'G_A', 'run_ref': 'r', 'plan_ref': 'p',
             'graph_ref': 'g', 'activation_id': 'a', 'input_refs': {}}
    asyncio.run(executor.execute('N05', state))


class ProposalModel:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def diagnose(self, context):
        self.calls.append(context)
        return self.payload, 5, 'synthetic:1'


def test_supervisor_without_model_records_explicit_gap_and_clears_pending():
    repo = MemoryRepository(FakeClock())
    _failed_node(repo)
    supervisor = AgentSupervisor(repo, gateway=None)
    assert len(supervisor.pending('agent:work:G_A')) == 1
    decisions = asyncio.run(supervisor.review('agent:work:G_A', allowed_fields={'case_hash'}))
    assert decisions[0]['status'] == 'model_unavailable' and decisions[0]['proposal'] is None
    assert supervisor.pending('agent:work:G_A') == []
    # 未配置模型时，测量/资源事实不得被改动。
    with repo.transaction() as tx:
        assert len(tx.scan('agent_supervision')) == 1
        assert len(tx.scan('agent_reviews')) == 1


def test_supervisor_with_model_produces_scoped_proposal_only():
    repo = MemoryRepository(FakeClock())
    _failed_node(repo)
    payload = {'facts': [{'statement': 's', 'evidence_refs': ['e1']}], 'hypotheses': ['h'],
               'suggested_actions': [], 'affected_scope': ['G_A'], 'required_decisions': []}
    model = ProposalModel(payload)
    gateway = AgentGateway(repo, model, registered_actions=set())
    supervisor = AgentSupervisor(repo, gateway=gateway)
    decisions = asyncio.run(supervisor.review('agent:work:G_A', allowed_fields={'case_hash'},
        accessible_evidence={'e1'}, budget=budget(), fields_by_node={'N05': {'case_hash': 'h', 'secret': 'x'}}))
    assert decisions[0]['status'] == 'proposed'
    assert decisions[0]['proposal']['hypotheses'] == ['h']
    # allowlist 生效：无关字段没进模型上下文
    assert model.calls[0]['fields'] == {'case_hash': 'h'}


def test_supervisor_never_sees_another_agents_records():
    repo = MemoryRepository(FakeClock())
    _failed_node(repo)
    supervisor = AgentSupervisor(repo, gateway=None)
    assert supervisor.pending('agent:group:G_A') == []
    assert len(supervisor.pending('agent:work:G_A')) == 1


def test_failed_node_enqueues_supervision_job_and_handler_records_gap():
    from sandbox_graph.runtime.worker import Worker, WorkerPolicy, SupervisionJobHandler
    repo = MemoryRepository(FakeClock())
    _failed_node(repo)
    worker = Worker(repo, {}, policy=WorkerPolicy(claim_seconds=10, job_seconds=10, poll_seconds=1))
    worker.pump_outbox()
    with repo.transaction() as tx:
        jobs = [r['body'] for r in tx.scan('jobs') if r['body']['job_kind'] == 'supervision']
    assert len(jobs) == 1
    assert jobs[0]['payload']['owner_agent_id'] == 'agent:work:G_A'
    handler = SupervisionJobHandler(AgentSupervisor(repo, None), allowed_fields={'case_hash'}, budget=budget())
    result = asyncio.run(handler(jobs[0]))
    assert result['execution_status'] == 'succeeded'
    assert result['decisions'][0]['status'] == 'model_unavailable'
    with repo.transaction() as tx:
        assert len(tx.scan('agent_reviews')) == 1
