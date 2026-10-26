"""ALU-11/ALI-02 real graph guards; controlled handlers have no sampler effects."""
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sandbox_graph.catalog.registry import NODE_GROUPS
from sandbox_graph.domain.models import NodeResult
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.runtime.driver import Activation, Driver


def setup_work(repository):
    with repository.transaction() as tx:
        tx.put('runs','run/a',{'run_id':'run/a','active_plan_ref':'plan/a','graph_ref':'graph/a'})
        tx.put('work_instances','work/a',{'work_id':'work/a','run_id':'run/a','plan_ref':'plan/a',
            'graph_ref':'graph/a','contract_ref':'contract/a','key':{'measurement_generation':1}})
        tx.put('fixtures','fixture/a',{'state':'ready'})


def activation(identity):
    return Activation(activation_id=identity,run_id='run/a',thread_id='thread/'+identity,
        scope='work/a',plan_ref='plan/a',graph_ref='graph/a',input_refs={'fixture':'fixture/a'},
        lease_seconds=30,segment_seconds=10)


def graph_driver(repository, visited, *, mutation=None, failure=None):
    async def action(context):
        visited.append(context.node_id)
        if context.node_id=='N04' and mutation:
            with repository.transaction() as tx:
                if mutation=='fixture': tx.put('fixtures','fixture/a',{'state':'destroyed'})
                else: tx.put('observations','segment/a',{'scope':'work/a','status':'running','interrupted':True,'segments':['a','b']})
        if context.node_id==failure:
            return NodeResult(execution_status='failed',data_status='insufficient',output={'Q':4,'successes':1},artifact_refs=('raw/failed',))
        return NodeResult(execution_status='succeeded',data_status='valid',artifact_refs=('raw/all',),performance_status='fail' if context.node_id=='N08' else 'not_evaluated')
    return Driver(repository,{n:action for n in NODE_GROUPS['work']},lambda token:InMemorySaver(),build_work)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['N04','N05','N06'])
async def test_new_activation_cannot_reopen_the_same_generation(repository,failure):
    setup_work(repository)
    visited=[]
    first=await graph_driver(repository,visited,failure=failure).start(activation('first'))
    raw=deepcopy(repository.rows[('receipts',first['result_refs'][failure])])
    second=[]
    result=await graph_driver(repository,second).start(activation('second'))
    assert not set(second)&{'N04','N05','N06'}
    assert result['results']['N04']['execution_status']=='blocked'
    assert result['results']['N08']['data_status']=='insufficient'
    assert second[-3:]==['N08','N09','N10']
    assert repository.rows[('receipts',first['result_refs'][failure])]==raw


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['fixture','segment'])
async def test_baseline_change_blocks_load_and_cannot_become_valid_analysis(repository,mutation):
    setup_work(repository)
    visited=[]
    result=await graph_driver(repository,visited,mutation=mutation).start(activation('first'))
    assert 'N05' not in visited and 'N06' not in visited
    assert result['results']['N05']['execution_status']=='blocked'
    assert result['results']['N08']['data_status']=='insufficient'
    assert visited[-4:]==['N07','N08','N09','N10']


@pytest.mark.asyncio
async def test_original_operation_takeover_queries_same_id_without_reopening_measurement(repository):
    from sandbox_graph.runtime.runner import OperationService,RunnerStatus
    from sandbox_graph.persistence.repository import LeaseToken
    setup_work(repository)
    with repository.transaction() as tx:
        tx.put('operations','original/op',{'scope':'work/a','run_id':'run/a','thread_id':'old',
            'phase':'submission_started','request':{'node_id':'N05'},'request_hash':'original-request'})
    class Gateway:
        async def reconcile(self,operation_id,scope):
            assert (operation_id,scope)==('original/op','work/a')
            return RunnerStatus(state='completed',authoritative=True)
        async def submit(self,operation):
            raise AssertionError('takeover must not resubmit')
    operations=OperationService(repository,Gateway(),{})
    visited=[]
    async def action(context):
        visited.append(context.node_id)
        if context.node_id=='N01':
            token=LeaseToken(context.thread_id,context.owner_epoch,context.lease_id,0)
            await operations.reconcile('original/op',context.scope,token)
            return NodeResult(execution_status='succeeded',operation_refs=('original/op',))
        return NodeResult(execution_status='succeeded')
    result=await Driver(repository,{n:action for n in NODE_GROUPS['work']},lambda token:InMemorySaver(),build_work).start(activation('takeover'))
    assert not set(visited)&{'N04','N05','N06'}
    assert visited[-4:]==['N07','N08','N09','N10']
    assert result['results']['N04']['execution_status']=='blocked'
    with repository.transaction() as tx:
        operation=tx.get('operations','original/op')['body']
        assert operation['phase']=='terminal'
        assert operation['request_hash']=='original-request'
        assert len(tx.scan('operations'))==1


