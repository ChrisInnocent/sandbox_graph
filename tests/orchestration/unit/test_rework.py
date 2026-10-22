"""Rework is explicit, bounded, and consumable by an existing worker kind."""
import asyncio
from sandbox_graph.domain.models import RunRecord
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.services.revisions import RevisionService
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.services.workflows import WorkflowHandlers


def setup(repository):
    RevisionService(repository).create(RunRecord(run_id='r',root_thread_id='root',graph_ref='g',active_plan_ref='p',contract_ref='c',auth_ref='a',lineage_id='r'))
    with repository.transaction() as tx:
        tx.put('work_instances','w',{'work_id':'w','run_id':'r','plan_ref':'p','group_id':'G','status':'delivered'})
        tx.put('analysis_inputs','frozen',{'plan_hash':'p','deliveries':{'G':['delivery',1]},'reviews':{'G':'review'},'delivery_kind':'deliveries','missing_scope':[]})
        tx.put('deliveries','delivery:1',{'scope':'G','plan_ref':'p','work_refs':['w']})
        tx.put('reviews','review',{'decision':'accepted'})
    token=LeaseService(repository).acquire_thread('root','owner',50)
    return token


def order(**changes):
    from sandbox_graph.services.rework import ReworkOrder
    return ReworkOrder(run_id='r',plan_ref='p',graph_ref='g',kind='recompute',action_ref='analysis.recompute:1',affected_work_refs=('w',),evidence_refs=('frozen',),issue_type='analysis_computation',max_dispatches=1,deadline=1050,analysis_input_ref='frozen',**changes)


def test_r6_dispatches_one_registered_analysis_job_and_replay_does_not_broadcast(repository):
    from sandbox_graph.services.rework import ReworkService
    token=setup(repository);service=ReworkService(repository);ref=service.register(order())
    executor=NodeExecutor(repository,WorkflowHandlers(repository,deadline=1080).mapping(),token)
    state={'thread_id':'root','run_ref':'r','scope':'r','plan_ref':'p','graph_ref':'g','activation_id':'a','input_refs':{'rework_ref':ref}}
    first,_=asyncio.run(executor.execute('R6',state))
    second,_=asyncio.run(executor.execute('A5',state|{'activation_id':'b'}))
    assert first.execution_status==second.execution_status=='succeeded'
    with repository.transaction() as tx:
        jobs=tx.scan('jobs');assert len(jobs)==1
        assert jobs[0]['body']['job_kind']=='analysis'
        assert jobs[0]['body']['payload']['analysis_input_ref']=='frozen'
        assert 'rework_ref' not in jobs[0]['body']['payload']
        assert tx.get('rework_execution',ref)['body']['status']=='dispatched'
        assert tx.scan('operations')==[]


def test_unregistered_remeasure_is_persisted_blocked_and_never_queued(repository):
    from sandbox_graph.services.rework import ReworkService
    token=setup(repository);service=ReworkService(repository)
    requested=order().model_copy(update={'kind':'remeasure','action_ref':'measurement.unregistered:1'})
    ref=service.register(requested)
    result=service.dispatch(ref,token=token,run_id='r',plan_ref='p',graph_ref='g')
    assert result.execution_status=='blocked' and result.error.code=='ADAPTER_MISSING'
    with repository.transaction() as tx:
        assert not tx.scan('jobs')
        assert tx.get('rework_execution',ref)['body']['status']=='blocked'


def test_rework_cannot_broaden_frozen_selected_scope(repository):
    from sandbox_graph.services.rework import ReworkService
    token=setup(repository);service=ReworkService(repository)
    ref=service.register(order().model_copy(update={'affected_work_refs':('foreign-work',)}))
    result=service.dispatch(ref,token=token,run_id='r',plan_ref='p',graph_ref='g')
    assert result.execution_status=='blocked' and result.error.code=='REPLAN_REQUIRED'
    with repository.transaction() as tx:assert not tx.scan('jobs')


async def test_registered_recompute_is_consumed_by_real_analysis_graph(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from tests.benchmark.integration.test_analysis_graph import setup as frozen_setup
    from sandbox_bench.workflow_services import AnalysisReportServices
    from sandbox_graph.services.rework import ReworkOrder,ReworkService
    from sandbox_graph.runtime.bootstrap import build_job_handlers
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    repo,store,reviews,frozen=frozen_setup(tmp_path)
    token=LeaseService(repo).acquire_thread('rework','planner',50)
    requested=ReworkOrder(run_id='r',plan_ref='plan1',graph_ref='graph1',kind='recompute',action_ref='analysis.recompute:1',issue_type='formula_recheck',evidence_refs=(frozen,),affected_work_refs=('case-ga','case-gb'),max_dispatches=1,deadline=1090,analysis_input_ref=frozen)
    service=ReworkService(repo);ref=service.register(requested)
    dispatched=service.dispatch(ref,token=token,run_id='r',plan_ref='plan1',graph_ref='graph1')
    services=AnalysisReportServices(repo,reviews,store,deadline=1090)
    saver=InMemorySaver()
    handlers=build_job_handlers(repo,graph_ref='graph1',saver_factory=lambda token:saver,node_service_factory=lambda token,activation:services.mapping(),review_service=reviews,deadline=1090,lease_seconds=30,segment_seconds=20)
    worker=Worker(repo,handlers,policy=WorkerPolicy(claim_seconds=60,job_seconds=30,poll_seconds=0.01))
    for _ in range(8):
        with repo.transaction() as tx:
            if tx.get('jobs',dispatched.output['job_ref'])['body']['status']!='pending': break
        assert await worker.run_once()
    worker.pump_outbox()
    with repo.transaction() as tx:
        assert tx.get('jobs',dispatched.output['job_ref'])['body']['status']=='complete'
        assert tx.get('rework_results',ref)['body']['status']=='complete'
        assert len(tx.scan('analysis_results'))==1 and len(tx.scan('analysis_exports'))==1
        assert not tx.scan('operations')
        assert all(job['body']['job_kind'] in {'analysis','report','run','supervision'} for job in tx.scan('jobs'))


def test_root_rework_does_not_dispatch_measurement_groups_before_r6(repository):
    token=setup(repository)
    executor=NodeExecutor(repository,WorkflowHandlers(repository,deadline=1080).mapping(),token)
    state={'thread_id':'root','run_ref':'r','scope':'r','plan_ref':'p','graph_ref':'g','activation_id':'root-rework','input_refs':{'rework_ref':'explicit-order'}}
    result,_=asyncio.run(executor.execute('R2',state))
    assert result.execution_status=='succeeded' and result.output['job_refs']==[]
    with repository.transaction() as tx:assert not tx.scan('jobs')
