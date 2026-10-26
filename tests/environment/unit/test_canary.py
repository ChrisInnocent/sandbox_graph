import pytest
from sandbox_graph.environment.canary import EnvironmentCanaryRunner,CanaryRequest
from sandbox_graph.domain.models import Operation,ScriptRef,canonical_hash
from sandbox_graph.runtime.leases import LeaseService
from sandbox_bench.transport import TransportError
from tests.environment.unit.test_workflow_handlers import setup

class Adapter:
    product='agentenv';cluster=None
    def __init__(self,lost=False,delete_stuck=False):
        self.calls=[];self.lost=lost;self.deleted=False;self.delete_stuck=delete_stuck
        self.headers={'X-API-Key':'correct-key'};self.unprotected=False
    def _control(self,method,path,timeout,**kwargs):
        from types import SimpleNamespace
        self.calls.append('auth')
        if not self.unprotected and self.headers.get('X-API-Key')!='correct-key':raise TransportError('denied',status=401)
        return SimpleNamespace(status=200,body=b'{"sandboxID":"canary"}')
    def create(self,owner,timeout):
        self.calls.append('create')
        if self.lost:raise TimeoutError('response lost')
        return {'sandbox_id':'canary','private':{'envdAccessToken':'never-public'}}
    def list_owned(self,owner,timeout):self.calls.append('list');return [{'sandbox_id':'canary'}]
    def inspect(self,sandbox,timeout):
        self.calls.append('inspect')
        if self.deleted and not self.delete_stuck:raise TransportError('missing',status=404)
        return {'sandbox_id':'canary','state':'running'}
    def effective_spec(self,sandbox,timeout):return {'vcpus':2,'memory_mib':2048}
    def delete(self,sandbox,timeout):self.calls.append('delete');self.deleted=True
    def execute(self,*a,**kw):raise AssertionError('canary must not consume FIRST')

def prepare(tmp_path,adapter,*,control=False,management=False):
    repo,handler,context,_,_=setup(tmp_path)
    with repo.transaction() as tx:
        token=tx.repo.leases['thread']
        auth=tx.get('environment_authorizations','env-auth');body=auth['body']|{'actions':['environment.canary.v1']}
        tx.put('environment_authorizations','env-auth',body)
        row=tx.get('environment_plans',context.plan_ref)
        # Create a new correctly frozen plan rather than editing an existing one.
    from sandbox_graph.environment.workflow import EnvironmentRunService
    base=handler._load(context)[1]
    d=base.model_copy(update={'product':'agentenv','profile':'agentenv.native.v1',
        'nodes':(base.nodes[0].model_copy(update={'roles':('server',)}),),
        'service_endpoints':{'node:n1':'http://127.0.0.1:8080'}})
    if control:
        d=d.model_copy(update={'nodes':d.nodes+(d.nodes[0].model_copy(update={'node_id':'c1','machine_id':'h2','roles':('gateway',)}),),
            'service_endpoints':d.service_endpoints|{'node:c1':'http://control:8080'}})
    if management:
        d=d.model_copy(update={'nodes':d.nodes+(d.nodes[0].model_copy(update={'node_id':'m1','machine_id':'h3','roles':('scheduler',)}),)})
    node_ids=[node.node_id for node in d.nodes]
    with repo.transaction() as tx:
        row=tx.get('environment_authorizations','env-auth');tx.put('environment_authorizations','env-auth',row['body']|{'declaration_hash':d.declaration_hash,'node_ids':node_ids})
    service=EnvironmentRunService(repo);plan=service.plan(d,mode='prepare',node_ids=node_ids,deadline=1200.,authorization_ref='env-auth')
    run=service.start('canary',plan.plan_hash)
    LeaseService(repo).acquire_all(['host:'+node.machine_id for node in d.nodes],scope=run.run_id,token=token,owner_run_id=run.run_id)
    config={'product':'agentenv','api_url':'http://127.0.0.1:8080'}
    request=CanaryRequest(declaration_hash=d.declaration_hash,node_ids=('n1',),config_hash=canonical_hash(config),
        boot_id='boot',expected_vcpus=2,expected_memory_mib=2048,timeout_seconds=2,poll_seconds=.001,max_observations=3,
        **({'control_routes':{'c1':{'endpoint_ref':'node:c1','boot_id':'control-boot'}}} if control else {}),
        **({'management_dependencies':{'m1':'management-boot'}} if management else {}))
    payload={'canary':request.model_dump(mode='json')}
    operation=Operation(operation_id='canary-operation',input_hash=canonical_hash(payload),input=payload,
        script_ref=ScriptRef(script_id='environment.canary',version='1',digest='a'*64),scope=run.run_id,
        run_id=run.run_id,node_id='C2',deadline=1200.,dispatch_generation=1)
    with repo.transaction() as tx:
        tx.put('operations',operation.operation_id,{'scope':run.run_id,'run_id':run.run_id,'request':operation.model_dump(mode='json'),
            'phase':'submission_started'})
    runner=EnvironmentCanaryRunner(repo,token=token,config=config,adapter_factory=lambda config:adapter,
        store=handler.store,private_root=tmp_path/'private')
    return repo,runner,operation,handler.store


