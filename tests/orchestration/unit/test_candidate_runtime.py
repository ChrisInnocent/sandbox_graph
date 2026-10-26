import hashlib
import pytest
from sandbox_graph.domain.models import canonical_hash
from tests.orchestration.unit.test_evolution_service import setup,edits


def observer(tmp_path):
    path=tmp_path/'controller_observer.py'
    path.write_text('from pathlib import Path\nasync def observe(context):\n'
        '    actual=(Path(context.candidate.workspace_ref)/"app.py").read_text()\n'
        '    return {"status":"pass" if actual==context.configuration["expected"] else "fail",'
        '"observations":{"actual":actual}}\n')
    path.chmod(0o644)
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'callable':'observe','version':'1','configuration':{'expected':'VALUE=2\n'}}


@pytest.mark.asyncio
async def test_runtime_uses_registered_external_observer_and_exact_frozen_target(repository,tmp_path):
    from sandbox_graph.runtime.candidate_runtime import CandidateGateRuntime
    service,proposal,target,token,repo=setup(repository,tmp_path)
    service.open(proposal,target=target,token=token)
    candidate=service.edit_revision(proposal.proposal_id,request_id='patch',edits=edits(2),graph_ref='g:2',token=token)
    from sandbox_graph.services.agent_lifecycle import decode
    from sandbox_graph.domain.evolution import CandidateRevision
    if not isinstance(candidate,CandidateRevision):
        with repository.transaction() as tx:candidate=decode(CandidateRevision,tx.get('candidate_revisions',candidate['candidate_ref'])['body'])
    gates=('G0','G1','G2','G3','G4','G6.pre')
    config={'profile_ref':'production','environment_ref':'qualified:1','environment_hash':'c'*64,
        'producer_identity':'trusted','producer_version':'1','runner_ref':'runner:1','runner_version':'1',
        'runner_bundle_hash':'a'*64,'cases':{gate+'/case':observer(tmp_path) for gate in gates}}
    runtime=CandidateGateRuntime(service,config)
    with repository.transaction() as tx:
        tx.put('trusted_gate_producers','trusted',{'version':'1','runner_ref':'runner:1','runner_version':'1',
            'bundle_hash':'a'*64,'revoked':False,'expires_at':1100.},immutable=True)
        tx.put('gate_profiles','production',{'profile_ref':'production','suite_hash':runtime.runner.suite_hash,
            'policy_hash':'b'*64,'cases':{gate:[gate+'/case'] for gate in gates}},immutable=True)
    result=await runtime(candidate,token)
    assert result['status']=='pass'
    assert await runtime(candidate,token)==result
    assert (repo/'app.py').read_text()=='VALUE=1\n'
    config['cases']['G0/case']['configuration']['expected']='forged'
    assert runtime.runner.cases['G0/case'].configuration=={'expected':'VALUE=2\n'}


def test_observer_cannot_be_loaded_from_candidate_or_changed_digest(repository,tmp_path):
    from sandbox_graph.runtime.candidate_runtime import load_cases
    service,proposal,target,token,repo=setup(repository,tmp_path)
    value=observer(tmp_path);value['sha256']='0'*64
    with pytest.raises(ValueError,match='digest'):load_cases(service,{'g/case':value})
    value=observer(repo)
    with pytest.raises(PermissionError,match='outside'):load_cases(service,{'g/case':value})


