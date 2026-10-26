"""ALI-02 offline composition: actual worker, compiler, Driver, StateGraph, executor.

Memory repository and controlled handlers do not qualify real backend/adapters.
"""
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sandbox_graph.catalog.registry import NODE_GROUPS
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.driver import Activation, Driver
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from sandbox_graph.services.recovery import RecoveryService
from tests.orchestration.unit.test_recovery_plan import recovery_ready


async def compiled(repository, **kwargs):
    lifecycle, token, _ = await recovery_ready(repository, **kwargs)
    plan = RecoveryService(repository).compile_recovery(lifecycle, token=token)
    with repository.transaction() as tx:
        LifecycleSafetyService.release_work_in(tx, 'work/a', token)
        tx.release_thread(token)
    return plan


def activation(plan, **kwargs):
    fields = dict(activation_id='recovered/a', run_id='run/a', thread_id='recover/thread',
        scope='work/a', plan_ref='plan/a', graph_ref='graph/a', input_refs={'fixture':'fixture/a'},
        recovery_plan_ref=plan.ref, lease_seconds=30, segment_seconds=20)
    fields.update(kwargs)
    return Activation(**fields)


def driver(repository, visited, *, results=None):
    async def action(context):
        visited.append(context)
        default = (NodeResult(execution_status='succeeded',data_status='valid',artifact_refs=('raw/original',))
                   if context.node_id=='N07' else
                   NodeResult(execution_status='succeeded',operation_refs=('op/a',))
                   if context.node_id=='N01' else NodeResult(execution_status='succeeded'))
        return (results or {}).get(context.node_id, default)
    saver = InMemorySaver()
    return Driver(repository, {n:action for n in NODE_GROUPS['work']}, lambda token:saver, build_work)


@pytest.mark.asyncio
async def test_destroyed_fixture_revalidates_before_original_first_measurement(repository):
    plan = await compiled(repository)
    original = deepcopy(repository.rows[('receipts','original/N00')])
    visited = []
    result = await driver(repository, visited).start(activation(plan))
    assert [c.node_id for c in visited] == ['N01','N02','N03','N04','N05','N06','N07','N08','N09','N10']
    assert visited[0].input_refs == {'fixture':'fixture/a'}
    assert visited[0].result_refs == {'N00':'original/N00'}
    assert visited[3].result_refs['N02'] != 'original/N02'
    assert result['result_refs']['N00'] == 'original/N00'
    assert repository.rows[('receipts','original/N00')] == original
    with repository.transaction() as tx:
        proof = tx.get('recovery_starts', 'recovered/a')['body']
        producer = tx.get('resource_disposition_receipts', proof['disposition_ref'])['body']['producer']
        assert producer['thread_id'] == 'recover/thread'
        assert not tx.get('work_execution_leases','work/a')


@pytest.mark.asyncio
@pytest.mark.parametrize('node', ['N04','N05','N06','N07','N08','N09','N10'])
async def test_recovery_preserves_measurement_provenance_without_resampling(repository, node):
    plan = await compiled(repository, node=node, measured=True, performance='fail')
    visited = []
    result = await driver(repository, visited).start(activation(plan))
    expected = {'N04':['N07','N08','N09','N10'], 'N05':['N07','N08','N09','N10'],
        'N06':['N07','N08','N09','N10'], 'N07':['N07','N08','N09','N10'],
        'N08':['N07','N08','N09','N10'], 'N09':['N07','N09','N10'], 'N10':['N07','N09','N10']}[node]
    assert [c.node_id for c in visited] == expected
    assert visited[0].result_refs['N05'] == ('receipt/a' if node == 'N05' else 'original/N05')
    assert result['results']['N06']['performance_status'] == 'fail'
    if node in {'N04','N05','N06'}:
        assert result['results'][node]['data_status']=='insufficient'
        assert result['result_refs'][node]=='receipt/a'
    assert result['measurement_blocked'] is True
    assert result['recovery_acceptance']['accepted'] is False  # bare execution success is not node acceptance
    with repository.transaction() as tx:
        assert tx.get('recovery_plans', plan.ref)['body']['measurement_generation'] == 1
        assert not tx.scan('measurement_authorizations')


@pytest.mark.asyncio
async def test_preparation_failure_still_runs_cleanup_and_delivery(repository):
    from sandbox_graph.domain.models import ErrorRecord
    plan = await compiled(repository)
    visited = []
    failed = NodeResult(execution_status='blocked', error=ErrorRecord(code='NO_FIXTURE', observed_at=1000))
    result = await driver(repository, visited, results={'N02':failed}).start(activation(plan))
    assert [c.node_id for c in visited] == ['N01','N02','N07','N08','N09','N10']
    assert result['results']['N02']['execution_status'] == 'blocked'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['ttl','binding','resource','cancel','gate','auth','identity','generation'])
