"""原 ID 安全续办：真实 Worker/TaskBackend，受控外部 Runner。"""
from copy import deepcopy

import pytest

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.runtime.runner import OperationService, RunnerStatus, CleanupResult
from sandbox_graph.runtime.tasks import TaskBackend
from sandbox_graph.runtime.worker import Worker, WorkerPolicy
from sandbox_graph.services.agent_lifecycle import LifecycleService
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from tests.orchestration.unit.test_repair_execution import modern_job, worker


class Runner:
    def __init__(self, status=None, *, lose_cleanup=False, mutate=None):
        self.status = status or RunnerStatus(state='completed', authoritative=True)
        self.calls = []
        self.lose_cleanup, self.mutate = lose_cleanup, mutate

    async def submit(self, operation):
        pytest.fail('安全消费者不得 submit')

    async def reconcile(self, operation_id, scope):
        self.calls.append(('query', operation_id, scope))
        if self.mutate:
            self.mutate()
        return self.status

    async def cancel(self, operation_id, scope, reason):
        self.calls.append(('stop', operation_id, scope))
        return RunnerStatus(state='running', authoritative=True)

    async def cleanup(self, scope, resource_ids):
        self.calls.append(('cleanup', scope, resource_ids))
        if self.lose_cleanup:
            raise ConnectionError('清理确认丢失')
        return CleanupResult(status='complete', resource_ids=resource_ids)


async def uncertain(repository, *, operation=True, resources=False, succeeded=True):
    request, ordered, claimed = modern_job(repository)
    if operation:
        with repository.transaction() as tx:
            tx.put('operations', 'op/original', {'scope':'work/a', 'run_id':'run/a',
                'thread_id':'work-thread', 'phase':'terminal',
                'status':RunnerStatus(state='completed',authoritative=True).model_dump(mode='json')})
            from sandbox_graph.runtime.runner import RunnerCollection
            tx.put('collections','op/original:0',RunnerCollection().model_dump(mode='json'),immutable=True)
    async def action(context):
        if not operation:
            raise ConnectionError('原操作绑定丢失')
        if operation:
            with repository.transaction() as tx:
                row=tx.get('operations','op/original')
                tx.put('operations','op/original',row['body'] | {'phase':'submission_started','status':{}})
                if resources:
                    tx.put('resources','resource/a',{'scope':'work/a','operation_id':'op/original','status':'active'})
                    tx.put('resource_leases','domain/a',{'scope':'work/a','run_id':'run/a','status':'quarantine'})
        return NodeResult(execution_status='succeeded' if succeeded else 'failed',
                          operation_refs=('op/original',) if operation else ())
    await worker(repository, action)(claimed)
    TaskBackend(repository).finish(claimed['job_id'], owner_epoch=claimed['owner_epoch'],
                                  result={'execution_status':'blocked'},status='blocked')
    return request.lifecycle_id, ordered['order_ref'], 'repair-outcome:' + ordered['order_ref']


def install(repository, lifecycle_id, order_ref, gateway, **changes):
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService, ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    config = ReconciliationConfig(schema_version=1, cost_unit='test_units', query_cost=1,
        stop_cost=1, cleanup_cost=1, backoff_seconds=1, **changes)
    service = LifecycleReconciliationService(repository, config=config,
        authorizations={lifecycle_id:'safety/auth'})
    service.authorize('safety/auth', operation_kind='site', target_ref=order_ref,
        principal='operator/a', expires_at=1140.0, permissions=('query','stop','cleanup'))
    handler = LifecycleReconciliationHandler(service, operation_service=OperationService(repository,gateway,{}),
        lease_seconds=30)
    return service, Worker(repository,{'reconcile':handler},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01),
        reconciliation=service)


@pytest.mark.asyncio
async def test_original_id_terminal_closure_appends_resolution_and_recovery_without_rewriting_unknown(repository):
    life, order, intent = await uncertain(repository)
    original = deepcopy(repository.rows[('repair_outcomes',order)])
    gateway = Runner()
    service, runtime = install(repository,life,order,gateway)
    assert await runtime.run_once()
    with repository.transaction() as tx:
        assert tx.get('outbox_delivery',intent)
        assert tx.get('repair_isolations','repair-isolation:' + order)
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a') == []
        assert tx.get('repair_reconciliation_outcomes',order)['body']['postcheck_status'] == 'accepted'
        assert LifecycleService.load_in(tx,life).phase == 'reentering'
        assert len([r for r in tx.scan('outbox') if r['body']['kind']=='repair_recovery_required']) == 1
        assert LifecycleBudgetService.snapshot_in(tx,life)['lifecycle']['safety_spent']['query_calls'] == 1
    assert repository.rows[('repair_outcomes',order)] == original
    runtime.pump_outbox()
    assert not await runtime.run_once()
    assert gateway.calls == [('query','op/original','work/a')]


@pytest.mark.asyncio
async def test_running_cancel_ack_retains_isolation_backoff_and_original_shared_limits(repository,clock):
    life, order, _ = await uncertain(repository)
    gateway = Runner(RunnerStatus(state='running',authoritative=True))
    service, runtime = install(repository,life,order,gateway)
    assert await runtime.run_once()
    with repository.transaction() as tx:
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        assert not tx.scan('repair_isolation_resolutions')
    assert not await runtime.run_once()
    clock.advance(1)
    runtime.owner='replacement/worker'
    assert await runtime.run_once()
    clock.advance(1)
    await runtime.run_once()
    with repository.transaction() as tx:
        assert 'budget' in tx.get('lifecycle_blockers',life)['body']['reason']
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        assert len(tx.scan('operations')) == 1
        assert LifecycleBudgetService.snapshot_in(tx,life)['lineage']['safety_spent']['query_calls'] == 2
    assert [c[0] for c in gateway.calls].count('query') == 2
    assert [c[0] for c in gateway.calls].count('stop') == 1


@pytest.mark.asyncio
async def test_missing_original_operation_never_uses_empty_inventory_as_closure(repository):
    life, order, _ = await uncertain(repository,operation=False)
    gateway = Runner()
    _, runtime = install(repository,life,order,gateway)
    await runtime.run_once()
    with repository.transaction() as tx:
        assert 'operation_binding_missing' in tx.get('lifecycle_blockers',life)['body']['reason']
        assert not tx.scan('repair_isolation_resolutions')
        assert not tx.scan('reconciliation_calls')
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['scope','inventory','work','auth'])
async def test_changed_original_identity_or_authorization_prevents_network(repository,change):
    life, order, _ = await uncertain(repository)
    gateway=Runner()
    _, runtime=install(repository,life,order,gateway)
    with repository.transaction() as tx:
        if change=='scope':
            row=tx.get('operations','op/original')
            tx.put('operations','op/original',row['body'] | {'scope':'work/other'})
        elif change=='inventory':
            tx.put('operations','op/replacement',{'scope':'work/a','run_id':'run/a','phase':'uncertain'})
        elif change=='work':
            row=tx.get('work_instances','work/a')
            tx.put('work_instances','work/a',row['body'] | {'status':'changed'})
        else:
            row=tx.get('reconciliation_authorizations','safety/auth')
            tx.put('reconciliation_authorizations','safety/auth',row['body'] | {'revoked':True})
    await runtime.run_once()
    assert gateway.calls==[]
    with repository.transaction() as tx:
        assert not tx.scan('repair_isolation_resolutions')


