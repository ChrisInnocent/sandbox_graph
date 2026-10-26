import hashlib
from copy import deepcopy
import pytest

from sandbox_graph.domain.evolution import ChangeProposal, TargetBinding
from sandbox_graph.domain.models import canonical_hash
from sandbox_graph.services.agent_lifecycle import decode
from sandbox_graph.services.evolution import EvolutionService
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from sandbox_graph.runtime.candidate_workspace import CandidateWorkspace
from tests.orchestration.unit.test_candidate_workspace import repository_at
from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle
from tests.orchestration.unit.test_evolution_models import proposal_data


def setup(repository, tmp_path, *, now=1000.):
    lifecycle, token = setup_lifecycle(repository, now=now)
    life = lifecycle.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    repo, commit = repository_at(tmp_path)
    workspace = CandidateWorkspace(repo, tmp_path/'candidates')
    body = proposal_data() | {'source_key':life.key.model_dump(mode='json'),
        'base_graph_ref':'graph/a','failure_evidence_refs':['receipt/a'],
        'node_scope':['N01'],'allowed_paths':['app.py']}
    proposal = decode(ChangeProposal, body)
    with repository.transaction() as tx:
        # Candidate admission consumes the same completed attempt proof as a
        # registered repair; a source failure alone is not closure evidence.
        tx.put('work_safety_facts','work/a',{'scope':'work/a','run_ref':'run/a','plan_ref':'plan/a',
            'graph_ref':'graph/a','contract_ref':'contract/a','work_revision':0,'schema_version':1,
            'stopped':True,'admission_status':'closed','sampler_status':'not_started','attempt_closed':True,
            'producer':'work_runtime','producer_version':'1','activation_id':'activation/a',
            'attempt_receipt_ref':'receipt/N10','receipt_refs':{node:'receipt/'+node for node in ('N07','N09','N10')}})
        for node in ('N07','N09','N10'):
            tx.put('receipts','receipt/'+node,{'identity':{'node_id':node,'scope':'work/a','run_ref':'run/a',
                'plan_ref':'plan/a','graph_ref':'graph/a','activation_id':'activation/a'},
                'result':{'execution_status':'succeeded','cleanup_status':'complete'}},immutable=True)
        tx.put('code_bundles','code/1',{'base_commit':commit},immutable=True)
        targets = {}
        for name, kind in [('plan','plans'),('contract','contracts'),('auth','execution_authorizations')]:
            value={'identity':name, 'revoked':False, 'expires_at':(now+100)}
            if name=='plan':value.update(stage='all',graph_ref='g:2',cases=[{'group_id':'group/a'}])
            if name=='auth':value.update(principal='operator',permissions=['run.execute'],plan_ref='plan/2',
                plan_hash=targets['target_plan_hash'],contract_ref='contract/2',contract_hash=targets['target_contract_hash'],
                authorized_groups=['group/a'],stages=['all'])
            tx.put(kind,name+'/2',value,immutable=True)
            targets['target_'+name+'_ref']=name+'/2'
            targets['target_'+name+'_hash']=canonical_hash(value)
        targets['target_auth_revision']=0
        alternate=dict(targets)
        plan3=tx.get('plans','plan/2')['body']|{'graph_ref':'g:3'}
        tx.put('plans','plan/3',plan3,immutable=True)
        alternate.update(target_plan_ref='plan/3',target_plan_hash=canonical_hash(plan3))
        auth3=tx.get('execution_authorizations','auth/2')['body']|{'plan_ref':'plan/3','plan_hash':canonical_hash(plan3)}
        tx.put('execution_authorizations','auth/3',auth3,immutable=True)
        alternate.update(target_auth_ref='auth/3',target_auth_hash=canonical_hash(auth3))
        tx.put('candidate_revision_targets',proposal.proposal_id+':2',{'target':{'schema_version':1,**alternate}},immutable=True)
        tx.put('evolution_authorizations',proposal.scope_auth,{
            'lifecycle_id':life.key.lifecycle_id,'run_ref':'run/a','scope_ref':'work/a',
            'allowed_paths':['app.py'],'node_scope':['N01'],'contract_scope':['contract/2'],
            'revoked':False,'expires_at':(now+100),'max_risk':'R3'},immutable=True)
        tx.put('evolution_features','run/a',{'candidate_enabled':True},immutable=True)
        LifecycleBudgetService.initialize_in(tx,lifecycle_id=life.key.lifecycle_id,
            lineage_budget_ref='lineage/a',limits={k:2 if k=='candidate_revisions' else 100
                for k in ('candidate_revisions','repair_attempts','diagnosis_calls','tokens','cost','child_jobs','observations')},
            deadline=(now+100),safety_deadline=(now+150),cost_unit='microUSD',
            safety_limits={'stop_calls':2,'query_calls':2,'cleanup_calls':2,'cost':10})
    service=EvolutionService(repository, workspace)
    target=TargetBinding(schema_version=1,**targets)
    return service, proposal, target, token, repo


def edits(value, old=1):
    return [{'path':'app.py','before_sha256':hashlib.sha256(f'VALUE={old}\n'.encode()).hexdigest(),
        'content':f'VALUE={value}\n'}]