async def test_driver_rejects_stale_plan_before_any_node(repository, clock, change):
    plan = await compiled(repository)
    with repository.transaction() as tx:
        if change == 'ttl': clock.advance(31)
        elif change == 'binding': tx.put('measurement_bindings', 'work/a', {'measurement_contract_hash':'a'*64})
        elif change == 'resource': tx.put('resources','fixture/a',{'scope':'work/a','status':'closed'})
        elif change == 'cancel':
            row = tx.get('agent_lifecycles', tx.get('recovery_contexts', plan.ref)['body']['lifecycle_id'])
            tx.put('agent_lifecycles', row['key'], row['body'] | {'terminal_intent':'cancelled','stop_reason':'explicit_cancel'})
        elif change == 'gate': tx.put('dispatch_gates','run/a',{'open':False,'generation':3})
        elif change == 'auth':
            row=tx.get('repair_authorizations','auth/a')
            tx.put('repair_authorizations','auth/a',row['body']|{'revoked':True})
        elif change == 'generation':
            row=tx.get('work_instances','work/a')
            tx.put('work_instances','work/a',row['body']|{'key':{'measurement_generation':2}})
    visited = []
    selected = activation(plan, input_refs={'fixture':'other'}) if change == 'identity' else activation(plan)
    with pytest.raises(Conflict): await driver(repository, visited).start(selected)
    assert visited == []
    with repository.transaction() as tx:
        assert not tx.get('activations','recovered/a')
        assert not tx.get('work_execution_leases','work/a')


@pytest.mark.asyncio
async def test_plan_cannot_be_replayed_by_another_activation(repository):
    plan = await compiled(repository)
    visited = []
    runner = driver(repository, visited)
    first = activation(plan)
    await runner.start(first)
    before = len(visited)
    with pytest.raises(Conflict):
        await runner.start(first.model_copy(update={'activation_id':'other','thread_id':'other/thread'}))
    assert len(visited) == before


@pytest.mark.asyncio
async def test_graph_job_handler_preserves_original_inputs_and_extracts_plan_control(repository):
    from sandbox_graph.runtime.worker import GraphJobHandler
    plan = await compiled(repository)
    visited=[]
    async def action(context):
        visited.append(context)
        return NodeResult(execution_status='succeeded')
    class InspectHandler(GraphJobHandler):
        async def finalize(self, job, result):
            return result
    saver=InMemorySaver()
    handler=InspectHandler(repository,build_work,lambda token:saver,
        lambda token,activation:{n:action for n in NODE_GROUPS['work']},lease_seconds=30,segment_seconds=20)
    result=await handler({'job_id':'recovery/job','job_kind':'work','activation_id':'recovered/a',
        'run_id':'run/a','thread_id':'recover/thread','scope_ref':'work/a',
        'payload':{'plan_ref':'plan/a','graph_ref':'graph/a','fixture':'fixture/a','recovery_plan_ref':plan.ref}})
    assert result['result_refs']['N00']=='original/N00'
    assert all(c.input_refs=={'fixture':'fixture/a'} for c in visited)
    with repository.transaction() as tx:
        stored=tx.get('activations','recovered/a')['body']['record']
        assert stored['input_refs']=={'fixture':'fixture/a'}
        assert stored['recovery_plan_ref']==plan.ref


@pytest.mark.asyncio
async def test_dispatch_stop_after_start_preserves_cleanup_permissions(repository):
    plan=await compiled(repository)
    visited=[]
    async def action(context):
        visited.append(context.node_id)
        if context.node_id=='N01':
            with repository.transaction() as tx:
                tx.put('dispatch_gates','run/a',{'open':False,'generation':3})
        return NodeResult(execution_status='succeeded',operation_refs=('op/a',) if context.node_id=='N01' else ())
    saver=InMemorySaver()
    runner=Driver(repository,{n:action for n in NODE_GROUPS['work']},lambda token:saver,build_work)
    result=await runner.start(activation(plan))
    assert visited==['N01','N07','N09','N10']
    assert result['results']['N02']['execution_status']=='blocked'
    assert result['results']['N08']['execution_status']=='blocked'
    with repository.transaction() as tx:
        assert not tx.get('work_execution_leases','work/a')


