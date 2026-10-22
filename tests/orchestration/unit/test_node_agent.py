"""节点 agent 层：小上下文 + 执行注册脚本 + 规则复审 + 落账（不改执行事实）。

对应设计意图：图控上下文、脚本保确定性、agent 只做执行/复审/落账。
"""
import asyncio
import dataclasses

import pytest

from tests.orchestration.conftest import MemoryRepository, FakeClock
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.executor import NodeContext, NodeExecutor
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.runtime.node_agent import NodeAgent


def _context(**overrides):
    base = dict(node_id='N05', owner_agent_id='agent:work:G_A', thread_id='t', scope='G_A',
                run_ref='r', plan_ref='p', graph_ref='g', activation_id='a', visit_id='v',
                input_refs={}, result_refs={}, owner_epoch=1, lease_id='l')
    base.update(overrides)
    return NodeContext(**base)


def test_agent_refuses_input_outside_its_duty():
    agent = NodeAgent('N05', 'agent:work:G_A', action=lambda c: None, allowed_input_keys={'environment_ref'})
    with pytest.raises(PermissionError, match='outside its duty'):
        agent.scope_context(_context(input_refs={'environment_ref': 'e', 'other_plan': 'x'}))


def test_agent_passes_only_allowed_inputs_to_action():
    seen = {}

    async def action(context):
        seen['refs'] = dict(context.input_refs)
        return NodeResult(execution_status='succeeded')

    agent = NodeAgent('N05', 'agent:work:G_A', action=action, allowed_input_keys={'environment_ref'})
    asyncio.run(agent.run(_context(input_refs={'environment_ref': 'e'})))
    assert seen['refs'] == {'environment_ref': 'e'}


def test_review_flags_valid_data_without_evidence_and_leaves_result_untouched():
    result = NodeResult(execution_status='succeeded', data_status='valid')
    agent = NodeAgent('N05', 'agent:work:G_A', action=lambda c: None)
    review = agent.review(_context(), result)
    assert review.decision == 'reject' and 'valid data requires raw evidence' in review.findings
    # 复审不覆盖执行事实
    assert result.data_status == 'valid'


def test_review_accepts_clean_result():
    result = NodeResult(execution_status='succeeded', data_status='valid', artifact_refs=('a1',))
    review = NodeAgent('N05', 'agent:work:G_A', action=lambda c: None).review(_context(), result)
    assert review.decision == 'accept' and review.findings == ()


def test_script_execution_is_deterministic_through_the_agent():
    calls = []

    async def action(context):
        calls.append(context.visit_id)
        return NodeResult(execution_status='succeeded', output={'x': 1})

    agent = NodeAgent('N05', 'agent:work:G_A', action=action)
    first, _ = asyncio.run(agent.run(_context(visit_id='v1')))
    second, _ = asyncio.run(agent.run(_context(visit_id='v1')))
    assert first == second and calls == ['v1', 'v1']


def test_executor_records_node_review_for_every_node():
    repo = MemoryRepository(FakeClock())
    token = LeaseService(repo).acquire_thread('t', 'worker', 100)

    async def handler(context):
        return NodeResult(execution_status='succeeded', data_status='valid')

    executor = NodeExecutor(repo, {'N05': handler}, token)
    state = {'thread_id': 't', 'scope': 'G_A', 'run_ref': 'r', 'plan_ref': 'p',
             'graph_ref': 'g', 'activation_id': 'a', 'input_refs': {}}
    asyncio.run(executor.execute('N05', state))
    with repo.transaction() as tx:
        rows = tx.scan('node_reviews')
    assert len(rows) == 1
    body = rows[0]['body']
    assert body['node_id'] == 'N05' and body['owner_agent_id'] == 'agent:work:G_A'
    assert body['decision'] == 'reject'   # valid 无证据 -> 复审拒绝，且不改 receipt 里的执行事实
    with repo.transaction() as tx:
        receipt = [r for r in tx.scan('receipts') if r['body']['identity']['node_id'] == 'N05'][0]
    assert receipt['body']['result']['data_status'] == 'valid'


def test_executor_enforces_per_node_input_allowlist():
    repo = MemoryRepository(FakeClock())
    token = LeaseService(repo).acquire_thread('t', 'worker', 100)

    async def handler(context):
        return NodeResult(execution_status='succeeded')

    executor = NodeExecutor(repo, {'N05': handler}, token, allowlists={'N05': {'environment_ref'}})
    state = {'thread_id': 't', 'scope': 'G_A', 'run_ref': 'r', 'plan_ref': 'p', 'graph_ref': 'g',
             'activation_id': 'a', 'input_refs': {'environment_ref': 'e', 'foreign_plan': 'x'}}
    result, _ = asyncio.run(executor.execute('N05', state))
    assert result.execution_status == 'failed' and result.error.code == 'EXECUTION_FAILED'
