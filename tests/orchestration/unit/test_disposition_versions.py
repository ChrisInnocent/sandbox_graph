"""ST-10/14: writer transactions invalidate old closure and attach late failures."""
from copy import deepcopy

import pytest

from sandbox_graph.services.lifecycle_disposition import DispositionVersions
from sandbox_graph.services.lifecycle_stop import LifecycleStopService
from sandbox_graph.interfaces.service import RunService
from sandbox_graph.planning.compiler import PlanCompiler


@pytest.mark.parametrize('kind,body', [
    ('resources', {'run_id':'run/a', 'scope':'work/a', 'status':'active'}),
    ('observations', {'run_id':'run/a', 'scope':'work/a', 'status':'running'}),
    ('receipts', {'identity':{'run_ref':'run/a', 'scope':'work/a'}, 'result':{'execution_status':'failed'}}),
])
@pytest.mark.asyncio
async def test_late_effect_invalidates_stop_proof_without_rewriting_it(repository, kind, body):
    from tests.orchestration.graphs.test_work_remediation import assembly, drain
    worker, _, _, _ = assembly(repository, failure='N09')
    for _ in range(3):
        assert await worker.run_once()
        worker.pump_outbox()
    RunService(repository, PlanCompiler([])).cancel('stop', 'run/a', 'requested')
    await drain(worker)
    with repository.transaction() as tx:
        intent = tx.scan('lifecycle_stop_requests')[0]
        status = tx.get('lifecycle_stop_status', intent['key'])['body']
        assert status['status'] == 'closed'
        proof = deepcopy(tx.get('lifecycle_stop_proofs', status['proof_ref']))
        DispositionVersions.verify_stop_in(tx, proof['body'])
        tx.put(kind, 'late', body)
        assert tx.get('lifecycle_stop_status', intent['key'])['body']['status'] == 'waiting_dependency'
        assert tx.get('lifecycle_stop_proofs', status['proof_ref']) == proof
        with pytest.raises(ValueError, match='disposition revision'):
            DispositionVersions.verify_stop_in(tx, proof['body'])


def test_failed_write_transaction_does_not_advance_disposition(repository):
    with repository.transaction() as tx:
        original = DispositionVersions.current_in(tx, 'run/a')
    with pytest.raises(RuntimeError):
        with repository.transaction() as tx:
            tx.put('resources', 'r', {'run_id':'run/a', 'status':'active'})
            raise RuntimeError('cut before commit')
    with repository.transaction() as tx:
        assert DispositionVersions.current_in(tx, 'run/a') == original
        assert tx.get('resources', 'r') is None


def test_foreign_effect_does_not_invalidate_current_run(repository):
    with repository.transaction() as tx:
        before = DispositionVersions.current_in(tx, 'run/a')
        tx.put('resources', 'r', {'run_id':'run/b', 'scope':'shared', 'status':'active'})
        assert DispositionVersions.current_in(tx, 'run/a') == before
        assert DispositionVersions.current_in(tx, 'run/b') > 0


def test_late_lifecycle_attaches_in_original_write_transaction(repository):
    from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle
    service, token = setup_lifecycle(repository)
    with repository.transaction() as tx:
        source = tx.get('receipts','receipt/a')['body']['identity']
        LifecycleStopService.request_in(tx, request_id='before-failure', run_ref=source['run_ref'],
            plan_ref=source['plan_ref'], graph_ref=source['graph_ref'], reason='requested', policy='cancel')
    record = service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    with repository.transaction() as tx:
        attached = tx.scan('lifecycle_stop_requests')
        assert len(attached) == 1 and attached[0]['body']['lifecycle_id'] == record.key.lifecycle_id
    service.observe_failure('receipt/a', policy_ref='policy/a', token=token)
    with repository.transaction() as tx:
        assert len(tx.scan('lifecycle_stop_requests')) == 1


def test_terminal_phase_does_not_substitute_for_pending_stop_proof(repository):
    from tests.orchestration.unit.test_lifecycle_closure import lifecycle, archive
    from sandbox_graph.persistence.repository import Conflict
    record = lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        LifecycleStopService.request_in(tx, request_id='await-proof', run_ref='run/a',
            plan_ref=record.plan_ref, graph_ref=record.graph_ref, reason='requested', policy='cancel')
    with pytest.raises(Conflict, match='stop proof'):
        archive(repository)


def test_current_archive_is_stale_after_new_owned_effect(repository):
    from tests.orchestration.unit.test_lifecycle_closure import lifecycle, archive
    lifecycle(repository, 'fulfilled')
    result = archive(repository)
    with repository.transaction() as tx:
        current = tx.get('current_archives', 'run/a')['body']
        original = deepcopy(tx.get('archives', current['archive_ref']))
        tx.put('resources', 'late', {'scope':'work/a', 'status':'active'})
        assert tx.get('current_archives', 'run/a')['body']['status'] == 'stale'
        assert tx.get('archives', current['archive_ref']) == original


def test_stop_history_migration_is_bounded_and_idempotent(repository):
    with repository.transaction() as tx:
        for index in range(3):
            tx.put('run_stop_requests', str(index), {'request_id':str(index), 'run_ref':'a',
                'plan_ref':'p','graph_ref':'g','reason':'requested','policy':'cancel'},immutable=True)
        first = LifecycleStopService.migrate_in(tx, limit=2)
        assert first == {'cursor':'1','complete':False}
        assert tx.get('run_stop_index','a')['body']['refs'] == ['0','1']
        last = LifecycleStopService.migrate_in(tx, cursor=first['cursor'], limit=2)
        assert last == {'cursor':'2','complete':True}
        assert tx.get('run_stop_index','a')['body']['refs'] == ['0','1','2']


@pytest.mark.asyncio
async def test_stop_prevents_fresh_measurement_generation(repository):
    from tests.orchestration.unit.test_measurement_generation import source_ready
    from sandbox_graph.services.measurement_generation import MeasurementGenerationService
    from sandbox_graph.services.agent_lifecycle import LifecycleService
    from sandbox_graph.persistence.repository import Conflict
    plan,work,life,token=await source_ready(repository)
    service=MeasurementGenerationService(repository)
    frozen=service.compile(life,target_plan_ref=plan.hash,authorization_ref='measure/a',token=token)
    service.authorize('measure/a',frozen.hash,principal='operator',expires_at=1090,permissions=('measure',))
    with repository.transaction() as tx:
        record=LifecycleService.load_in(tx,life)
        LifecycleStopService.request_in(tx,request_id='cutoff',run_ref=record.key.run_ref,
            plan_ref=record.plan_ref,graph_ref=record.graph_ref,reason='stop',policy='cancel')
    with pytest.raises(Conflict,match='stop requested'):
        service.request('fresh',frozen.hash,authorization_ref='measure/a',principal='operator',token=token)
    with repository.transaction() as tx:
        assert not tx.scan('measurement_generation_requests')
