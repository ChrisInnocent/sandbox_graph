"""Version boundary, exact operator authority, atomic generation identity."""
from copy import deepcopy
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from sandbox_graph.catalog.registry import NODE_GROUPS
from sandbox_graph.domain.models import NodeResult, RunRecord
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.planning.compiler import Plan
from sandbox_graph.runtime.driver import Activation, Driver
from sandbox_graph.services.agent_lifecycle import LifecycleService
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from sandbox_graph.services.scheduling import SchedulingService
from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle

LEGACY = Path(__file__).parents[1] / 'fixtures/legacy_measurement_plan.json'


def test_legacy_plan_wire_and_hash_are_not_reinterpreted():
    raw=LEGACY.read_text()
    plan=Plan.model_validate_json(raw)
    assert plan.hash=='a561d2ac65552dd5e519bc81eace37a81142ffa7c5d363d4616e5c5c9c7f9d0f'
    assert plan.model_dump_json(indent=2)+'\n'==raw


async def source_ready(repository, *, fail='N05'):
    setup_lifecycle(repository)  # Shared declared repair policy only; no synthetic stop proof.
    plan=Plan.model_validate_json(LEGACY.read_text())
    with repository.transaction() as tx:
        tx.put('plans',plan.hash,plan.model_dump(mode='json'),immutable=True)
        run=RunRecord(run_id='source',root_thread_id='source/thread',active_plan_ref=plan.hash,
            graph_ref='graph/a',contract_ref='contract/a',auth_ref=plan.auth_hash,lineage_id='lineage/source')
        tx.put('runs','source',run.model_dump(mode='json'),immutable=True)
        tx.put('dispatch_gates','source',{'open':True,'generation':1})
        tx.put('contracts','contract/a',{'Q':4,'resource':'host/a'},immutable=True)
    work=SchedulingService(repository).expand_work('source','group/a',plan_ref=plan.hash)[0]
    async def action(context):
        return NodeResult(execution_status='failed' if context.node_id==fail else 'succeeded',
            data_status='insufficient' if context.node_id==fail else 'unverified',
            artifact_refs=('raw/all-four',),output={'Q':4,'successes':1})
    activation=Activation(activation_id='source/attempt',run_id='source',thread_id='source/work',
        scope=work,plan_ref=plan.hash,graph_ref='graph/a',input_refs={},lease_seconds=30,segment_seconds=10)
    result=await Driver(repository,{n:action for n in NODE_GROUPS['work']},lambda token:InMemorySaver(),build_work).start(activation)
    with repository.transaction() as tx:
        token=tx.acquire_thread('source/work','operator',90)
        LifecycleSafetyService.acquire_work_in(tx,work,token,purpose='diagnosis')
        record=LifecycleService.observe_failure_in(tx,result['result_refs'][fail],policy_ref='policy/a',token=token)
    LifecycleBudgetService(repository).initialize(lifecycle_id=record.key.lifecycle_id,
        lineage_budget_ref='lineage/source',limits={'repair_attempts':2,'diagnosis_calls':2,
        'candidate_revisions':2,'tokens':100,'child_jobs':3,'observations':3,'cost':100},
        deadline=1100,safety_limits={'stop_calls':2,'query_calls':2,'cleanup_calls':2,'cost':10},
        safety_deadline=1150,cost_unit='test')
    return plan,work,record.key.lifecycle_id,token


@pytest.mark.asyncio
async def test_explicit_generation_is_frozen_and_cannot_reset_the_source(repository):
    from sandbox_graph.services.measurement_generation import MeasurementGenerationService
    plan,work,life,token=await source_ready(repository)
    service=MeasurementGenerationService(repository)
    frozen=service.compile(life,target_plan_ref=plan.hash,authorization_ref='measure/a',token=token)
    before=deepcopy(repository.rows[('runs','source')])
    budget=LifecycleBudgetService(repository).snapshot(life)
    with pytest.raises(ValidationError): Plan.model_validate(frozen.model_dump(mode='json'))
    with pytest.raises(PermissionError): service.request('request/a',frozen.hash,authorization_ref='missing',principal='operator',token=token)
    service.authorize('measure/a',frozen.hash,principal='operator',expires_at=1090,permissions=('measure',))
    result=service.request('request/a',frozen.hash,authorization_ref='measure/a',principal='operator',token=token)
    assert service.request('request/a',frozen.hash,authorization_ref='measure/a',principal='operator',token=token)==result
    assert result['run_id']!='source'
    with repository.transaction() as tx:
        target=tx.get('work_instances',result['work_id'])['body']
        source=tx.get('work_instances',work)['body']
        assert target['key']['measurement_generation']==2
        assert target['key']['condition_hash']==source['key']['condition_hash']
        assert target['condition']=={'Q':4}
        assert target['source_lifecycle_ref']==life
        assert tx.get('runs',result['run_id'])['body']['lineage_id']=='lineage/source'
        assert len([r for r in tx.scan('work_instances') if r['body']['run_id']==result['run_id']])==1
    assert repository.rows[('runs','source')]==before
    assert LifecycleBudgetService(repository).snapshot(life)==budget  # No dispatch yet.