@pytest.mark.asyncio
async def test_direct_graph_cannot_inject_compiled_entry_without_driver_validation(repository):
    from sandbox_graph.graphs.executor import NodeExecutor
    plan=await compiled(repository)
    with repository.transaction() as tx:
        token=tx.acquire_thread('recover/thread','bypass',30)
        LifecycleSafetyService.acquire_work_in(tx,'work/a',token,purpose='recovery')
    visited=[]
    async def action(context):
        visited.append(context)
        return NodeResult(execution_status='succeeded')
    graph=build_work(NodeExecutor(repository,{n:action for n in NODE_GROUPS['work']},token))
    with pytest.raises(Conflict,match='owner changed'):
        await graph.ainvoke({'thread_id':'recover/thread','scope':'work/a','run_ref':'run/a',
            'plan_ref':'plan/a','graph_ref':'graph/a','activation_id':'bypass','input_refs':{'fixture':'fixture/a'},
            'recovery_plan_ref':plan.ref,'recovery_route':['N05']})
    assert visited==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('node', ['N08','N09','N10'])
async def test_every_recovery_tail_produces_fresh_attempt_closure(repository, node):
    plan=await compiled(repository,node=node,measured=True)
    visited=[]
    result=await driver(repository,visited).start(activation(plan))
    assert not {c.node_id for c in visited}&{'N04','N05','N06'}
    with repository.transaction() as tx:
        facts=tx.get('work_safety_facts','work/a')['body']
        assert facts['attempt_closed'] is True
        for closing in ('N07','N09','N10'):
            receipt=tx.get('receipts',result['result_refs'][closing])['body']
            assert receipt['identity']['activation_id']=='recovered/a'
        token=tx.acquire_thread('fresh/disposition','safety',20)
        LifecycleSafetyService.acquire_work_in(tx,'work/a',token,purpose='diagnosis')
        proof=LifecycleSafetyService.produce_disposition_in(tx,'work/a',token,ttl=10)
        disposition=LifecycleSafetyService.verify_disposition_in(tx,proof,'work/a',token)
        assert disposition.stopped is True


