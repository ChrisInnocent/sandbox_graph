"""WF-04：运行时的三级 owner agent 身份与事件驱动看护记录。

需求 `WF-04`：主 agent / 环境组统筹 sub-agent / 工作节点 sub-agent 三级责任；
`RT-24`：工作节点 agent 只加载必要合同与引用、组统筹二次复核；
`WF-04` 的 Scenario 又要求执行由程序完成、看护不持续轮询 —— 因此看护以
**账本记录 + 事件**表达，而不是模型轮询。
"""
import asyncio

import pytest

from tests.orchestration.conftest import MemoryRepository, FakeClock
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.runtime.agents import agent_id_for, node_agent_level
from sandbox_graph.runtime.leases import LeaseService


def test_node_agent_level_covers_three_levels():
    assert node_agent_level('R0') == 'root'
    assert node_agent_level('RP') == 'root'
    assert node_agent_level('A3') == 'root'          # 主 agent 侧统一分析
    assert node_agent_level('F4') == 'root'
    assert node_agent_level('G0') == 'group'
    assert node_agent_level('E2') == 'group'
    assert node_agent_level('T5') == 'group'
    assert node_agent_level('N00') == 'work'
    assert node_agent_level('N10') == 'work'


def test_unknown_node_has_no_owner_agent():
    with pytest.raises(KeyError):
        node_agent_level('X99')


def test_unregistered_node_falls_back_to_node_scoped_identity():
    from sandbox_graph.runtime.agents import owner_agent_for
    assert owner_agent_for('N05', 'G_A') == 'agent:work:G_A'
    assert owner_agent_for('a', 'synthetic') == 'agent:node:synthetic:a'


def test_agent_identity_is_stable_and_level_scoped():
    assert agent_id_for('root', 'run-1') == 'agent:root:run-1'
    assert agent_id_for('group', 'G_A') == 'agent:group:G_A'
    assert agent_id_for('work', 'work-1') == 'agent:work:work-1'
    with pytest.raises(ValueError):
        agent_id_for('squad', 'x')


def test_node_context_owner_agent_uses_registered_identity():
    repo = MemoryRepository(FakeClock())
    token = LeaseService(repo).acquire_thread('t', 'worker', 100)
    seen = {}

    async def handler(context):
        seen['owner'] = context.owner_agent_id
        return NodeResult(execution_status='succeeded', output={})

    executor = NodeExecutor(repo, {'N05': handler}, token)
    state = {'thread_id': 't', 'scope': 'G_A', 'run_ref': 'r', 'plan_ref': 'p',
             'graph_ref': 'g', 'activation_id': 'a', 'input_refs': {}}
    asyncio.run(executor.execute('N05', state))
    assert seen['owner'] == agent_id_for('work', 'G_A')


def test_failed_node_records_supervision_for_owning_agent():
    repo = MemoryRepository(FakeClock())
    token = LeaseService(repo).acquire_thread('t', 'worker', 100)

    async def handler(context):
        return NodeResult(execution_status='failed', output={})

    executor = NodeExecutor(repo, {'N05': handler}, token)
    state = {'thread_id': 't', 'scope': 'G_A', 'run_ref': 'r', 'plan_ref': 'p',
             'graph_ref': 'g', 'activation_id': 'a', 'input_refs': {}}
    asyncio.run(executor.execute('N05', state))
    with repo.transaction() as tx:
        rows = tx.scan('agent_supervision')
    assert len(rows) == 1
    body = rows[0]['body']
    assert body['owner_agent_id'] == agent_id_for('work', 'G_A')
    assert body['node_id'] == 'N05' and body['execution_status'] == 'failed'
    assert body['scope'] == 'G_A'


def test_successful_node_does_not_create_supervision_noise():
    repo = MemoryRepository(FakeClock())
    token = LeaseService(repo).acquire_thread('t', 'worker', 100)

    async def handler(context):
        return NodeResult(execution_status='succeeded', output={})

    executor = NodeExecutor(repo, {'N05': handler}, token)
    state = {'thread_id': 't', 'scope': 'G_A', 'run_ref': 'r', 'plan_ref': 'p',
             'graph_ref': 'g', 'activation_id': 'a', 'input_refs': {}}
    asyncio.run(executor.execute('N05', state))
    with repo.transaction() as tx:
        assert tx.scan('agent_supervision') == []
