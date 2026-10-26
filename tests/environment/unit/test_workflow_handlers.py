from dataclasses import replace
import pytest
from sandbox_graph.environment.workflow import EnvironmentRunService
from sandbox_graph.environment.handlers import EnvironmentWorkflowHandlers
from sandbox_graph.environment.planner import RepairPlanner
from sandbox_graph.environment.contracts import StepReceipt
from sandbox_graph.domain.models import canonical_hash
from sandbox_graph.graphs.executor import NodeContext
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.artifacts.store import FileArtifactStore
from tests.orchestration.conftest import MemoryRepository,FakeClock
from tests.environment.unit.test_planner import declaration,report,catalog

class Probe:
    def __init__(self, reports):self.reports=reports;self.calls=[]
    async def check(self, declaration, node_ids, *, fresh):
        self.calls.append(fresh)
        return self.reports[min(len(self.calls)-1,len(self.reports)-1)]

class Gateway:
    def __init__(self,state='completed'):self.state=state;self.submitted=[];self.queried=[];self.plan_refs={}
    def bind_plan(self,plan,ref):self.plan_refs[plan.plan_hash]=ref
    async def submit_step(self,**kw):self.submitted.append(kw);return self.receipt(kw)
    async def reconcile_step(self,**kw):self.queried.append(kw);return self.receipt(kw)
    def receipt(self,kw):
        return StepReceipt(plan_ref=self.plan_refs[kw['plan'].plan_hash],step_id=kw['step'].step_id,
            attempt=1,started_at='2026-10-24T00:00:00Z',input_hash=canonical_hash(kw['step']),
            effect_certainty='unknown' if self.state=='unknown' else 'confirmed',result=self.state)

def setup(tmp_path,mode='prepare',states=('missing','satisfied'),gateway_state='completed'):
    repo=MemoryRepository(FakeClock());d=declaration();service=EnvironmentRunService(repo)
    with repo.transaction() as tx:
        tx.put('environment_authorizations','env-auth',{'declaration_hash':d.declaration_hash,'node_ids':['n1'],
            'actions':['cube.service.start.v1'],'revoked':False,'expires_at':1200.})
    plan=service.plan(d,mode=mode,node_ids=['n1'],deadline=1200.,authorization_ref='env-auth' if mode=='prepare' else None)
    run=service.start('start',plan.plan_hash)
    token=LeaseService(repo).acquire_thread('thread','owner',100)
    context=NodeContext('E1','owner','thread','default',run.run_id,plan.plan_hash,'environment:1','activation','visit',{}, {},token.owner_epoch,token.lease_id)
    reports=[report(state).model_copy(update={'source_set':d.source_set,'readiness':{'installation':'ready' if state=='satisfied' else 'blocked'}}) for state in states]
    probe=Probe(reports);gateway=Gateway(gateway_state)
    handler=EnvironmentWorkflowHandlers(repo,store=FileArtifactStore(tmp_path/'artifacts'),probe=probe,
        planner=RepairPlanner(catalog()),gateway=gateway)
    return repo,handler,context,probe,gateway

async def execute(handler,context,nodes):
    results={}
    for node in nodes:results[node]=await handler.mapping()[node](replace(context,node_id=node))
    return results

@pytest.mark.asyncio
async def test_check_only_produces_report_without_install_or_case(tmp_path):
    repo,handler,context,probe,gateway=setup(tmp_path,'check',('missing',))
    results=await execute(handler,context,['E1','E2','T8','GE'])
    assert results['E2'].continuation=='T8' and not gateway.submitted
    assert results['GE'].output['ready'] is False
    with repo.transaction() as tx:
        delivery=tx.scan('environment_deliveries')[0]['body']
        assert delivery['manifest_ref'].startswith('artifact://') and not tx.scan('work_instances')

@pytest.mark.asyncio
async def test_prepare_executes_once_and_freshly_verifies_with_real_artifacts(tmp_path):
    repo,handler,context,probe,gateway=setup(tmp_path)
    results=await execute(handler,context,['E1','E2','C1','C2','C3','T8','GE'])
    assert all(value.execution_status=='succeeded' for value in results.values())
    assert results['GE'].output['ready'] is True and probe.calls==[False,True]
    assert len(gateway.submitted)==1
    repeated=await handler.configure(replace(context,node_id='C2'))
    assert repeated.execution_status=='succeeded' and len(gateway.submitted)==1

@pytest.mark.asyncio
async def test_unknown_original_step_is_queried_never_resubmitted(tmp_path):
    repo,handler,context,probe,gateway=setup(tmp_path,gateway_state='unknown')
    results=await execute(handler,context,['E1','E2','C1','C2','T8','GE'])
    assert results['C2'].execution_status=='blocked' and results['GE'].output['ready'] is False
    gateway.state='completed'
    recovered=await handler.configure(replace(context,node_id='C2'))
    assert recovered.execution_status=='succeeded'
    assert len(gateway.submitted)==len(gateway.queried)==1

@pytest.mark.asyncio
async def test_repair_revision_budget_stops_unchanged_drift(tmp_path):
    repo,handler,context,probe,gateway=setup(tmp_path,states=('missing',))
    results=await execute(handler,context,['E1','E2','C1','C2','C3','T8','GE'])
    assert results['C3'].execution_status=='blocked' and results['GE'].output['ready'] is False
    assert len(gateway.submitted)==2 and len(probe.calls)==3
    assert len({call['plan'].plan_hash for call in gateway.submitted})==2

