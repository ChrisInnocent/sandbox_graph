"""Durable dispatcher boundaries; synthetic repository, no backend acceptance."""
from copy import deepcopy
import pytest

from sandbox_graph.runtime.worker import Worker, WorkerPolicy
from tests.orchestration.graphs.test_work_remediation import assembly, drain


def test_missing_consumer_retains_modern_intent_with_recovery_entry(repository):
    with repository.transaction() as tx:
        tx.put('outbox','repair/unknown',{'kind':'repair_reconciliation_required',
            'lifecycle_id':'missing','operation_refs':['original/op']},immutable=True)
    worker=Worker(repository,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    worker.pump_outbox()
    with repository.transaction() as tx:
        assert not tx.get('outbox_delivery','repair/unknown')
        error=tx.get('outbox_errors','repair/unknown')['body']
        assert error['status']=='waiting_dependency' and error['recovery_entry']


@pytest.mark.asyncio
async def test_healthy_graph_produces_zero_lifecycle_and_diagnosis_jobs(repository):
    worker, _, _, _=assembly(repository)
    with repository.transaction() as tx:
        row=tx.get('jobs','original/job')
        tx.put('jobs',row['key'],row['body']|{'activation_id':'healthy/a'})
    await drain(worker)
    with repository.transaction() as tx:
        assert not tx.scan('agent_lifecycles')
        assert not [r for r in tx.scan('jobs') if r['body']['job_kind'] in {'lifecycle','supervision','remedy'}]


@pytest.mark.asyncio
async def test_unavailable_node_has_finite_waiting_decision_without_model(repository):
    worker, _, _, repairs=assembly(repository,failure='N08')
    await drain(worker)
    with repository.transaction() as tx:
        assert not tx.scan('repair_orders')
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='waiting_decision'
        before=len(tx.scan('jobs'))
        assert tx.scan('lifecycle_blockers')[0]['body']['status']=='waiting_decision'
    await drain(worker)
    with repository.transaction() as tx:
        assert len(tx.scan('jobs'))==before
    assert not repairs


@pytest.mark.asyncio
async def test_exhaustion_stops_without_a_third_repair(repository,clock):
    worker, _, _, repairs=assembly(repository,repair_failures=10)
    await drain(worker)
    clock.advance(1.0)
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='blocked'
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['repair_attempts']==2
        assert tx.scan('lifecycle_blockers')[0]['body']['reason']=='budget_exhausted'
    clock.advance(10.0)
    await drain(worker)
    assert len(repairs)==2


@pytest.mark.asyncio
async def test_preclosure_authorization_is_rejected_then_operator_can_resume_exact_intent(repository):
    worker, supervisor, _, repairs=assembly(repository,auth_revision=0)
    await drain(worker)
    with repository.transaction() as tx:
        assert not tx.scan('repair_orders')
        assert tx.get('work_instances','work/a')['revision']==1
        assert tx.get('repair_authorizations','auth/runtime')['body']['work_revision']==0
        blocker=tx.scan('lifecycle_blockers')[0]['body']
        assert 'authorization' in blocker['reason']
    with pytest.raises(PermissionError):
        supervisor.resume_intent(blocker['intent_ref'], request_id='resume/denied',
            principal='operator/a',authorization_ref='auth/runtime')
    # This is an explicit operator authorization update after actual completion,
    # not a scheduler rewrite. Permissions and authority remain operator inputs.
    with repository.transaction() as tx:
        row=tx.get('repair_authorizations','auth/runtime')
        current=tx.get('work_instances','work/a')
        tx.put('repair_authorizations',row['key'],row['body']|{'work_revision':current['revision']})
    first=supervisor.resume_intent(blocker['intent_ref'],request_id='resume/authorized',
        principal='operator/a',authorization_ref='auth/runtime')
    assert supervisor.resume_intent(blocker['intent_ref'],request_id='resume/authorized',
        principal='operator/a',authorization_ref='auth/runtime')==first
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='fulfilled'
        assert len(tx.scan('repair_orders'))==1
        assert not tx.scan('lifecycle_blockers')
    assert len(repairs)==1


@pytest.mark.asyncio
async def test_deadline_watchdog_exposes_expired_dispatch_instead_of_silent_completion(repository,clock):
    worker, _, _, repairs=assembly(repository)
    await worker.run_once()
    worker.pump_outbox()
    worker.pump_outbox()
    clock.advance(101.0)
    await drain(worker)
    with repository.transaction() as tx:
        assert not tx.scan('repair_orders')
        blocker=tx.scan('lifecycle_blockers')[0]['body']
        assert blocker['status']=='blocked'
        assert blocker['reason']=='lifecycle_job_deadline_or_attempt_budget'
        assert blocker['resume_entry']
        bounds=tx.get('lifecycle_budgets','lineage:lineage/a')['body']
        assert bounds['spent']['child_jobs']==1 and bounds['reserved']['child_jobs']==0
    assert not repairs


@pytest.mark.asyncio
async def test_unknown_without_operation_binding_preserves_isolation_without_fabricating_id(repository):
    worker, _, _, repairs=assembly(repository,repair_unknown=True,no_operations=True)
    await drain(worker)
    with repository.transaction() as tx:
        assert not tx.scan('operations')
        assert tx.scan('repair_isolations')[0]['body']['operation_refs']==[]
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='remediating'
        blocker=tx.scan('lifecycle_blockers')[0]['body']
        assert blocker['status']=='waiting_dependency' and blocker['operation_refs']==[]
        assert 'operation_binding_missing' in blocker['reason']
        assert not tx.get('outbox_delivery',blocker['intent_ref'])
    await drain(worker)
    assert len(repairs)==1


@pytest.mark.asyncio
async def test_closed_gate_never_authorizes_a_queued_ordinary_repair(repository):
    worker, _, _, repairs=assembly(repository)
    await worker.run_once()
    worker.pump_outbox()
    worker.pump_outbox()
    with repository.transaction() as tx:
        tx.put('dispatch_gates','run/a',{'open':False,'generation':3})
    await drain(worker)
    with repository.transaction() as tx:
        assert not tx.scan('repair_orders')
        assert tx.scan('lifecycle_blockers')
    assert not repairs


@pytest.mark.asyncio
async def test_outbox_cut_rolls_back_supervision_job_and_its_dual_budget(repository,monkeypatch):
    from tests.orchestration.conftest import MemoryTransaction
    worker, _, _, repairs=assembly(repository)
    await worker.run_once()
    worker.pump_outbox()  # publish closure and its final work revision first
    original=MemoryTransaction.put
    def crash(tx,kind,key,body,*args,**kwargs):
        if kind=='outbox' and body.get('kind')=='job_ready' and body['job_id'].startswith('lifecycle-job:'):
            raise RuntimeError('cut while queuing supervisor')
        return original(tx,kind,key,body,*args,**kwargs)
    monkeypatch.setattr(MemoryTransaction,'put',crash)
    with pytest.raises(RuntimeError,match='cut while queuing supervisor'):
        worker.pump_outbox()
    with repository.transaction() as tx:
        assert not tx.scan('lifecycle_budgets')
        assert not tx.scan('budget_reservations')
        assert not [r for r in tx.scan('jobs') if r['body']['job_kind']=='lifecycle']
        pending=next(r for r in tx.scan('outbox') if r['body']['kind']=='agent_supervision')
        assert not tx.get('outbox_delivery',pending['key'])
    monkeypatch.setattr(MemoryTransaction,'put',original)
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='fulfilled'
    assert len(repairs)==1


@pytest.mark.asyncio
async def test_recovery_plan_expiry_after_queue_cut_never_extends_proof_or_reexecutes_repair(repository,clock,monkeypatch):
    class Cut(BaseException):
        pass
    worker,supervisor,_,repairs=assembly(repository)
    original=supervisor._recover
    def crash(*args):
        original(*args)
        raise Cut()
    monkeypatch.setattr(supervisor,'_recover',crash)
    with pytest.raises(Cut):
        await drain(worker)
    monkeypatch.setattr(supervisor,'_recover',original)
    clock.advance(31.0)
    await drain(worker)
    with repository.transaction() as tx:
        assert len(tx.scan('recovery_plans'))==1
        assert len(tx.scan('repair_orders'))==1
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='reentering'
        assert tx.scan('lifecycle_blockers')
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['repair_attempts']==1
    assert len(repairs)==1


async def completed_with_legacy_consumer(repository,**kwargs):
    worker,supervisor,seen,repairs=assembly(repository,**kwargs)
    worker.lifecycle_supervisor=None
    del worker.handlers['lifecycle']
    await drain(worker)
    with repository.transaction() as tx:
        source=next(r for r in tx.scan('receipts')
                    if r['body']['identity']['activation_id']=='original/a'
                    and r['body']['identity']['node_id']=='N01')
        assert tx.get('outbox_delivery','agent-supervision:' + source['key'])
        assert tx.get('event_ids','completion:original/job')
        assert not tx.scan('agent_lifecycles')
    worker.handlers['lifecycle']=supervisor
    worker.lifecycle_supervisor=supervisor
    command={'request_id':'attach/a','binding_ref':'binding/a','run_ref':'run/a',
        'work_ref':'work/a','node_id':'N01','original_job_id':'original/job',
        'source_receipt':source['key'],'expected_work_revision':1,
        'principal':'operator/a','authorization_ref':'auth/runtime'}
    return worker,supervisor,repairs,command


@pytest.mark.asyncio
async def test_late_attach_after_legacy_delivery_queues_once_and_preserves_original_records(repository):
    worker,supervisor,repairs,command=await completed_with_legacy_consumer(repository,child_job_limit=6)
    with repository.transaction() as tx:
        original_receipt=tx.get('receipts',command['source_receipt'])
        original_review=tx.get('node_reviews',command['source_receipt'])
        delivery=tx.get('outbox_delivery','agent-supervision:' + command['source_receipt'])
    result=supervisor.attach_failure(**command)
    assert supervisor.attach_failure(**command)==result
    from sandbox_graph.persistence.repository import Conflict
    with pytest.raises(Conflict,match='request ID'):
        supervisor.attach_failure(**(command|{'expected_work_revision':0}))
    # A second explicit request for the same source still represents one
    # diagnosis; both durable intents may be consumed by different Workers.
    second=supervisor.attach_failure(**(command|{'request_id':'attach/b'}))
    assert second['lifecycle_id']==result['lifecycle_id']
    await drain(worker)
    assert supervisor.attach_failure(**command)==result  # historical request replay after recovery revision
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='fulfilled'
        assert len(tx.scan('repair_orders'))==1
        assert len(tx.scan('lifecycle_attachments'))==2
        assert tx.get('outbox_delivery',result['intent_ref'])
        assert tx.get('receipts',command['source_receipt'])==original_receipt
        assert tx.get('node_reviews',command['source_receipt'])==original_review
        assert tx.get('outbox_delivery','agent-supervision:' + command['source_receipt'])==delivery
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['child_jobs']==6
    assert len(repairs)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('patch',[
    {'work_ref':'work/other'}, {'run_ref':'run/other'}, {'node_id':'N09'},
    {'binding_ref':'binding/other'}, {'expected_work_revision':0},
    {'principal':'other'}, {'authorization_ref':'auth/other'},
    {'original_job_id':'missing'}, {'source_receipt':'missing'},
])
async def test_attach_rejects_wrong_scope_version_owner_or_source_before_creating_intent(repository,patch):
    from sandbox_graph.persistence.repository import Conflict
    worker,supervisor,repairs,command=await completed_with_legacy_consumer(repository)
    with pytest.raises((Conflict,PermissionError)):
        supervisor.attach_failure(**(command|patch))
    with repository.transaction() as tx:
        assert not tx.scan('lifecycle_attachments')
        assert not tx.scan('lifecycle_budgets')
        assert not [r for r in tx.scan('outbox') if r['body']['kind']=='lifecycle_attach_requested']
    assert not repairs


@pytest.mark.asyncio
async def test_attach_rejects_a_previous_attempt_even_with_current_work_revision(repository):
    from sandbox_graph.persistence.repository import Conflict
    from sandbox_graph.runtime.tasks import TaskBackend,JobSpec
    worker,supervisor,repairs,command=await completed_with_legacy_consumer(repository)
    worker.lifecycle_supervisor=None
    del worker.handlers['lifecycle']
    TaskBackend(repository).enqueue(JobSpec(job_id='newer/job',job_kind='work',scope_ref='work/a',
        thread_id='newer/thread',activation_id='newer/a',dedupe_key='newer/job',
        due_at=1000.0,deadline=1100.0,run_id='run/a',dispatch_generation=3,
        payload={'plan_ref':'plan/a','graph_ref':'graph/a','fixture':'fixture/a'}))
    await drain(worker)
    with repository.transaction() as tx:
        current=tx.get('work_instances','work/a')
        row=tx.get('repair_authorizations','auth/runtime')
        tx.put('repair_authorizations',row['key'],row['body']|{'work_revision':current['revision']})
    with pytest.raises(Conflict,match='attempt|completion'):
        supervisor.attach_failure(**(command|{'expected_work_revision':current['revision']}))
    with repository.transaction() as tx:
        assert not tx.scan('lifecycle_attachments') and not tx.scan('agent_lifecycles')
    assert not repairs


def test_same_work_proof_binds_distinct_lifecycles_without_rewriting_legacy_evidence(repository):
    from sandbox_graph.runtime.lifecycle_supervisor import LifecycleSupervisor
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    from tests.orchestration.unit.test_remediation_requests import ready
    _,request,token=ready(repository)
    service=LifecycleService(repository)
    with repository.transaction() as tx:
        source=tx.get('receipts','receipt/a')['body']
        tx.put('receipts','receipt/b',source,immutable=True)
    second=service.observe_failure('receipt/b',policy_ref='policy/a',token=token)
    with repository.transaction() as tx:
        first=service.load_in(tx,request.lifecycle_id)
        ref=LifecycleSupervisor._proof_in(tx,first,token,30)
        legacy={'lifecycle_id':first.key.lifecycle_id,'kind':'resource_disposition'}
        tx.put('lifecycle_evidence',ref,legacy,immutable=True)
        frozen=deepcopy(tx.get('lifecycle_evidence',ref))
        assert LifecycleSupervisor._proof_in(tx,second,token,30)==ref
        service._evidence_in(tx,first,[ref])
        service._evidence_in(tx,second,[ref])
        assert tx.get('lifecycle_evidence',ref)==frozen
        for record in (first,second):
            assert tx.get('lifecycle_evidence_bindings',service.evidence_binding_key(record.key.lifecycle_id,ref))


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['revoked','permission','auth_revision','source_receipt','work_revision','gate'])
async def test_attachment_consumer_revalidates_frozen_authority_and_source_before_queue(repository,change):
    worker,supervisor,repairs,command=await completed_with_legacy_consumer(repository)
    result=supervisor.attach_failure(**command)
    with repository.transaction() as tx:
        if change in {'revoked','permission','auth_revision'}:
            row=tx.get('repair_authorizations','auth/runtime')
            patch={'revoked':True} if change=='revoked' else {'permissions':[]} if change=='permission' else {}
        elif change=='source_receipt':
            row=tx.get('receipts',command['source_receipt'])
            patch={'result':row['body']['result']|{'output':{'changed':'after attach'}}}
        elif change=='work_revision':
            row=tx.get('work_instances','work/a');patch={}
        else:
            row=tx.get('dispatch_gates','run/a');patch={'open':False}
        kind=('repair_authorizations' if change in {'revoked','permission','auth_revision'}
              else 'receipts' if change=='source_receipt'
              else 'work_instances' if change=='work_revision' else 'dispatch_gates')
        tx.put(kind,row['key'],row['body']|patch)
    await drain(worker)
    with repository.transaction() as tx:
        assert not tx.scan('agent_lifecycles') and not tx.scan('repair_orders')
        assert not tx.scan('lifecycle_budgets')
        assert not tx.get('outbox_delivery',result['intent_ref'])
        assert tx.get('lifecycle_intent_results',result['intent_ref'])['body']['reason']
    assert not repairs