async def authorized_target(repository):
    from sandbox_graph.services.measurement_generation import MeasurementGenerationService
    plan,work,life,token=await source_ready(repository)
    service=MeasurementGenerationService(repository)
    frozen=service.compile(life,target_plan_ref=plan.hash,authorization_ref='measure/a',token=token)
    service.authorize('measure/a',frozen.hash,principal='operator',expires_at=1090,permissions=('measure',))
    target=service.request('request/a',frozen.hash,authorization_ref='measure/a',principal='operator',token=token)
    return service,frozen,target,token


def target_handler(repository,seen,*,revoke=None,fail=None):
    from sandbox_graph.runtime.worker import GraphJobHandler
    from sandbox_graph.domain.models import DeliveryManifest
    async def action(context):
        seen.append(context.node_id)
        if context.node_id==revoke:
            from sandbox_graph.services.measurement_generation import MeasurementGenerationService
            MeasurementGenerationService(repository).revoke('measure/a')
        result=NodeResult(execution_status='failed' if context.node_id==fail else 'succeeded',
            data_status='insufficient' if context.node_id==fail else 'unverified',artifact_refs=('raw/generation2',))
        if context.node_id=='N10':
            manifest=DeliveryManifest(delivery_id=context.scope,version=1,scope=context.scope,
                plan_ref=context.plan_ref,contract_ref='contract/a',work_refs=(context.scope,),evidence=(),result=result)
            result=result.model_copy(update={'output':{'delivery_manifest':manifest.model_dump(mode='json')}})
        return result
    return GraphJobHandler(repository,build_work,lambda token:InMemorySaver(),
        lambda token,activation:{n:action for n in NODE_GROUPS['work']},lease_seconds=30,segment_seconds=10)


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke',[None,'before_start','N04'])
async def test_authorized_generation_uses_real_scheduler_worker_and_rechecks_authority(repository,revoke):
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    service,plan,target,token=await authorized_target(repository)
    ids=SchedulingService(repository).dispatch_work(target['run_id'],'group/a',token=token,max_concurrency=1,deadline=1080)
    assert len(ids)==1
    seen=[]
    if revoke=='before_start': service.revoke('measure/a')
    worker=Worker(repository,{'work':target_handler(repository,seen,revoke=revoke)},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    for _ in range(10):
        with repository.transaction() as tx:
            if tx.get('jobs',ids[0])['body']['status'] not in {'pending','running'}: break
        await worker.run_once()
    if revoke=='before_start': assert seen==[]
    elif revoke=='N04':
        assert 'N05' not in seen and 'N06' not in seen
        assert seen[-4:]==['N07','N08','N09','N10']
    else: assert seen==list(NODE_GROUPS['work'])
    budget=LifecycleBudgetService(repository).snapshot(plan.source_lifecycle_ref)
    assert budget['lifecycle']['spent']['child_jobs']==1
    assert budget['lineage']['spent']['child_jobs']==1
    assert budget['lifecycle']['spent']['observations']==0


@pytest.mark.asyncio
async def test_failed_generation_links_original_issue_without_new_lifecycle(repository):
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    service,plan,target,token=await authorized_target(repository)
    ids=SchedulingService(repository).dispatch_work(target['run_id'],'group/a',token=token,max_concurrency=1,deadline=1080)
    seen=[]
    worker=Worker(repository,{'work':target_handler(repository,seen,fail='N05')},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    for _ in range(10):
        with repository.transaction() as tx:
            if tx.get('jobs',ids[0])['body']['status'] not in {'pending','running'}: break
        await worker.run_once()
    with repository.transaction() as tx:
        links=tx.scan('generation_failure_links')
        assert len(links)==1
        assert links[0]['body']['source_lifecycle_ref']==plan.source_lifecycle_ref
        assert links[0]['body']['status']=='blocked_needs_decision'
        assert len(tx.scan('agent_lifecycles'))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['auth','run','node','lifecycle_run','lifecycle_plan','revision','source_receipt','work_key','condition','unknown_sampler'])
async def test_frozen_identity_or_unknown_site_cannot_dispatch(repository,mutation):
    from sandbox_graph.persistence.repository import Conflict
    service,plan,target,token=await authorized_target(repository)
    with repository.transaction() as tx:
        if mutation=='auth':
            row=tx.get('measurement_authorizations','measure/a'); tx.put('measurement_authorizations','measure/a',row['body']|{'permissions':['cleanup']})
        elif mutation=='run':
            row=tx.get('runs',target['run_id']); tx.put('runs',target['run_id'],row['body']|{'graph_ref':'wrong'})
        elif mutation=='node':
            row=tx.get('agent_lifecycles',plan.source_lifecycle_ref); body=row['body']; body['key']['node_id']='N09'; tx.put('agent_lifecycles',row['key'],body)
        elif mutation in {'lifecycle_run','lifecycle_plan'}:
            row=tx.get('agent_lifecycles',plan.source_lifecycle_ref); body=row['body']
            if mutation=='lifecycle_run': body['key']['run_ref']='wrong'
            else: body['plan_ref']='wrong'
            tx.put('agent_lifecycles',row['key'],body)
        elif mutation=='revision':
            row=tx.get('work_instances',plan.source_work_ref); tx.put('work_instances',row['key'],row['body'])
        elif mutation=='source_receipt':
            row=tx.get('receipts',plan.source_receipt_ref); body=row['body']; body['identity']['run_ref']='wrong'; tx.put('receipts',row['key'],body)
        elif mutation in {'work_key','condition'}:
            row=tx.get('work_instances',target['work_id']); body=row['body']
            if mutation=='work_key': body['key']['measurement_generation']=55
            else: body['condition']={'Q':1}
            tx.put('work_instances',row['key'],body)
        else:
            tx.put('observations','unknown',{'scope':plan.source_work_ref,'status':'stopped'})
    with pytest.raises((Conflict,PermissionError)):
        SchedulingService(repository).dispatch_work(target['run_id'],'group/a',token=token,max_concurrency=1,deadline=1080)
    with repository.transaction() as tx: assert not [r for r in tx.scan('jobs') if r['body']['run_id']==target['run_id']]


@pytest.mark.asyncio
async def test_request_rejects_stale_owner_replay_change_and_second_successor(repository):
    from sandbox_graph.persistence.repository import Conflict
    service,plan,target,token=await authorized_target(repository)
    with pytest.raises((Conflict,PermissionError)):
        service.request('request/a',plan.hash,authorization_ref='measure/a',principal='other',token=token)
    with pytest.raises(Conflict):
        service.request('request/b',plan.hash,authorization_ref='measure/a',principal='operator',token=token)
    with repository.transaction() as tx: tx.release_thread(token)
    with pytest.raises(Conflict):
        service.request('request/a',plan.hash,authorization_ref='measure/a',principal='operator',token=token)


@pytest.mark.asyncio
async def test_direct_revision_and_analysis_permission_cannot_authorize_measure(repository):
    from sandbox_graph.services.revisions import RevisionService
    from sandbox_graph.persistence.repository import Conflict
    service,plan,target,token=await authorized_target(repository)
    with pytest.raises(PermissionError):
        service.authorize('other',plan.hash,principal='operator',expires_at=1080,permissions=('analysis',))
    with pytest.raises(Conflict):
        RevisionService(repository).activate_revision(request_id='bypass',predecessor_run_id='source',expected_revision=0,
            candidate_plan_hash=plan.hash,graph_ref='graph/a',disposition_policy='drain')


def legacy_module(path,package):
    """Execute checked-in verbatim old production classes; no git/runtime snapshot dependency."""
    import hashlib,types
    name=path.rsplit('/',1)[1].removesuffix('.py')
    hashes={'scheduling':'5856ccb2ec90cbf064678729b262fc84a23425c4fc266084b2d4caa6dc502889',
            'worker':'a82335cb451616985ae875e9c82abff9fd2179a140940715419a2f61ad5332e0'}
    raw=(LEGACY.parent / f'legacy_{name}_2dfe8aee.py.txt').read_bytes()
    assert hashlib.sha256(raw).hexdigest()==hashes[name]
    module=types.ModuleType(package+'.legacy_generation_test'); module.__package__=package
    exec(compile(raw,path,'exec'),module.__dict__)
    return module


@pytest.mark.asyncio
async def test_actual_legacy_expansion_rejects_envelope_before_creating_work(repository):
    legacy=legacy_module('sandbox_graph/services/scheduling.py','sandbox_graph.services')
    service,plan,target,token=await authorized_target(repository)
    with repository.transaction() as tx: before=tx.scan('work_instances')
    with pytest.raises(ValidationError):
        legacy.SchedulingService(repository).expand_work(target['run_id'],'group/a',plan_ref=plan.hash)
    with repository.transaction() as tx: assert tx.scan('work_instances')==before


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['run','group','work','resume','graph:all'])
async def test_actual_legacy_worker_cannot_run_any_generation_entry(repository,kind):
    from sandbox_graph.runtime.tasks import TaskBackend,JobSpec
    legacy=legacy_module('sandbox_graph/runtime/worker.py','sandbox_graph.runtime')
    service,plan,target,token=await authorized_target(repository)
    called=[]
    async def forbidden(job):
        called.append(job); return {'execution_status':'succeeded'}
    # Remove unrelated source outbox events so this is exactly a legacy entry test.
    with repository.transaction() as tx:
        for row in tx.scan('outbox'): tx.put('outbox_delivery',row['key'],{'status':'delivered'},immutable=True)
    TaskBackend(repository).enqueue(JobSpec(job_id='legacy-test',job_kind=kind,scope_ref=target['work_id'],
        thread_id='legacy',activation_id='legacy',dedupe_key='legacy',due_at=1000,deadline=1080,
        run_id=target['run_id'],payload={'plan_ref':plan.hash,'graph_ref':'graph/a'}))
    worker=legacy.Worker(repository,{k:forbidden for k in ('run','group','work','resume')},
        policy=legacy.WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    await worker.run_once()
    assert called==[]
    with repository.transaction() as tx: assert tx.get('jobs','legacy-test')['body']['status']=='blocked'


@pytest.mark.asyncio
async def test_multi_case_target_expands_only_original_condition_and_other_group_continues(repository):
    from sandbox_graph.planning.compiler import PlanCompiler,Selection
    from sandbox_graph.services.measurement_generation import MeasurementGenerationService
    plan,work,life,token=await source_ready(repository)
    other=plan.cases[0].model_copy(update={'case_id':'case/b','group_id':'group/b','conflict_domains':('host/b',)})
    target_plan=PlanCompiler([plan.cases[0],other]).compile(Selection(),authorized_groups={'group/a','group/b'},graph_ref='graph/a',contract_ref='contract/a')
    with repository.transaction() as tx: tx.put('plans',target_plan.hash,target_plan.model_dump(mode='json'),immutable=True)
    service=MeasurementGenerationService(repository)
    extension=service.compile(life,target_plan_ref=target_plan.hash,authorization_ref='measure/a',token=token)
    service.authorize('measure/a',extension.hash,principal='operator',expires_at=1090,permissions=('measure',))
    target=service.request('one',extension.hash,authorization_ref='measure/a',principal='operator',token=token)
    scheduler=SchedulingService(repository)
    assert scheduler.expand_work(target['run_id'],'group/b',plan_ref=extension.hash)==[]
    with repository.transaction() as tx:
        run=RunRecord(run_id='independent',root_thread_id='independent',active_plan_ref=target_plan.hash,
            graph_ref='graph/a',contract_ref='contract/a',auth_ref=target_plan.auth_hash,lineage_id='independent')
        tx.put('runs',run.run_id,run.model_dump(mode='json'));tx.put('dispatch_gates',run.run_id,{'open':True,'generation':1})
    independent=scheduler.expand_work('independent','group/b',plan_ref=target_plan.hash)
    assert len(independent)==1
    service.revoke('measure/a')
    assert len(scheduler.dispatch_work('independent','group/b',token=token,max_concurrency=1,deadline=1080))==1


@pytest.mark.asyncio
async def test_budget_exhaustion_prevents_atomic_dispatch_without_new_local_allowance(repository):
    from sandbox_graph.persistence.repository import Conflict
    service,plan,target,token=await authorized_target(repository)
    with repository.transaction() as tx:
        for row in tx.scan('lifecycle_budgets'):
            body=row['body'];body['spent']['child_jobs']=body['limits']['child_jobs'];tx.put('lifecycle_budgets',row['key'],body)
    before=LifecycleBudgetService(repository).snapshot(plan.source_lifecycle_ref)
    with pytest.raises(Conflict,match='budget exhausted'):
        SchedulingService(repository).dispatch_work(target['run_id'],'group/a',token=token,max_concurrency=1,deadline=1080)
    assert LifecycleBudgetService(repository).snapshot(plan.source_lifecycle_ref)==before
    with repository.transaction() as tx:
        assert not tx.get('resource_leases','host/a')
        assert tx.get('work_instances',target['work_id'])['body']['status']=='pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('endpoint_ref','replacement'),('environment_ref','replacement')])
async def test_frozen_target_cannot_replace_resource_endpoint(repository,field,value):
    from sandbox_graph.services.measurement_generation import MeasurementGenerationService
    from sandbox_graph.planning.compiler import PlanCompiler,Selection
    from sandbox_graph.persistence.repository import Conflict
    plan,work,life,token=await source_ready(repository)
    changed=PlanCompiler([plan.cases[0].model_copy(update={field:value})]).compile(Selection(),
        authorized_groups={'group/a'},graph_ref='graph/a',contract_ref='contract/a')
    with repository.transaction() as tx: tx.put('plans',changed.hash,changed.model_dump(mode='json'),immutable=True)
    with pytest.raises(Conflict):
        MeasurementGenerationService(repository).compile(life,target_plan_ref=changed.hash,authorization_ref='measure/a',token=token)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['reconcile','cleanup'])
