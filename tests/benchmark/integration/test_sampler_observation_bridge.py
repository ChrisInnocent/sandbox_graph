"""Actual StateGraph/OperationService/BenchSession sampler evidence chain."""
from copy import deepcopy

import pytest

from sandbox_bench.bridge import BenchGateway, work_handlers
from sandbox_graph.domain.models import RunRecord, ScriptRef
from sandbox_graph.graphs.executor import NodeExecutor
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.runtime.leases import LeaseService
from sandbox_graph.runtime.observation import ObservationService
from sandbox_graph.runtime.runner import OperationService
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from sandbox_graph.services.revisions import RevisionService
from sandbox_graph.persistence.repository import Conflict
from tests.benchmark.unit.test_sampler_lifecycle import ControlledSampler, session
from tests.orchestration.conftest import MemoryRepository, FakeClock


async def measured(tmp_path):
    bench = session(tmp_path, ControlledSampler())
    repo = MemoryRepository(FakeClock())
    RevisionService(repo).create(RunRecord(run_id='r', root_thread_id='t', graph_ref='graph1',
        contract_ref='contracthash',active_plan_ref='plan1',auth_ref='fixture',lineage_id='r'))
    token = LeaseService(repo).acquire_thread('t','worker',100)
    with repo.transaction() as tx:
        tx.put('work_instances','w',dict(work_id='w',run_id='r',group_id='g',plan_ref='plan1',
            graph_ref='graph1',contract_ref='contracthash',key={'measurement_generation':3}))
    LifecycleSafetyService(repo).acquire_work('w', token)
    script = ScriptRef(script_id='benchmark.step',version='1',digest='a'*64)
    gateway = BenchGateway({'w':bench},script)
    service = OperationService(repo,gateway,{('benchmark.step','1'):script})
    handlers = work_handlers(service,token,bench,run_id='r',scope='w',dispatch_generation=1,deadline=1050)
    state = dict(thread_id='t',scope='w',run_ref='r',plan_ref='plan1',graph_ref='graph1',
        activation_id='a',input_refs={'case':bench.case.digest()})
    # N01 intentionally blocks with no external configuration; the real graph
    # still takes its mandatory N07/N09/N10 path. N04 is invoked first via the
    # exact production executor and registered operation to exercise capture.
    executor = NodeExecutor(repo,handlers,token)
    result, _ = await executor.execute('N04',state)
    assert result.execution_status == 'succeeded'
    from langgraph.checkpoint.memory import InMemorySaver
    from sandbox_graph.runtime.driver import Driver,Activation
    LifecycleSafetyService(repo).release_work('w',token)
    with repo.transaction() as tx:
        tx.release_thread(token)
    driver=Driver(repo,{},lambda _:InMemorySaver(),build_work,
        handler_factory=lambda current,_:work_handlers(service,current,bench,run_id='r',scope='w',
            dispatch_generation=1,deadline=1050))
    output=await driver.start(Activation(activation_id='a',run_id='r',thread_id='t',scope='w',
        plan_ref='plan1',graph_ref='graph1',input_refs=state['input_refs'],lease_seconds=100,segment_seconds=20))
    token=LeaseService(repo).acquire_thread('t','verification',100)
    LifecycleSafetyService(repo).acquire_work('w',token,purpose='reconcile')
    return repo, token, bench, gateway, service, result, output


@pytest.mark.asyncio
async def test_actual_graph_collects_bound_sampler_stop_and_does_not_rewrite_raw(tmp_path):
    repo,token,bench,gateway,service,start,output = await measured(tmp_path)
    with repo.transaction() as tx:
        rows = tx.scan('observations')
        assert len(rows) == 1, 'benchmark adapter collection must register its actual sampler'
        body = rows[0]['body']
        assert body['operation_id'] == start.operation_refs[0]
        assert body['measurement_generation'] == 3
        assert ObservationService.closed_in(tx, rows[0])
        assert ObservationService.sampler_status_in(tx,rows) == 'stopped'
        assert body['data_status'] == 'insufficient'  # fixture gap remains a gap
        proof = tx.get('sampler_receipts',body['physical_stop_ref'])
        assert proof['body']['producer'] == 'operation_service'
        assert proof['body']['receipt']['binding']['run_id'] == 'r'
        assert proof['body']['receipt']['evidence']['process_identity']['start_ticks'] > 0
    raw = {p:p.read_bytes() for p in (bench.folder/'resource_samples').glob('*.json')}
    n07 = next(key for (kind,key),row in repo.rows.items()
               if kind=='operations' and row['body']['request']['node_id']=='N07')
    await service.collect(n07,'w',token)
    assert all(p.read_bytes() == value for p,value in raw.items())
    with repo.transaction() as tx:
        assert len(tx.scan('observations')) == 1
        assert ObservationService.closed_in(tx,tx.scan('observations')[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('scope','foreign'),('measurement_generation',4),
    ('run_id','foreign'),('case_hash','0'*64),('operation_id','wrong')])
