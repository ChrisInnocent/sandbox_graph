import pytest
from langgraph.graph import END
from sandbox_graph.environment.workflow import EnvironmentRunService,environment_definition,environment_routers
from sandbox_graph.graphs.factory import build_definition
from sandbox_graph.domain.models import NodeResult
from tests.orchestration.conftest import MemoryRepository,FakeClock
from tests.environment.unit.test_acceptance_binding import inputs


def test_environment_plan_and_start_need_no_benchmark_case_or_capability():
    declaration,_,_=inputs();repo=MemoryRepository(FakeClock());service=EnvironmentRunService(repo)
    plan=service.plan(declaration,mode='check',node_ids=['n1'],deadline=1100.)
    assert plan.graph_ref=='environment:1' and not hasattr(plan,'cases')
    run=service.start('check/1',plan.plan_hash)
    assert service.start('check/1',plan.plan_hash)==run
    with repo.transaction() as tx:
        assert not tx.scan('work_instances')
        assert tx.get('environment_plans',plan.plan_hash)
        assert tx.scan('jobs')[0]['body']['job_kind']=='environment'
        assert tx.get('graph_definitions','environment:1')


def test_mutation_requires_exact_environment_scope_authority_and_reentry_is_immutable():
    declaration,_,_=inputs();repo=MemoryRepository(FakeClock());service=EnvironmentRunService(repo)
    with pytest.raises(PermissionError):service.plan(declaration,mode='prepare',node_ids=['n1'],deadline=1100.)
    with repo.transaction() as tx:
        tx.put('environment_authorizations','auth/env',{'declaration_hash':declaration.declaration_hash,
            'node_ids':['n1'],'actions':['product.install.v1'],'revoked':False,'expires_at':1100.})
    plan=service.plan(declaration,mode='prepare',node_ids=['n1'],deadline=1100.,authorization_ref='auth/env')
    with repo.transaction() as tx:
        row=tx.get('environment_authorizations','auth/env');tx.put('environment_authorizations','auth/env',row['body']|{'revoked':True})
    with pytest.raises(PermissionError):service.start('install/1',plan.plan_hash)


@pytest.mark.asyncio
@pytest.mark.parametrize('failed',['E1','E2','C1','C2','C3',None])
async def test_every_environment_path_archives_including_unknown_install(failed):
    class Executor:
        def __init__(self):self.handlers={node:object() for node in environment_definition().nodes};self.seen=[]
        async def execute(self,node,state):
            self.seen.append(node)
            return NodeResult(execution_status='blocked' if node==failed else 'succeeded'),node+'/receipt'
    executor=Executor()
    graph=build_definition(environment_definition(),executor,routers=environment_routers())
    await graph.ainvoke({'graph_ref':'environment:1','input_refs':{}})
    assert executor.seen[-2:]==['T8','GE']
    if failed:assert executor.seen[executor.seen.index(failed)+1]=='T8'


def test_environment_reconcile_uses_new_activation_and_original_plan():
    declaration,_,_=inputs();repo=MemoryRepository(FakeClock());service=EnvironmentRunService(repo)
    plan=service.plan(declaration,mode='check',node_ids=['n1'],deadline=1100.)
    run=service.start('check',plan.plan_hash)
    with repo.transaction() as tx:
        row=tx.scan('jobs')[0];tx.put('jobs',row['key'],row['body']|{'status':'complete'})
    job=service.reconcile('reconcile/1',run.run_id)
    assert service.reconcile('reconcile/1',run.run_id)==job
    with repo.transaction() as tx:
        value=tx.get('jobs',job)['body']
        assert value['payload']['plan_ref']==plan.plan_hash and value['activation_id']!=run.run_id+':prepare'
        assert not tx.scan('work_instances')


def test_reconcile_can_observe_original_operations_after_dispatch_gate_closes():
    from sandbox_graph.runtime.tasks import TaskBackend
    declaration,_,_=inputs();repo=MemoryRepository(FakeClock());service=EnvironmentRunService(repo)
    plan=service.plan(declaration,mode='check',node_ids=['n1'],deadline=1100.)
    run=service.start('check',plan.plan_hash)
    with repo.transaction() as tx:
        row=tx.scan('jobs')[0];tx.put('jobs',row['key'],row['body']|{'status':'complete'})
        tx.put('dispatch_gates',run.run_id,{'open':False,'generation':2})
    identity=service.reconcile('reconcile/closed',run.run_id)
    claimed=TaskBackend(repo).claim('observer',30,accepted_job_kinds=('environment',))
    assert claimed and claimed['job_id']==identity