@pytest.mark.asyncio
@pytest.mark.parametrize('healthy',[True,False])
async def test_management_role_requires_fresh_service_proof_and_group_canary(tmp_path,healthy):
    from datetime import datetime,timezone
    from sandbox_graph.environment.contracts import EnvironmentDeclaration
    from sandbox_graph.environment.verification import ProductVerification
    adapter=Adapter();repo,runner,op,store=prepare(tmp_path,adapter,control=True,management=True)
    calls=[]
    async def management_probe(declaration,node,boot_id):
        calls.append((node.node_id,boot_id))
        return {'status':'verified' if healthy else 'unavailable','boot_id':boot_id,
            'evidence_level':'group_dependency','node_id':node.node_id,'roles':list(node.roles),
            'service_observations':{'installed':healthy}}
    runner.management_probe=management_probe
    result=await runner.submit(op)
    assert result.state==('completed' if healthy else 'failed')
    assert adapter.deleted and calls==[('m1','management-boot')]
    assert adapter.calls.count('auth')==6  # scheduler has no invented sandbox API
    with repo.transaction() as tx:
        d=EnvironmentDeclaration.model_validate(tx.get('environment_declarations',runner._request(op).declaration_hash)['body'])
    verifier=ProductVerification(probe=None,repository=repo,scope=op.scope,store=store)
    verdict,_,proof=verifier._proofs(d,d.nodes[-1],'management-boot',datetime.now(timezone.utc))
    assert (verdict=='satisfied') is healthy
    if healthy:
        assert proof['functional']['management_dependencies']['m1']['evidence_level']=='group_dependency'
    assert verifier._proofs(d,d.nodes[-1],'new-boot',datetime.now(timezone.utc))[0]!='satisfied'


@pytest.mark.asyncio
async def test_management_dependency_without_probe_never_passes_but_cleans_up(tmp_path):
    adapter=Adapter();repo,runner,op,_=prepare(tmp_path,adapter,control=True,management=True)
    result=await runner.submit(op)
    assert result.state=='failed' and adapter.deleted

@pytest.mark.asyncio
async def test_canary_has_state_and_spec_proof_and_independent_deletion_proof(tmp_path):
    adapter=Adapter();repo,runner,op,store=prepare(tmp_path,adapter)
    result=await runner.submit(op)
    assert result.state=='completed' and result.authoritative
    assert adapter.calls.count('create')==adapter.calls.count('delete')==1
    assert len(result.artifact_refs)==2
    with repo.transaction() as tx:
        assert all(row['body']['status']=='closed' for row in tx.scan('resources'))
        for ref in result.artifact_refs:
            from sandbox_graph.domain.models import EvidenceRef
            record=EvidenceRef.model_validate(tx.get('environment_evidence',ref)['body'])
            assert 'never-public' not in store.path_for(record).read_text()
    assert (await runner.submit(op)).state=='completed' and adapter.calls.count('create')==1