@pytest.mark.asyncio
async def test_unknown_cleanup_is_never_resent_after_lost_reply(repository,clock):
    life,order,_=await uncertain(repository,resources=True)
    gateway=Runner(lose_cleanup=True)
    _,runtime=install(repository,life,order,gateway)
    await runtime.run_once()
    clock.advance(1)
    await runtime.run_once()
    with repository.transaction() as tx:
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        assert 'cleanup' in tx.get('lifecycle_blockers',life)['body']['reason']
    assert [c[0] for c in gateway.calls].count('cleanup')==1


@pytest.mark.asyncio
async def test_terminal_operation_cannot_release_running_sampler_or_missing_closure(repository):
    life,order,_=await uncertain(repository)
    gateway=Runner()
    _,runtime=install(repository,life,order,gateway)
    with repository.transaction() as tx:
        row=tx.get('work_safety_facts','work/a')
        tx.put('work_safety_facts','work/a',row['body'] | {'sampler_status':'running'})
    await runtime.run_once()
    with repository.transaction() as tx:
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        assert not tx.scan('repair_reconciliation_outcomes')


@pytest.mark.asyncio
async def test_closed_gate_and_business_deadline_do_not_remove_explicit_safety_authority(repository,clock):
    life,order,_=await uncertain(repository)
    gateway=Runner()
    _,runtime=install(repository,life,order,gateway)
    clock.advance(101)
    with repository.transaction() as tx:
        tx.put('dispatch_gates','run/a',{'open':False,'generation':4})
    await runtime.run_once()
    assert gateway.calls==[('query','op/original','work/a')]
    with repository.transaction() as tx:
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')==[]
        assert not [r for r in tx.scan('outbox') if r['body']['kind']=='repair_recovery_required']


@pytest.mark.asyncio
async def test_lost_real_job_owner_during_remote_query_cannot_publish_status_or_resolution(repository):
    life,order,_=await uncertain(repository)
    def lose():
        with repository.transaction() as tx:
            row=next(r for r in tx.scan('jobs') if r['body']['job_kind']=='reconcile')
            tx.put('jobs',row['key'],row['body'] | {'owner_epoch':99})
    gateway=Runner(mutate=lose)
    _,runtime=install(repository,life,order,gateway)
    from sandbox_graph.persistence.repository import StaleOwner
    with pytest.raises(StaleOwner):
        await runtime.run_once()
    with repository.transaction() as tx:
        assert tx.get('operations','op/original')['body']['phase']=='submission_started'
        assert not tx.scan('repair_isolation_resolutions')


@pytest.mark.asyncio
async def test_known_failed_receipt_uses_finite_failure_route_without_recovery(repository):
    life,order,_=await uncertain(repository,succeeded=False)
    _,runtime=install(repository,life,order,Runner())
    for _ in range(3):
        await runtime.run_once()
    with repository.transaction() as tx:
        assert tx.get('repair_reconciliation_outcomes',order), tx.get('lifecycle_blockers',life)
        assert tx.get('repair_reconciliation_outcomes',order)['body']['postcheck_status']=='rejected'
        assert LifecycleService.load_in(tx,life).phase=='blocked'
        assert not [r for r in tx.scan('outbox') if r['body']['kind']=='repair_recovery_required']