async def test_closed_generation_gate_still_queries_and_cleans_original_operation(repository,kind):
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy,RecoveryJobHandler
    from sandbox_graph.runtime.runner import OperationService,RunnerStatus,CleanupResult
    from sandbox_graph.runtime.tasks import TaskBackend,JobSpec
    service,plan,target,token=await authorized_target(repository)
    with repository.transaction() as tx:
        tx.put('dispatch_gates',target['run_id'],{'open':False,'generation':2})
        tx.put('operations','target/op',{'scope':target['work_id'],'run_id':target['run_id'],
            'thread_id':'prior','phase':'submission_started','request':{'node_id':'N05'}})
        for row in tx.scan('outbox'): tx.put('outbox_delivery',row['key'],{'status':'delivered'},immutable=True)
    class Gateway:
        async def reconcile(self,identity,scope):
            assert (identity,scope)==('target/op',target['work_id'])
            return RunnerStatus(state='running' if kind=='cleanup' else 'completed',authoritative=True)
        async def cancel(self,identity,scope,reason):
            assert (identity,scope)==('target/op',target['work_id'])
            return RunnerStatus(state='cancelled',authoritative=True)
        async def cleanup(self,scope,resources): return CleanupResult(status='complete',resource_ids=resources)
    handler=RecoveryJobHandler(repository,OperationService(repository,Gateway(),{}),lease_seconds=30)
    TaskBackend(repository).enqueue(JobSpec(job_id='safety',job_kind=kind,scope_ref=target['work_id'],
        thread_id='safety',activation_id='safety',dedupe_key='safety',due_at=1000,deadline=1080,
        run_id=target['run_id'],payload={'plan_ref':plan.hash,'graph_ref':'graph/a'}))
    service.revoke('measure/a')
    await Worker(repository,{kind:handler},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1)).run_once()
    with repository.transaction() as tx:
        assert tx.get('jobs','safety')['body']['status']=='complete'
        assert tx.get('operations','target/op')['body']['status']['state']==('cancelled' if kind=='cleanup' else 'completed')


