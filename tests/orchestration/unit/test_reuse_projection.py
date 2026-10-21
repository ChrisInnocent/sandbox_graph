"""Synthetic raw files; reuse must preserve measurement identity and source scope."""
import pytest
from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.domain.models import DeliveryManifest,NodeResult,WorkKey,RunRecord,canonical_hash
from sandbox_graph.services.reviews import ReviewService
from sandbox_graph.services.reuse import ReuseService,ReuseContract
from sandbox_graph.services.revisions import RevisionService
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.persistence.repository import Conflict


def setup_reuse(repository,tmp_path):
    store=FileArtifactStore(tmp_path);reviews=ReviewService(repository,store)
    condition=canonical_hash({'q':5})
    contract=ReuseContract(environment_ref='environment:1',spec_hash='spec:1',method_ref='B01.TEMPLATE:1',completion_event='ready',time_domain='host:boot:mono',script_ref='script:1',condition_hash=condition,measurement_contract_hash=canonical_hash({'actual_load':5}),valid_until=1100)
    key=WorkKey(group_id='old-group',scenario_id='B01',method_id='B01.TEMPLATE',method_version='1',condition_hash=condition,repeat_id=1)
    raw=store.commit([b'{"case_id":"old-work","plan_ref":"old-plan","samples":[1,2]}'],scope='old-group',media_type='application/json')
    source=DeliveryManifest(delivery_id='old-delivery',version=1,scope='old-group',plan_ref='old-plan',contract_ref='old-contract',work_refs=('old-work',),evidence=(raw,),result=NodeResult(execution_status='succeeded',data_status='valid',cleanup_status='complete',output={'reuse_contract':contract.model_dump(mode='json'),'case_hash':'original-case-hash'}))
    reviews.review_delivery(source,expected_scope='old-group',plan_ref='old-plan',contract_ref='old-contract')
    RevisionService(repository).create(RunRecord(run_id='new-run',root_thread_id='root',graph_ref='g',active_plan_ref='new-plan',contract_ref='new-contract',auth_ref='a',lineage_id='new-run'))
    with repository.transaction() as tx:
        tx.put('work_instances','old-work',{'work_id':'old-work','key':key.model_dump(mode='json'),'run_id':'old-run','plan_ref':'old-plan','group_id':'old-group','environment_ref':'environment:1','status':'delivered'},immutable=True)
        tx.put('work_instances','new-work',{'work_id':'new-work','key':key.model_copy(update={'group_id':'new-group'}).model_dump(mode='json'),'run_id':'new-run','plan_ref':'new-plan','graph_ref':'g','group_id':'new-group','environment_ref':'environment:1','contract_ref':'new-contract','status':'pending','domains':['host']},immutable=True)
    with repository.transaction() as tx:
        for work_id,run_id,plan_ref in [('old-work','old-run','old-plan'),('new-work','new-run','new-plan')]:
            tx.put('measurement_bindings',work_id,{'work_id':work_id,'run_id':run_id,'plan_ref':plan_ref,'case_hash':'original-case-hash' if work_id=='old-work' else 'new-case-hash','measurement_contract_hash':contract.measurement_contract_hash,'script_ref':contract.script_ref},immutable=True)
    token=LeaseService(repository).acquire_thread('new-group-driver','owner',50)
    return ReuseService(repository,reviews),reviews,store,source,contract,token


def test_accepted_reuse_publishes_reference_delivery_without_copying_or_recounting(repository,tmp_path):
    reuse,reviews,store,source,contract,token=setup_reuse(repository,tmp_path)
    old_bytes=store.path_for(source.evidence[0]).read_bytes()
    reuse.consider('new-work','old-delivery:1',old_contract=contract,new_contract=contract,deadline=1050,allowed_source_scopes={'old-group'})
    projected=reuse.project_accepted('new-work',token=token)
    assert projected.work_refs==('new-work',) and projected.evidence==()
    assert projected.plan_ref=='new-plan' and projected.scope=='new-group'
    proof=reuse.validate_projection(projected.result.output['reuse_projection_ref'])
    assert proof['source_delivery']['work_refs']==['old-work']
    assert proof['source_delivery']['plan_ref']=='old-plan'
    assert store.path_for(source.evidence[0]).read_bytes()==old_bytes
    assert reuse.project_accepted('new-work',token=token)==projected
    with repository.transaction() as tx:
        assert tx.get('work_instances','new-work')['body']['status']=='delivered'
        assert len(tx.scan('work_deliveries'))==1
        assert tx.get('deliveries','old-delivery:1')['body']==source.model_dump(mode='json')
        assert tx.scan('jobs')==[] and tx.scan('operations')==[]
        assert tx.get('delivery_reviews',f'{projected.delivery_id}:1')['body']['decision']=='accepted'


def test_reuse_rejects_cross_scope_grant_repeat_mismatch_and_corrupt_source(repository,tmp_path):
    reuse,reviews,store,source,contract,token=setup_reuse(repository,tmp_path)
    with pytest.raises(PermissionError):
        reuse.consider('new-work','old-delivery:1',old_contract=contract,new_contract=contract,deadline=1050,allowed_source_scopes=set())
    reuse.consider('new-work','old-delivery:1',old_contract=contract,new_contract=contract,deadline=1050,allowed_source_scopes={'old-group'})
    with repository.transaction() as tx:
        work=tx.get('work_instances','new-work')['body']
        tx.put('work_instances','new-work',work|{'key':work['key']|{'repeat_id':2}})
    with pytest.raises(Conflict,match='repeat|identity'): reuse.project_accepted('new-work',token=token)
    with repository.transaction() as tx:tx.put('work_instances','new-work',work)
    store.path_for(source.evidence[0]).write_bytes(b'corrupt')
    with pytest.raises((Conflict,ValueError)):reuse.project_accepted('new-work',token=token)
    with repository.transaction() as tx:assert not tx.scan('work_deliveries')


