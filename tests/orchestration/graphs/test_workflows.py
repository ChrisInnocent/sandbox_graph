import asyncio
import pytest
from sandbox_graph.services.workflows import WorkflowHandlers,run_definition,run_routers,analysis_definition,linear_routers
from sandbox_graph.interfaces.service import RunService
from sandbox_graph.services.scheduling import SchedulingService
from sandbox_graph.planning.compiler import PlanCompiler,Case,Selection
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.graphs.factory import build_run,build_analysis
from sandbox_graph.runtime.leases import LeaseService


def setup(repository):
    compiler=PlanCompiler([Case(capability='supported',adapter_verified=True,case_id='case',group_id='G_A',sandbox_type='synthetic',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'cpu':1},),conflict_domains=('host_A',))])
    service=RunService(repository,compiler);plan=service.preview(Selection(),authorized_groups={'G_A'})
    run=service.start('start',plan.hash,authorized_groups={'G_A'})
    token=LeaseService(repository).acquire_thread(run.root_thread_id,'worker',100)
    handlers=WorkflowHandlers(repository,deadline=1090)
    executor=NodeExecutor(repository,handlers.mapping(),token)
    state={'thread_id':run.root_thread_id,'scope':run.run_id,'run_ref':run.run_id,'plan_ref':plan.hash,'graph_ref':plan.graph_ref,'activation_id':'root','input_refs':{}}
    return handlers,executor,state


def test_real_run_graph_dispatches_group_then_parks_without_waiting_for_group(repository):
    handlers,executor,state=setup(repository)
    graph=build_run(run_definition(),executor,routers=run_routers())
    output=asyncio.run(graph.ainvoke(state))
    assert output['results']['R2']['execution_status']=='succeeded'
    assert output['results']['R7']['execution_status']=='succeeded'
    with repository.transaction() as tx:
        groups=[j for j in tx.scan('jobs') if j['body']['job_kind']=='group']
        assert len(groups)==1 and groups[0]['body']['status']=='pending'
        assert len(tx.scan('waits'))==1


def test_analysis_missing_actual_statistics_service_is_blocked_not_success(repository):
    handlers,executor,state=setup(repository)
    state['input_refs']={'analysis_input_ref':'missing'}
    definition=analysis_definition()
    with pytest.raises(ValueError,match='unregistered handler'):
        build_analysis(definition,executor,routers=linear_routers(definition.nodes))
    async def forbidden_downstream(context):
        raise AssertionError('missing input must stop before analysis services')
    executor.handlers.update({node:forbidden_downstream for node in ('A3','A4','A7')})
    graph=build_analysis(definition,executor,routers=linear_routers(definition.nodes))
    output=asyncio.run(graph.ainvoke(state))
    assert output['results']['A1']['execution_status']=='blocked'
    assert 'A3' not in output['results']


def test_terminal_failed_work_enters_group_review_with_explicit_gaps(repository):
    handlers,executor,state=setup(repository)
    state=state|{'scope':'G_A','activation_id':'group-recovery'}
    with repository.transaction() as tx:
        tx.put('work_instances','failed-work',{'work_id':'failed-work','run_id':state['run_ref'],'group_id':'G_A','plan_ref':state['plan_ref'],'status':'failed','result_ref':'job:failed'},immutable=True)
    result,_=asyncio.run(executor.execute('G0',state))
    assert result.continuation=='T6'
    review,_=asyncio.run(executor.execute('T6',state))
    assert review.execution_status=='succeeded'
    assert review.review_status=='rejected'
    assert review.output['gaps'][0]['work_id']=='failed-work'


def test_all_preflight_blocked_work_can_route_to_review_and_group_delivery():
    from sandbox_graph.services.workflows import group_definition,group_routers
    assert 'T6' in group_definition().dynamic_routes['T2']
    assert group_routers()['T2']({'results':{'T2':{'execution_status':'succeeded','continuation':'T6'}}})=='T6'
