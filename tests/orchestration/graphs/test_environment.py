import asyncio
import pytest
from sandbox_graph.services.environment import EnvironmentHandlers,EnvironmentSnapshot
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.domain.models import ScriptRef,RunRecord
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.services.revisions import RevisionService

class Probe:
    def __init__(self,state='satisfied',authenticated=True): self.state=state;self.authenticated=authenticated
    async def connect(self,scope,environment_ref): return EnvironmentSnapshot(scope=scope,connected=True,authenticated=self.authenticated,identity_confirmed=True,state='unknown',source_ref='connection')
    async def inspect(self,scope,environment_ref): return EnvironmentSnapshot(scope=scope,connected=True,authenticated=self.authenticated,identity_confirmed=True,state=self.state,source_ref='actual-inspect',differences=('service_version',) if self.state=='drift' else ())

@pytest.mark.parametrize(('state','expected'),[('satisfied','T0'),('drift','C1'),('unknown','GB')])
def test_environment_routes_on_actual_probe_state_without_installation(repository,state,expected):
    handlers=EnvironmentHandlers(repository,Probe(state),deadline=1090)
    token=LeaseService(repository).acquire_thread('t','worker',100)
    executor=NodeExecutor(repository,handlers.mapping(),token)
    graph_state={'thread_id':'t','scope':'G_A','run_ref':'r','plan_ref':'p','graph_ref':'g','activation_id':'a','input_refs':{'environment_ref':'fixture-env'}}
    result,_=asyncio.run(executor.execute('E2',graph_state))
    assert result.continuation==expected
    with repository.transaction() as tx: assert tx.scan('operations')==[]


def test_auth_failure_and_unverified_configuration_adapter_do_not_apply(repository):
    token=LeaseService(repository).acquire_thread('t','worker',100)
    handlers=EnvironmentHandlers(repository,Probe('drift',False),deadline=1090)
    executor=NodeExecutor(repository,handlers.mapping(),token)
    state={'thread_id':'t','scope':'G_A','run_ref':'r','plan_ref':'p','graph_ref':'g','activation_id':'a','input_refs':{'environment_ref':'fixture-env'}}
    result,_=asyncio.run(executor.execute('E1',state))
    assert result.execution_status=='blocked'
    result,_=asyncio.run(executor.execute('C1',state))
    assert result.execution_status=='blocked'
    with repository.transaction() as tx: assert tx.scan('operations')==[]


def test_configuration_reserves_shared_domain_until_independent_verified_completion(repository):
    from sandbox_graph.planning.compiler import Case,PlanCompiler,Selection
    from sandbox_graph.interfaces.service import RunService
    from sandbox_graph.runtime.runner import OperationService,RunnerStatus
    cases=[Case(capability='supported',adapter_verified=True,case_id=g,group_id=g,sandbox_type='x',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'size':1},),conflict_domains=('shared-host',)) for g in ('G_A','G_B')]
    api=RunService(repository,PlanCompiler(cases));plan=api.preview(Selection(),authorized_groups={'G_A','G_B'});run=api.start('r',plan.hash,authorized_groups={'G_A','G_B'})
    probe=Probe('drift');script=ScriptRef(script_id='config',version='1',digest='a'*64)
    class Gateway:
        unknown=False
        async def submit(self,op): return RunnerStatus(state='unknown' if self.unknown else 'completed',authoritative=not self.unknown)
    gateway=Gateway();ops=OperationService(repository,gateway,{('config','1'):script})
    handlers=EnvironmentHandlers(repository,probe,deadline=1100,operation_service=ops,configuration_script=script,configuration_verified=True)
    executors={};states={}
    for g in ('G_A','G_B'):
        token=LeaseService(repository).acquire_thread(g,'worker',100)
        executors[g]=NodeExecutor(repository,handlers.mapping(),token)
        states[g]={'thread_id':g,'scope':g,'run_ref':run.run_id,'plan_ref':plan.hash,'graph_ref':plan.graph_ref,'activation_id':g,'input_refs':{'environment_ref':'fixture'},'result_refs':{}}
    async def step(group,node):
        result,ref=await executors[group].execute(node,states[group]);states[group]['result_refs'][node]=ref;return result
    for group in states: asyncio.run(step(group,'E2'))
    assert asyncio.run(step('G_A','C1')).execution_status=='succeeded'
    assert asyncio.run(step('G_B','C1')).execution_status=='blocked'
    with repository.transaction() as tx: assert tx.get('resource_leases','shared-host')['body']['scope']=='G_A'
    assert asyncio.run(step('G_A','C2')).execution_status=='succeeded'
    probe.state='satisfied'
    assert asyncio.run(step('G_A','C3')).execution_status=='succeeded'
    with repository.transaction() as tx: assert tx.get('resource_leases','shared-host') is None
    states['G_B']['activation_id']='G_B-retry'
    assert asyncio.run(step('G_B','C1')).execution_status=='succeeded'
    gateway.unknown=True
    assert asyncio.run(step('G_B','C2')).execution_status=='blocked'
    # A superficially satisfied probe cannot release an unknown configuration operation.
    assert asyncio.run(step('G_B','C3')).execution_status=='blocked'
    with repository.transaction() as tx: assert tx.get('resource_leases','shared-host')['body']['status']=='quarantine'
