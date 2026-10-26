"""ST-11/13: outbox routes; bounded fenced jobs own stop advancement."""
import pytest

from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.tasks import TaskBackend
from sandbox_graph.runtime.worker import Worker, WorkerPolicy
from sandbox_graph.services.lifecycle_stop import LifecycleStopService
from sandbox_graph.services.lifecycle_stop_jobs import StopJobs


def stopped(repository):
    from tests.orchestration.unit.test_lifecycle_closure import lifecycle
    record = lifecycle(repository, 'fulfilled')
    with repository.transaction() as tx:
        LifecycleStopService.request_in(tx,request_id='stop',run_ref='run/a',plan_ref=record.plan_ref,
            graph_ref=record.graph_ref,reason='requested',policy='cancel')
        target = tx.scan('lifecycle_stop_requests')[0]['key']
        owner=tx.get('lifecycle_owners',record.key.lifecycle_id)['body']
        from sandbox_graph.persistence.repository import LeaseToken
        tx.release_thread(LeaseToken(owner['thread_id'],owner['owner_epoch'],owner['lease_id'],0))
    from tests.orchestration.unit.test_lifecycle_safety import seed
    safety, token=seed(repository)
    with repository.transaction() as tx:
        facts=tx.get('work_safety_facts','work/a')['body']
        tx.put('work_safety_facts','work/a',facts|{'work_revision':tx.get('work_instances','work/a')['revision']})
        safety.release_work_in(tx,'work/a',token)
        tx.release_thread(token)
    return Worker(repository,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1)), target


def test_pump_only_routes_without_advancing_stop(repository):
    worker,target=stopped(repository)
    worker.pump_outbox(); worker.pump_outbox()
    with repository.transaction() as tx:
        assert tx.get('lifecycle_stop_status',target)['body']['status']=='pending'
        assert not tx.scan('lifecycle_stop_proofs')
        jobs=[r for r in tx.scan('jobs') if r['body']['job_kind']=='lifecycle_stop']
        assert len(jobs)==1
        assert StopJobs.valid_in(tx,jobs[0]['body'])
        assert tx.get('outbox_delivery',target)


@pytest.mark.asyncio
async def test_stop_job_closes_terminal_lifecycle_with_fresh_fence(repository):
    worker,target=stopped(repository)
    assert await worker.run_once()
    with repository.transaction() as tx:
        status=tx.get('lifecycle_stop_status',target)['body']
        assert status['status']=='closed'
        proof=tx.get('lifecycle_stop_proofs',status['proof_ref'])['body']
        assert proof['owner_epoch'] > 0
        from sandbox_graph.services.lifecycle_disposition import DispositionVersions
        DispositionVersions.verify_stop_in(tx,proof)


@pytest.mark.asyncio
async def test_waiting_stop_is_bounded_and_does_not_poll_at_same_time(repository):
    worker,target=stopped(repository)
    with repository.transaction() as tx:
        tx.put('resources','busy',{'run_id':'run/a','scope':'work/a','status':'active'})
    assert await worker.run_once()
    with repository.transaction() as tx:
        status=tx.get('lifecycle_stop_status',target)['body']
        assert status['status']=='waiting_dependency'
        assert status['due_at']>tx.now
        assert len(tx.scan('jobs'))==2
    assert not await worker.run_once()
    with repository.transaction() as tx:
        tx.put('resources','busy',{'run_id':'run/a','scope':'work/a','status':'closed'})
    assert await worker.run_once()
    with repository.transaction() as tx:
        assert tx.get('lifecycle_stop_status',target)['body']['status']=='closed'


def test_stop_job_name_does_not_bypass_dispatch_gate(repository):
    worker,target=stopped(repository)
    worker.pump_outbox()
    with repository.transaction() as tx:
        real=tx.scan('jobs')[0]['body']
        fake=real|{'job_id':'forged','scope_ref':'outside','dedupe_key':'forged','due_at':tx.now-1}
        tx.put('jobs','forged',fake)
        tx.put('dispatch_gates','run/a',{'open':False,'generation':2})
        assert not StopJobs.valid_in(tx,fake)
    claimed=TaskBackend(repository).claim('worker',30)
    assert claimed['job_id'] != 'forged'
    with repository.transaction() as tx:
        assert tx.get('jobs','forged')['body']['status']=='superseded'


def test_stop_job_binding_cannot_be_replayed_for_changed_payload(repository):
    worker,target=stopped(repository);worker.pump_outbox()
    job=TaskBackend(repository).claim('worker',30)
    with repository.transaction() as tx:
        bad=job|{'payload':job['payload']|{'stop_target_ref':'wrong'}}
        assert not StopJobs.valid_in(tx,bad)


def test_stop_command_replay_conflict_and_cutoff_snapshot(repository):
    worker,target=stopped(repository)
    with repository.transaction() as tx:
        request=tx.get('lifecycle_stop_requests',target)['body']
        command=tx.get('run_stop_requests',request['command_ref'])['body']
        assert command['cutoff_generation']==3
        assert command['producer']=='run_control.v1'
        before=tx.scan('lifecycle_stop_requests')
        LifecycleStopService.request_in(tx,request_id='stop',run_ref='run/a',plan_ref=command['plan_ref'],
            graph_ref=command['graph_ref'],reason='requested',policy='cancel')
        assert tx.scan('lifecycle_stop_requests')==before
        with pytest.raises(Conflict):
            LifecycleStopService.request_in(tx,request_id='stop',run_ref='run/a',plan_ref=command['plan_ref'],
                graph_ref=command['graph_ref'],reason='different',policy='cancel')


@pytest.mark.asyncio
async def test_unstored_or_changed_proof_cannot_be_consumed(repository):
    from sandbox_graph.services.lifecycle_disposition import DispositionVersions
    worker,target=stopped(repository);await worker.run_once()
    with repository.transaction() as tx:
        proof=tx.scan('lifecycle_stop_proofs')[0]['body']
        with pytest.raises(Conflict,match='stored|producer'):
            DispositionVersions.verify_stop_in(tx,proof|{'owner_epoch':proof['owner_epoch']+1})


def test_postgres_harness_initialization_uses_valid_actual_scope(repository):
    from tests.orchestration.persistence.test_lifecycle_stop_transactions import seed_stop
    worker,record=seed_stop(repository)
    assert record.key.scope_kind=='group' and record.key.scope_ref=='g'
