"""Offline ALI-01/04: real scheduling, StateGraph, executor and recovery checks."""
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sandbox_graph.catalog.registry import NODE_GROUPS
from sandbox_graph.domain.models import DeliveryManifest, ErrorRecord, NodeResult
from sandbox_graph.graphs.factory import build_work
from sandbox_graph.runtime.tasks import JobSpec, TaskBackend
from sandbox_graph.runtime.worker import GraphJobHandler, RemedyJobHandler, Worker, WorkerPolicy
from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle


def assembly(repository, *, configure=True, denied=False, failure='N01', repair_unknown=False,
             source_rejected=False, repair_failures=0, auth_revision=1, no_operations=False,
             child_job_limit=24, repair_attempt_limit=2, recovery_failure=False,
             failure_code='BENCHMARK_STEP_FAILED'):
    from sandbox_graph.runtime.lifecycle_supervisor import LifecycleSupervisor
    service, token = setup_lifecycle(repository)
    with repository.transaction() as tx:
        tx.release_thread(token)
        tx.delete('receipts', 'receipt/a')
        tx.delete('node_reviews', 'receipt/a')
        policy = tx.get('repair_policies', 'policy/a')['body']
        tx.put('repair_policies', 'policy/runtime', policy | {
            'actions':['case.reconcile@1','evidence.seal@1','resource.cleanup@1'],
            'child_job_limit':child_job_limit, 'max_same_failure':3})
        tx.put('contracts', 'contract/a', {'assertions':['owned-operation']}, immutable=True)
        tx.put('dispatch_gates', 'run/a', {'open':True,'generation':3})
        tx.put('repair_authorizations', 'auth/runtime', {'principal':'operator/a',
            'run_ref':'run/a','scope_ref':'work/a','plan_ref':'plan/a','graph_ref':'graph/a',
            'contract_ref':'contract/a','work_revision':auth_revision,
            'actions':['case.reconcile@1','evidence.seal@1','resource.cleanup@1'],
            'permissions':[] if denied else ['operation.read_owned','evidence.seal_owned','resource.cleanup_owned'],
            'revoked':False,'expires_at':1100.0})
    binding = {'binding_ref':'binding/a','run_ref':'run/a','work_ref':'work/a','node_id':failure,
        'policy_ref':'policy/runtime','authorization_ref':'auth/runtime','principal':'operator/a',
        'limits':{'repair_attempts':repair_attempt_limit,'diagnosis_calls':2,'candidate_revisions':2,
                  'tokens':100,'child_jobs':child_job_limit,'observations':2,'cost':100},
        'lineage_limits':{'repair_attempts':2,'diagnosis_calls':2,'candidate_revisions':2,
                          'tokens':100,'child_jobs':child_job_limit,'observations':2,'cost':100},
        'deadline':1100.0,'lineage_deadline':1100.0,
        'safety_limits':{'stop_calls':2,'query_calls':2,'cleanup_calls':2,'cost':10},
        'lineage_safety_limits':{'stop_calls':2,'query_calls':2,'cleanup_calls':2,'cost':10},
        'safety_deadline':1150.0,'lineage_safety_deadline':1150.0,'cost_unit':'test_units'}
    supervisor = LifecycleSupervisor(repository, bindings=[binding] if configure else [], lease_seconds=30)
    seen=[]
    repairs=[]
    async def action(context):
        seen.append((context.activation_id,context.node_id))
        if context.node_id=='N01' and not no_operations:
            with repository.transaction() as tx:
                if not tx.get('operations','op/original'):
                    tx.put('operations','op/original', {'scope':'work/a','run_id':'run/a',
                        'thread_id':context.thread_id,'request':{'node_id':'N01'},'phase':'terminal',
                        'status':{'state':'completed','authoritative':True}})
        if context.activation_id=='original/a' and context.node_id==failure:
            return NodeResult(execution_status='failed',operation_refs=() if no_operations else ('op/original',),
                error=ErrorRecord(code=failure_code,observed_at=1000.0,retry_class='bounded'))
        if context.activation_id.startswith('activation:') and context.node_id==failure:
            repairs.append(context)
            if repair_unknown:
                return NodeResult(execution_status='blocked',operation_refs=() if no_operations else ('op/original',),
                    error=ErrorRecord(code='LOST_REPLY',observed_at=1000.0,effect_certainty='unknown'))
            if len(repairs)<=repair_failures:
                return NodeResult(execution_status='failed',operation_refs=('op/original',),
                    error=ErrorRecord(code='STILL_BROKEN',observed_at=1000.0,retry_class='bounded'))
        if source_rejected and context.activation_id!='original/a' and not context.activation_id.startswith('activation:') and context.node_id==failure:
            return NodeResult(execution_status='succeeded',cleanup_status='unknown')
        if recovery_failure and context.activation_id.startswith('recovery-work:') and context.node_id==failure:
            return NodeResult(execution_status='failed',operation_refs=('op/original',),
                error=ErrorRecord(code='RECOVERY_FAILED',observed_at=1000.0,retry_class='bounded'))
        return NodeResult(execution_status='succeeded',operation_refs=('op/original',) if context.node_id in {'N01','N09'} and not no_operations else (),
            cleanup_status='complete' if context.node_id=='N09' else 'not_needed')
    factory=lambda token,activation:{node:action for node in NODE_GROUPS['work']}
    async def finalizer(job,result):
        return DeliveryManifest(delivery_id=job['activation_id'],version=1,scope='work/a',
            plan_ref='plan/a',contract_ref='contract/a',work_refs=('work/a',),evidence=(),
            result=NodeResult(execution_status='succeeded',cleanup_status='complete'))
    graph=GraphJobHandler(repository,build_work,lambda token:InMemorySaver(),factory,
        lease_seconds=30,segment_seconds=20,delivery_finalizer=finalizer)
    worker=Worker(repository,{'work':graph,'remedy':RemedyJobHandler(repository,factory,
        lease_seconds=30,segment_seconds=20),'lifecycle':supervisor},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    TaskBackend(repository).enqueue(JobSpec(job_id='original/job',job_kind='work',scope_ref='work/a',
        thread_id='original/thread',activation_id='original/a',dedupe_key='original/job',
        due_at=1000.0,deadline=1100.0,run_id='run/a',dispatch_generation=3,
        payload={'plan_ref':'plan/a','graph_ref':'graph/a','fixture':'fixture/a'}))
    return worker, supervisor, seen, repairs


async def drain(worker, limit=30):
    for _ in range(limit):
        if not await worker.run_once():
            worker.pump_outbox()
            if not await worker.run_once():
                break


@pytest.mark.asyncio
async def test_real_first_failure_repairs_recovers_and_accepts_source_once(repository):
    worker, supervisor, seen, repairs=assembly(repository)
    assert await worker.run_once()
    with repository.transaction() as tx:
        original=next(r for r in tx.scan('receipts') if r['body']['identity']['node_id']=='N01')
        frozen=deepcopy(original)
        assert not tx.scan('agent_lifecycles')
    await drain(worker)
    with repository.transaction() as tx:
        life=tx.scan('agent_lifecycles')[0]['body']
        assert life['phase']=='fulfilled'
        assert [e['kind'] for e in life['events']]==[
            'disposition_confirmed','known_action','claimed','postcheck_pass','node_accepted']
        assert tx.get('receipts',original['key'])==frozen
        assert tx.get('lifecycle_sources',life['key']['lifecycle_id'])['body']['work_revision']==1
        assert len(tx.scan('repair_orders'))==len(tx.scan('repair_outcomes'))==len(tx.scan('recovery_plans'))==1
        assert tx.scan('lifecycle_decision_consumptions')[0]['body']['result']['status']=='ordered'
        source_jobs=[r['body'] for r in tx.scan('jobs') if r['body']['job_kind']=='work']
        recovered=next(j for j in source_jobs if j['job_id']!='original/job')
        assert recovered['payload']['fixture']=='fixture/a'
        assert recovered['payload']['recovery_plan_ref'].startswith('recovery:')
        assert 'resume_from' not in recovered['payload']
        assert recovered['result']['graph_result']['recovery_acceptance']['accepted'] is True
        totals=tx.get('lifecycle_budgets','lineage:lineage/a')['body']
        assert totals['spent']['repair_attempts']==1
        assert totals['spent']['diagnosis_calls']==0
        assert totals['spent']['child_jobs']>=4
    before=list(seen)
    for _ in range(4):
        worker.pump_outbox()
    await drain(worker)
    assert seen==before and len(repairs)==1


@pytest.mark.asyncio
async def test_supervision_is_deferred_until_original_completion_metadata(repository):
    worker, _, _, _=assembly(repository)
    graph=worker.handlers['work']
    original_finalize=graph.finalize
    async def finalize(job,result):
        worker.pump_outbox()  # failure event is visible before actual closure publication
        with repository.transaction() as tx:
            assert not [r for r in tx.scan('jobs') if r['body']['job_kind']=='lifecycle']
            assert not tx.scan('agent_lifecycles')
        return await original_finalize(job,result)
    graph.finalize=finalize
    await worker.run_once()
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='fulfilled'


@pytest.mark.asyncio
@pytest.mark.parametrize('denied,configure',[(True,True),(False,False)])
async def test_denied_or_absent_binding_never_creates_repair(repository,denied,configure):
    worker, _, _, repairs=assembly(repository,denied=denied,configure=configure)
    await drain(worker)
    with repository.transaction() as tx:
        assert not tx.scan('repair_orders') and not repairs
        if configure:
            assert tx.scan('lifecycle_blockers')[0]['body']['reason']
        else:
            assert not tx.scan('agent_lifecycles')


@pytest.mark.asyncio
async def test_unknown_effect_keeps_original_id_isolation_and_resumable_intent(repository):
    worker, _, _, repairs=assembly(repository,repair_unknown=True)
    await drain(worker)
    with repository.transaction() as tx:
        blocker=next(r['body'] for r in tx.scan('lifecycle_blockers') if r['body']['status']=='waiting_dependency')
        assert blocker['operation_refs']==['op/original']
        assert blocker['resume_entry'] and blocker['intent_ref']
        assert not tx.get('outbox_delivery',blocker['intent_ref'])
        assert tx.get('lifecycle_intent_results',blocker['intent_ref'])['body']['status']=='waiting_dependency'
        assert tx.scan('repair_isolations') and not tx.scan('recovery_plans')
    await drain(worker)
    assert len(repairs)==1


@pytest.mark.asyncio
async def test_backoff_is_persisted_and_last_allowed_success_is_accepted(repository,clock):
    worker, _, _, repairs=assembly(repository,repair_failures=1)
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='backoff'
    assert len(repairs)==1
    await drain(worker)
    assert len(repairs)==1
    clock.advance(1.0)
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='fulfilled'
        assert tx.get('lifecycle_budgets','lineage:lineage/a')['body']['spent']['repair_attempts']==2
    assert len(repairs)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['_diagnose','_decide','_recover','_complete'])