@pytest.mark.asyncio
async def test_attachment_request_and_observation_intent_commit_atomically(repository,monkeypatch):
    from tests.orchestration.conftest import MemoryTransaction
    worker,supervisor,repairs,command=await completed_with_legacy_consumer(repository)
    original=MemoryTransaction.put
    def crash(tx,kind,key,body,*args,**kwargs):
        if kind=='outbox' and body.get('kind')=='lifecycle_attach_requested':
            raise RuntimeError('cut after attachment request')
        return original(tx,kind,key,body,*args,**kwargs)
    monkeypatch.setattr(MemoryTransaction,'put',crash)
    with pytest.raises(RuntimeError,match='cut after attachment request'):
        supervisor.attach_failure(**command)
    with repository.transaction() as tx:
        assert not tx.scan('lifecycle_attachments')
        assert not [r for r in tx.scan('outbox') if r['body']['kind']=='lifecycle_attach_requested']
    monkeypatch.setattr(MemoryTransaction,'put',original)
    supervisor.attach_failure(**command)
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='fulfilled'
    assert len(repairs)==1


async def failed_recovery_attachment(repository):
    worker,supervisor,seen,repairs=assembly(repository,repair_attempt_limit=1,recovery_failure=True)
    await drain(worker)
    with repository.transaction() as tx:
        life=tx.scan('agent_lifecycles')[0]['body']
        assert life['phase']=='draining'
        recovery=next(r['body'] for r in tx.scan('jobs')
                      if r['body']['job_kind']=='work' and r['body']['payload'].get('recovery_plan_ref'))
        ref=recovery['result']['graph_result']['result_refs']['N01']
        assert tx.get('receipts',ref)['body']['result']['execution_status']=='failed'
        work=tx.get('work_instances','work/a')
        auth=tx.get('repair_authorizations','auth/runtime')
        # An explicit operator revision authorization is not a new local budget grant.
        tx.put('repair_authorizations',auth['key'],auth['body']|{'work_revision':work['revision']})
        command={'request_id':'attach/recovery','binding_ref':'binding/a','run_ref':'run/a',
            'work_ref':'work/a','node_id':'N01','original_job_id':recovery['job_id'],
            'source_receipt':ref,'expected_work_revision':work['revision'],
            'principal':'operator/a','authorization_ref':'auth/runtime'}
    return worker,supervisor,repairs,command,life['key']['lifecycle_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('strip_markers',['none','job_payload','job_and_activation'])
async def test_attach_recovery_child_preserves_exhausted_local_budget_and_reports_original_owner(repository,strip_markers):
    from sandbox_graph.persistence.repository import Conflict
    worker,supervisor,repairs,command,original_life=await failed_recovery_attachment(repository)
    if strip_markers!='none':
        with repository.transaction() as tx:
            row=tx.get('jobs',command['original_job_id'])
            payload=dict(row['body']['payload']);payload.pop('recovery_plan_ref')
            tx.put('jobs',row['key'],row['body']|{'payload':payload})
            if strip_markers=='job_and_activation':
                activation=tx.get('activations',row['body']['activation_id'])
                record=dict(activation['body']['record']);record.pop('recovery_plan_ref')
                graph=dict(activation['body']['result']);graph.pop('recovery_plan_ref',None)
                tx.put('activations',activation['key'],activation['body']|{'record':record,'result':graph})
    with repository.transaction() as tx:
        protected={kind:tx.scan(kind) for kind in ('agent_lifecycles','lifecycle_budgets','repair_orders',
            'repair_attempt_counters','repair_failure_fingerprints','lifecycle_blockers',
            'receipts','node_reviews','outbox','outbox_delivery')}
    result=supervisor.attach_failure(**command)
    assert result['status']=='rejected'
    assert result['reason']=='lifecycle_owned_recovery_attempt'
    assert result['lifecycle_id']==original_life
    assert result['source_receipt']!=command['source_receipt']
    assert result['attempted_source_receipt']==command['source_receipt']
    assert result['recovery_plan_ref'].startswith('recovery:')
    assert result['recovery_entry'] and result['resume_entry']
    assert supervisor.attach_failure(**command)==result
    assert supervisor.attach_failure(**(command|{'request_id':'attach/recovery/again'}))==result
    with pytest.raises(Conflict,match='request ID'):
        supervisor.attach_failure(**(command|{'source_receipt':'other'}))
    await drain(worker)
    with repository.transaction() as tx:
        for kind,rows in protected.items():
            assert tx.scan(kind)==rows
        assert not tx.scan('lifecycle_attachments')
        assert len(tx.scan('lifecycle_attachment_rejections'))==2
        local=tx.get('lifecycle_budgets','lifecycle:' + original_life)['body']
        assert local['limits']['repair_attempts']==local['spent']['repair_attempts']==1
        lineage=tx.get('lifecycle_budgets','lineage:lineage/a')['body']
        assert lineage['limits']['repair_attempts']==2 and lineage['spent']['repair_attempts']==1
    assert len(repairs)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('broken_assignment',[False,True])
async def test_attachment_consumer_blocks_legacy_accepted_recovery_child_without_allocating(repository,broken_assignment):
    from sandbox_graph.domain.models import canonical_hash
    worker,supervisor,repairs,command,original_life=await failed_recovery_attachment(repository)
    # Simulate a pending accepted command written by the reviewed Round1 version;
    # all job/receipt/RecoveryPlan facts still come from the real executed chain.
    with repository.transaction() as tx:
        frozen_command={'schema_version':1,**{k:v for k,v in command.items() if k!='request_id'}}
        child_life='lifecycle:' + canonical_hash({'source_receipt':command['source_receipt']})
        intent_ref='lifecycle-attach:' + canonical_hash({'request_id':command['request_id']})
        saved={'command':frozen_command,'result':{'status':'accepted','lifecycle_id':child_life,'intent_ref':intent_ref},
            'binding_hash':canonical_hash(next(iter(supervisor.bindings.values()))),
            'authorization_revision':tx.get('repair_authorizations','auth/runtime')['revision'],
            'source_job_hash':canonical_hash(tx.get('jobs',command['original_job_id'])),
            'source_receipt_hash':canonical_hash(tx.get('receipts',command['source_receipt']))}
        tx.put('lifecycle_attachments',command['request_id'],saved,immutable=True)
        tx.put('outbox',intent_ref,{'kind':'lifecycle_attach_requested','lifecycle_id':child_life,
            'request_ref':command['request_id']},immutable=True)
        if broken_assignment:
            plan=tx.get('jobs',command['original_job_id'])['body']['payload']['recovery_plan_ref']
            assigned=tx.get('recovery_plan_activations',plan)
            tx.put('recovery_plan_activations',plan,assigned['body']|{'thread_id':'wrong/thread'})
        frozen_request=tx.get('lifecycle_attachments',command['request_id'])
        frozen_intent=tx.get('outbox',intent_ref)
        budgets=tx.scan('lifecycle_budgets')
    await drain(worker)
    with repository.transaction() as tx:
        assert len(tx.scan('agent_lifecycles'))==len(tx.scan('repair_orders'))==1
        assert tx.scan('lifecycle_budgets')==budgets
        assert tx.get('lifecycle_attachments',command['request_id'])==frozen_request
        assert tx.get('outbox',intent_ref)==frozen_intent
        assert not tx.get('outbox_delivery',intent_ref)
        refusal=tx.get('lifecycle_intent_results',intent_ref)['body']
        if broken_assignment:
            assert refusal['status']=='blocked' and 'plan ownership invalid' in refusal['reason']
        else:
            assert refusal['lifecycle_id']==original_life and refusal['status']=='rejected'
    if broken_assignment:
        from sandbox_graph.persistence.repository import Conflict
        with pytest.raises(Conflict):
            supervisor.attach_failure(**command)
    else:
        assert supervisor.attach_failure(**command)['lifecycle_id']==original_life
    assert len(repairs)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('broken_link',['job_receipt','activation_job','plan_assignment','plan_hash'])
async def test_recovery_attachment_requires_actual_job_activation_plan_identity(repository,broken_link):
    from sandbox_graph.persistence.repository import Conflict
    worker,supervisor,repairs,command,original_life=await failed_recovery_attachment(repository)
    with repository.transaction() as tx:
        job=tx.get('jobs',command['original_job_id'])['body']
        ref=job['payload']['recovery_plan_ref']
        if broken_link=='job_receipt':
            command['original_job_id']='original/job'  # child receipt cannot identify this earlier job
        elif broken_link=='activation_job':
            row=tx.get('activation_jobs',job['activation_id'])
            tx.put('activation_jobs',row['key'],row['body']|{'job_id':'original/job'})
        elif broken_link=='plan_assignment':
            row=tx.get('recovery_plan_activations',ref)
            tx.put('recovery_plan_activations',ref,row['body']|{'thread_id':'another/thread'})
        else:
            row=tx.get('recovery_contexts',ref)
            tx.put('recovery_contexts',ref,row['body']|{'source_receipt':'replacement/source'})
        budgets=tx.scan('lifecycle_budgets')
    with pytest.raises(Conflict):
        supervisor.attach_failure(**command)
    with repository.transaction() as tx:
        assert len(tx.scan('agent_lifecycles'))==len(tx.scan('repair_orders'))==1
        assert tx.scan('lifecycle_budgets')==budgets
        assert not tx.scan('lifecycle_attachments')
    assert len(repairs)==1


@pytest.mark.asyncio
async def test_unrelated_work_can_attach_its_own_first_failure_while_other_lifecycle_is_exhausted(repository):
    from sandbox_graph.domain.models import NodeResult,ErrorRecord,DeliveryManifest
    from sandbox_graph.runtime.tasks import TaskBackend,JobSpec
    from sandbox_graph.runtime.lifecycle_supervisor import LifecycleSupervisor
    from sandbox_graph.catalog.registry import NODE_GROUPS
    worker,supervisor,repairs,_,original_life=await failed_recovery_attachment(repository)
    graph=worker.handlers['work'];remedy=worker.handlers['remedy']
    prior_graph=graph.handler_factory;prior_remedy=remedy.node_service_factory
    prior_delivery=graph.delivery_finalizer
    async def second_action(context):
        if context.node_id=='N01':
            with repository.transaction() as tx:
                if not tx.get('operations','op/b'):
                    tx.put('operations','op/b',{'scope':'work/b','run_id':'run/a','thread_id':context.thread_id,
                        'request':{'node_id':'N01'},'phase':'terminal',
                        'status':{'state':'completed','authoritative':True}})
            if context.activation_id=='original/b':
                return NodeResult(execution_status='failed',operation_refs=('op/b',),
                    error=ErrorRecord(code='BENCHMARK_STEP_FAILED',observed_at=1000.0,retry_class='bounded'))
        return NodeResult(execution_status='succeeded',operation_refs=('op/b',) if context.node_id in {'N01','N09'} else (),
            cleanup_status='complete' if context.node_id=='N09' else 'not_needed')
    graph.handler_factory=lambda token,activation: ({n:second_action for n in NODE_GROUPS['work']}
        if activation.scope=='work/b' else prior_graph(token,activation))
    remedy.node_service_factory=lambda token,activation: ({n:second_action for n in NODE_GROUPS['work']}
        if activation.scope=='work/b' else prior_remedy(token,activation))
    async def delivery(job,result):
        if job['scope_ref']!='work/b':
            return await prior_delivery(job,result)
        return DeliveryManifest(delivery_id=job['activation_id'],version=1,scope='work/b',
            plan_ref='plan/a',contract_ref='contract/a',work_refs=('work/b',),evidence=(),
            result=NodeResult(execution_status='succeeded',cleanup_status='complete'))
    graph.delivery_finalizer=delivery
    with repository.transaction() as tx:
        body=tx.get('work_instances','work/a')['body']
        body.pop('result_ref',None)
        tx.put('work_instances','work/b',body|{'work_id':'work/b','group_id':'group/b','status':'pending'})
    TaskBackend(repository).enqueue(JobSpec(job_id='original/job/b',job_kind='work',scope_ref='work/b',
        thread_id='original/thread/b',activation_id='original/b',dedupe_key='original/job/b',
        due_at=1000.0,deadline=1100.0,run_id='run/a',dispatch_generation=3,
        payload={'plan_ref':'plan/a','graph_ref':'graph/a','fixture':'fixture/b'}))
    await drain(worker)  # B has no binding yet: its legacy event is really delivered.
    binding=deepcopy(next(iter(supervisor.bindings.values())))
    binding.update(binding_ref='binding/b',work_ref='work/b',authorization_ref='auth/b')
    with repository.transaction() as tx:
        auth=tx.get('repair_authorizations','auth/runtime')['body']
        revision=tx.get('work_instances','work/b')['revision']
        tx.put('repair_authorizations','auth/b',auth|{'scope_ref':'work/b','work_revision':revision},immutable=True)
        source=tx.get('jobs','original/job/b')['body']['result']['graph_result']['result_refs']['N01']
        assert tx.get('outbox_delivery','agent-supervision:' + source)
    attached=LifecycleSupervisor(repository,bindings=[next(iter(supervisor.bindings.values())),binding],lease_seconds=30)
    worker.lifecycle_supervisor=worker.handlers['lifecycle']=attached
    result=attached.attach_failure(request_id='attach/work/b',binding_ref='binding/b',run_ref='run/a',
        work_ref='work/b',node_id='N01',original_job_id='original/job/b',source_receipt=source,
        expected_work_revision=revision,principal='operator/a',authorization_ref='auth/b')
    assert result['status']=='accepted' and result['lifecycle_id']!=original_life
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.get('agent_lifecycles',original_life)['body']['phase']=='draining'
        assert tx.get('agent_lifecycles',result['lifecycle_id'])['body']['phase']=='fulfilled'
        assert len(tx.scan('repair_orders'))==2
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['repair_attempts']==2
        assert tx.get('lifecycle_budgets','lifecycle:' + original_life)['body']['spent']['repair_attempts']==1
    assert len(repairs)==1