@pytest.mark.asyncio
async def test_unknown_creation_reconciles_owned_id_without_second_create(tmp_path):
    adapter=Adapter(lost=True);repo,runner,op,_=prepare(tmp_path,adapter)
    assert (await runner.submit(op)).state=='unknown'
    result=await runner.reconcile(op.operation_id,op.scope)
    assert result.state=='completed' and adapter.calls.count('create')==1 and 'list' in adapter.calls

@pytest.mark.asyncio
async def test_delete_ack_is_not_cleanup_and_retry_only_queries_original_id(tmp_path):
    adapter=Adapter(delete_stuck=True);repo,runner,op,_=prepare(tmp_path,adapter)
    assert (await runner.submit(op)).state=='unknown'
    assert adapter.calls.count('delete')==1
    adapter.delete_stuck=False
    assert (await runner.reconcile(op.operation_id,op.scope)).state=='completed'
    assert adapter.calls.count('delete')==1

@pytest.mark.asyncio
async def test_bad_spec_is_retained_as_failure_but_owned_canary_still_cleaned(tmp_path):
    adapter=Adapter();adapter.effective_spec=lambda *_:{'vcpus':4,'memory_mib':4096}
    repo,runner,op,_=prepare(tmp_path,adapter)
    result=await runner.submit(op)
    assert result.state=='failed' and result.authoritative and adapter.deleted

@pytest.mark.asyncio
async def test_revoked_authority_never_creates_canary(tmp_path):
    adapter=Adapter();repo,runner,op,_=prepare(tmp_path,adapter)
    with repo.transaction() as tx:
        row=tx.get('environment_authorizations','env-auth');tx.put('environment_authorizations','env-auth',row['body']|{'revoked':True})
    with pytest.raises(PermissionError):await runner.submit(op)
    assert not adapter.calls

@pytest.mark.asyncio
async def test_canary_checks_real_protected_route_and_rejects_anonymous_access(tmp_path):
    adapter=Adapter();adapter.unprotected=True
    repo,runner,op,_=prepare(tmp_path,adapter)
    result=await runner.submit(op)
    assert result.state=='failed' and adapter.deleted
    assert adapter.calls.count('auth')==3


@pytest.mark.asyncio
async def test_control_only_node_proof_uses_real_owned_resource_auth_and_compute_cleanup(tmp_path):
    from datetime import datetime,timezone
    from sandbox_graph.environment.verification import ProductVerification
    from sandbox_graph.environment.contracts import EnvironmentDeclaration
    adapter=Adapter();repo,runner,op,store=prepare(tmp_path,adapter,control=True)
    configurations=[]
    def factory(config):
        configurations.append(config)
        return adapter
    runner.adapter_factory=factory
    result=await runner.submit(op)
    assert result.state=='completed' and adapter.calls.count('create')==1
    assert adapter.calls.count('auth')==6  # compute + independent control endpoint positives/negatives
    assert len(configurations)==2
    assert configurations[0]['api_url']!=configurations[1]['api_url']
    assert configurations[0]['instance_credentials']==configurations[1]['instance_credentials']
    with repo.transaction() as tx:
        d=EnvironmentDeclaration.model_validate(tx.get('environment_declarations',runner._request(op).declaration_hash)['body'])
    verifier=ProductVerification(probe=None,repository=repo,scope=op.scope,store=store)
    assert verifier._proofs(d,d.nodes[1],'control-boot',datetime.now(timezone.utc))[0]=='satisfied'
    assert verifier._proofs(d,d.nodes[1],'different-boot',datetime.now(timezone.utc))[0]!='satisfied'