@pytest.mark.asyncio
async def test_data_only_worker_assembly_executes_coder_trusted_gate_and_independent_review(repository,tmp_path,monkeypatch):
    from sandbox_graph.runtime.lifecycle_assembly import assemble_lifecycle
    from sandbox_graph.environment.cli import LocalReferences
    from sandbox_graph.artifacts.store import FileArtifactStore
    from tests.orchestration.unit.test_candidate_worker import Coder,config
    from tests.orchestration.unit.test_candidate_review_worker import Reviewer
    service,proposal,target,token,repo=setup(repository,tmp_path)
    secret=tmp_path/'bridge-secret';secret.write_text('private-bearer');secret.chmod(0o600)
    resolver=LocalReferences({'secret://group/bridge/v1':{'path':str(secret),'sha256':hashlib.sha256(secret.read_bytes()).hexdigest()}})
    scope={'schema_version':1,'kind':'candidate','ref':proposal.proposal_id}
    provider={'bridge':{'schema_version':1,'endpoint':'http://localhost:9911','provider_id':'local','scope':scope,
        'timeout_seconds':2.,'max_request_bytes':1048576,'max_response_bytes':1048576},'credential_ref':'secret://group/bridge/v1'}
    coder,reviewer=Coder(proposal),Reviewer(proposal);providers=iter((coder,reviewer))
    monkeypatch.setattr('sandbox_graph.runtime.agent_bridge.HttpAgentProvider',lambda bridge:next(providers))
    gates=('G0','G1','G2','G3','G4','G6.pre')
    gate={'profile_ref':'production','environment_ref':'qualified:1','environment_hash':'c'*64,
        'producer_identity':'trusted','producer_version':'1','runner_ref':'runner:1','runner_version':'1',
        'runner_bundle_hash':'a'*64,'cases':{name+'/case':observer(tmp_path) for name in gates}}
    value={'schema_version':1,'bindings':[],'evolution':{'repository':str(repo),'candidate_root':str(service.workspace.root),
        'bindings':[{'proposal':proposal.model_dump(mode='json'),'target':target.model_dump(mode='json')}],
        'coding':{'provider':provider,'config':config().wire(),'identity':'coder'},
        'review':{'provider':provider,'config':config().wire(),'identity':'reviewer'},'gates':gate,'lease_seconds':30.,'owner':'evolution'}}
    result=assemble_lifecycle(repository,value,store=FileArtifactStore(tmp_path/'artifacts'),resolver=resolver)
    evolution=result['evolution'];service=evolution.service
    with repository.transaction() as tx:
        tx.put('trusted_gate_producers','trusted',{'version':'1','runner_ref':'runner:1','runner_version':'1',
            'bundle_hash':'a'*64,'revoked':False,'expires_at':1100.},immutable=True)
        tx.put('gate_profiles','production',{'profile_ref':'production','suite_hash':evolution.worker.verify.runner.suite_hash,
            'policy_hash':'b'*64,'cases':{name:[name+'/case'] for name in gates}},immutable=True)
        tx.put('candidate_reviewers','reviewer',{'kind':'model','version':'1','revoked':False,'expires_at':1100.,
            'provider_id':'local','model_id':'coder','model_version':'1'},immutable=True)
    service.open(proposal,target=target,token=token)
    assert (await evolution.worker.tick(proposal.proposal_id,token=token))['status']=='review_approved'
    assert len(coder.submissions)==len(reviewer.submissions)==1
    assert (repo/'app.py').read_text()=='VALUE=1\n'


@pytest.mark.asyncio
async def test_isolation_exit_claim_cannot_override_independent_unknown_cleanup(repository,tmp_path):
    from types import SimpleNamespace
    from sandbox_graph.runtime.candidate_runtime import BoundIsolation
    service,proposal,target,token,_=setup(repository,tmp_path)
    service.open(proposal,target=target,token=token)
    candidate=service.edit_revision(proposal.proposal_id,request_id='patch',edits=edits(2),graph_ref='g:2',token=token)
    class Backend:
        def identity(self,*args,**kwargs):return {'request':'original'}
        async def execute(self,*args,**kwargs):return {'status':'completed','cleanup':'complete','returncode':0,'stdout':'42'}
        async def reconcile(self,*args):return {'status':'unknown','cleanup':'unknown'}
    bound=BoundIsolation(service,Backend(),'backend',SimpleNamespace(candidate=candidate,request_id='gate'),token)
    result=await bound.execute('command')
    assert result['status']=='unknown' and result['cleanup']=='unknown'
    with repository.transaction() as tx:assert tx.scan('candidate_disposition_proofs')==[]