async def test_hard_cut_after_durable_stage_replays_without_new_attempt(repository,clock,monkeypatch,stage):
    class Cut(BaseException):
        pass
    worker, supervisor, seen, repairs=assembly(repository)
    worker.policy=worker.policy.model_copy(update={'claim_seconds':1.0})
    original=getattr(supervisor,stage)
    cut=[]
    def crash(*args):
        result=original(*args)
        if not cut:
            cut.append(True)
            raise Cut()
        return result
    monkeypatch.setattr(supervisor,stage,crash)
    with pytest.raises(Cut):
        await drain(worker)
    monkeypatch.setattr(supervisor,stage,original)
    # Simulate a successor process after the persisted job claim expires. The
    # business deadline and original allowance remain unchanged.
    clock.advance(1.1)
    await drain(worker)
    with repository.transaction() as tx:
        assert len(tx.scan('repair_orders'))==len(tx.scan('repair_outcomes'))==1
        assert len(tx.scan('recovery_plans'))==1
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='fulfilled'
        assert not tx.scan('lifecycle_blockers')
        bounds=tx.get('lifecycle_budgets','lineage:lineage/a')['body']
        assert bounds['spent']['repair_attempts']==1
        assert bounds['reserved']['child_jobs']==0
    assert len(repairs)==1