@pytest.mark.asyncio
async def test_authority_revocation_blocks_before_next_mutation(tmp_path):
    repo,handler,context,probe,gateway=setup(tmp_path)
    await execute(handler,context,['E1','E2','C1'])
    with repo.transaction() as tx:
        row=tx.get('environment_authorizations','env-auth');tx.put('environment_authorizations','env-auth',row['body']|{'revoked':True})
    with pytest.raises(PermissionError):await handler.configure(replace(context,node_id='C2'))
    assert not gateway.submitted

@pytest.mark.asyncio
async def test_failed_configuration_receipt_cannot_become_ready_on_report_only(tmp_path):
    _,handler,context,probe,gateway=setup(tmp_path,gateway_state='failed')
    results=await execute(handler,context,['E1','E2','C1','C2','T8','GE'])
    assert results['GE'].output['ready'] is False
    assert results['T8'].cleanup_status=='complete'

@pytest.mark.asyncio
async def test_environment_job_runs_real_langgraph_driver_and_publishes_delivery(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from sandbox_graph.environment.handlers import build_environment_handler
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    repo,handler,context,probe,gateway=setup(tmp_path)
    job_handler=build_environment_handler(repo,saver_factory=lambda token:InMemorySaver(),
        service_factory=lambda token,activation:handler,lease_seconds=30,segment_seconds=20)
    worker=Worker(repo,{'environment':job_handler},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,
        poll_seconds=1,accepted_job_kinds=('environment',)))
    assert await worker.run_once()
    with repo.transaction() as tx:
        job=next(row for row in tx.scan('jobs') if row['body']['job_kind']=='environment')['body']
        assert job['status']=='complete',job.get('result')
        assert len(tx.scan('environment_deliveries'))==1
        assert tx.scan('environment_deliveries')[0]['body']['ready']
        assert {row['body']['identity']['node_id'] for row in tx.scan('receipts')}=={'E1','E2','C1','C2','C3','T8','GE'}

@pytest.mark.asyncio
async def test_resource_release_wakes_environment_without_benchmark_cases(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from sandbox_graph.environment.handlers import build_environment_handler
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    repo,handler,context,probe,gateway=setup(tmp_path,states=('missing','satisfied'))
    owner=LeaseService(repo).acquire_thread('foreign','owner',100)
    LeaseService(repo).acquire_all(['host:h1'],scope='foreign',token=owner,owner_run_id='foreign-run')
    job_handler=build_environment_handler(repo,saver_factory=lambda token:InMemorySaver(),
        service_factory=lambda token,activation:handler,lease_seconds=30,segment_seconds=20)
    worker=Worker(repo,{'environment':job_handler},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,
        poll_seconds=1,accepted_job_kinds=('environment',)))
    assert await worker.run_once() and not gateway.submitted
    with repo.transaction() as tx:assert tx.scan('waits')
    LeaseService(repo).release(['host:h1'],scope='foreign',token=owner,closed=True,owner_run_id='foreign-run')
    worker.pump_outbox()
    assert await worker.run_once()
    assert len(gateway.submitted)==1
    with repo.transaction() as tx:assert any(row['body']['ready'] for row in tx.scan('environment_deliveries'))

@pytest.mark.asyncio
async def test_closed_gate_allows_original_query_but_never_new_install(tmp_path):
    repo,handler,context,probe,gateway=setup(tmp_path,gateway_state='unknown')
    await execute(handler,context,['E1','E2','C1','C2'])
    with repo.transaction() as tx:
        row=tx.get('dispatch_gates',context.run_ref)
        tx.put('dispatch_gates',context.run_ref,row['body']|{'open':False})
    gateway.state='completed'
    assert (await handler.configure(context)).execution_status=='succeeded'
    assert len(gateway.submitted)==len(gateway.queried)==1

@pytest.mark.asyncio
async def test_cleanup_cannot_release_host_with_durable_unknown_operation(tmp_path):
    repo,handler,context,_,_=setup(tmp_path)
    await execute(handler,context,['E1','E2','C1','C2'])
    with repo.transaction() as tx:
        tx.put('operations','unacknowledged',{'scope':context.run_ref,'run_id':context.run_ref,'phase':'uncertain',
            'status':{'state':'unknown','authoritative':False}})
    assert (await handler.cleanup(context)).cleanup_status=='unknown'
    with repo.transaction() as tx:assert tx.get('resource_leases','host:h1')['body']['status']=='reserved'

@pytest.mark.asyncio
async def test_reentry_executes_all_frozen_steps_even_if_probe_is_ready(tmp_path):
    _,handler,context,probe,gateway=setup(tmp_path,states=('missing','satisfied'))
    await execute(handler,context,['E1','E2','C1'])
    result=await handler.inspect(context)
    assert result.continuation=='C1'
    await handler.configure(context)
    assert len(gateway.submitted)==1

@pytest.mark.asyncio
async def test_known_failed_step_gets_fresh_diagnosis_and_finite_revision(tmp_path):
    _,handler,context,probe,gateway=setup(tmp_path,states=('missing','missing','satisfied'),gateway_state='failed')
    await execute(handler,context,['E1','E2','C1'])
    configured=await handler.configure(context)
    assert configured.execution_status=='succeeded' and configured.output['known_failure']
    gateway.state='completed'
    verified=await handler.verify(context)
    assert verified.execution_status=='succeeded' and probe.calls==[False,True,True]
    assert len(gateway.submitted)==2 and not gateway.queried
    assert len({row['plan'].plan_hash for row in gateway.submitted})==2
    assert (await handler.cleanup(context)).cleanup_status=='complete'

@pytest.mark.asyncio
async def test_probe_different_source_version_rejected(tmp_path):
    _,handler,context,probe,_=setup(tmp_path)
    probe.reports=[probe.reports[0].model_copy(update={'source_set':('foreign=version',)})]
    with pytest.raises(Exception,match='source|environment'):
        await handler.inspect(context)