@pytest.mark.asyncio
@pytest.mark.parametrize('node,source_result', [
    ('N07',NodeResult(execution_status='succeeded',data_status='insufficient',
                      artifact_refs=('actual/raw',),operation_refs=('op/a',))),
    ('N07',NodeResult(execution_status='succeeded',data_status='valid',
                      artifact_refs=('unverified/raw',),operation_refs=('op/a',))),
    ('N09',NodeResult(execution_status='succeeded',cleanup_status='unknown',operation_refs=('op/a',))),
    ('N09',NodeResult(execution_status='succeeded',cleanup_status='incomplete',operation_refs=('op/a',))),
    ('N09',NodeResult(execution_status='succeeded',cleanup_status='complete')),
])
async def test_generic_review_cannot_accept_unresolved_recovery_source(repository,node,source_result):
    plan=await compiled(repository,node=node,measured=True)
    from sandbox_graph.runtime.runner import RunnerCollection
    with repository.transaction() as tx:
        tx.put('collections','op/a:0',RunnerCollection(artifact_refs=('actual/raw',)).model_dump(mode='json'))
    visited=[]
    result=await driver(repository,visited,results={node:source_result}).start(activation(plan))
    with repository.transaction() as tx:
        ref=result['result_refs'][node]
        assert tx.get('node_reviews',ref)['body']['decision']=='accept'
        assert tx.get('receipts',ref)['body']['result']==source_result.model_dump(mode='json')
        assert tx.get('activations','recovered/a')['body']['result']['recovery_acceptance']['accepted'] is False
    assert result['recovery_acceptance']['accepted'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('node', ['N01','N07','N09'])
async def test_source_acceptance_requires_and_records_authoritative_postcheck(repository,node):
    from sandbox_graph.runtime.runner import RunnerCollection
    plan=await compiled(repository,node=node,measured=node!='N01')
    with repository.transaction() as tx:
        tx.put('collections','op/a:0',RunnerCollection(artifact_refs=('actual/raw',)).model_dump(mode='json'))
    source=NodeResult(execution_status='succeeded',operation_refs=('op/a',),
        data_status='valid' if node=='N07' else 'unverified',
        artifact_refs=('actual/raw',) if node=='N07' else (),
        cleanup_status='complete' if node=='N09' else 'not_needed')
    visited=[]
    result=await driver(repository,visited,results={node:source}).start(activation(plan))
    acceptance=result['recovery_acceptance']
    assert acceptance['accepted'] is True
    assert acceptance['execution_review_accepted'] is True
    assert acceptance['reason']=='node_postcheck_accepted'
    assert acceptance['receipt_ref']==result['result_refs'][node]
    with repository.transaction() as tx:
        assert tx.get('work_safety_facts','work/a')['body']['attempt_closed'] is True
        disposition=tx.get('resource_disposition_receipts',acceptance['disposition_ref'])['body']
        assert disposition['producer']['thread_id']=='recover/thread'
        assert disposition['receipt']['stopped'] is True
        assert tx.get('activations','recovered/a')['body']['result']['recovery_acceptance']==acceptance


@pytest.mark.asyncio
async def test_unknown_cleanup_ledger_cannot_accept_complete_source_receipt(repository):
    plan=await compiled(repository,node='N09',measured=True)
    visited=[]
    async def action(context):
        visited.append(context.node_id)
        if context.node_id=='N09':
            with repository.transaction() as tx:
                tx.put('resources','residual/owned',{'scope':'work/a','status':'unknown'})
            return NodeResult(execution_status='succeeded',cleanup_status='complete',
                operation_refs=('op/a',),resource_refs=('residual/owned',))
        return NodeResult(execution_status='succeeded')
    saver=InMemorySaver()
    runner=Driver(repository,{n:action for n in NODE_GROUPS['work']},lambda token:saver,build_work)
    result=await runner.start(activation(plan))
    assert visited==['N07','N09','N10']
    assert result['recovery_acceptance']['execution_review_accepted'] is True
    assert result['recovery_acceptance']['accepted'] is False
    with repository.transaction() as tx:
        proof=tx.get('resource_disposition_receipts',result['recovery_acceptance']['disposition_ref'])['body']
        assert proof['receipt']['residual_resources']==['residual/owned']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation_ref',['op/replacement','op/a'])
async def test_n01_reentry_reconciles_only_the_frozen_original_operation(repository,operation_ref):
    plan=await compiled(repository,node='N01')
    visited=[]
    async def action(context):
        visited.append(context.node_id)
        if context.node_id=='N01':
            if operation_ref!='op/a':
                with repository.transaction() as tx:
                    tx.put('operations',operation_ref,{
                        'scope':'work/a','run_id':'run/a','thread_id':context.thread_id,
                        'request':{'node_id':'N01'},'phase':'terminal',
                        'status':{'state':'completed','authoritative':True}})
            return NodeResult(execution_status='succeeded',operation_refs=(operation_ref,))
        return NodeResult(execution_status='succeeded')
    saver=InMemorySaver()
    runner=Driver(repository,{n:action for n in NODE_GROUPS['work']},lambda token:saver,build_work)
    result=await runner.start(activation(plan))
    assert result['recovery_acceptance']['accepted'] is (operation_ref=='op/a')
    if operation_ref!='op/a':
        assert not set(visited)&{'N02','N03','N04','N05','N06'}
        assert result['recovery_acceptance']['reason']=='original_operation_mismatch'
        assert result['recovery_dependency_failures']['N01']=='original_operation_mismatch'
    with repository.transaction() as tx:
        facts=tx.get('work_safety_facts','work/a')['body']
        assert facts['attempt_closed'] is True
        for closing in ('N07','N09','N10'):
            assert tx.get('receipts',result['result_refs'][closing])['body']['identity']['activation_id']=='recovered/a'


@pytest.mark.asyncio
@pytest.mark.parametrize('raw_ref',['raw/unrelated','raw/original'])
async def test_n08_never_recomputes_a_different_resealed_raw_identity(repository,raw_ref):
    plan=await compiled(repository,node='N08',measured=True)
    visited=[]
    consumed=[]
    async def action(context):
        visited.append(context.node_id)
        if context.node_id=='N07':
            return NodeResult(execution_status='succeeded',data_status='valid',artifact_refs=(raw_ref,))
        if context.node_id=='N08':
            with repository.transaction() as tx:
                consumed.append(tx.get('receipts',context.result_refs['N07'])['body']['result']['artifact_refs'])
            return NodeResult(execution_status='succeeded',data_status='valid',artifact_refs=('analysis/recomputed',))
        return NodeResult(execution_status='succeeded')
    saver=InMemorySaver()
    runner=Driver(repository,{n:action for n in NODE_GROUPS['work']},lambda token:saver,build_work)
    result=await runner.start(activation(plan))
    assert consumed==([['raw/original']] if raw_ref=='raw/original' else [])
    if raw_ref!='raw/original':
        assert result['recovery_dependency_failures']['N08']=='analysis_raw_mismatch'
        assert result['results']['N08']['execution_status']=='blocked'
        assert result['recovery_acceptance']['accepted'] is False
    assert visited==(['N07','N08','N09','N10'] if raw_ref=='raw/original' else ['N07','N09','N10'])
    assert not set(visited)&{'N04','N05','N06'}
    with repository.transaction() as tx:
        assert tx.get('work_safety_facts','work/a')['body']['attempt_closed'] is True
        assert tx.get('receipts',result['result_refs']['N07'])['body']['identity']['activation_id']=='recovered/a'
        assert tx.get('receipts','original/N07')['body']['result']['artifact_refs']==['raw/original']