@pytest.mark.asyncio
async def test_interrupted_original_activation_can_drain_after_measure_revocation(repository):
    import asyncio
    service,plan,target,token=await authorized_target(repository)
    seen=[]
    async def action(context):
        seen.append(context.node_id)
        if context.node_id=='N04': raise asyncio.CancelledError()
        return NodeResult(execution_status='succeeded')
    saver=InMemorySaver()
    driver=Driver(repository,{n:action for n in NODE_GROUPS['work']},lambda token:saver,build_work)
    activation=Activation(activation_id='interrupted',run_id=target['run_id'],thread_id='interrupted',
        scope=target['work_id'],plan_ref=plan.hash,graph_ref='graph/a',input_refs={},lease_seconds=30,segment_seconds=10)
    from langgraph.errors import NodeCancelledError
    with pytest.raises(NodeCancelledError): await driver.start(activation)
    service.revoke('measure/a')
    with repository.transaction() as tx: tx.put('dispatch_gates',target['run_id'],{'open':False,'generation':2})
    result=await driver.resume('interrupted')
    assert seen.count('N04')==1 and 'N05' not in seen and 'N06' not in seen
    assert seen[-4:]==['N07','N08','N09','N10']
    assert result['results']['N04']['execution_status']=='blocked'


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['missing_binding','wrong_plan','unknown_version'])
async def test_wire_prefix_never_grants_safety_effects_without_binding(repository,mutation):
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy,RecoveryJobHandler
    from sandbox_graph.runtime.runner import OperationService,RunnerStatus
    from sandbox_graph.runtime.tasks import TaskBackend,JobSpec
    service,plan,target,token=await authorized_target(repository)
    queried=[]
    class Gateway:
        async def reconcile(self,identity,scope):
            queried.append(identity); return RunnerStatus(state='completed',authoritative=True)
    with repository.transaction() as tx:
        tx.put('operations','target/op',{'scope':target['work_id'],'run_id':target['run_id'],
            'thread_id':'prior','phase':'submission_started','request':{'node_id':'N05'}})
        for row in tx.scan('outbox'): tx.put('outbox_delivery',row['key'],{'status':'delivered'},immutable=True)
    TaskBackend(repository).enqueue(JobSpec(job_id='safety',job_kind='reconcile',scope_ref=target['work_id'],
        thread_id='safety',activation_id='safety',dedupe_key='safety',due_at=1000,deadline=1080,
        run_id=target['run_id'],payload={'plan_ref':plan.hash,'graph_ref':'graph/a'}))
    with repository.transaction() as tx:
        if mutation=='missing_binding': tx.delete('run_generation_bindings',target['run_id'])
        else:
            row=tx.get('jobs','safety');body=row['body']
            if mutation=='wrong_plan': body['payload']['plan_ref']='wrong'
            else: body['job_kind']='measurement-v2:reconcile'
            tx.put('jobs','safety',body)
    handler=RecoveryJobHandler(repository,OperationService(repository,Gateway(),{}),lease_seconds=30)
    await Worker(repository,{'reconcile':handler},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1)).run_once()
    assert queried==[]