@pytest.mark.asyncio
async def test_provider_safety_after_original_job_deadline_closes_slot_without_new_model_or_refund(repository,tmp_path,clock):
    from tests.orchestration.unit.test_agent_team import prepared,release_parent,runner
    from tests.orchestration.unit.test_agent_bridge import ControlledProvider,capabilities,scope,receipt
    from sandbox_graph.runtime.agent_bridge import Usage
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    team,_,record,parent,token=prepared(repository,tmp_path)
    target=team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    provider=ControlledProvider(capabilities(scopes=(scope('work/a'),)))
    await runner(repository,team,provider).run_once()
    with repository.transaction() as tx:
        assert tx.get('agent_team_tasks',target)['body']['status']=='waiting_provider'
        before=deepcopy(tx.get('budget_settlements',target)['body'])
    config=ReconciliationConfig(schema_version=1,cost_unit='microunits',query_cost=1,
        stop_cost=1,cleanup_cost=1,backoff_seconds=1)
    service=LifecycleReconciliationService(repository,config=config,
        authorizations={record.key.lifecycle_id:'provider/auth'})
    service.authorize('provider/auth',operation_kind='provider',target_ref=target,
        principal='operator/a',expires_at=1140.0,permissions=('query','stop'))
    handler=LifecycleReconciliationHandler(service,agent_provider=provider,lease_seconds=30)
    runtime=Worker(repository,{'reconcile':handler},reconciliation=service,
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    clock.advance(101)
    provider.current=receipt(provider.submissions[0],state='cancelled',children_stopped=True,
        receipt_id='late/terminal',usage=Usage(schema_version=1,input_tokens=1,output_tokens=0,cost_microunits=1))
    assert await runtime.run_once()
    with repository.transaction() as tx:
        assert tx.get('agent_team_tasks',target)['body']['status']=='reconciled'
        assert tx.get('lifecycle_blockers',record.key.lifecycle_id) is None
        assert tx.get('agent_team_reconciliations',target)['body']['children_stopped'] is True
        assert tx.get('budget_settlements',target)['body']==before
        assert not tx.scan('repair_isolation_resolutions')
        assert not tx.scan('repair_orders')
        assert not tx.scan('lifecycle_decisions')
    assert len(provider.submissions)==1


@pytest.mark.asyncio
async def test_duplicate_original_intent_identity_cannot_repeat_unknown_cleanup(repository,clock):
    life,order,intent=await uncertain(repository,resources=True)
    gateway=Runner(lose_cleanup=True)
    _,runtime=install(repository,life,order,gateway)
    await runtime.run_once()
    with repository.transaction() as tx:
        tx.put('outbox','duplicate/intent',tx.get('outbox',intent)['body'],immutable=True)
    clock.advance(1)
    for _ in range(3):
        await runtime.run_once()
    assert len([c for c in gateway.calls if c[0]=='cleanup'])==1


@pytest.mark.asyncio
async def test_successful_cleanup_requires_real_full_closure_before_releasing_resource_domain(repository):
    life,order,_=await uncertain(repository,resources=True)
    gateway=Runner()
    _,runtime=install(repository,life,order,gateway)
    await runtime.run_once()
    with repository.transaction() as tx:
        assert not LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        assert tx.get('resources','resource/a')['body']['status']=='closed'
        assert not tx.get('resource_leases','domain/a')
        assert tx.get('repair_isolations','repair-isolation:' + order)
        assert any(r['body']['kind']=='resource_domains_available' for r in tx.scan('outbox'))
    assert [c[0] for c in gateway.calls]==['query','cleanup']


@pytest.mark.asyncio
async def test_forged_resolution_is_not_accepted_by_any_isolation_reader(repository):
    life,order,_=await uncertain(repository)
    with repository.transaction() as tx:
        tx.put('repair_isolation_resolutions','repair-resolution:' + order,
            {'status':'closed','producer':'lifecycle_reconciliation','version':1,
             'isolation_ref':'repair-isolation:' + order})
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')


@pytest.mark.asyncio
async def test_provider_lost_owner_after_status_cannot_commit_bridge_terminal(repository,tmp_path):
    from tests.orchestration.unit.test_agent_team import prepared,release_parent,runner
    from tests.orchestration.unit.test_agent_bridge import ControlledProvider,capabilities,scope,receipt
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    from sandbox_graph.persistence.repository import StaleOwner
    team,_,record,parent,token=prepared(repository,tmp_path)
    target=team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    provider=ControlledProvider(capabilities(scopes=(scope('work/a'),)))
    await runner(repository,team,provider).run_once()
    original_status=provider.status
    async def lost_owner(handle):
        await original_status(handle)
        with repository.transaction() as tx:
            row=next(r for r in tx.scan('jobs') if r['body']['job_kind']=='reconcile')
            tx.put('jobs',row['key'],row['body'] | {'owner_epoch':99})
        return receipt(provider.submissions[0],state='cancelled',children_stopped=True,receipt_id='late/fenced')
    provider.status=lost_owner
    config=ReconciliationConfig(schema_version=1,cost_unit='microunits',query_cost=1,stop_cost=1,cleanup_cost=1,backoff_seconds=1)
    service=LifecycleReconciliationService(repository,config=config,authorizations={record.key.lifecycle_id:'safety/auth'})
    service.authorize('safety/auth',operation_kind='provider',target_ref=target,principal='operator',expires_at=1140.0,permissions=('query','stop'))
    handler=LifecycleReconciliationHandler(service,agent_provider=provider,lease_seconds=30)
    runtime=Worker(repository,{'reconcile':handler},reconciliation=service,
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    with pytest.raises(StaleOwner):
        await runtime.run_once()
    with repository.transaction() as tx:
        assert tx.get('agent_provider_requests',target)['body']['state']!='terminal'
        assert not tx.get('agent_team_reconciliations',target)


@pytest.mark.asyncio
async def test_timeout_watchdog_settles_started_safety_call_without_repeating_effect(repository):
    import asyncio
    life,order,_=await uncertain(repository)
    class HangingRunner(Runner):
        async def reconcile(self,operation_id,scope):
            self.calls.append(('query',operation_id,scope))
            await asyncio.Event().wait()
    gateway=HangingRunner()
    _,runtime=install(repository,life,order,gateway)
    runtime.policy=WorkerPolicy(claim_seconds=30,job_seconds=0.01,poll_seconds=0.01)
    assert await runtime.run_once()
    runtime.pump_outbox()
    with repository.transaction() as tx:
        calls=tx.scan('reconciliation_calls')
        assert len(calls)==1
        assert tx.get('budget_settlements',calls[0]['key'])
        assert 'safety_job_stopped' in tx.get('lifecycle_blockers',life)['body']['reason']
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
    assert len(gateway.calls)==1


@pytest.mark.asyncio
async def test_provider_unknown_acceptance_uses_original_lookup_without_submit_or_site_cleanup(repository,tmp_path):
    from tests.orchestration.unit.test_agent_team import prepared,release_parent,runner
    from tests.orchestration.unit.test_agent_bridge import ControlledProvider,capabilities,scope,receipt
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    team,_,record,parent,token=prepared(repository,tmp_path)
    target=team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    provider=ControlledProvider(capabilities(scopes=(scope('work/a'),)))
    provider.lose_submit=True; provider.unknown_lookup=True
    await runner(repository,team,provider).run_once()
    provider.unknown_lookup=False
    provider.current=receipt(provider.submissions[0],state='cancelled',children_stopped=True,receipt_id='looked/up')
    provider.saved=provider.current
    service=LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1,cost_unit='microunits',query_cost=1,stop_cost=1,cleanup_cost=1,backoff_seconds=1),
        authorizations={record.key.lifecycle_id:'safety/auth'})
    service.authorize('safety/auth',operation_kind='provider',target_ref=target,principal='operator',expires_at=1140.0,permissions=('query','stop'))
    handler=LifecycleReconciliationHandler(service,agent_provider=provider,lease_seconds=30)
    runtime=Worker(repository,{'reconcile':handler},reconciliation=service,
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    await runtime.run_once()
    assert provider.lookups==[target,target]
    assert len(provider.submissions)==1
    with repository.transaction() as tx:
        assert tx.get('agent_team_reconciliations',target)
        assert not tx.scan('cleanup_records')


def test_bootstrap_explicit_safety_service_installs_real_handler_and_worker_consumer(repository):
    from sandbox_graph.runtime.bootstrap import build_job_handlers
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    service=LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1,cost_unit='test_units',query_cost=1,stop_cost=1,cleanup_cost=1,backoff_seconds=1),
        authorizations={})
    handlers=build_job_handlers(repository,graph_ref='graph/a',saver_factory=lambda:None,
        node_service_factory=lambda *a:{},review_service=None,deadline=1100.0,lease_seconds=30,
        segment_seconds=20,operation_service=OperationService(repository,Runner(),{}),reconciliation=service)
    runtime=Worker(repository,handlers,policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    assert runtime.reconciliation is service
    assert handlers['reconcile'].service is service


@pytest.mark.asyncio
async def test_other_isolation_and_missing_collection_cannot_be_released_as_this_order(repository):
    life,order,_=await uncertain(repository)
    _,runtime=install(repository,life,order,Runner())
    with repository.transaction() as tx:
        tx.put('repair_isolations','unrelated/isolation',{'order_ref':'another/order','work_ref':'work/a','operation_refs':[]},immutable=True)
    await runtime.run_once()
    with repository.transaction() as tx:
        assert len(LifecycleSafetyService._repair_isolations_in(tx,'work/a'))==2
        assert not tx.scan('repair_isolation_resolutions')


@pytest.mark.asyncio
async def test_explicit_cancel_finishes_cancelled_only_after_authoritative_closure(repository):
    from sandbox_graph.domain.agent_lifecycle import LifecycleEvent,TransitionGuards,transition_lifecycle
    life,order,_=await uncertain(repository)
    with repository.transaction() as tx:
        record=LifecycleService.load_in(tx,life)
        event=LifecycleEvent(schema_version=1,event_id='user/cancel',kind='cancel',source='authenticated_user',
            expected_revision=record.revision,evidence_refs=('receipt/a',),observed_at=tx.now)
        updated=transition_lifecycle(record,event,TransitionGuards(schema_version=1,work_lease_valid=True,
            effect_known=False,effects_in_flight=True,safety_budget_available=True,safety_deadline=1150.0))
        LifecycleService.persist_transition_in(tx,record,event,updated)
    _,runtime=install(repository,life,order,Runner())
    await runtime.run_once()
    with repository.transaction() as tx:
        record=LifecycleService.load_in(tx,life)
        assert record.phase=='cancelled'
        assert record.terminal_intent=='cancelled'
        assert not LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        assert not [r for r in tx.scan('outbox') if r['body']['kind']=='repair_recovery_required']


@pytest.mark.asyncio
async def test_terminal_without_original_collection_remains_isolated(repository):
    life,order,_=await uncertain(repository)
    _,runtime=install(repository,life,order,Runner())
    with repository.transaction() as tx:
        tx.delete('collections','op/original:0')
    await runtime.run_once()
    with repository.transaction() as tx:
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        assert 'collection' in tx.get('lifecycle_blockers',life)['body']['reason']
        assert not tx.scan('repair_isolation_resolutions')


@pytest.mark.asyncio
async def test_changed_frozen_original_raw_receipt_prevents_reconciliation(repository):
    life,order,_=await uncertain(repository)
    gateway=Runner()
    _,runtime=install(repository,life,order,gateway)
    with repository.transaction() as tx:
        ref=next(r for r in tx.scan('receipts') if r['body'].get('identity',{}).get('node_id')=='N01'
            and r['key']!='receipt/a')['key']
        row=tx.get('receipts',ref)
        tx.put('receipts',ref,row['body'] | {'result':NodeResult(execution_status='failed').model_dump(mode='json')})
    await runtime.run_once()
    assert gateway.calls==[]
    with repository.transaction() as tx:
        assert not tx.scan('repair_isolation_resolutions')


@pytest.mark.asyncio
@pytest.mark.parametrize('loss',['thread','lifecycle','work'])
async def test_every_real_owner_loss_during_query_prevents_operation_commit(repository,loss):
    from sandbox_graph.persistence.repository import StaleOwner
    life,order,_=await uncertain(repository)
    def lose():
        with repository.transaction() as tx:
            session=tx.scan('lifecycle_reconciliations')[0]['body']
            if loss=='thread':
                thread=tx.get('jobs',session['job_ref'])['body']['thread_id']
                token=repository.leases[thread]
                tx.release_thread(token)
                tx.acquire_thread(thread,'replacement',30)
            elif loss=='lifecycle':
                row=tx.get('lifecycle_owners',life)
                tx.put('lifecycle_owners',life,row['body'] | {'lease_id':'replacement'})
            else:
                row=tx.get('work_execution_leases','work/a')
                tx.put('work_execution_leases','work/a',row['body'] | {'owner':row['body']['owner'] | {'lease_id':'replacement'}})
    _,runtime=install(repository,life,order,Runner(mutate=lose))
    with pytest.raises(StaleOwner):
        await runtime.run_once()
    with repository.transaction() as tx:
        assert tx.get('operations','op/original')['body']['phase']=='submission_started'
        assert not tx.scan('repair_isolation_resolutions')


@pytest.mark.asyncio
async def test_verified_historical_resolution_survives_legitimate_next_work_revision(repository):
    life,order,_=await uncertain(repository)
    _,runtime=install(repository,life,order,Runner())
    await runtime.run_once()
    with repository.transaction() as tx:
        assert not LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        row=tx.get('work_instances','work/a')
        tx.put('work_instances','work/a',row['body'] | {'status':'recovering'})
        assert not LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        tx.put('operations','new/unknown',{'scope':'work/a','run_id':'run/a','phase':'submission_started'})
        token=tx.acquire_thread('next/work','worker',30)
        LifecycleSafetyService.acquire_work_in(tx,'work/a',token)
        proof=LifecycleSafetyService.produce_disposition_in(tx,'work/a',token,ttl=30)
        from sandbox_graph.persistence.repository import Conflict
        with pytest.raises(Conflict,match='closed'):
            LifecycleSafetyService.verify_disposition_in(tx,proof,'work/a',token)


@pytest.mark.asyncio
async def test_started_cleanup_commit_cut_replays_original_persisted_completion_without_recharge(repository,monkeypatch):
    life,order,_=await uncertain(repository,resources=True)
    gateway=Runner(RunnerStatus(state='completed',authoritative=True,resource_refs=('resource/a',)))
    service,runtime=install(repository,life,order,gateway)
    original=service.finish_call_in
    cut=[False]
    def cut_once(tx,job,token,call_ref,*,completed):
        if tx.get('reconciliation_calls',call_ref)['body']['kind']=='cleanup' and not cut[0]:
            cut[0]=True
            raise RuntimeError('清理账本提交后进程中断')
        return original(tx,job,token,call_ref,completed=completed)
    monkeypatch.setattr(service,'finish_call_in',cut_once)
    await runtime.run_once()
    runtime.pump_outbox()
    with repository.transaction() as tx:
        session=tx.scan('lifecycle_reconciliations')[0]['body']
        assert session['state']=='blocked'
    service.resume(session['ref'])
    await runtime.run_once()
    with repository.transaction() as tx:
        assert not LifecycleSafetyService._repair_isolations_in(tx,'work/a')
    assert [c[0] for c in gateway.calls].count('cleanup')==1


@pytest.mark.asyncio
async def test_provider_cancel_ack_keeps_original_slot_until_children_actually_stopped(repository,tmp_path,clock):
    from tests.orchestration.unit.test_agent_team import prepared,release_parent,runner
    from tests.orchestration.unit.test_agent_bridge import ControlledProvider,capabilities,scope,receipt
    from sandbox_graph.runtime.agent_bridge import CancelReply
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    team,_,record,parent,token=prepared(repository,tmp_path)
    target=team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    class Provider(ControlledProvider):
        stops=0
        async def cancel(self,handle):
            self.stops+=1
            env=self.submissions[0]
            return CancelReply(schema_version=1,request_id=env.request_id,scope=env.scope,handle=handle,accepted=True)
    provider=Provider(capabilities(scopes=(scope('work/a'),)))
    provider.lose_submit=True; provider.unknown_lookup=True
    await runner(repository,team,provider).run_once()
    provider.unknown_lookup=False
    service=LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1,cost_unit='microunits',query_cost=1,stop_cost=1,cleanup_cost=1,backoff_seconds=1),
        authorizations={record.key.lifecycle_id:'safety/auth'})
    service.authorize('safety/auth',operation_kind='provider',target_ref=target,principal='operator',expires_at=1140.0,permissions=('query','stop'))
    runtime=Worker(repository,{'reconcile':LifecycleReconciliationHandler(service,agent_provider=provider,lease_seconds=30)},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    await runtime.run_once()
    with repository.transaction() as tx:
        assert tx.get('agent_team_tasks',target)['body']['status']=='waiting_provider'
        assert tx.get('agent_provider_requests',target)['body']['cancel_accepted'] is True
        assert not tx.get('agent_team_reconciliations',target)
    provider.current=receipt(provider.submissions[0],state='cancelled',children_stopped=True,receipt_id='stopped/children')
    clock.advance(1)
    await runtime.run_once()
    assert provider.stops==1
    with repository.transaction() as tx:
        assert tx.get('agent_team_tasks',target)['body']['status']=='reconciled'


@pytest.mark.asyncio
async def test_site_cleanup_requires_new_explicit_permission_and_never_inherits_repair_authority(repository):
    life,order,_=await uncertain(repository,resources=True)
    gateway=Runner()
    service,runtime=install(repository,life,order,gateway)
    with repository.transaction() as tx:
        row=tx.get('reconciliation_authorizations','safety/auth')
        tx.put('reconciliation_authorizations','safety/auth',row['body'] | {'permissions':['query']})
    await runtime.run_once()
    assert [c[0] for c in gateway.calls]==['query']
    with repository.transaction() as tx:
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        assert 'permission_missing:cleanup' in tx.get('lifecycle_blockers',life)['body']['reason']


@pytest.mark.asyncio
async def test_missing_independent_authority_can_resume_only_with_exact_explicit_grant(repository):
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    life,order,intent=await uncertain(repository)
    service=LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1,cost_unit='test_units',query_cost=1,stop_cost=1,cleanup_cost=1,backoff_seconds=1),
        authorizations={})
    gateway=Runner()
    runtime=Worker(repository,{'reconcile':LifecycleReconciliationHandler(service,operation_service=OperationService(repository,gateway,{}),lease_seconds=30)},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    assert not await runtime.run_once()
    with repository.transaction() as tx:
        session=tx.scan('lifecycle_reconciliations')[0]['body']
        assert not tx.get('outbox_delivery',intent)
    service.authorize('late/auth',operation_kind='site',target_ref=order,principal='operator',expires_at=1140.0,permissions=('query',))
    service.resume(session['ref'],authorization_ref='late/auth')
    assert await runtime.run_once()
    assert gateway.calls==[('query','op/original','work/a')]


@pytest.mark.asyncio
async def test_ledger_observation_stop_is_not_a_physical_sampler_receipt(repository):
    from sandbox_graph.runtime.observation import ObservationService
    life,order,_=await uncertain(repository)
    observation=ObservationService(repository)
    observation.start('sampler/a','work/a',operation_id='op/original',clock_epoch='epoch/a',deadline=1090.0)
    observation.stop('sampler/a',last_seq=0)
    _,runtime=install(repository,life,order,Runner())
    await runtime.run_once()
    with repository.transaction() as tx:
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        assert 'sampler' in tx.get('lifecycle_blockers',life)['body']['reason']
        assert not tx.scan('repair_isolation_resolutions')


async def compiled_reconciliation(repository,clock,*,revoke=False,remove_resolution=False):
    from sandbox_graph.runtime.tasks import JobSpec
    from sandbox_graph.services.recovery import RecoveryService
    life,order,_=await uncertain(repository)
    with repository.transaction() as tx:
        source=tx.get('receipts','receipt/a')['body']
        tx.put('receipts','receipt/zero',{'identity':source['identity'] | {'node_id':'N00'},
            'result':NodeResult(execution_status='succeeded').model_dump(mode='json')},immutable=True)
        tx.put('contracts','contract/a',{'schema_version':1,'assertions':['fixed']},immutable=True)
    _,runtime=install(repository,life,order,Runner())
    clock.advance(35)  # 原未知 outcome 的 disposition 已过期，续办必须提供新证明。
    await runtime.run_once()
    with repository.transaction() as tx:
        assert not LifecycleSafetyService._repair_isolations_in(tx,'work/a')
        if revoke:
            row=tx.get('repair_authorizations','auth/a')
            tx.put('repair_authorizations','auth/a',row['body'] | {'revoked':True})
        if remove_resolution:
            tx.delete('repair_isolation_resolutions','repair-resolution:' + order)
        TaskBackend.enqueue_in(tx,JobSpec(job_id='actual/compiler',job_kind='lifecycle',scope_ref='work/a',
            thread_id='actual/compiler',activation_id='actual/compiler',dedupe_key='actual/compiler',
            due_at=tx.now,deadline=1090.0,run_id='run/a',dispatch_generation=3,payload={'lifecycle_id':life}))
    claimed=TaskBackend(repository).claim('recovery/worker',30)
    with repository.transaction() as tx:
        token=tx.acquire_thread(claimed['thread_id'],'recovery/worker',30)
    LifecycleService(repository).claim(life,token=token)
    LifecycleSafetyService(repository).acquire_work('work/a',token,purpose='reconcile')
    return RecoveryService(repository).compile_recovery(life,token=token),order


@pytest.mark.asyncio
async def test_reconciled_accepted_outcome_actually_compiles_recovery_with_fresh_proof(repository,clock):
    plan,order=await compiled_reconciliation(repository,clock)
    assert plan.entry_node=='N01'
    with repository.transaction() as tx:
        assert tx.get('repair_outcomes',order)['body']['postcheck_status']=='unknown'
        assert tx.get('recovery_contexts',plan.ref)['body']['proof_expires_at']>tx.now


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke,remove_resolution',[(True,False),(False,True)])
async def test_safety_resolution_does_not_replace_recovery_authority_or_allow_missing_proof(repository,clock,revoke,remove_resolution):
    from sandbox_graph.persistence.repository import Conflict
    with pytest.raises(Conflict,match='authority|resolution|accepted'):
        await compiled_reconciliation(repository,clock,revoke=revoke,remove_resolution=remove_resolution)
    with repository.transaction() as tx:
        assert not tx.scan('recovery_plans')


def test_unconfigured_provider_safety_intent_is_retained_with_explicit_dependency(repository):
    with repository.transaction() as tx:
        tx.put('outbox','provider/original',{'kind':'provider_reconciliation_required',
            'lifecycle_id':'life/original','task_ref':'request/original','operation_refs':['request/original']},immutable=True)
    runtime=Worker(repository,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    runtime.pump_outbox()
    with repository.transaction() as tx:
        assert tx.get('outbox_errors','provider/original')['body']['status']=='waiting_dependency'
        assert not tx.get('outbox_delivery','provider/original')


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_gap',[False,True])
async def test_site_closed_transaction_survives_outer_result_write_cut_and_actual_reclaim(repository,clock,monkeypatch,legacy_gap):
    class Cut(BaseException):
        pass
    life,order,_=await uncertain(repository)
    gateway=Runner()
    _,runtime=install(repository,life,order,gateway)
    runtime.pump_outbox()
    claimed=runtime.tasks.claim('first/owner',30)
    handler=runtime.handlers['reconcile']
    original=handler.site
    expected=[]
    async def cut_after_close(job,token):
        expected.append(await original(job,token))
        raise Cut()
    monkeypatch.setattr(handler,'site',cut_after_close)
    with pytest.raises(Cut):
        await handler(claimed)
    with repository.transaction() as tx:
        if legacy_gap:
            # 兼容旧版本已经提交 closed、却缺失外层结果的真实遗留窗口。
            tx.delete('reconciliation_job_results',claimed['job_id'])
        resolution=deepcopy(tx.get('repair_isolation_resolutions','repair-resolution:' + order))
        outcome=deepcopy(tx.get('repair_reconciliation_outcomes',order))
        charges=deepcopy(tx.scan('budget_settlements'))
        calls=deepcopy(tx.scan('reconciliation_calls'))
        assert tx.scan('lifecycle_reconciliations')[0]['body']['state']=='closed'
        assert not LifecycleSafetyService._repair_isolations_in(tx,'work/a')
    monkeypatch.setattr(handler,'site',original)
    clock.advance(31)
    assert await runtime.run_once()
    with repository.transaction() as tx:
        actual=tx.get('jobs',claimed['job_id'])['body']
        assert actual['retry_count']==2 and actual['owner_epoch']==claimed['owner_epoch']+1
        assert actual['result']==expected[0]
        assert tx.scan('lifecycle_reconciliations')[0]['body']['state']=='closed'
        assert tx.get('repair_isolation_resolutions','repair-resolution:' + order)==resolution
        assert tx.get('repair_reconciliation_outcomes',order)==outcome
        assert tx.scan('budget_settlements')==charges
        assert tx.scan('reconciliation_calls')==calls
        assert not LifecycleSafetyService._repair_isolations_in(tx,'work/a')
    assert gateway.calls==[('query','op/original','work/a')]


@pytest.mark.asyncio
async def test_provider_intent_before_original_job_finish_waits_without_blocking_other_outbox(repository,tmp_path):
    from tests.orchestration.unit.test_agent_team import prepared,release_parent,runner
    from tests.orchestration.unit.test_agent_bridge import ControlledProvider,capabilities,scope,receipt
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    team,_,record,parent,token=prepared(repository,tmp_path)
    target=team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    provider=ControlledProvider(capabilities(scopes=(scope('work/a'),)))
    role_runtime=runner(repository,team,provider)
    role_runtime.pump_outbox()
    claimed=role_runtime.tasks.claim('original/owner',30)
    role_result=await role_runtime.handlers['agent_team'](claimed)
    intent='provider-reconciliation:' + target
    service=LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1,cost_unit='microunits',query_cost=1,stop_cost=1,cleanup_cost=1,backoff_seconds=1),
        authorizations={})
    runtime=Worker(repository,{'reconcile':LifecycleReconciliationHandler(service,agent_provider=provider,lease_seconds=30)},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    with repository.transaction() as tx:
        tx.put('outbox','independent/ready',{'kind':'job_ready','job_id':'independent'},immutable=True)
        original_blocker=deepcopy(tx.get('lifecycle_blockers',record.key.lifecycle_id))
        original_owners=deepcopy(tx.get('lifecycle_owners',record.key.lifecycle_id))
    runtime.pump_outbox()
    with repository.transaction() as tx:
        assert tx.get('jobs',target)['body']['status']=='running'
        assert tx.get('agent_team_tasks',target)['body']['status']=='waiting_provider'
        assert tx.get('lifecycle_owners',record.key.lifecycle_id)==original_owners
        assert tx.get('lifecycle_blockers',record.key.lifecycle_id)==original_blocker
        assert tx.get('outbox_errors',intent)['body']['status']=='waiting_dependency'
        assert tx.get('outbox_errors',intent)['body']['reason']=='original_provider_job_not_finished'
        assert not tx.get('outbox_delivery',intent)
        assert tx.get('outbox_delivery','independent/ready')
        assert not tx.scan('lifecycle_reconciliations')
        assert not tx.scan('reconciliation_calls')
    role_runtime.tasks.finish(target,owner_epoch=claimed['owner_epoch'],result=role_result,status='blocked')
    # 原 job 结束本身仍不授予安全调用；必须取得独立精确安全授权。
    service.authorize('handoff/auth',operation_kind='provider',target_ref=target,
        principal='operator',expires_at=1140.0,permissions=('query','stop'))
    service.authorizations[target]='handoff/auth'
    provider.current=receipt(provider.submissions[0],state='cancelled',children_stopped=True,receipt_id='handoff/terminal')
    assert await runtime.run_once()
    with repository.transaction() as tx:
        assert tx.get('agent_team_reconciliations',target)
        assert tx.get('outbox_delivery',intent)
    assert len(provider.submissions)==1


@pytest.mark.parametrize('body',[
    {'kind':'provider_reconciliation_required','lifecycle_id':'not/original',
     'task_ref':'missing/request','operation_refs':['missing/request']},
    {'kind':'repair_reconciliation_required','lifecycle_id':'not/original',
     'order_ref':'missing/order','operation_refs':['missing/operation']},
    {'kind':'lifecycle_safety_continue','lifecycle_id':'not/original',
     'reconciliation_ref':'missing/session','round':1},
])
def test_bad_reconciliation_target_is_contained_per_outbox_item(repository,body):
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    service=LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1,cost_unit='test_units',query_cost=1,stop_cost=1,cleanup_cost=1,backoff_seconds=1),
        authorizations={})
    with repository.transaction() as tx:
        tx.put('outbox','bad/provider',body,immutable=True)
        tx.put('outbox','independent/ready',{'kind':'job_ready','job_id':'independent'},immutable=True)
    runtime=Worker(repository,{},reconciliation=service,
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    runtime.pump_outbox()
    with repository.transaction() as tx:
        assert tx.get('outbox_errors','bad/provider')['body']['status']=='blocked'
        assert not tx.get('outbox_delivery','bad/provider')
        assert tx.get('outbox_delivery','independent/ready')
        assert not tx.scan('lifecycle_reconciliations')


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_gap',[False,True])
async def test_provider_close_commit_reclaim_returns_original_fact_without_rewriting_task(repository,tmp_path,clock,monkeypatch,legacy_gap):
    from tests.orchestration.unit.test_agent_team import prepared,release_parent,runner
    from tests.orchestration.unit.test_agent_bridge import ControlledProvider,capabilities,scope,receipt
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    class Cut(BaseException):
        pass
    team,_,record,parent,token=prepared(repository,tmp_path)
    target=team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    provider=ControlledProvider(capabilities(scopes=(scope('work/a'),)))
    await runner(repository,team,provider).run_once()
    service=LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1,cost_unit='microunits',query_cost=1,stop_cost=1,cleanup_cost=1,backoff_seconds=1),
        authorizations={target:'safety/auth'})
    service.authorize('safety/auth',operation_kind='provider',target_ref=target,
        principal='operator',expires_at=1140.0,permissions=('query','stop'))
    handler=LifecycleReconciliationHandler(service,agent_provider=provider,lease_seconds=30)
    runtime=Worker(repository,{'reconcile':handler},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    provider.current=receipt(provider.submissions[0],state='cancelled',children_stopped=True,receipt_id='closed/provider')
    runtime.pump_outbox()
    claimed=runtime.tasks.claim('first/owner',30)
    original=handler.provider
    expected=[]
    async def cut_after_close(job,token):
        expected.append(await original(job,token))
        raise Cut()
    monkeypatch.setattr(handler,'provider',cut_after_close)
    with pytest.raises(Cut):
        await handler(claimed)
    with repository.transaction() as tx:
        if legacy_gap:
            tx.delete('reconciliation_job_results',claimed['job_id'])
        task=deepcopy(tx.get('agent_team_tasks',target))
        fact=deepcopy(tx.get('agent_team_reconciliations',target))
        session=deepcopy(tx.scan('lifecycle_reconciliations'))
        charges=deepcopy(tx.scan('budget_settlements'))
        calls=deepcopy(tx.scan('reconciliation_calls'))
    monkeypatch.setattr(handler,'provider',original)
    clock.advance(31)
    assert await runtime.run_once()
    with repository.transaction() as tx:
        actual=tx.get('jobs',claimed['job_id'])['body']
        assert actual['retry_count']==2
        assert actual['result']==expected[0]
        assert tx.get('agent_team_tasks',target)==task
        assert tx.get('agent_team_reconciliations',target)==fact
        assert tx.scan('lifecycle_reconciliations')==session
        assert tx.scan('budget_settlements')==charges
        assert tx.scan('reconciliation_calls')==calls


@pytest.mark.asyncio
async def test_closure_rolls_back_if_atomic_completion_result_cannot_be_saved(repository,monkeypatch):
    from tests.orchestration.conftest import MemoryTransaction
    life,order,_=await uncertain(repository)
    _,runtime=install(repository,life,order,Runner())
    original=MemoryTransaction.put
    def cut(tx,kind,key,body,*args,**kwargs):
        if kind=='reconciliation_job_results':
            raise RuntimeError('关闭结果写入失败')
        return original(tx,kind,key,body,*args,**kwargs)
    monkeypatch.setattr(MemoryTransaction,'put',cut)
    assert await runtime.run_once()
    with repository.transaction() as tx:
        assert not tx.get('repair_isolation_resolutions','repair-resolution:' + order)
        assert not tx.get('repair_reconciliation_outcomes',order)
        assert tx.scan('lifecycle_reconciliations')[0]['body']['state']!='closed'
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')


@pytest.mark.asyncio
async def test_closed_session_missing_result_does_not_bypass_resolution_validation(repository,clock):
    life,order,_=await uncertain(repository)
    gateway=Runner()
    _,runtime=install(repository,life,order,gateway)
    runtime.pump_outbox()
    claimed=runtime.tasks.claim('first/owner',30)
    await runtime.handlers['reconcile'](claimed)
    with repository.transaction() as tx:
        tx.delete('reconciliation_job_results',claimed['job_id'])
        tx.delete('repair_isolation_resolutions','repair-resolution:' + order)
    clock.advance(31)
    assert await runtime.run_once()
    with repository.transaction() as tx:
        assert tx.get('jobs',claimed['job_id'])['body']['status']=='failed'
        assert not tx.get('reconciliation_job_results',claimed['job_id'])
        assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
    assert gateway.calls==[('query','op/original','work/a')]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['binding','schema'])
