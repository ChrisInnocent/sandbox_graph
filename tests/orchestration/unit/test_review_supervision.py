"""Quality rejection is an independent issue; original facts remain immutable."""
import pytest

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.catalog.registry import NODE_GROUPS
from tests.orchestration.conftest import FakeClock, MemoryRepository


@pytest.mark.asyncio
@pytest.mark.parametrize('result,source', [
    (NodeResult(execution_status='succeeded', data_status='valid'), 'node_review'),
    (NodeResult(execution_status='succeeded', review_status='rejected'), 'result_review'),
    (NodeResult(execution_status='failed'), 'execution'),
])
async def test_rejections_create_evidence_bound_issues_without_rewriting_receipt(result, source):
    repo = MemoryRepository(FakeClock())
    with repo.transaction() as tx:
        token = tx.acquire_thread('t', 'owner', 30)
    async def handler(context):
        return result
    executor = NodeExecutor(repo, {'N02': handler}, token)
    state = dict(thread_id='t', scope='w', run_ref='r', plan_ref='p', graph_ref='g',
                 activation_id='a', input_refs={'case_ref': 'case'}, result_refs={'N01': 'previous'})
    returned, ref = await executor.execute('N02', state)
    await executor.execute('N02', state)
    assert returned == result
    with repo.transaction() as tx:
        rows = tx.scan('agent_supervision')
        assert len(rows) == 1
        record = rows[0]['body']
        assert source in record['issue_sources']
        assert record['receipt_ref'] == ref
        assert record['input_refs'] == {'case_ref': 'case'}
        assert record['result_refs'] == {'N01': 'previous'}
        assert record['node_supervisor_id']
        assert tx.get('receipts', ref)['body']['result'] == result.model_dump(mode='json')


@pytest.mark.asyncio
async def test_review_rejection_routes_real_graph_through_cleanup():
    repo = MemoryRepository(FakeClock())
    with repo.transaction() as tx:
        token = tx.acquire_thread('t', 'owner', 30)
    visited = []
    def action(node):
        async def run(context):
            visited.append(node)
            return NodeResult(execution_status='succeeded', data_status='valid' if node == 'N02' else 'unverified')
        return run
    graph = build_work(NodeExecutor(repo, {n: action(n) for n in NODE_GROUPS['work']}, token))
    result = await graph.ainvoke(dict(thread_id='t', scope='w', run_ref='r', plan_ref='p',
                                    graph_ref='g', activation_id='a', input_refs={}))
    assert visited == ['N00', 'N01', 'N02', 'N07', 'N08', 'N09', 'N10']
    assert result['results']['N02']['execution_status'] == 'succeeded'
    assert result['results']['N02']['node_review_decision'] == 'reject'


@pytest.mark.asyncio
async def test_performance_fail_alone_does_not_trigger_repair_and_nodes_have_distinct_supervisors():
    repo = MemoryRepository(FakeClock())
    with repo.transaction() as tx:
        token = tx.acquire_thread('t', 'owner', 30)
    seen = []
    async def handler(context):
        seen.append((context.owner_agent_id, context.node_supervisor_id))
        return NodeResult(execution_status='succeeded', performance_status='fail')
    executor = NodeExecutor(repo, {'N05': handler, 'N06': handler}, token)
    state = dict(thread_id='t', scope='w', run_ref='r', plan_ref='p', graph_ref='g',
                 activation_id='a', input_refs={})
    await executor.execute('N05', state)
    await executor.execute('N06', state)
    assert seen[0][0] == seen[1][0] and seen[0][1] != seen[1][1]
    with repo.transaction() as tx:
        assert tx.scan('agent_supervision') == []