@pytest.mark.asyncio
async def test_concurrent_same_request_replays_one_successor(repository):
    from concurrent.futures import ThreadPoolExecutor
    from sandbox_graph.services.measurement_generation import MeasurementGenerationService
    plan,work,life,token=await source_ready(repository)
    service=MeasurementGenerationService(repository)
    frozen=service.compile(life,target_plan_ref=plan.hash,authorization_ref='measure/a',token=token)
    service.authorize('measure/a',frozen.hash,principal='operator',expires_at=1090,permissions=('measure',))
    def request(_):
        return service.request('concurrent',frozen.hash,authorization_ref='measure/a',principal='operator',token=token)
    with ThreadPoolExecutor(max_workers=3) as pool: replies=list(pool.map(request,range(3)))
    assert replies[0]==replies[1]==replies[2]
    with repository.transaction() as tx:
        assert len(tx.scan('measurement_successors'))==1
        assert len(tx.scan('run_generation_bindings'))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,status',[('reconcile','pending'),('reconcile','running'),('cleanup','draining'),('work','pending')])
async def test_known_versioned_inflight_jobs_prevent_archive_closure(repository,kind,status):
    from sandbox_graph.runtime.tasks import TaskBackend,JobSpec
    from sandbox_graph.services.closure import ClosureService
    from sandbox_graph.persistence.repository import Conflict
    _,plan,target,_=await authorized_target(repository)
    TaskBackend(repository).enqueue(JobSpec(job_id='inflight',job_kind=kind,scope_ref=target['work_id'],
        thread_id='inflight',activation_id='inflight',dedupe_key='inflight',due_at=1000,deadline=1080,
        run_id=target['run_id'],payload={'plan_ref':plan.hash,'graph_ref':'graph/a'}))
    with repository.transaction() as tx:
        row=tx.get('jobs','inflight');tx.put('jobs','inflight',row['body']|{'status':status})
    args=dict(expected_revision=0,analysis_ref='analysis',report_analysis_ref='analysis',excel_analysis_ref='analysis',
        scopes={target['work_id']:(NodeResult(execution_status='succeeded',data_status='valid',scope_disposition='fulfilled'), 'processed')},
        inflight=(),artifact_refs=())
    with pytest.raises(Conflict,match='in-flight job'):
        ClosureService(repository).seal_archive(target['run_id'],**args)
    # A globally owned work conflicts with a foreign run; it cannot hide the job.
    with repository.transaction() as tx:
        row=tx.get('jobs','inflight');tx.put('jobs','inflight',row['body']|{'run_id':'unrelated-run'})
    with pytest.raises(Conflict,match='ambiguous jobs'):
        ClosureService(repository).seal_archive(target['run_id'],**args)
    # Reusable group labels can legitimately belong to a different run.
    with repository.transaction() as tx:
        row=tx.get('jobs','inflight');tx.put('jobs','inflight',row['body']|{'scope_ref':plan.source_key.group_id})
    assert ClosureService(repository).seal_archive(target['run_id'],**args)['status']=='complete'


