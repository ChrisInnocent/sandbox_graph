import pytest
from sandbox_graph.domain.models import RunRecord,canonical_hash
from sandbox_graph.domain.evolution import RollbackRecord
from sandbox_graph.services.agent_lifecycle import decode
from sandbox_graph.services.release_observation import ReleaseObservationService
from sandbox_graph.services.rollback import RollbackService
from tests.orchestration.unit.test_promotion import prepared


async def rollback_fixture(repository,tmp_path, *, now=1000.):
    promotion,request,token,candidate,_=await prepared(repository,tmp_path,now=now)
    good=promotion.promote(request,token=token)
    failed=RunRecord(run_id='failed-release',root_thread_id='failed-thread',graph_ref='broken:3',
        active_plan_ref='plan/2',contract_ref='contract/2',auth_ref='auth/2',lineage_id='lineage/a')
    with repository.transaction() as tx:
        tx.put('runs',failed.run_id,failed.model_dump(mode='json'),immutable=True)
        tx.put('dispatch_gates',failed.run_id,{'open':True,'generation':1})
        tx.put('run_bundle_bindings',failed.run_id,{'candidate_ref':'broken-candidate','bundle_hash':'f'*64,'promotion_ref':'broken-publication'})
        tx.put('promotion_records','broken-publication',{'result':{'run_ref':failed.run_id}})
        tx.put('jobs','failed-business',{'job_kind':'work','run_id':failed.run_id,'status':'failed'})
        ReleaseObservationService.observe_in(tx,failed.run_id)
        auth={'principal':'operator','failed_run_ref':failed.run_id,'failed_run_revision':0,
            'candidate_hash':candidate.candidate_hash,'target_plan_ref':candidate.target_plan_ref,
            'gate_manifest_hash':request.gate_manifest_hash,'review_hash':request.review_hash,
            'worker_bundle_hash':request.worker_bundle_hash,'reason':'regression','expires_at':(now+90),'revoked':False}
        tx.put('rollback_authorizations','rollback-auth',auth,immutable=True)
    rollback=decode(RollbackRecord,request.model_dump(mode='json')|{'request_id':'rollback-request',
        'authorization_ref':'rollback-auth','authorization_hash':canonical_hash(auth),'authorization_revision':0,
        'failed_run_ref':failed.run_id,'expected_predecessor_revision':0,'reason':'regression'})
    return rollback,token,good['run_ref']


@pytest.mark.asyncio
async def test_rollback_creates_fresh_run_and_never_reopens_old_checkpoint(repository,tmp_path):
    request,token,good=await rollback_fixture(repository,tmp_path);service=RollbackService(repository)
    result=service.rollback(request,token=token)
    assert result['status']=='activated' and result['run_ref'] not in {good,'run/a',request.failed_run_ref}
    assert service.rollback(request,token=token)==result
    with repository.transaction() as tx:
        new=tx.get('runs',result['run_ref'])['body']
        assert new['graph_ref']=='g:2' and new['supersedes_run_id']==request.failed_run_ref
        assert tx.get('dispatch_gates',request.failed_run_ref)['body']['open'] is False
        assert tx.get('dispatch_gates','run/a')['body']['open'] is False
        assert tx.get('jobs','failed-business')['body']['status']=='failed'
        assert ReleaseObservationService.observe_in(tx,new['run_id'])['status']=='pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['authorization','gate','worker'])
async def test_rollback_cannot_bypass_authority_gate_or_ready_worker(repository,tmp_path,fault):
    request,token,_=await rollback_fixture(repository,tmp_path)
    with repository.transaction() as tx:
        if fault=='authorization':tx.put('rollback_authorizations','rollback-auth',{'revoked':True})
        if fault=='gate':tx.delete('gate_attestations',request.gate_manifest_ref)
        if fault=='worker':tx.delete('worker_registrations','worker/new')
    if fault=='worker':assert RollbackService(repository).rollback(request,token=token)['status']=='verified_waiting_worker'
    else:
        with pytest.raises((ValueError,PermissionError)):RollbackService(repository).rollback(request,token=token)
    with repository.transaction() as tx:assert tx.get('runs',request.failed_run_ref)['body']['successor_run_id'] is None


@pytest.mark.asyncio
async def test_rollback_operator_entry_retains_service_authority_and_request_identity(repository,tmp_path):
    from sandbox_graph.runtime.lifecycle_cli import rollback_command,parser
    request,_,_=await rollback_fixture(repository,tmp_path)
    assert parser().parse_args(['rollback','--input','/tmp/request.json']).command=='rollback'
    result=rollback_command(repository,request.model_dump(mode='json'))
    assert result['status']=='activated'
    assert rollback_command(repository,request.model_dump(mode='json'))==result
    changed=request.model_copy(update={'reason':'another reason'})
    with pytest.raises(Exception,match='input changed'):
        rollback_command(repository,changed.model_dump(mode='json'))


@pytest.mark.asyncio
async def test_waiting_rollback_is_durable_and_conflicting_reuse_rejected(repository,tmp_path):
    request,token,_=await rollback_fixture(repository,tmp_path)
    with repository.transaction() as tx:tx.delete('worker_registrations','worker/new')
    service=RollbackService(repository)
    result=service.rollback(request,token=token)
    with repository.transaction() as tx:
        waiting=tx.get('rollback_requests',request.request_id)['body']
        assert waiting['result']==result and waiting['request']==request.model_dump(mode='json')
    with pytest.raises(Exception,match='input changed'):
        service.rollback(request.model_copy(update={'reason':'another reason'}),token=token)


@pytest.mark.asyncio
async def test_publication_fault_harness_fixture_supports_real_clock_epoch(repository,tmp_path,clock):
    clock.now=2000000000.
    request,token,_=await rollback_fixture(repository,tmp_path,now=clock.now)
    assert RollbackService(repository).rollback(request,token=token)['status']=='activated'
