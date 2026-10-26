"""T6 uses actual failed graph deliveries; review text is never authority."""
from copy import deepcopy

import pytest

from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.services.reviews import ReviewService
from sandbox_graph.services.workflows import WorkflowHandlers
from tests.orchestration.graphs.test_work_remediation import assembly, drain


async def failed_delivery(repository, node, *, recovery_failure=False):
    worker, supervisor, seen, repairs = assembly(repository, failure=node, recovery_failure=recovery_failure)
    with repository.transaction() as tx:
        work = tx.get('work_instances', 'work/a')['body']
        tx.put('work_instances', 'work/a', work | {'group_id':'group-a'})
    if node == 'N07':
        # Synthetic runner boundary, real graph/executor/postcheck. The common
        # N01 fixture does not supply the complete collection N07 requires.
        from sandbox_graph.runtime.runner import RunnerCollection
        factory = worker.handlers['work'].handler_factory
        def with_collection(token, activation):
            handlers = factory(token, activation)
            original = handlers['N07']
            async def seal(context):
                result = await original(context)
                if result.execution_status == 'succeeded':
                    with repository.transaction() as tx:
                        tx.put('collections', 'op/original:0',
                            RunnerCollection(artifact_refs=('fixture/raw',)).model_dump(mode='json'), immutable=True)
                    return result.model_copy(update={'data_status':'valid',
                        'artifact_refs':('fixture/raw',), 'operation_refs':('op/original',)})
                return result
            handlers['N07'] = seal
            return handlers
        worker.handlers['work'].handler_factory = with_collection
        worker.handlers['remedy'].node_service_factory = with_collection
    bindings = supervisor.bindings
    supervisor.bindings = {}
    await drain(worker)  # Complete a real attempt before attaching late supervision.
    supervisor.bindings = bindings
    with repository.transaction() as tx:
        work = tx.get('work_instances', 'work/a')
        auth = tx.get('repair_authorizations', 'auth/runtime')['body']
        tx.put('repair_authorizations', 'auth/runtime', auth | {'work_revision': work['revision']})
    return worker, supervisor, seen, repairs


def reviewer(repository, tmp_path):
    token = LeaseService(repository).acquire_thread('group-review', 'reviewer', 100)
    handlers = WorkflowHandlers(repository, deadline=1090,
        review_service=ReviewService(repository, FileArtifactStore(tmp_path)))
    executor = NodeExecutor(repository, handlers.mapping(), token)
    state = {'thread_id': token.thread_id, 'scope': 'group-a', 'run_ref': 'run/a',
        'plan_ref': 'plan/a', 'graph_ref': 'graph/a', 'activation_id': 'review/1',
        'input_refs': {}, 'result_refs': {}}
    return executor, state


@pytest.mark.asyncio
@pytest.mark.parametrize('node', ['N07', 'N09'])
async def test_t6_routes_real_failure_once_without_new_budget(repository, tmp_path, node):
    worker, _, seen, repairs = await failed_delivery(repository, node)
    executor, state = reviewer(repository, tmp_path)
    result, _ = await executor.execute('T6', state)
    assert result.review_status == 'rejected'
    with repository.transaction() as tx:
        gaps = tx.scan('delivery_rework_gaps')
        assert len(gaps) == 1
        gap = deepcopy(gaps[0]['body'])
        assert gap['source']['node_id'] == node
        assert gap['source']['job_id'] == 'original/job'
        assert gap['review_ref'] in result.output['review_refs']
        receipt = deepcopy(tx.get('receipts', gap['source']['receipt_ref']))
    # Replay is a distinct review visit, not NodeExecutor receipt caching.
    await executor.execute('T6', state | {'activation_id': 'review/2'})
    await drain(worker)
    with repository.transaction() as tx:
        assert len(tx.scan('delivery_rework_gaps')) == 1
        assert len(tx.scan('agent_lifecycles')) == 1
        assert tx.scan('agent_lifecycles')[0]['body']['phase'] == 'fulfilled'
        assert tx.get('receipts', gap['source']['receipt_ref']) == receipt
        assert tx.get('delivery_rework_gaps', gaps[0]['key'])['body'] == gap
        assert len(tx.scan('repair_orders')) == 1
    assert len(repairs) == 1
    for activation in {activation for activation, _ in seen}:
        if activation.startswith('activation:'):
            continue
        assert {'N07', 'N09', 'N10'} <= {n for a, n in seen if a == activation}


@pytest.mark.asyncio
async def test_recovery_child_failure_links_original_owner_without_fresh_budget(repository, tmp_path):
    worker, _, _, repairs = await failed_delivery(repository, 'N09', recovery_failure=True)
    original_finalizer = worker.handlers['work'].delivery_finalizer
    async def new_version(job, result):
        manifest = await original_finalizer(job, result)
        return manifest.model_copy(update={'delivery_id':'original/a','version':2})
    worker.handlers['work'].delivery_finalizer = new_version
    executor, state = reviewer(repository, tmp_path)
    await executor.execute('T6', state)
    await drain(worker)
    with repository.transaction() as tx:
        budgets = deepcopy(tx.scan('lifecycle_budgets'))
        life_id = tx.scan('agent_lifecycles')[0]['key']
    result, _ = await executor.execute('T6', state | {'activation_id':'review/child'})
    assert result.review_status == 'rejected'
    await drain(worker)
    with repository.transaction() as tx:
        assert len(tx.scan('agent_lifecycles')) == 1
        assert len(tx.scan('delivery_rework_gaps')) == 2
        status = tx.get('delivery_rework_status', result.output['rework_refs'][0])['body']
        assert status == {'status':'linked','lifecycle_id':life_id}
        assert tx.scan('lifecycle_budgets') == budgets
    assert len(repairs) == 1