@pytest.mark.asyncio
async def test_known_versioned_pending_report_is_superseded_by_new_analysis(repository):
    from sandbox_graph.runtime.tasks import TaskBackend,JobSpec
    from sandbox_graph.services.reviews import ReviewService
    _,plan,target,_=await authorized_target(repository)
    with repository.transaction() as tx:
        tx.put('analysis_results','old-analysis',{'plan_ref':plan.hash,'analysis_input_ref':'old-input'})
    for identity,kind,status in [('stale','report','pending'),('running','report','running'),('unknown','measurement-v2:report','pending')]:
        TaskBackend(repository).enqueue(JobSpec(job_id=identity,job_kind=kind,scope_ref=target['run_id'],
            thread_id=identity,activation_id=identity,dedupe_key=identity,due_at=1000,deadline=1080,run_id=target['run_id'],
            payload={'plan_ref':plan.hash,'graph_ref':'graph/a','analysis_ref':'old-analysis'}))
        with repository.transaction() as tx:
            row=tx.get('jobs',identity);tx.put('jobs',identity,row['body']|{'status':status})
    assert ReviewService(repository,None).supersede_stale_reports(plan.hash,'new-input')==['stale']
    with repository.transaction() as tx:
        assert tx.get('jobs','running')['body']['status']=='running'
        assert tx.get('jobs','unknown')['body']['status']=='pending'