async def test_collection_rejects_mismatched_or_forged_receipt_atomically(tmp_path,field,value):
    repo,token,bench,gateway,service,start,output = await measured(tmp_path)
    n07 = next(key for (kind,key),row in repo.rows.items()
               if kind=='operations' and row['body']['request']['node_id']=='N07')
    original = gateway.collect
    async def tampered(*args,**kwargs):
        collection = await original(*args,**kwargs)
        receipts = deepcopy(list(collection.sampler_receipts))
        receipts[-1]['binding'][field] = value
        return collection.model_copy(update={'sampler_receipts':tuple(receipts)})
    gateway.collect = tampered
    before = deepcopy(repo.rows)
    with pytest.raises((Conflict,ValueError,PermissionError)):
        await service.collect(n07,'w',token)
    assert repo.rows == before


@pytest.mark.asyncio
async def test_legacy_stop_and_caller_booleans_are_never_physical_stop(tmp_path):
    repo,token,bench,gateway,service,start,output = await measured(tmp_path)
    obs = ObservationService(repo)
    obs.start('legacy','w',operation_id=start.operation_refs[0],clock_epoch='old',deadline=1100)
    obs.stop('legacy',last_seq=0)
    with repo.transaction() as tx:
        row = tx.get('observations','legacy')
        tx.put('observations','legacy',row['body'] | {'stopped':True,'children_stopped':True})
        assert not ObservationService.closed_in(tx,tx.get('observations','legacy'))
        assert ObservationService.sampler_status_in(tx,tx.scan('observations')) == 'unknown'


@pytest.mark.asyncio
async def test_verified_history_survives_new_gateway_but_unconsumed_file_does_not(tmp_path):
    repo,token,bench,gateway,service,start,output = await measured(tmp_path/'verified')
    with repo.transaction() as tx:
        observation=tx.scan('observations')[0]
    replacement=session(tmp_path/'verified',ControlledSampler())
    replacement_gateway=BenchGateway({'w':replacement},gateway.script_ref)
    with repo.transaction() as tx:
        assert ObservationService.closed_in(tx,observation)
    n07=next(key for (kind,key),row in repo.rows.items()
             if kind=='operations' and row['body']['request']['node_id']=='N07')
    collection=await gateway.collect(n07,'w')
    with pytest.raises(ValueError):
        replacement_gateway.verify_sampler_receipt(collection.sampler_receipts[-1],n07,'w')
    assert (await replacement._N07())['status']=='unknown'
    replacement._sampler_pool.shutdown(wait=False)


@pytest.mark.asyncio
async def test_original_operation_cancel_waits_for_actual_capture_and_trusted_collection(tmp_path):
    from sandbox_graph.domain.models import Operation,canonical_hash
    repo,token,bench,gateway,service,start,output = await measured(tmp_path/'old')
    # A separate ordinary work, dispatched under its own actual lease, isolates
    # this stop exercise from the already sealed earlier observation.
    body=bench.case.model_dump(mode='json');body['work_id']='w2'
    from sandbox_bench.session import BenchSession
    from sandbox_bench.contracts import CaseContract
    from tests.benchmark.unit.test_session import Scenario
    sampler=ControlledSampler(block_at=2)
    active=BenchSession(CaseContract.model_validate(body),bench.config,tmp_path/'active',
        scenario=Scenario(),sampler=sampler,clock_domain='host-clock')
    with repo.transaction() as tx:
        tx.put('work_instances','w2',dict(work_id='w2',run_id='r',group_id='g',plan_ref='plan1',
            graph_ref='graph1',contract_ref='contracthash',key={'measurement_generation':3}))
    LifecycleSafetyService(repo).acquire_work('w2',token)
    gateway.sessions['w2']=active
    payload={'step_id':'N04','case_hash':active.case.digest()}
    op=Operation(operation_id='original-capture',input_hash=canonical_hash(payload),input=payload,
        script_ref=gateway.script_ref,scope='w2',deadline=1050,node_id='N04',run_id='r')
    assert (await service.submit(op,token)).state=='completed'
    await service.collect(op.operation_id,'w2',token)
    import asyncio
    for _ in range(100):
        if sampler.entered.is_set(): break
        await asyncio.sleep(.001)
    result=await service.cancel(op.operation_id,'w2','bounded stop',token)
    assert result.state=='completed'  # original baseline outcome remains completed
    assert result.output['sampler_stop']['status']=='unknown'
    with repo.transaction() as tx:
        rows=LifecycleSafetyService._rows_in(tx,'observations','w2')
        assert not ObservationService.closed_in(tx,rows[0])
    sampler.release.set()
    # Wait for the actual capture loop and executor join. Wall-clock sleeps
    # race the native thread on loaded hosts and do not prove termination.
    await asyncio.wait_for(asyncio.gather(
        asyncio.shield(active._sample_task),
        asyncio.shield(active._sampler_shutdown_future)), timeout=5)
    result=await service.reconcile(op.operation_id,'w2',token)
    assert result.state=='completed'
    assert result.output['sampler_stop']['status']=='completed'
    with repo.transaction() as tx:
        rows=LifecycleSafetyService._rows_in(tx,'observations','w2')
        assert ObservationService.closed_in(tx,rows[0])
    assert sampler.calls==2


