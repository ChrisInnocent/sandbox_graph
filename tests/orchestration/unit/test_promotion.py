import pytest
from sandbox_graph.domain.evolution import ReviewRecord,PromotionRecord,TARGET_FIELDS
from sandbox_graph.domain.models import RunRecord,canonical_hash
from sandbox_graph.services.agent_lifecycle import decode
from sandbox_graph.services.candidate_reviews import CandidateReviewService
from sandbox_graph.services.promotion import PromotionService
from sandbox_graph.services.revisions import RevisionService
from tests.orchestration.unit.test_evolution_service import setup,edits
from tests.orchestration.unit.test_candidate_worker import verified


async def prepared(repository,tmp_path, *, now=1000.):
    service,proposal,target,token,_=setup(repository,tmp_path,now=now)
    service.open(proposal,target=target,token=token)
    candidate=service.edit_revision(proposal.proposal_id,request_id='edit/1',edits=edits(2),graph_ref='g:2',token=token)
    gate=await verified(repository,candidate,token,'VALUE=2\n',now=now)
    from sandbox_graph.services.worker_bundles import WorkerBundleService
    from pathlib import Path
    import sys
    from types import SimpleNamespace
    bundles=WorkerBundleService(repository,root=tmp_path/'bundles')
    bundle_ref=bundles.build(candidate,token=token,workspace=service.workspace)
    bundle=bundles.verify(bundle_ref)
    sys.modules['promotion_fixture']=SimpleNamespace(__file__=str(Path(bundle['path'])/'tree/app.py'))
    try:bundles.register_ready('worker/new',bundle_ref,token=token,modules=('promotion_fixture',),ttl=60)
    finally:sys.modules.pop('promotion_fixture',None)
    targets={key:getattr(candidate,key) for key in TARGET_FIELDS}
    review=decode(ReviewRecord,{'schema_version':1,'review_ref':'review/1','candidate_ref':candidate.candidate_ref,
        'candidate_hash':candidate.candidate_hash,**targets,'gate_manifest_ref':gate['manifest_ref'],
        'gate_manifest_hash':gate['manifest_hash'],'policy_hash':'b'*64,'coder_identity':'coder/task',
        'reviewer_identity':'reviewer/task','reviewer_kind':'human','producer_version':'1','result':'approved',
        'findings':[],'evidence_refs':[candidate.candidate_ref],'observed_at':now,'expires_at':(now+90),'clock':'wall'})
    with repository.transaction() as tx:
        tx.put('candidate_reviewers','reviewer/task',{'version':'1','kind':'human','revoked':False,'expires_at':(now+100)})
        CandidateReviewService.assign_in(tx,candidate.candidate_ref,review_ref=review.review_ref,
            coder_identity='coder/task',reviewer_identity='reviewer/task',coder_context='coding/context',
            reviewer_context='review/context',gate_manifest_ref=gate['manifest_ref'],token=token)
        CandidateReviewService.record_in(tx,review,authenticated_identity='reviewer/task',token=token)
        old=RunRecord(run_id='run/a',root_thread_id='run-thread',graph_ref='graph/a',contract_ref='contract/a',
            active_plan_ref='plan/a',auth_ref='old/auth',lineage_id='lineage/a')
        tx.put('runs','run/a',old.model_dump(mode='json'))
        tx.put('dispatch_gates','run/a',{'open':True,'generation':1})
        auth={'candidate_hash':candidate.candidate_hash,'predecessor_run_id':'run/a',
            'gate_manifest_hash':gate['manifest_hash'],'review_hash':canonical_hash(review),
            'revoked':False,'expires_at':(now+100),'disposition_policy':'drain','principal':'human/owner'}
        tx.put('publication_authorizations','publish/1',auth,immutable=True)
        revision=tx.get('runs','run/a')['revision']
    request=decode(PromotionRecord,{'schema_version':1,'request_id':'publish/request','candidate_ref':candidate.candidate_ref,
        'candidate_hash':candidate.candidate_hash,**targets,'gate_manifest_ref':gate['manifest_ref'],
        'gate_manifest_hash':gate['manifest_hash'],'review_ref':review.review_ref,'review_hash':canonical_hash(review),
        'authorization_ref':'publish/1','authorization_hash':canonical_hash(auth),'authorization_revision':0,
        'worker_bundle_ref':bundle_ref,'worker_bundle_hash':canonical_hash(bundle),
        'expected_predecessor_revision':revision,'created_at':now,'expires_at':(now+80),'clock':'wall'})
    return PromotionService(repository),request,token,candidate,review