async def failed_child_ready(repository):
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    service,plan,target,token=await authorized_target(repository)
    ids=SchedulingService(repository).dispatch_work(target['run_id'],'group/a',token=token,max_concurrency=1,deadline=1080)
    worker=Worker(repository,{'work':target_handler(repository,[],fail='N05')},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    for _ in range(10):
        with repository.transaction() as tx:
            if tx.get('jobs',ids[0])['body']['status'] not in {'pending','running'}: break
        await worker.run_once()
    worker.pump_outbox()
    with repository.transaction() as tx:
        links=[r for r in tx.scan('generation_failure_links') if r['body']['work_ref']==target['work_id']]
        assert len(links)==1
        LifecycleSafetyService.acquire_work_in(tx,target['work_id'],token,purpose='diagnosis')
    return service,plan,target,token,links[0]['key']


@pytest.mark.asyncio
async def test_explicit_failed_child_authorization_dispatches_generation_three_without_new_budget(repository):
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    service,parent,child,token,failure=await failed_child_ready(repository)
    before=LifecycleBudgetService(repository).snapshot(parent.source_lifecycle_ref)
    with repository.transaction() as tx:
        historical=deepcopy(tx.get('receipts',failure)); original=deepcopy(tx.get('agent_lifecycles',parent.source_lifecycle_ref))
    plan=service.compile(parent.source_lifecycle_ref,target_plan_ref=parent.target_plan_hash,
        authorization_ref='measure/g3',source_failure_ref=failure,token=token)
    assert plan.measurement_generation==3 and plan.source_work_ref==child['work_id']
    service.authorize('measure/g3',plan.hash,principal='operator',expires_at=1090,permissions=('measure',))
    target=service.request('generation-three',plan.hash,authorization_ref='measure/g3',principal='operator',token=token)
    assert service.request('generation-three',plan.hash,authorization_ref='measure/g3',principal='operator',token=token)==target
    assert LifecycleBudgetService(repository).snapshot(parent.source_lifecycle_ref)==before
    ids=SchedulingService(repository).dispatch_work(target['run_id'],'group/a',token=token,max_concurrency=1,deadline=1080)
    seen=[]
    worker=Worker(repository,{'work':target_handler(repository,seen)},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    for _ in range(10):
        with repository.transaction() as tx:
            if tx.get('jobs',ids[0])['body']['status'] not in {'pending','running'}: break
        await worker.run_once()
    assert seen==list(NODE_GROUPS['work'])
    with repository.transaction() as tx:
        assert len(tx.scan('agent_lifecycles'))==1
        assert tx.get('agent_lifecycles',parent.source_lifecycle_ref)==original
        assert tx.get('receipts',failure)==historical
        assert tx.get('work_instances',target['work_id'])['body']['key']['measurement_generation']==3
        assert tx.get('runs',target['run_id'])['body']['lineage_id']=='lineage/source'
        assert tx.get('measurement_successors',child['work_id'])['body']['work_id']==target['work_id']
    after=LifecycleBudgetService(repository).snapshot(parent.source_lifecycle_ref)
    for scope in ('lifecycle','lineage'):
        assert after[scope]['spent']['child_jobs']==before[scope]['spent']['child_jobs']+1==2
        assert after[scope]['limits']==before[scope]['limits']
        assert after[scope]['deadline']==before[scope]['deadline']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['missing','forged_node','cross_source','wrong_lifecycle','ancestor','changed_after_compile','unknown_site','missing_child_owner'])
async def test_failed_child_source_requires_exact_link_ancestry_and_closed_owned_site(repository,mutation):
    from sandbox_graph.persistence.repository import Conflict
    service,parent,child,token,failure=await failed_child_ready(repository)
    if mutation in {'changed_after_compile','unknown_site','missing_child_owner'}:
        plan=service.compile(parent.source_lifecycle_ref,target_plan_ref=parent.target_plan_hash,
            authorization_ref='measure/g3',source_failure_ref=failure,token=token)
        service.authorize('measure/g3',plan.hash,principal='operator',expires_at=1090,permissions=('measure',))
    with repository.transaction() as tx:
        row=tx.get('generation_failure_links',failure);body=row['body']
        if mutation=='missing': tx.delete('generation_failure_links',failure)
        elif mutation=='forged_node': tx.put('generation_failure_links',failure,body|{'node_id':'N06'})
        elif mutation=='cross_source': tx.put('generation_failure_links',failure,body|{'work_ref':parent.source_work_ref})
        elif mutation=='wrong_lifecycle': tx.put('generation_failure_links',failure,body|{'source_lifecycle_ref':'different-root'})
        elif mutation=='ancestor':
            row=tx.get('measurement_successors',parent.source_work_ref)
            tx.put('measurement_successors',row['key'],row['body']|{'work_id':'another-child'})
        elif mutation=='changed_after_compile': tx.put('generation_failure_links',failure,body|{'unexpected':'changed'})
        elif mutation=='unknown_site': tx.put('observations','child/unknown',{'scope':child['work_id'],'status':'stopped'})
        else: LifecycleSafetyService.release_work_in(tx,child['work_id'],token)
    with pytest.raises((Conflict,PermissionError)):
        if mutation in {'changed_after_compile','unknown_site','missing_child_owner'}:
            service.request('generation-three',plan.hash,authorization_ref='measure/g3',principal='operator',token=token)
        else:
            service.compile(parent.source_lifecycle_ref,target_plan_ref=parent.target_plan_hash,
                authorization_ref='measure/g3',source_failure_ref=failure,token=token)
    with repository.transaction() as tx:
        assert not tx.get('measurement_successors',child['work_id'])
        assert len(tx.scan('agent_lifecycles'))==1


@pytest.mark.asyncio
async def test_failed_child_requires_fresh_exact_grant_and_dedupes_immediate_source(repository):
    from sandbox_graph.persistence.repository import Conflict
    service,parent,child,token,failure=await failed_child_ready(repository)
    plan=service.compile(parent.source_lifecycle_ref,target_plan_ref=parent.target_plan_hash,
        authorization_ref='measure/g3',source_failure_ref=failure,token=token)
    for grant in ('measure/a','measure/g3'):
        with pytest.raises(PermissionError):
            service.request('generation-three',plan.hash,authorization_ref=grant,principal='operator',token=token)
    service.authorize('measure/g3',plan.hash,principal='operator',expires_at=1090,permissions=('measure',))
    service.request('generation-three',plan.hash,authorization_ref='measure/g3',principal='operator',token=token)
    with pytest.raises(Conflict,match='already has a successor'):
        service.request('another',plan.hash,authorization_ref='measure/g3',principal='operator',token=token)
    budget=LifecycleBudgetService(repository).snapshot(parent.source_lifecycle_ref)
    assert budget['lifecycle']['spent']['child_jobs']==1
    assert budget['lineage']['spent']['child_jobs']==1