def test_scheduler_projects_accepted_candidate_without_measurement_job(repository,tmp_path):
    from sandbox_graph.services.scheduling import SchedulingService
    reuse,reviews,store,source,contract,token=setup_reuse(repository,tmp_path)
    reuse.consider('new-work','old-delivery:1',old_contract=contract,new_contract=contract,deadline=1050,allowed_source_scopes={'old-group'})
    scheduler=SchedulingService(repository,review_service=reviews)
    assert scheduler.dispatch_work('new-run','new-group',token=token,max_concurrency=1,deadline=1080)==[]
    with repository.transaction() as tx:
        assert tx.get('work_instances','new-work')['body']['status']=='delivered'
        assert tx.scan('resource_leases')==[] and tx.scan('jobs')==[]


def test_analysis_validation_rechecks_projection_raw_even_without_alias_evidence(repository,tmp_path):
    import asyncio
    from sandbox_graph.graphs.executor import NodeExecutor
    from sandbox_graph.services.workflows import WorkflowHandlers
    reuse,reviews,store,source,contract,token=setup_reuse(repository,tmp_path)
    reuse.consider('new-work','old-delivery:1',old_contract=contract,new_contract=contract,deadline=1050,allowed_source_scopes={'old-group'})
    alias=reuse.project_accepted('new-work',token=token)
    frozen=reviews.freeze_analysis('new-plan',{'new-group':(alias.delivery_id,1)},rules_ref='manifest:1')
    resolved=reviews.resolve_analysis(frozen)['groups']['new-group']['measurement_sources']
    assert len(resolved)==1 and resolved[0]['source_delivery']['delivery_id']=='old-delivery'
    store.path_for(source.evidence[0]).write_bytes(b'corrupt')
    executor=NodeExecutor(repository,WorkflowHandlers(repository,deadline=1080,review_service=reviews).mapping(),token)
    result,_=asyncio.run(executor.execute('A2',{'thread_id':token.thread_id,'scope':'new-run','run_ref':'new-run','plan_ref':'new-plan','graph_ref':'g','activation_id':'analysis','input_refs':{'analysis_input_ref':frozen}}))
    assert result.execution_status=='blocked' and result.error.code=='EVIDENCE_INSUFFICIENT'


def test_source_review_outbox_wakes_exact_waiting_reuse_target(repository,tmp_path):
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    reuse,reviews,store,source,contract,token=setup_reuse(repository,tmp_path)
    with repository.transaction() as tx:
        tx.put('reuse_candidates','new-work',{'work_id':'new-work','delivery_ref':'old-delivery:1','old_contract':contract.model_dump(mode='json'),'new_contract':contract.model_dump(mode='json'),'deadline':1050,'status':'waiting','authorized_source_scopes':['old-group']})
    Worker(repository,{},policy=WorkerPolicy(claim_seconds=10,job_seconds=30,poll_seconds=0.01)).pump_outbox()
    with repository.transaction() as tx:
        events=[r['body'] for r in tx.scan('events') if r['body']['aggregate_id']=='group:new-run:new-group']
        assert len(events)==1 and events[0]['kind']=='reuse_source_reviewed'
        assert events[0]['payload']['work_id']=='new-work'
    Worker(repository,{},policy=WorkerPolicy(claim_seconds=10,job_seconds=30,poll_seconds=0.01)).pump_outbox()
    with repository.transaction() as tx:assert len(tx.scan('events'))==1


def test_new_accepted_review_preserves_frozen_candidate_review(repository,tmp_path):
    reuse,reviews,store,source,contract,token=setup_reuse(repository,tmp_path)
    reuse.consider('new-work','old-delivery:1',old_contract=contract,new_contract=contract,deadline=1050,allowed_source_scopes={'old-group'})
    with repository.transaction() as tx: original=tx.get('reuse_refs','new-work')['body']['review_ref']
    reviews.review_delivery(source,expected_scope=source.scope,plan_ref=source.plan_ref,contract_ref=source.contract_ref,rules_ref='manifest:2')
    reuse.consider('new-work','old-delivery:1',old_contract=contract,new_contract=contract,deadline=1050,allowed_source_scopes={'old-group'})
    alias=reuse.project_accepted('new-work',token=token)
    assert reuse.validate_projection(alias.result.output['reuse_projection_ref'])['source_review']['review_id']==original


def test_target_producer_binding_required_and_template_content_change_rejects_reuse(repository,tmp_path):
    from sandbox_graph.services.scheduling import SchedulingService
    reuse,reviews,store,source,contract,token=setup_reuse(repository,tmp_path)
    reuse.consider('new-work','old-delivery:1',old_contract=contract,new_contract=contract,deadline=1050,allowed_source_scopes={'old-group'})
    with repository.transaction() as tx:
        binding=tx.get('measurement_bindings','new-work')['body']
        tx.delete('measurement_bindings','new-work')
    scheduler=SchedulingService(repository,review_service=reviews)
    assert scheduler.materialize_reuse('new-run','new-group',token=token)==[]
    with repository.transaction() as tx:
        assert tx.get('work_instances','new-work')['body']['status']=='pending'
        tx.put('measurement_bindings','new-work',binding|{'measurement_contract_hash':canonical_hash({'actual_load':99})},immutable=True)
    assert scheduler.materialize_reuse('new-run','new-group',token=token)==[]
    with repository.transaction() as tx:
        assert tx.get('work_instances','new-work')['body']['status']=='blocked'
        assert not tx.scan('work_deliveries') and not tx.scan('jobs')
