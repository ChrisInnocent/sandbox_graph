"""ALU-10/11: recovery is derived from frozen receipts and current ledgers."""
from copy import deepcopy

import pytest

from sandbox_graph.domain.models import NodeResult
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.services.agent_lifecycle import LifecycleService
from sandbox_graph.services.lifecycle_safety import LifecycleSafetyService
from sandbox_graph.services.recovery import RecoveryService
from tests.orchestration.unit.test_repair_execution import modern_job, closed_operation, worker


async def recovery_ready(repository, *, node='N01', measured=False, performance='not_evaluated'):
    request, order, job = modern_job(repository)
    closed_operation(repository)
    with repository.transaction() as tx:
        tx.put('operations', 'op/a', tx.get('operations', 'op/a')['body'] | {'request':{'node_id':'N01'}})
    async def action(context):
        return NodeResult(execution_status='succeeded', operation_refs=('op/a',))
    await worker(repository, action)(job)
    with repository.transaction() as tx:
        token = tx.acquire_thread('recovery-owner', 'scheduler', 90)
        # A new lifecycle owner and work owner must establish their own proof.
        tx.put('lifecycle_owners', request.lifecycle_id, LifecycleService._owner_body(token))
        LifecycleSafetyService.acquire_work_in(tx, 'work/a', token, purpose='recovery')
        tx.put('contracts', 'contract/a', {'schema_version': 1, 'assertions': ['fixed']}, immutable=True)
        source = tx.get('receipts', 'receipt/a')['body']
        source['identity']['node_id'] = node
        source['result']['performance_status'] = performance
        if measured and node in {'N04','N05','N06'}:
            source['result']['data_status']='insufficient'
        tx.put('receipts', 'receipt/a', source)
        life = tx.get('agent_lifecycles', request.lifecycle_id)['body']
        life['key']['node_id'] = node
        tx.put('agent_lifecycles', request.lifecycle_id, life)
        frozen = tx.get('repair_orders', order['order_ref'])['body']
        frozen['node_id'] = node
        if node in {'N07','N09'}:
            frozen['action_ref'] = {'N07':'evidence.seal@1','N09':'resource.cleanup@1'}[node]
            auth=tx.get('repair_authorizations',frozen['authorization_ref'])
            tx.put('repair_authorizations',auth['key'],auth['body'] | {'actions':[frozen['action_ref']]})
            frozen['authorization_revision']=auth['revision']+1
        repair_ref = tx.get('repair_outcomes', order['order_ref'])['body']['return_result']['output']['receipt_ref']
        repair_body = tx.get('receipts', repair_ref)['body']
        repair_body['identity']['node_id'] = node
        tx.put('receipts', repair_ref, repair_body)
        refs = {}
        for n in ('N00', 'N01', 'N02', 'N03') + (('N04', 'N05', 'N06', 'N07') if measured else ()):
            identity = source['identity'] | {'node_id': n}
            result = NodeResult(execution_status='succeeded', data_status='valid',
                                artifact_refs=('raw/original',) if n == 'N07' else (),
                                performance_status=performance if n == 'N06' else 'not_evaluated')
            ref = f'original/{n}'
            tx.put('receipts', ref, {'identity': identity, 'result': result.model_dump(mode='json')}, immutable=True)
            refs[n] = ref
        frozen['result_refs'] = refs
        tx.put('repair_orders', order['order_ref'], frozen)
        src = tx.get('lifecycle_sources', request.lifecycle_id)['body']
        src['result_refs'] = refs
        tx.put('lifecycle_sources', request.lifecycle_id, src)
    return request.lifecycle_id, token, order['order_ref']


@pytest.mark.asyncio
async def test_destroyed_fixture_invalidates_resume(repository):
    lifecycle, token, order = await recovery_ready(repository)
    before = deepcopy(repository.rows[('repair_orders', order)])
    plan = RecoveryService(repository).compile_recovery(lifecycle, token=token)
    assert plan.entry_node == 'N01'
    assert plan.revalidate_nodes == ('N01', 'N02', 'N03')
    with repository.transaction() as tx:
        context = tx.get('recovery_contexts', plan.ref)['body']
        assert context['input_refs'] == {'fixture': 'fixture/a'}
        assert context['result_refs'] == {'N00': 'original/N00'}
        assert set(plan.invalidated_refs) >= {'original/N01', 'original/N02', 'original/N03'}
    assert repository.rows[('repair_orders', order)] == before
    assert RecoveryService(repository).compile_recovery(lifecycle, token=token) == plan