@pytest.mark.asyncio
async def test_activation_uses_frozen_new_contract_and_auth_and_is_idempotent(repository,tmp_path):
    service,request,token,candidate,_=await prepared(repository,tmp_path)
    result=service.promote(request,token=token)
    assert result['status']=='activated'
    assert service.promote(request,token=token)==result
    with repository.transaction() as tx:
        old=tx.get('runs','run/a')['body'];new=tx.get('runs',result['run_ref'])['body']
        assert old['contract_ref']=='contract/a' and old['auth_ref']=='old/auth'
        assert new['contract_ref']=='contract/2' and new['auth_ref']=='auth/2'
        assert new['graph_ref']=='g:2' and new['active_plan_ref']=='plan/2'
        assert new['lineage_id']==old['lineage_id']
        assert tx.get('dispatch_gates','run/a')['body']['open'] is False


@pytest.mark.asyncio
async def test_no_ready_worker_does_not_close_old_run(repository,tmp_path):
    service,request,token,_,_=await prepared(repository,tmp_path)
    with repository.transaction() as tx:tx.delete('worker_registrations','worker/new')
    assert service.promote(request,token=token)['status']=='verified_waiting_worker'
    with repository.transaction() as tx:
        assert tx.get('dispatch_gates','run/a')['body']['open'] is True
        assert tx.get('runs','run/a')['body']['successor_run_id'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['auth','target','reviewer','gate','bundle','revision','bypass'])
async def test_publication_mutations_fail_without_closing_dispatch(repository,tmp_path,fault):
    service,request,token,candidate,review=await prepared(repository,tmp_path)
    with repository.transaction() as tx:
        if fault=='auth':
            row=tx.get('publication_authorizations','publish/1');tx.put('publication_authorizations','publish/1',row['body']|{'revoked':True})
        if fault=='target':tx.put('execution_authorizations','auth/2',{'revoked':True})
        if fault=='reviewer':tx.put('candidate_reviewers','reviewer/task',{'revoked':True})
        if fault=='gate':tx.delete('gate_attestations',request.gate_manifest_ref)
        if fault=='bundle':tx.put('worker_bundles',request.worker_bundle_ref,{'candidate_hash':'0'*64})
        if fault=='revision':tx.put('runs','run/a',tx.get('runs','run/a')['body'])
    with pytest.raises((ValueError,PermissionError)):
        if fault=='bypass':
            RevisionService(repository).activate_revision(request_id='bypass',predecessor_run_id='run/a',
                expected_revision=request.expected_predecessor_revision,candidate_plan_hash='plan/2',graph_ref='g:2',disposition_policy='drain')
        else:service.promote(request,token=token)
    with repository.transaction() as tx:assert tx.get('dispatch_gates','run/a')['body']['open'] is True

@pytest.mark.asyncio
async def test_waiting_worker_is_durable_lifecycle_phase_and_ready_retry_preserves_version(repository,tmp_path):
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    service,request,token,candidate,_=await prepared(repository,tmp_path)
    with repository.transaction() as tx:
        registration=tx.get('worker_registrations','worker/new')['body']
        tx.delete('worker_registrations','worker/new')
    assert service.promote(request,token=token)['status']=='verified_waiting_worker'
    with repository.transaction() as tx:
        proposal=tx.get('change_proposals',candidate.proposal_id)['body']['proposal']
        life_id=proposal['source_key']['lifecycle_id']
        assert LifecycleService.load_in(tx,life_id).phase=='verified_waiting_worker'
        tx.put('worker_registrations','worker/new',registration)
    result=service.promote(request,token=token)
    assert result['status']=='activated'
    with repository.transaction() as tx:
        assert LifecycleService.load_in(tx,life_id).phase=='promotion_pending'
        assert tx.get('lifecycle_promotion_bindings',life_id)['body']['successor_run_ref']==result['run_ref']
        assert tx.scan('lifecycle_stop_requests')  # real drain proves supersession later

@pytest.mark.asyncio
async def test_promoted_evaluation_requires_exact_candidate_deployment_and_binds_successor(repository,tmp_path):
    service,request,token,candidate,_=await prepared(repository,tmp_path)
    with repository.transaction() as tx:
        tx.put('evaluation_deployments','run/a',{'runtime_hash':'old','graph_ref':'graph/a','deadline':1100.})
    with pytest.raises(PermissionError,match='deployment'):service.promote(request,token=token)
    with repository.transaction() as tx:
        assert tx.get('dispatch_gates','run/a')['body']['open']
        tx.put('candidate_deployment_bindings',candidate.candidate_hash,{
            'candidate_hash':candidate.candidate_hash,'bundle_ref':request.worker_bundle_ref,'bundle_hash':request.worker_bundle_hash,
            'plan_ref':candidate.target_plan_ref,'contract_ref':candidate.target_contract_ref,'auth_ref':candidate.target_auth_ref,
            'graph_ref':candidate.graph_ref,'runtime_hash':'new-runtime','execution_seconds':60.,'worker_id':'worker/new'},immutable=True)
    result=service.promote(request,token=token)
    with repository.transaction() as tx:
        bound=tx.get('evaluation_deployments',result['run_ref'])['body']
        assert bound['runtime_hash']=='new-runtime' and bound['deadline']==1060.
