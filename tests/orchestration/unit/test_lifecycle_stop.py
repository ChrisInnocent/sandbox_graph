"""Run stop/supersede retains original safety work and all lifecycle scopes."""
import pytest

from sandbox_graph.domain.models import RunRecord
from sandbox_graph.interfaces.service import RunService
from sandbox_graph.planning.compiler import PlanCompiler
from sandbox_graph.services.revisions import RevisionService


def setup_run(repository):
    RevisionService(repository).create(RunRecord(run_id='r', root_thread_id='root',
        graph_ref='g', active_plan_ref='p', contract_ref='c', auth_ref='auth', lineage_id='lineage'))


@pytest.mark.parametrize('policy', ['drain', 'cancel'])
@pytest.mark.parametrize('kind', ['cleanup', 'reconcile', 'measurement-v1:cleanup', 'measurement-v1:reconcile'])
def test_revision_keeps_every_versioned_safety_job(repository, policy, kind):
    setup_run(repository)
    with repository.transaction() as tx:
        tx.put('jobs', 'safety', {'run_id':'r','scope_ref':'group','job_kind':kind,'status':'pending'})
    RevisionService(repository).activate_revision(request_id='rev', predecessor_run_id='r',
        expected_revision=0, candidate_plan_hash='next', graph_ref='g2', disposition_policy=policy)
    with repository.transaction() as tx:
        assert tx.get('jobs', 'safety')['body']['status'] == 'pending'


@pytest.mark.parametrize('command', ['cancel', 'revision'])
def test_stop_records_all_scopes_without_releasing_unknown_effects(repository, command):
    setup_run(repository)
    # Scope inventory is intentionally independent of the operation ledger.
    with repository.transaction() as tx:
        for kind, scope in [('run','r'), ('group','shared-group'), ('work','work'), ('node','node')]:
            tx.put('agent_lifecycles', 'life:' + kind, {'key':{'run_ref':'r',
                'scope_kind':kind,'scope_ref':scope,'lifecycle_id':'life:' + kind},
                'phase':'waiting_dependency','plan_ref':'p','graph_ref':'g','revision':3})
        tx.put('agent_lifecycles', 'other-life', {'key':{'run_ref':'other',
            'scope_kind':'group','scope_ref':'shared-group','lifecycle_id':'other-life'},
            'phase':'waiting_dependency','plan_ref':'p','graph_ref':'g','revision':3})
        tx.put('repair_isolations', 'quarantine', {'work_ref':'work','operation_refs':['unknown']})
        tx.put('lifecycle_budgets', 'budget', {'spent':{'child_jobs':2}})
    service = RunService(repository, PlanCompiler([]))
    if command == 'cancel':
        invoke = lambda: service.cancel('stop', 'r', 'requested stop')
    else:
        invoke = lambda: RevisionService(repository).activate_revision(request_id='stop',
            predecessor_run_id='r', expected_revision=0, candidate_plan_hash='next',
            graph_ref='g2', disposition_policy='cancel')
    invoke()
    invoke()
    with repository.transaction() as tx:
        stops = tx.scan('lifecycle_stop_requests')
        assert {row['body']['lifecycle_id'] for row in stops} == {'life:run','life:group','life:work','life:node'}
        assert all(row['body']['plan_ref']=='p' and row['body']['graph_ref']=='g' for row in stops)
        assert tx.get('repair_isolations', 'quarantine')
        assert tx.get('lifecycle_budgets', 'budget')['body']['spent']['child_jobs'] == 2
        assert tx.get('agent_lifecycles', 'life:work')['body']['phase'] == 'waiting_dependency'
        assert not tx.get('lifecycle_stop_requests', 'other-life')
        assert len([r for r in tx.scan('outbox') if r['body']['kind']=='lifecycle_stop_requested']) == 4


@pytest.mark.asyncio
async def test_cancel_reaches_actual_failed_work_lifecycle_without_running_pending_repair(repository):
    from sandbox_graph.runtime.tasks import TaskBackend
    from tests.orchestration.graphs.test_work_remediation import assembly, drain
    worker, _, _, repairs = assembly(repository, failure='N09')
    assert await worker.run_once()  # original graph and finalization
    worker.pump_outbox()
    assert await worker.run_once()  # lifecycle diagnosis, no repair executed yet
    assert await worker.run_once()  # commit selected action and pending repair
    with repository.transaction() as tx:
        assert len(tx.scan('agent_lifecycles')) == 1
        assert tx.scan('agent_lifecycles')[0]['body']['phase'] == 'remediation_pending'
        jobs_before = len(tx.scan('repair_orders'))
    RunService(repository, PlanCompiler([])).cancel('cancel-real', 'run/a', 'stop before repair')
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase'] == 'cancelled'
        assert len(tx.scan('repair_orders')) == jobs_before
        assert tx.scan('lifecycle_stop_status')[0]['body']['status'] == 'closed'
        assert tx.scan('lifecycle_stop_proofs')
    assert repairs == []