def test_frozen_proposal_revision_budget_history_and_idempotency(repository,tmp_path):
    service,proposal,target,token,repo=setup(repository,tmp_path)
    service.open(proposal,target=target,token=token)
    first=service.edit_revision(proposal.proposal_id,request_id='edit/1',edits=edits(2),graph_ref='g:2',token=token)
    assert first.revision==1 and first.risk=='R3'
    assert service.edit_revision(proposal.proposal_id,request_id='edit/1',edits=edits(2),graph_ref='g:2',token=token)==first
    second=service.edit_revision(proposal.proposal_id,request_id='edit/2',edits=edits(3,2),graph_ref='g:3',token=token)
    assert second.revision==2 and second.parent_revision==1
    with pytest.raises(Exception,match='budget'):
        service.edit_revision(proposal.proposal_id,request_id='edit/3',edits=edits(4,3),graph_ref='g:4',token=token)
    assert (repo/'app.py').read_text()=='VALUE=1\n'
    with repository.transaction() as tx:
        assert len(tx.scan('candidate_revisions'))==2
        assert LifecycleBudgetService.snapshot_in(tx,proposal.source_key.lifecycle_id)['lineage']['spent']['candidate_revisions']==2


@pytest.mark.parametrize('fault',['feature','auth','target','scope','base'])
def test_proposal_requires_registered_frozen_inputs_and_authorized_scope(repository,tmp_path,fault):
    service,proposal,target,token,_=setup(repository,tmp_path)
    with repository.transaction() as tx:
        if fault=='feature':tx.put('evolution_features','run/a',{'candidate_enabled':False})
        if fault=='auth':tx.put('evolution_authorizations',proposal.scope_auth,{'revoked':True})
        if fault=='target':tx.put('contracts','contract/2',{'changed':True})
        if fault=='base':tx.delete('code_bundles','code/1')
    if fault=='scope':proposal=proposal.model_copy(update={'allowed_paths':('requirements.lock',)})
    with pytest.raises((ValueError,PermissionError,KeyError)):
        service.open(proposal,target=target,token=token)
    assert not service.workspace._folder(proposal.proposal_id).exists()


def test_revocation_and_cancel_stop_new_edits_and_preserve_original_candidate(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path)
    service.open(proposal,target=target,token=token)
    candidate=service.edit_revision(proposal.proposal_id,request_id='edit/1',edits=edits(2),graph_ref='g:2',token=token)
    service.cancel(proposal.proposal_id,token=token)
    with pytest.raises(PermissionError):
        service.edit_revision(proposal.proposal_id,request_id='edit/2',edits=edits(3,2),graph_ref='g:3',token=token)
    assert service.status(proposal.proposal_id)['status']=='cancelled'
    assert service.status(proposal.proposal_id)['candidate_refs']==[candidate.candidate_ref]


def test_unknown_edit_never_replays_file_mutation(repository,tmp_path,monkeypatch):
    service,proposal,target,token,_=setup(repository,tmp_path)
    service.open(proposal,target=target,token=token)
    original=service.workspace.freeze
    def interrupted(*args,**kwargs):raise OSError('simulated worker death after edit')
    monkeypatch.setattr(service.workspace,'freeze',interrupted)
    with pytest.raises(OSError):service.edit_revision(proposal.proposal_id,request_id='edit/1',edits=edits(2),graph_ref='g:2',token=token)
    monkeypatch.setattr(service.workspace,'freeze',original)
    with pytest.raises(Exception,match='reconcil'):
        service.edit_revision(proposal.proposal_id,request_id='edit/1',edits=edits(2),graph_ref='g:2',token=token)
    candidate=service.reconcile_revision('edit/1',token=token)
    assert candidate.revision==1
    assert service.edit_revision(proposal.proposal_id,request_id='edit/1',edits=edits(2),graph_ref='g:2',token=token)==candidate


def test_candidate_cannot_start_over_unknown_site_effect(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path)
    with repository.transaction() as tx:
        tx.put('operations','unknown',{'scope':'work/a','run_id':'run/a','phase':'uncertain',
            'status':{'state':'unknown','authoritative':False}})
    with pytest.raises(Exception,match='disposition|closed|unresolved'):
        service.open(proposal,target=target,token=token)
    assert not service.workspace._folder(proposal.proposal_id).exists()


@pytest.mark.parametrize('fault',['permission','plan_scope','contract_scope','groups','stage'])
def test_successor_auth_must_authorize_execution_not_only_match_its_digest(repository,tmp_path,fault):
    service,proposal,target,token,_=setup(repository,tmp_path)
    with repository.transaction() as tx:
        row=tx.get('execution_authorizations',target.target_auth_ref)
        body=row['body']|{'principal':'operator','permissions':['run.execute'],'plan_ref':target.target_plan_ref,
            'plan_hash':target.target_plan_hash,'contract_ref':target.target_contract_ref,'contract_hash':target.target_contract_hash,
            'authorized_groups':['group/a'],'stages':['all']}
        if fault=='permission':body['permissions']=[]
        if fault=='plan_scope':body['plan_ref']='foreign'
        if fault=='contract_scope':body['contract_ref']='foreign'
        if fault=='groups':body['authorized_groups']=[]
        if fault=='stage':body['stages']=[]
        saved=tx.put('execution_authorizations',target.target_auth_ref,body)
        target=target.model_copy(update={'target_auth_hash':canonical_hash(body),'target_auth_revision':row['revision']+1})
    with pytest.raises(PermissionError,match='execution|scope|permission'):
        service.open(proposal,target=target,token=token)


def test_candidate_graph_must_match_frozen_revision_target_before_any_file_change(repository,tmp_path):
    service,proposal,target,token,_=setup(repository,tmp_path)
    service.open(proposal,target=target,token=token)
    with pytest.raises(PermissionError,match='graph'):
        service.edit_revision(proposal.proposal_id,request_id='wrong-graph',edits=edits(2),graph_ref='unapproved:2',token=token)
    assert (service.workspace._folder(proposal.proposal_id)/'work/app.py').read_text()=='VALUE=1\n'
    with repository.transaction() as tx:assert not tx.scan('candidate_operations')