@pytest.mark.asyncio
@pytest.mark.parametrize('node', ['N04', 'N05', 'N06', 'N07', 'N08', 'N09', 'N10'])
async def test_measurement_continuity_and_generation(repository, node):
    lifecycle, token, _ = await recovery_ready(repository, node=node, measured=True, performance='fail')
    plan = RecoveryService(repository).compile_recovery(lifecycle, token=token)
    assert plan.entry_node == 'N07'
    assert plan.measurement_generation == 1
    with repository.transaction() as tx:
        context = tx.get('recovery_contexts', plan.ref)['body']
        assert not set(context['route']) & {'N04', 'N05', 'N06'}
        assert context['result_refs']['N05'] == ('receipt/a' if node == 'N05' else 'original/N05')
        assert context['measurement_blocked'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['contract', 'graph', 'work', 'input', 'ttl'])
async def test_startup_rejects_changed_plan_dependencies(repository, clock, mutation):
    lifecycle, token, _ = await recovery_ready(repository)
    service = RecoveryService(repository)
    plan = service.compile_recovery(lifecycle, token=token)
    with repository.transaction() as tx:
        if mutation == 'contract': tx.put('contracts', 'contract/a', {'assertions': ['changed']})
        elif mutation == 'graph': tx.put('graph_definitions', 'graph/a', {'nodes': ['N05']})
        elif mutation == 'work': tx.put('work_instances', 'work/a', tx.get('work_instances', 'work/a')['body'])
        elif mutation == 'input':
            row = tx.get('recovery_contexts', plan.ref)['body']
            tx.put('recovery_contexts', plan.ref, row | {'input_refs': {'fixture': 'other'}})
        else: clock.advance(31)
    with repository.transaction() as tx, pytest.raises(Conflict):
        service.revalidate_in(tx, plan.ref, scope='work/a', run_ref='run/a', plan_ref='plan/a',
            graph_ref='graph/a', input_refs={'fixture': 'fixture/a'}, activation_id='new', token=token)


@pytest.mark.asyncio
async def test_compile_requires_real_lifecycle_and_shared_work_owner(repository):
    lifecycle, token, _ = await recovery_ready(repository)
    with repository.transaction() as tx:
        other = tx.acquire_thread('other', 'other', 60)
    with pytest.raises(Conflict):
        RecoveryService(repository).compile_recovery(lifecycle, token=other)


@pytest.mark.asyncio
async def test_legacy_remedy_respects_live_work_mutex_and_never_numeric_continues():
    from sandbox_graph.runtime.worker import RemedyJobHandler
    from tests.orchestration.unit.test_remedy_leases import setup_job
    repo, job = setup_job()
    with repo.transaction() as tx:
        old = tx.get('remedy_orders','legacy')['body']
        tx.put('remedy_orders','legacy',old | {'node_id':'N01'})
        tx.put('work_instances','w',{'work_id':'w','run_id':'r','plan_ref':'p','graph_ref':'g','contract_ref':'c'})
        original = tx.acquire_thread('original/work','graph',80)
        LifecycleSafetyService.acquire_work_in(tx,'w',original)
    calls=[]
    async def action(context):
        calls.append(context)
        with repo.transaction() as tx:
            owner=tx.get('work_execution_leases','w')['body']['owner']
            assert owner['thread_id'] == job['thread_id']
            assert owner['lease_id'] == context.lease_id
        return NodeResult(execution_status='succeeded')
    handler=RemedyJobHandler(repo,lambda token,activation:{'N01':action},lease_seconds=30,segment_seconds=20)
    with pytest.raises(Conflict): await handler(job)
    assert calls == []
    with repo.transaction() as tx:
        assert tx.get('work_execution_leases','w')['body']['owner']['thread_id'] == 'original/work'
        LifecycleSafetyService.release_work_in(tx,'w',original)
        tx.release_thread(original)
    result=await handler(job)
    assert result['execution_status'] == 'succeeded'
    assert result['output']['reason'] == 'lifecycle_contract_required'
    assert 'resume_from' not in result['output']
    with repo.transaction() as tx:
        assert not tx.get('work_execution_leases','w')
        assert not [r for r in tx.scan('jobs') if r['body']['job_kind']=='work']