@pytest.mark.asyncio
async def test_safety_worker_releases_only_original_isolation_with_actual_sampler_proof(tmp_path):
    from sandbox_graph.domain.agent_lifecycle import RepairPolicy,LifecycleEvent,RepairRequest
    from sandbox_graph.domain.models import NodeResult
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
    from sandbox_graph.services.remediation import NodeRemediationService
    from sandbox_graph.runtime.tasks import TaskBackend
    from sandbox_graph.runtime.worker import RemedyJobHandler
    from tests.orchestration.unit.test_lifecycle_reconciliation import install
    repo,token,bench,gateway,operations,start,output=await measured(tmp_path)
    with repo.transaction() as tx:
        refs=tuple(r['key'] for r in tx.scan('operations'))
        assert tx.get('work_safety_facts','w')['body']['sampler_status']=='stopped'
    for ref in refs:
        await operations.collect(ref,'w',token)
    policy=RepairPolicy(schema_version=1,policy_version='v1',actions=('case.reconcile@1',),
        max_repair_attempts=1,max_diagnosis_calls=2,max_candidate_revisions=2,token_limit=100,
        token_unit='tokens',child_job_limit=3,max_depth=1,max_concurrency=1,deadline=1100.,clock='wall',
        backoff={'schema_version':1,'seconds':1.,'unit':'seconds'},max_same_failure=2,max_observations=2,
        safety_budget={'schema_version':1,'max_stop_calls':2,'max_query_calls':2,'max_cleanup_calls':2,
            'cost_limit':10.,'cost_unit':'test_units','deadline':1150.,'clock':'wall'},postcheck_ref='postcheck/v1')
    lives=LifecycleService(repo);lives.register_policy('actual-policy',policy)
    source=output['result_refs']['N01']
    record=lives.observe_failure(source,policy_ref='actual-policy',token=token)
    with repo.transaction() as tx:
        LifecycleBudgetService.initialize_in(tx,lifecycle_id=record.key.lifecycle_id,lineage_budget_ref='r',
            limits={'repair_attempts':1,'diagnosis_calls':2,'candidate_revisions':2,'tokens':100,
                    'child_jobs':2,'observations':2,'cost':100},deadline=1100.,
            safety_limits={'stop_calls':2,'query_calls':2,'cleanup_calls':2,'cost':10},
            safety_deadline=1150.,cost_unit='test_units')
        tx.put('repair_authorizations','actual-auth',dict(principal='supervisor/a',run_ref='r',scope_ref='w',
            plan_ref='plan1',graph_ref='graph1',contract_ref='contracthash',work_revision=0,
            actions=['case.reconcile@1'],permissions=['operation.read_owned'],expires_at=1100.,revoked=False))
    proof=LifecycleSafetyService(repo).produce_disposition('w',token,ttl=50)
    lives.bind_disposition(record.key.lifecycle_id,proof,token=token)
    record=lives.advance(record.key.lifecycle_id,LifecycleEvent(schema_version=1,event_id='actual-closed',
        kind='disposition_confirmed',source='safety',expected_revision=0,evidence_refs=(proof,),
        observed_at=1000.),expected_revision=0,token=token)
    decision=lives.record_decision(record.key.lifecycle_id,request_id='actual-decision',route='known_action',
        action_ref='case.reconcile@1',expected_revision=record.revision,evidence_refs=(source,),token=token)
    repair=NodeRemediationService(repo)
    repair.register_input(record.key.lifecycle_id,'actual-input',{'schema_version':1,
        'source_receipt':source,'evidence_refs':[source],'input_refs':{'case':bench.case.digest()}},token=token)
    request=RepairRequest(schema_version=1,request_id='actual-repair',lifecycle_id=record.key.lifecycle_id,
        source_receipt=source,expected_revision=record.revision,action_ref='case.reconcile@1',
        input_manifest_ref='actual-input',proposal_ref=decision['decision_ref'],authorization_ref='actual-auth')
    order=repair.request_remediation(request,principal='supervisor/a',token=token)
    LifecycleSafetyService(repo).release_work('w',token)
    with repo.transaction() as tx: tx.release_thread(token)
    claimed=TaskBackend(repo).claim('repair-worker',80)
    original=start.operation_refs[0]
    async def reconcile_original(context):
        # A lost read response leaves the original operation uncertain. This
        # action neither starts sampling nor writes raw measurement data.
        with repo.transaction() as tx:
            op=tx.get('operations',original)
            tx.put('operations',original,op['body'] | {'phase':'submission_started','status':{}})
        return NodeResult(execution_status='succeeded',operation_refs=refs)
    handler=RemedyJobHandler(repo,lambda token,activation:{'N01':reconcile_original},
        lease_seconds=30,segment_seconds=20)
    await handler(claimed)
    TaskBackend(repo).finish(claimed['job_id'],owner_epoch=claimed['owner_epoch'],
        result={'execution_status':'blocked'},status='blocked')
    with repo.transaction() as tx:
        assert LifecycleSafetyService._repair_isolations_in(tx,'w')
        original_outcome=deepcopy(tx.get('repair_outcomes',order['order_ref']))
    service,runtime=install(repo,record.key.lifecycle_id,order['order_ref'],gateway)
    for _ in range(8):
        assert await runtime.run_once()
        with repo.transaction() as tx:
            if tx.get('repair_isolation_resolutions','repair-resolution:'+order['order_ref']): break
    with repo.transaction() as tx:
        assert LifecycleSafetyService._repair_isolations_in(tx,'w')==[],tx.scan('lifecycle_blockers')
        assert tx.get('repair_isolation_resolutions','repair-resolution:'+order['order_ref'])
        assert tx.get('repair_outcomes',order['order_ref'])==original_outcome
        assert LifecycleBudgetService.snapshot_in(tx,record.key.lifecycle_id)['lifecycle']['safety_spent']['query_calls']==1
    assert bench.sampler.calls>=1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['version','caller_boolean','collection','artifact','raw'])