@pytest.mark.asyncio
async def test_same_group_label_in_another_run_cannot_consume_delivery(repository, tmp_path):
    worker, _, _, repairs = await failed_delivery(repository, 'N09')
    executor, state = reviewer(repository, tmp_path)
    result, _ = await executor.execute('T6', state | {'run_ref':'other/run'})
    assert result.execution_status == 'blocked'
    with repository.transaction() as tx:
        assert not tx.scan('delivery_rework_gaps')
    assert repairs == []


@pytest.mark.asyncio
async def test_t6_missing_authority_keeps_intent_and_never_repairs(repository, tmp_path):
    worker, _, _, repairs = await failed_delivery(repository, 'N09')
    with repository.transaction() as tx:
        tx.delete('repair_authorizations', 'auth/runtime')
    executor, state = reviewer(repository, tmp_path)
    await executor.execute('T6', state)
    await drain(worker)
    with repository.transaction() as tx:
        gaps = tx.scan('delivery_rework_gaps')
        assert len(gaps) == 1
        status = tx.get('delivery_rework_status', gaps[0]['key'])['body']
        assert status['status'] == 'needs_input'
        assert 'authorization' in status['reason']
        assert not tx.scan('repair_orders') and not tx.scan('agent_lifecycles')
    assert repairs == []


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['missing_source', 'wrong_job', 'wrong_receipt', 'ambiguous'])
async def test_untrusted_or_ambiguous_source_cannot_authorize_rework(repository, tmp_path, fault):
    worker, _, _, repairs = await failed_delivery(repository, 'N09')
    with repository.transaction() as tx:
        source = tx.get('work_delivery_sources', 'original/a:1')['body']
        if fault == 'missing_source':
            tx.delete('work_delivery_sources', 'original/a:1')
        elif fault == 'wrong_job':
            source['job_id'] = 'other/job'
            tx.delete('work_delivery_sources', 'original/a:1')
            tx.put('work_delivery_sources', 'original/a:1', source, immutable=True)
        elif fault == 'wrong_receipt':
            ref = source['result_refs']['N09']
            receipt = tx.get('receipts', ref)['body']
            receipt['identity']['scope'] = 'other/work'
            tx.delete('receipts', ref)
            tx.put('receipts', ref, receipt, immutable=True)
        else:
            tx.put('agent_supervision', source['result_refs']['N07'], {'reason':'second independent failure'})
    executor, state = reviewer(repository, tmp_path)
    result, _ = await executor.execute('T6', state)
    assert result.review_status == 'rejected'
    await drain(worker)
    with repository.transaction() as tx:
        gap = tx.scan('delivery_rework_gaps')[0]
        assert gap['body']['source'] is None
        assert tx.get('delivery_rework_status', gap['key'])['body']['status'] == 'needs_input'
        assert not tx.scan('agent_lifecycles') and not tx.scan('repair_orders')
    assert repairs == []


@pytest.mark.asyncio
async def test_pending_t6_gap_prevents_clean_archive_even_before_consumer_runs(repository, tmp_path):
    from sandbox_graph.domain.models import NodeResult
    from sandbox_graph.persistence.repository import Conflict
    from sandbox_graph.services.closure import ClosureService
    await failed_delivery(repository, 'N09')
    executor, state = reviewer(repository, tmp_path)
    await executor.execute('T6', state)
    with repository.transaction() as tx:
        revision = tx.get('closure_revisions', 'run/a')['body']['revision']
    clean = NodeResult(execution_status='succeeded', data_status='valid',
        cleanup_status='complete', scope_disposition='fulfilled')
    with pytest.raises(Conflict, match='delivery rework'):
        ClosureService(repository).seal_archive('run/a', expected_revision=revision,
            analysis_ref='analysis', report_analysis_ref='analysis', excel_analysis_ref='analysis',
            scopes={'work/a':(clean,'processing')}, inflight=(), artifact_refs=())


@pytest.mark.asyncio
async def test_new_accepted_delivery_resolves_gap_but_preserves_rejected_history(repository, tmp_path):
    worker, _, _, _ = await failed_delivery(repository, 'N09')
    original_finalizer = worker.handlers['work'].delivery_finalizer
    artifact = FileArtifactStore(tmp_path).commit([b'{"synthetic_runner":true}'],
        scope='group-a', media_type='application/json')
    async def new_version(job, result):
        manifest = await original_finalizer(job, result)
        return manifest.model_copy(update={'delivery_id':'original/a', 'version':2, 'scope':'group-a',
            'evidence':(artifact,), 'result':manifest.result.model_copy(update={'data_status':'valid'})})
    worker.handlers['work'].delivery_finalizer = new_version
    executor, state = reviewer(repository, tmp_path)
    await executor.execute('T6', state)
    with repository.transaction() as tx:
        gap = deepcopy(tx.scan('delivery_rework_gaps')[0])
    await drain(worker)
    accepted, _ = await executor.execute('T6', state | {'activation_id':'review/accepted'})
    assert accepted.review_status == 'accepted'
    with repository.transaction() as tx:
        assert tx.get('delivery_rework_gaps', gap['key']) == gap
        status = tx.get('delivery_rework_status', gap['key'])['body']
        assert status['status'] == 'resolved'
        assert status['resolution_review_ref'] == accepted.output['review_refs'][0]
        assert tx.get('reviews', gap['body']['review_ref'])['body']['decision'] == 'rejected'
        assert len(tx.scan('repair_orders')) == 1
        tx.delete('outbox_delivery', gap['key'])  # Crash/replay after resolution.
    worker.pump_outbox()
    with repository.transaction() as tx:
        assert tx.get('delivery_rework_status', gap['key'])['body'] == status
