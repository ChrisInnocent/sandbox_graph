"""ST-20: kill actual controller processes across PostgreSQL commit boundaries.

The separate file ledger models an external accepted effect. It is not evidence
of a product backend installation or a verified model provider.
"""
import asyncio
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from sandbox_graph.persistence.repository import PostgresRepository, UnitOfWork
from sandbox_graph.runtime.worker import Worker,WorkerPolicy
from sandbox_graph.services.lifecycle_stop import LifecycleStopService

pytestmark=pytest.mark.postgres


def worker(repo):
    return Worker(repo,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))


def stop(repo):
    with repo.transaction() as tx:
        LifecycleStopService.request_in(tx,request_id='stop',run_ref='r',plan_ref='p',
            graph_ref='g1',reason='requested',policy='cancel')


def parked(channel):
    channel.send('cutpoint')
    channel.recv()  # Parent terminates this process, never a graceful exception.


def stop_child(dsn,cut,channel):
    repo=PostgresRepository(dsn)
    before={'T1.before':'run_stop_requests','T2.enqueue':'stop_job_bindings',
        'T2.ack':'outbox_delivery','T6.before':'lifecycle_stop_proofs'}
    original=UnitOfWork.put
    def intercepted(tx,kind,key,body,*args,**kwargs):
        result=original(tx,kind,key,body,*args,**kwargs)
        if kind==before.get(cut): parked(channel)
        return result
    UnitOfWork.put=intercepted
    stop(repo)
    if cut=='T1.after': parked(channel)
    engine=worker(repo);engine.pump_outbox()
    if cut=='T2.after': parked(channel)
    asyncio.run(engine.run_once())
    if cut=='T6.after': parked(channel)
    raise AssertionError('cutpoint never reached')


def kill_at(context,target,args):
    parent,child=context.Pipe()
    process=context.Process(target=target,args=(*args,child))
    process.start();child.close()
    try:
        assert parent.poll(30),f'child never reached cutpoint (exit={process.exitcode})'
        assert parent.recv()=='cutpoint'
        process.kill();process.join(10)
        assert process.exitcode is not None and process.exitcode!=0
    finally:
        if process.is_alive(): process.kill();process.join(10)
        parent.close()


@pytest.mark.parametrize('cut',['T1.before','T1.after','T2.enqueue','T2.ack','T2.after','T6.before','T6.after'])
def test_crash_cutpoints(postgres,cut):
    from tests.orchestration.persistence.test_lifecycle_stop_transactions import seed_stop
    from sandbox_graph.services.lifecycle_disposition import DispositionVersions
    repo,dsn=postgres;seed_stop(repo)
    kill_at(multiprocessing.get_context('spawn'),stop_child,(dsn,cut))
    stop(repo);engine=worker(repo)
    for _ in range(12):
        engine.pump_outbox()
        with repo.transaction() as tx:
            targets=tx.scan('lifecycle_stop_requests')
            assert len(targets)==1
            status=tx.get('lifecycle_stop_status',targets[0]['key'])['body']
            if status['status']=='closed':
                DispositionVersions.verify_stop_in(tx,tx.get('lifecycle_stop_proofs',status['proof_ref'])['body'])
                break
            # Crash leaves a real claim until expiry; simulate expiry in SQL,
            # not deletion of the job, thread identity, or effect ledger.
            for row in tx.scan('jobs'):
                if row['body']['status']=='running':
                    tx.put('jobs',row['key'],row['body']|{'lease_until':tx.now-1})
        assert asyncio.run(engine.run_once())
    else: pytest.fail('restarted worker did not consume original stop target')
    with repo.transaction() as tx:
        assert len(tx.scan('run_stop_requests'))==1
        assert not tx.scan('repair_orders')
        assert not tx.scan('agent_provider_requests')


class FileRunner:
    def __init__(self,path,channel=None): self.path=Path(path);self.channel=channel
    async def submit(self,op):
        from sandbox_graph.runtime.runner import RunnerStatus
        with self.path.open('a') as stream:
            stream.write(op.operation_id+'\n');stream.flush();os.fsync(stream.fileno())
        if self.channel: parked(self.channel)
        return RunnerStatus(state='completed',authoritative=True)
    async def reconcile(self,operation_id,scope):
        from sandbox_graph.runtime.runner import RunnerStatus
        assert self.path.read_text().splitlines()==[operation_id]
        return RunnerStatus(state='completed',task_handle=operation_id,authoritative=True)


def effect_child(dsn,path,wire,token,channel):
    from sandbox_graph.domain.models import Operation
    from sandbox_graph.runtime.runner import OperationService
    op=Operation.model_validate(wire)
    service=OperationService(PostgresRepository(dsn),FileRunner(path,channel),
        {(op.script_ref.script_id,op.script_ref.version):op.script_ref})
    asyncio.run(service.submit(op,token))


def test_hard_kill_after_external_acceptance_reconciles_original_id(postgres,tmp_path):
    from tests.orchestration.unit.test_runner_protocol import service
    from sandbox_graph.runtime.runner import OperationService
    repo,dsn=postgres
    _,_,op,token=service(repo)
    with repo.transaction() as tx: op=op.model_copy(update={'deadline':tx.now+90})
    path=tmp_path/'external-effects.log'
    kill_at(multiprocessing.get_context('spawn'),effect_child,(dsn,str(path),op.model_dump(),token))
    restarted=OperationService(repo,FileRunner(path),{('bench','1'):op.script_ref})
    assert asyncio.run(restarted.submit(op,token)).state=='completed'
    assert path.read_text().splitlines()==['op']


def late_child(dsn,cut,channel):
    repo=PostgresRepository(dsn)
    with repo.transaction() as tx:
        tx.put('resources','late',{'run_id':'r','scope':'g','status':'active'})
        if cut=='before': parked(channel)
    parked(channel)


@pytest.mark.parametrize('cut',['before','after'])
def test_late_fact_crash_preserves_atomic_proof_invalidation(postgres,cut):
    from tests.orchestration.persistence.test_lifecycle_stop_transactions import seed_stop
    repo,dsn=postgres;engine,_=seed_stop(repo);stop(repo)
    asyncio.run(engine.run_once())
    with repo.transaction() as tx:
        target=tx.scan('lifecycle_stop_requests')[0]['key']
        before=tx.get('lifecycle_stop_status',target)['body']
        assert before['status']=='closed'
    kill_at(multiprocessing.get_context('spawn'),late_child,(dsn,cut))
    with repo.transaction() as tx:
        status=tx.get('lifecycle_stop_status',target)['body']['status']
        assert status==('closed' if cut=='before' else 'waiting_dependency')
        assert bool(tx.get('resources','late'))==(cut=='after')