async def test_live_producer_rejects_forged_fields_reference_and_artifact(tmp_path,mutation):
    repo,token,bench,gateway,service,start,output=await measured(tmp_path)
    ref=start.operation_refs[0]
    collection=await gateway.collect(ref,'w')
    receipt=deepcopy(collection.sampler_receipts[-1])
    if mutation=='version': receipt['contract']='bench-sampler-collection.v2'
    elif mutation=='caller_boolean': receipt['children_stopped']=True
    elif mutation=='collection': receipt['collection_operation_id']='another-operation'
    elif mutation=='raw':
        next((bench.folder/'resource_samples').glob('*.json')).write_text('{}')
    else:
        from sandbox_bench.runner import digest
        artifact=bench.folder/'sampler_lifecycle'/('sampler-'+digest(receipt['evidence'])+'.json')
        artifact.write_text('{}')
    with pytest.raises(ValueError):
        gateway.verify_sampler_receipt(receipt,ref,'w')


@pytest.mark.asyncio
async def test_changed_generation_or_expired_owner_rejects_new_consumption_but_keeps_history(tmp_path):
    from sandbox_graph.persistence.repository import StaleOwner
    repo,token,bench,gateway,service,start,output=await measured(tmp_path)
    ref=start.operation_refs[0]
    with repo.transaction() as tx:
        history=tx.scan('observations')[0]
        work=tx.get('work_instances','w')
        tx.put('work_instances','w',work['body'] | {'key':{'measurement_generation':4}})
    before=deepcopy(repo.rows)
    with pytest.raises(Conflict):
        await service.collect(ref,'w',token)
    assert repo.rows==before
    repo.clock.advance(101)
    with pytest.raises(StaleOwner):
        await service.collect(ref,'w',token)
    with repo.transaction() as tx:
        assert ObservationService.closed_in(tx,history)


@pytest.mark.asyncio
async def test_legacy_sequence_seal_after_trusted_import_keeps_insufficient_and_physical_proof(tmp_path):
    repo,token,bench,gateway,service,start,output=await measured(tmp_path)
    with repo.transaction() as tx: row=tx.scan('observations')[0]
    result=ObservationService(repo).stop(row['key'],last_seq=0)
    assert result['data_status']=='insufficient'
    with repo.transaction() as tx:
        assert ObservationService.closed_in(tx,tx.get('observations',row['key']))