@pytest.mark.asyncio
async def test_valid_performance_fail_stays_valid_and_does_not_schedule_repair(repository):
    setup_work(repository)
    visited=[]
    result=await graph_driver(repository,visited).start(activation('valid-fail'))
    assert result['results']['N08']['data_status']=='valid'
    assert result['results']['N08']['performance_status']=='fail'
    with repository.transaction() as tx:
        assert not tx.scan('agent_supervision')
        assert not tx.scan('measurement_authorizations')
        assert not tx.scan('jobs')


@pytest.mark.asyncio
@pytest.mark.parametrize('barrier',['revoke_before','expire_before','source_changes_before','admit_before_revoke'])
async def test_generation_effect_admission_is_linearized_with_authority(repository,clock,barrier):
    from tests.orchestration.unit.test_measurement_generation import authorized_target
    from sandbox_graph.runtime.runner import OperationService,RunnerStatus
    from sandbox_graph.domain.models import Operation,ScriptRef,canonical_hash
    from sandbox_graph.persistence.repository import LeaseToken
    service,plan,target,_=await authorized_target(repository)
    submitted=[];queried=[];visited=[]
    script=ScriptRef(script_id='controlled',version='1',digest='a'*64)
    class Gateway:
        async def submit(self,operation):
            submitted.append(operation.operation_id)
            if barrier=='admit_before_revoke' and operation.operation_id=='fresh': service.revoke('measure/a')
            return RunnerStatus(state='completed',authoritative=True)
        async def reconcile(self,identity,scope):
            queried.append(identity);return RunnerStatus(state='completed',authoritative=True)
    operations=OperationService(repository,Gateway(),{('controlled','1'):script})
    async def action(context):
        visited.append(context.node_id)
        token=LeaseToken(context.thread_id,context.owner_epoch,context.lease_id,0)
        def operation(identity,node,kind='ordinary'):
            return Operation(operation_id=identity,input_hash=canonical_hash({}),script_ref=script,
                scope=target['work_id'],run_id=target['run_id'],input={},deadline=2000,
                node_id=node,submission_kind=kind)
        if context.node_id=='N04':
            if barrier=='revoke_before': service.revoke('measure/a')
            elif barrier=='expire_before': clock.advance(91)
            elif barrier=='source_changes_before':
                with repository.transaction() as tx:
                    tx.put('observations','new-unknown',{'scope':plan.source_work_ref,'status':'running'})
            await operations.submit(operation('fresh','N04'),token)
            if barrier=='admit_before_revoke':
                # Already admitted ID can still be read; a second fresh effect cannot.
                await operations.reconcile('fresh',target['work_id'],token)
                await operations.submit(operation('second','N04'),token)
        if context.node_id=='N07' and barrier=='admit_before_revoke':
            await operations.submit(operation('mandatory-tail','N07','cleanup'),token)
        return NodeResult(execution_status='succeeded')
    activation=Activation(activation_id='effect',run_id=target['run_id'],thread_id='effect',scope=target['work_id'],
        plan_ref=plan.hash,graph_ref='graph/a',input_refs={},lease_seconds=300,segment_seconds=10)
    result=await Driver(repository,{n:action for n in NODE_GROUPS['work']},lambda token:InMemorySaver(),build_work).start(activation)
    assert submitted==(['fresh','mandatory-tail'] if barrier=='admit_before_revoke' else [])
    assert queried==(['fresh'] if barrier=='admit_before_revoke' else [])
    assert visited[-4:]==['N07','N08','N09','N10']
    assert result['results']['N04']['execution_status']=='failed'
    with repository.transaction() as tx:
        assert not tx.get('submission_permits','second')
        if barrier!='admit_before_revoke': assert not tx.get('submission_permits','fresh')