@pytest.mark.asyncio
async def test_authoritative_source_rejection_survives_successful_tail_delivery(repository):
    worker, _, seen, repairs=assembly(repository,source_rejected=True)
    await drain(worker)
    with repository.transaction() as tx:
        life=tx.scan('agent_lifecycles')[0]['body']
        assert life['phase']=='draining'
        assert life['events'][-1]['kind']=='node_failed'
        recovered=next(r['body'] for r in tx.scan('jobs')
            if r['body']['job_kind']=='work' and r['key']!='original/job')
        graph=recovered['result']['graph_result']
        assert graph['results']['N10']['execution_status']=='succeeded'
        assert graph['recovery_acceptance']['accepted'] is False
        assert graph['recovery_dependency_failures']['N01']=='original_operation_mismatch'
        assert tx.scan('lifecycle_blockers')[0]['body']['reason']=='original_operation_mismatch'
    assert len(repairs)==1


@pytest.mark.asyncio
async def test_other_group_runs_while_failed_work_is_held(repository):
    worker, _, _, repairs=assembly(repository,repair_unknown=True)
    with repository.transaction() as tx:
        work=tx.get('work_instances','work/a')['body']
        tx.put('work_instances','work/b',work | {'work_id':'work/b','group_id':'group/b'})
    TaskBackend(repository).enqueue(JobSpec(job_id='other/job',job_kind='work',scope_ref='work/b',
        thread_id='other/thread',activation_id='other/a',dedupe_key='other/job',
        due_at=1000.0,deadline=1100.0,run_id='run/a',dispatch_generation=3,
        payload={'plan_ref':'plan/a','graph_ref':'graph/a','fixture':'fixture/b'}))
    # Reuse the real graph/Driver/NodeExecutor, with this group's own delivery.
    handler=worker.handlers['work']
    prior=handler.delivery_finalizer
    async def finalize(job,result):
        if job['scope_ref']=='work/b':
            return DeliveryManifest(delivery_id=job['activation_id'],version=1,scope='work/b',
                plan_ref='plan/a',contract_ref='contract/a',work_refs=('work/b',),evidence=(),
                result=NodeResult(execution_status='succeeded',cleanup_status='complete'))
        return await prior(job,result)
    handler.delivery_finalizer=finalize
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.get('jobs','other/job')['body']['status']=='complete'
        assert tx.get('work_instances','work/b')['body']['status']=='delivered'
        assert tx.scan('repair_isolations')
    assert len(repairs)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['observed','before_decision'])