@pytest.mark.asyncio
async def test_cancel_unknown_site_keeps_original_reconciler_and_isolation(repository):
    from tests.orchestration.unit.test_lifecycle_reconciliation import uncertain, install, Runner
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    life, order, _ = await uncertain(repository)
    RunService(repository, PlanCompiler([])).cancel('stop-unknown', 'run/a', 'stop')
    _, runtime = install(repository, life, order, Runner())
    from sandbox_graph.runtime.worker import RecoveryJobHandler
    runtime.handlers['cleanup']=RecoveryJobHandler(repository,object(),lease_seconds=30)
    runtime.pump_outbox()
    with repository.transaction() as tx:
        # Delivery only schedules the bounded stop coordinator.
        assert LifecycleService.load_in(tx, life).terminal_intent is None
        assert any(r['body']['job_kind']=='lifecycle_stop' for r in tx.scan('jobs'))
        assert tx.scan('repair_isolations')
    from tests.orchestration.graphs.test_work_remediation import drain
    await drain(runtime)
    with repository.transaction() as tx:
        assert LifecycleService.load_in(tx, life).phase == 'cancelled'
        assert not [r for r in tx.scan('outbox') if r['body']['kind']=='repair_recovery_required']
        assert not [r for r in tx.scan('jobs') if r['body']['dedupe_key'].startswith('run-cleanup:')]


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger,entry', [
    ('resources',{'scope':'work/a','status':'active'}),
    ('observations',{'scope':'work/a','status':'running'}),
    ('resource_leases',{'scope':'work/a','run_id':'run/a','status':'quarantine'}),
])
async def test_cancel_does_not_drop_unlinked_resource_or_sampler(repository, ledger, entry):
    from tests.orchestration.graphs.test_work_remediation import assembly, drain
    worker, _, _, _ = assembly(repository, failure='N09')
    for _ in range(3):
        assert await worker.run_once()
        worker.pump_outbox()
    with repository.transaction() as tx:
        tx.put(ledger,'unlinked',entry)
    RunService(repository, PlanCompiler([])).cancel('stop-unlinked','run/a','stop')
    await drain(worker)
    with repository.transaction() as tx:
        assert tx.scan('agent_lifecycles')[0]['body']['phase'] != 'cancelled'
        assert tx.scan('lifecycle_stop_status')[0]['body']['status']=='waiting_dependency'
        assert tx.get(ledger,'unlinked')