async def test_provider_identity_permission_failure_does_not_abort_shared_pump(repository,tmp_path,failure):
    from tests.orchestration.unit.test_agent_team import prepared,release_parent,runner
    from tests.orchestration.unit.test_agent_bridge import ControlledProvider,capabilities,scope
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    team,_,record,parent,token=prepared(repository,tmp_path)
    target=team.request_role(parent,token=token,role='investigator')
    release_parent(repository,parent,token)
    provider=ControlledProvider(capabilities(scopes=(scope('work/a'),)))
    await runner(repository,team,provider).run_once()
    with repository.transaction() as tx:
        row=tx.get('agent_provider_requests',target)
        damaged=row['body'] | {'provider_id':'other/provider'}
        if failure=='schema':
            damaged.pop('envelope')
        tx.put('agent_provider_requests',target,damaged)
        tx.put('outbox','independent/ready',{'kind':'job_ready','job_id':'independent'},immutable=True)
        original_owners=deepcopy(tx.get('lifecycle_owners',record.key.lifecycle_id))
    service=LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1,cost_unit='microunits',query_cost=1,stop_cost=1,cleanup_cost=1,backoff_seconds=1),
        authorizations={})
    runtime=Worker(repository,{},reconciliation=service,
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    runtime.pump_outbox()
    with repository.transaction() as tx:
        assert tx.get('outbox_errors','provider-reconciliation:' + target)['body']['status']=='blocked'
        assert tx.get('outbox_delivery','independent/ready')
        assert not tx.scan('lifecycle_reconciliations')
        assert not tx.scan('reconciliation_calls')
        assert tx.get('lifecycle_owners',record.key.lifecycle_id)==original_owners


@pytest.mark.asyncio
@pytest.mark.parametrize('restriction',['query_only','stop_exhausted','allowed','stop_timeout','query_exhausted'])
async def test_sampler_session_cancel_cannot_spend_query_as_first_native_stop(repository,tmp_path,restriction):
    """Fault fixture discovers a real running task behind the original unknown ID.

    Its gateway inventory is recovered explicitly; this is not an ordinary new
    measurement authorization or a synthetic stopped receipt.
    """
    from sandbox_bench.bridge import BenchGateway
    from sandbox_bench.session import BenchSession
    from sandbox_bench.contracts import CaseContract
    from sandbox_graph.domain.models import Operation,ScriptRef,canonical_hash
    from sandbox_graph.runtime.leases import LeaseService
    from tests.benchmark.unit.test_contract_models import sample
    from tests.benchmark.unit.test_session import Scenario
    from tests.benchmark.unit.test_sampler_lifecycle import ControlledSampler
    life,order,_=await uncertain(repository)
    body=sample();body.update(work_id='work/a',group_id='group/a',plan_ref='plan/a',
        graph_ref='graph/a',contract_ref='contract/a',cleanup_timeout_s=.05,
        resources=dict(interval_s=.01,window_s=.1,max_wait_s=1.,max_gap_s=.1,min_samples=1,inventory_ref='fault-fixture'))
    delayed=restriction in {'stop_timeout','query_exhausted'}
    sampler=ControlledSampler(block_at=2 if delayed else None)
    bench=BenchSession(CaseContract.model_validate(body),
        {'product':'kuasar','api_url':'http://localhost:1','data_url':'http://localhost:1'},
        tmp_path,scenario=Scenario(),sampler=sampler,clock_domain='host-clock')
    script=ScriptRef(script_id='benchmark.step',version='1',digest='a'*64)
    gateway=BenchGateway({'work/a':bench},script)
    token=LeaseService(repository).acquire_thread('fixture-inventory','fixture',50)
    LifecycleSafetyService(repository).acquire_work('work/a',token,purpose='reconcile')
    payload={'step_id':'N04','case_hash':bench.case.digest()}
    op=Operation(operation_id='op/original',input_hash=canonical_hash(payload),input=payload,
        script_ref=script,scope='work/a',run_id='run/a',node_id='N04',deadline=1090)
    with repository.transaction() as tx:
        work=tx.get('work_instances','work/a')['body']
        binding={key:work[key] for key in ('run_id','group_id','plan_ref','graph_ref','contract_ref')}
        binding.update(scope='work/a',work_id='work/a',measurement_generation=1,
            owner=LifecycleSafetyService._owner(token),job_refs=[])
        binding=gateway.bind_execution(op,binding)
        original=tx.get('operations','op/original')['body']
        tx.put('operations','op/original',original | {'request':op.model_dump(mode='json'),
            'request_hash':canonical_hash(op),'execution_binding':binding})
    assert (await gateway.submit(op)).state=='completed'
    await OperationService(repository,gateway,{}).collect('op/original','work/a',token)
    if delayed:
        import asyncio
        for _ in range(100):
            if sampler.entered.is_set():break
            await asyncio.sleep(.002)
        assert sampler.entered.is_set()
    await bench.cancel('unrelated session admission cancellation')
    LifecycleSafetyService(repository).release_work('work/a',token)
    with repository.transaction() as tx:tx.release_thread(token)
    service,runtime=install(repository,life,order,gateway)
    runtime.pump_outbox()
    with repository.transaction() as tx:
        if restriction=='query_only':
            auth=tx.get('reconciliation_authorizations','safety/auth')
            tx.put('reconciliation_authorizations','safety/auth',auth['body'] | {'permissions':['query']})
        elif restriction=='stop_exhausted':
            LifecycleBudgetService.reserve_in(tx,request_id='prior-stop-spend',lifecycle_id=life,
                input_ref='prior-original-stop',amounts={'stop_calls':2,'cost':2},safety=True,operation_kind='stop')
            LifecycleBudgetService.settle_in(tx,request_id='prior-stop-spend')
    try:
        with repository.transaction() as tx:
            original_outcome=deepcopy(tx.get('repair_outcomes',order))
            original_receipt=deepcopy(bench.journal.receipt('N04:op/original',op.input_hash))
        assert await runtime.run_once()
        if delayed:
            with repository.transaction() as tx:
                session_ref=tx.scan('lifecycle_reconciliations')[0]['key']
                assert tx.get('lifecycle_reconciliations',session_ref)['body']['state']=='blocked'
                assert tx.get('operations','op/original')['body']['status']['state']=='completed'
                assert len([r for r in tx.scan('reconciliation_calls') if r['body']['kind']=='stop'])==1
                assert len([r for r in tx.scan('reconciliation_calls') if r['body']['kind']=='query'])==1
            close=bench._sampler_close_future
            sampler.release.set()
            for _ in range(100):
                if bench._sampler_shutdown_future.done():break
                await asyncio.sleep(.002)
            assert bench._sampler_shutdown_future.result() is True
            with repository.transaction() as tx:
                if restriction=='query_exhausted':
                    LifecycleBudgetService.reserve_in(tx,request_id='prior-query-spend',lifecycle_id=life,
                        input_ref='prior-original-query',amounts={'query_calls':1,'cost':1},safety=True,operation_kind='query')
                    LifecycleBudgetService.settle_in(tx,request_id='prior-query-spend')
            service.resume(session_ref)
            assert await runtime.run_once()
            with repository.transaction() as tx:
                from sandbox_graph.runtime.observation import ObservationService
                closed=ObservationService.closed_in(tx,tx.scan('observations')[0])
                assert closed is (restriction=='stop_timeout')
                queries=[r for r in tx.scan('reconciliation_calls') if r['body']['kind']=='query']
                assert len(queries)==(2 if restriction=='stop_timeout' else 1)
                assert len([r for r in tx.scan('reconciliation_calls') if r['body']['kind']=='stop'])==1
                assert all(b['safety_spent']['query_calls']==2 and b['safety_spent']['stop_calls']==1
                    for b in LifecycleBudgetService.snapshot_in(tx,life).values())
                assert bool(LifecycleSafetyService._repair_isolations_in(tx,'work/a')) is (restriction=='query_exhausted')
                assert tx.get('repair_outcomes',order)==original_outcome
                assert tx.get('operations','op/original')['body']['status']['state']=='completed'
            assert bench._sampler_close_future is close
            assert bench.journal.receipt('N04:op/original',op.input_hash)==original_receipt
            assert sampler.calls==2
            return
        if restriction=='allowed':
            assert sampler.closed
            with repository.transaction() as tx:
                calls=[r for r in tx.scan('reconciliation_calls') if r['body']['kind']=='stop']
                assert len(calls)==1
                intent=tx.get('sampler_stop_intents','op/original')['body']
                assert intent['authority']['call_ref']==calls[0]['key']
                assert all(b['safety_spent']['stop_calls']==1 for b in LifecycleBudgetService.snapshot_in(tx,life).values())
                assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')==[]
            return
        assert not sampler.closed
        assert bench._sampler_close_future is None
        with repository.transaction() as tx:
            assert not tx.scan('sampler_stop_intents')
            assert not [r for r in tx.scan('reconciliation_calls') if r['body']['kind']=='stop']
            budget=LifecycleBudgetService.snapshot_in(tx,life)
            assert all(b['safety_spent']['query_calls']==1 for b in budget.values())
            assert all(b['safety_spent']['stop_calls']==(2 if restriction=='stop_exhausted' else 0) for b in budget.values())
            assert LifecycleSafetyService._repair_isolations_in(tx,'work/a')
    finally:
        sampler.release.set()
        await bench._N07()