@pytest.mark.parametrize('repair_failures,child_jobs',[(0,6),(1,9)])
async def test_independent_consumer_interleaving_reuses_diagnosis_with_exact_budget(repository,clock,monkeypatch,boundary,repair_failures,child_jobs):
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    worker,_,_,repairs=assembly(repository,child_job_limit=child_jobs,repair_failures=repair_failures)
    peer=Worker(repository,worker.handlers,policy=worker.policy,owner='other-worker')
    method='observe_failure' if boundary=='observed' else 'record_decision'
    original=getattr(LifecycleService,method)
    def interleave(service,*args,**kwargs):
        if boundary=='observed':
            result=original(service,*args,**kwargs)
        # Actual second Worker's separate transactions see committed observation
        # or disposition, before the first worker has committed its decision.
        with repository.transaction() as tx:
            for row in tx.scan('outbox'):
                if (not row['key'].startswith('duplicate:')
                        and row['body']['kind'] in {'lifecycle_observed','lifecycle_advanced'}):
                    tx.put('outbox','duplicate:' + row['key'],row['body'],immutable=True)
        peer.pump_outbox()
        peer.pump_outbox()
        if boundary=='before_decision':
            result=original(service,*args,**kwargs)
        return result
    monkeypatch.setattr(LifecycleService,method,interleave)
    await drain(worker)
    if repair_failures:
        clock.advance(1.0)
        await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase']=='fulfilled'
        diagnosis=[r for r in tx.scan('jobs') if r['body']['job_kind']=='lifecycle'
                   and r['body']['payload']['stage']=='diagnose']
        assert len(diagnosis)==1 + repair_failures
        assert not tx.scan('lifecycle_blockers')
        for bounds in tx.scan('lifecycle_budgets'):
            assert bounds['body']['spent']['child_jobs']==child_jobs
            assert bounds['body']['reserved']['child_jobs']==0
    assert len(repairs)==1 + repair_failures