@pytest.mark.asyncio
@pytest.mark.parametrize('certainty', ['none','unknown'])
@pytest.mark.parametrize('node,scope', [('E1','shared'),('R0','r'),('A3','r'),('F1','r')])
async def test_quiescent_group_run_analysis_report_lifecycles_cancel(repository, node, scope, certainty):
    from sandbox_graph.domain.models import NodeResult, ErrorRecord
    from sandbox_graph.graphs.executor import NodeExecutor
    from sandbox_graph.runtime.worker import Worker, WorkerPolicy
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle
    service, original_token = setup_lifecycle(repository)
    with repository.transaction() as tx:
        policy = tx.get('repair_policies','policy/a')['body']
        tx.release_thread(original_token)
        tx.put('groups','r:shared',{'run_id':'r','group_id':'shared','plan_ref':'p'})
    setup_run(repository)
    async def fail(context):
        return NodeResult(execution_status='failed',error=ErrorRecord(code='INPUT_MISSING',observed_at=1000,effect_certainty=certainty))
    with repository.transaction() as tx:
        token=tx.acquire_thread('source-thread','node',30)
    executor=NodeExecutor(repository,{node:fail},token)
    _, ref=await executor.execute(node,{'thread_id':token.thread_id,'scope':scope,'run_ref':'r',
        'plan_ref':'p','graph_ref':'g','activation_id':'original','input_refs':{}})
    life=service.observe_failure(ref,policy_ref='policy/a',token=token)
    with repository.transaction() as tx:
        tx.release_thread(token)
    RunService(repository,PlanCompiler([])).cancel('stop-scopes','r','stop')
    runtime=Worker(repository,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    # The run-wide cleanup job is independent; consume its unsupported-adapter
    # result before asserting this pure control lifecycle's terminal state.
    from tests.orchestration.graphs.test_work_remediation import drain
    await drain(runtime)
    with repository.transaction() as tx:
        phase=service.load_in(tx,life.key.lifecycle_id).phase
        if certainty=='none':
            assert phase=='cancelled', tx.scan('lifecycle_stop_status')
        else:
            assert phase!='cancelled'
            assert tx.scan('lifecycle_stop_status')[0]['body']['status']=='waiting_dependency'


@pytest.mark.asyncio
@pytest.mark.parametrize('remote_state', ['running', 'cancelled'])
async def test_cancel_waits_for_original_provider_children_without_resubmission(repository,tmp_path,clock,remote_state):
    from tests.orchestration.unit.test_agent_team import prepared,release_parent,runner
    from tests.orchestration.unit.test_agent_bridge import ControlledProvider,capabilities,scope,receipt
    from tests.orchestration.graphs.test_work_remediation import drain
    from sandbox_graph.runtime.agent_bridge import CancelReply
    from sandbox_graph.services.lifecycle_reconciliation import LifecycleReconciliationService,ReconciliationConfig
    from sandbox_graph.runtime.lifecycle_reconciliation import LifecycleReconciliationHandler
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
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
    provider.lose_submit=True
    provider.unknown_lookup=True
    await runner(repository,team,provider).run_once()
    provider.unknown_lookup=False
    # A terminal parent state is insufficient while its children still run.
    provider.saved = receipt(provider.submissions[0], state=remote_state,
                             children_stopped=False, receipt_id='children-still-running')
    service=LifecycleReconciliationService(repository,
        config=ReconciliationConfig(schema_version=1,cost_unit='microunits',query_cost=1,
            stop_cost=1,cleanup_cost=1,backoff_seconds=1),authorizations={record.key.lifecycle_id:'safety/auth'})
    service.authorize('safety/auth',operation_kind='provider',target_ref=target,
        principal='operator',expires_at=1140.0,permissions=('query','stop'))
    with repository.transaction() as tx:
        run=tx.get('runs','run/a')['body']
        tx.put('dispatch_gates','run/a',{'open':True,'generation':run['dispatch_generation']})
    RunService(repository,PlanCompiler([])).cancel('provider-stop','run/a','stop unknown provider')
    with repository.transaction() as tx:
        # The shared context fixture contains both N02 and N09 lifecycles.
        # Select this provider's original lifecycle, not the first ledger row.
        requests = [row for row in tx.scan('lifecycle_stop_requests')
                    if row['body']['lifecycle_id'] == record.key.lifecycle_id
                    and row['body']['request_id'] == 'cancel:provider-stop']
        assert len(requests) == 1
        stop_ref = requests[0]['key']
    runtime=Worker(repository,{'reconcile':LifecycleReconciliationHandler(service,agent_provider=provider,lease_seconds=30)},
        policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=0.01))
    await drain(runtime)
    with repository.transaction() as tx:
        assert tx.get('agent_team_tasks',target)['body']['status']=='waiting_provider'
        status = tx.get('lifecycle_stop_status', stop_ref)['body']
        assert status['status']=='waiting_dependency', status
        assert target in status['remaining']['providers']
        assert tx.get('agent_provider_requests',target)['body']['cancel_accepted'] is True
        assert tx.get('agent_lifecycles',record.key.lifecycle_id)['body']['phase'] != 'cancelled'
        assert not [row for row in tx.scan('lifecycle_stop_proofs')
                    if row['body']['request_ref'] == stop_ref]
        source_before = tx.get('lifecycle_sources', record.key.lifecycle_id)['body']
    provider.current=receipt(provider.submissions[0],state='cancelled',children_stopped=True,receipt_id='original-children-stopped')
    clock.advance(1)
    await drain(runtime)
    assert len(provider.submissions)==1 and provider.stops==1
    with repository.transaction() as tx:
        assert tx.get('agent_team_tasks',target)['body']['status']=='reconciled'
        assert tx.get('agent_lifecycles',record.key.lifecycle_id)['body']['terminal_intent']=='cancelled'
        assert tx.get('agent_lifecycles',record.key.lifecycle_id)['body']['phase']=='cancelled'
        assert tx.get('lifecycle_stop_status', stop_ref)['body']['status'] == 'closed'
        assert tx.get('lifecycle_sources', record.key.lifecycle_id)['body'] == source_before
        assert not tx.scan('repair_orders')
        assert not [row for row in tx.scan('outbox')
                    if row['body']['kind'] == 'repair_recovery_required']