@pytest.mark.asyncio
@pytest.mark.parametrize('fixture_state', ['deleted','expired','unknown','version_changed'])
async def test_fixture_evidence_never_skips_dependency_preparation(repository, fixture_state):
    lifecycle, token, _ = await recovery_ready(repository)
    with repository.transaction() as tx:
        if fixture_state != 'deleted':
            tx.put('fixtures','fixture/a',{'scope':'work/a','status':fixture_state,
                'expires_at':999.0 if fixture_state=='expired' else 1090.0,
                'specification_hash':'changed' if fixture_state=='version_changed' else 'original'})
    plan=RecoveryService(repository).compile_recovery(lifecycle,token=token)
    assert plan.entry_node=='N01' and plan.revalidate_nodes==('N01','N02','N03')
    assert not set(plan.reuse_receipts)&{'original/N01','original/N02','original/N03'}


@pytest.mark.asyncio
async def test_stable_inputs_compile_idempotently_without_new_budget_or_generation(repository, clock):
    lifecycle, token, _=await recovery_ready(repository)
    with repository.transaction() as tx:
        budgets=deepcopy(tx.scan('lifecycle_budgets'))
    service=RecoveryService(repository)
    plan=service.compile_recovery(lifecycle,token=token)
    clock.advance(1)
    assert service.compile_recovery(lifecycle,token=token)==plan
    with repository.transaction() as tx:
        assert tx.scan('lifecycle_budgets')==budgets
        assert len(tx.scan('recovery_plans'))==1


@pytest.mark.asyncio
async def test_analysis_requires_frozen_original_raw_and_contract_binding(repository):
    lifecycle,token,_=await recovery_ready(repository,node='N08',measured=True)
    with repository.transaction() as tx:
        receipt=tx.get('receipts','original/N07')['body']
        receipt['result']['artifact_refs']=[]
        tx.put('receipts','original/N07',receipt)
    with pytest.raises(Conflict,match='frozen raw'):
        RecoveryService(repository).compile_recovery(lifecycle,token=token)
    with repository.transaction() as tx:
        assert not tx.scan('recovery_plans')


@pytest.mark.asyncio
async def test_measurement_binding_supplies_deployed_contract_identity(repository):
    lifecycle,token,_=await recovery_ready(repository)
    with repository.transaction() as tx:
        tx.delete('contracts','contract/a')
        tx.put('measurement_bindings','work/a',{'work_id':'work/a','run_id':'run/a',
            'plan_ref':'plan/a','measurement_contract_hash':'a'*64})
    assert RecoveryService(repository).compile_recovery(lifecycle,token=token).contract_hash=='a'*64


@pytest.mark.asyncio
async def test_recovery_accepts_valid_receipt_composition_across_attempts(repository):
    lifecycle,token,_=await recovery_ready(repository)
    with repository.transaction() as tx:
        receipt=tx.get('receipts','original/N00')['body']
        receipt['identity']['activation_id']='older/original/attempt'
        tx.put('receipts','original/N00',receipt)
    plan=RecoveryService(repository).compile_recovery(lifecycle,token=token)
    assert plan.source_attempt=='activation/a'
    with repository.transaction() as tx:
        assert tx.get('recovery_contexts',plan.ref)['body']['result_refs']['N00']=='original/N00'


@pytest.mark.asyncio
async def test_recovery_rejects_receipt_from_another_work(repository):
    lifecycle,token,_=await recovery_ready(repository)
    with repository.transaction() as tx:
        receipt=tx.get('receipts','original/N00')['body']
        receipt['identity']['scope']='other/work'
        tx.put('receipts','original/N00',receipt)
    with pytest.raises(Conflict,match='provenance'):
        RecoveryService(repository).compile_recovery(lifecycle,token=token)
