import pytest
from sandbox_graph.environment.remote_runner import RemoteRecipeRunner,RecipeRepairPlanner
from sandbox_graph.environment.execution import EnvironmentRunnerGateway
from sandbox_graph.runtime.runner import OperationService
from sandbox_graph.domain.models import ScriptRef
from sandbox_graph.runtime.leases import LeaseService
from tests.environment.unit.test_workflow_handlers import setup,execute
from tests.environment.unit.test_planner import catalog
from sandbox_graph.environment.planner import RepairPlanner
from sandbox_graph.environment.permits import ExecutionGrantIssuer
from sandbox_graph.environment.grant_service import FencedExecutionGrantService
from sandbox_graph.environment.products.common import command
from sandbox_graph.domain.models import canonical_hash
from dataclasses import asdict

class Resolver:
    def resolve(self,ref):return '/private/'+ref.split('/')[-1]

class Transport:
    def __init__(self,repo):self.calls=[];self.queries=[];self.repo=repo;self.rows={}
    async def request_granted(self,request,issue):
        self.calls.append(request);data=request.payload['input']
        challenge={'protocol':'environment-grant.v1','nonce':str(len(self.calls)).zfill(43),'machine_id':'h1','boot_id':'boot',
            'wall_time':self.repo.clock.now,'monotonic_time':10.,'challenge_seconds':15.,
            'input_hash':canonical_hash(request.payload)}
        issue(challenge,.01)
        row={'operation_id':data['operation_id'],'machine_id':'h1','boot_id':'boot','action':'product.recipe.step.v1',
            'input_hash':canonical_hash(request.payload),'state':'completed','result':{'status':'completed'}}
        self.rows[data['operation_id']]=row
        return row
    async def request(self,request):
        import json
        from sandbox_graph.environment.transport import SSHResult
        self.queries.append(request)
        return SSHResult(0,json.dumps({'observation':self.rows.get(request.payload['input']['operation_id']),
            'machine_id':'h1','boot_id':'boot'}).encode(),b'')

@pytest.mark.asyncio
async def test_graph_actions_expand_into_separately_granted_durable_remote_steps(tmp_path):
    repo,handler,context,probe,_=setup(tmp_path)
    steps=(command('reload','systemctl','daemon-reload'),command('start','systemctl','start','cubelet.service'))
    digest=canonical_hash([asdict(row) for row in steps])
    bindings={('n1','cube.service.start.v1'):{'recipe':'cube','recipe_hash':digest,'steps':steps,'expected_before':{}}}
    handler.planner=RecipeRepairPlanner(RepairPlanner(catalog()),bindings)
    with repo.transaction() as tx:token=tx.repo.leases['thread']
    transport=Transport(repo)
    runner=RemoteRecipeRunner(repo,token=token,declaration=handler._load(context)[1],transport=transport,
        resolver=Resolver(),grants=FencedExecutionGrantService(repo,ExecutionGrantIssuer(b'x'*32,clock=lambda:repo.clock.now)),
        store=handler.store)
    ref=ScriptRef(script_id='environment.recipe.step',version='1',digest='a'*64)
    operations=OperationService(repo,runner,{(ref.script_id,ref.version):ref})
    handler.gateway=EnvironmentRunnerGateway(operations,action_scripts={'cube.service.start.v1':ref})
    results=await execute(handler,context,['E1','E2','C1','C2','C3','T8','GE'])
    assert results['GE'].output['ready'] and len(transport.calls)==2
    with repo.transaction() as tx:
        assert len(tx.scan('operations'))==2 and len(tx.scan('execution_grants'))==2
        assert all(row['body']['phase']=='terminal' for row in tx.scan('operations'))
    op=next(iter(repo.rows[key]['body']['request']['operation_id'] for key in repo.rows if key[0]=='operations'))
    status=await runner.reconcile(op,context.run_ref)
    assert status.state=='completed' and len(transport.calls)==2 and len(transport.queries)==1

@pytest.mark.asyncio
async def test_remote_compensation_records_original_identity_and_reuses_receipt(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from sandbox_graph.environment.transport import SSHResult
    import json
    repo,handler,context,_,_=setup(tmp_path)
    with repo.transaction() as tx:token=tx.repo.leases['thread']
    class T:
        calls=0
        async def request(self,request):
            self.calls+=1
            return SSHResult(0,json.dumps({'machine_id':'h1','boot_id':'boot',
                'observation':{'status':'completed','path':'/etc/service.yaml','restored_sha256':'a'*64}}).encode(),b'')
    transport=T()
    grants=SimpleNamespace(issue_safety=lambda **kwargs:SimpleNamespace(model_dump=lambda **kw:{}))
    runner=RemoteRecipeRunner(repo,token=token,declaration=handler._load(context)[1],transport=transport,
        resolver=Resolver(),grants=grants,store=handler.store)
    original=SimpleNamespace(run_id=context.run_ref)
    monkeypatch.setattr(runner,'_original',lambda *args:original)
    monkeypatch.setattr(runner,'_binding',lambda op:(None,runner.nodes['n1'],
        {'operation_id':'remote-original','step_id':'file:config','expected_before':{'/etc/service.yaml':'a'*64}}))
    one=await runner.compensate('original',context.run_ref)
    two=await runner.compensate('original',context.run_ref)
    assert one==two and transport.calls==1
    with repo.transaction() as tx:
        rows=tx.scan('environment_compensations')
        assert len(rows)==1 and rows[0]['body']['status']=='completed'
        assert rows[0]['body']['operation_id']=='original' and rows[0]['body']['evidence_ref'].startswith('artifact://')
