"""Real PostgreSQL transaction cuts; no simulated pass without a dedicated DSN.

Candidate/reviewer facts are controlled inputs. These tests prove atomic database
publication, not a live coding-provider or isolation-backend acceptance.
"""
import asyncio
from contextlib import contextmanager
import multiprocessing
import os
import signal
import pytest
from sandbox_graph.persistence.repository import PostgresRepository
from sandbox_graph.services.remediation import NodeRemediationService
from sandbox_graph.services.promotion import PromotionService
from sandbox_graph.services.rollback import RollbackService
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from tests.orchestration.unit.test_remediation_requests import ready
from tests.orchestration.unit.test_promotion import prepared
from tests.orchestration.unit.test_release_rollback import rollback_fixture

pytestmark=pytest.mark.postgres


class CutRepository(PostgresRepository):
    """Instrument only the real transaction; production clock/fences stay intact."""
    def __init__(self,dsn,cut,pipe):super().__init__(dsn);self.cut=cut;self.pipe=pipe
    def barrier(self):self.pipe.send(self.cut);self.pipe.recv()
    @contextmanager
    def transaction(self):
        with super().transaction() as tx:
            touched=set()
            original=tx.put
            def put(kind,key,body,*args,**kwargs):
                result=original(kind,key,body,*args,**kwargs);touched.add(kind)
                if self.cut=='write:'+kind:self.barrier()
                return result
            tx.put=put
            yield tx
            if self.cut=='before_commit' or self.cut.startswith('before_commit:') and self.cut.split(':',1)[1] in touched:self.barrier()
        if self.cut=='after_commit' or self.cut.startswith('after_commit:') and self.cut.split(':',1)[1] in touched:self.barrier()


def perform(repo,operation,request,token):
    if operation=='repair':return NodeRemediationService(repo).request_remediation(request,principal='supervisor/a',token=token)
    if operation=='promote':return PromotionService(repo).promote(request,token=token)
    if operation=='rollback':return RollbackService(repo).rollback(request,token=token)
    raise ValueError(operation)


def child(dsn,operation,request,token,cut,pipe):
    perform(CutRepository(dsn,cut,pipe),operation,request,token)
    pipe.send('unexpected_completion')


MATRIX=[('repair',c) for c in ('write:repair_orders','write:jobs','write:repair_requests','before_commit','after_commit')]+[
    (operation,c) for operation in ('promote','rollback') for c in ('write:run_bundle_bindings','before_commit','after_commit')]


@pytest.mark.parametrize('operation,cut',MATRIX)
def test_original_publication_request_survives_hardkill_without_half_commit(postgres,tmp_path,operation,cut):
    repo,dsn=postgres
    with repo.transaction() as tx:now=float(tx.now)
    if operation=='repair':_,request,token=ready(repo,now=now)
    elif operation=='promote':_,request,token,_,_=asyncio.run(prepared(repo,tmp_path,now=now))
    else:request,token,_=asyncio.run(rollback_fixture(repo,tmp_path,now=now))
    with repo.transaction() as tx:
        baseline={kind:tx.scan(kind) for kind in ('runs','dispatch_gates','jobs','budget_reservations','outbox')}
    context=multiprocessing.get_context('spawn');parent,remote=context.Pipe()
    process=context.Process(target=child,args=(dsn,operation,request,token,cut,remote));process.start();remote.close()
    try:
        assert parent.poll(30),'real writer did not reach requested cut'
        assert parent.recv()==cut
        os.kill(process.pid,signal.SIGKILL);process.join(10)
        assert process.exitcode==-signal.SIGKILL
        if cut!='after_commit':
            with repo.transaction() as tx:
                for kind,rows in baseline.items():assert tx.scan(kind)==rows,kind
        first=perform(repo,operation,request,token)
        assert perform(repo,operation,request,token)==first
        with repo.transaction() as tx:
            if operation=='repair':
                assert len(tx.scan('repair_orders'))==len(tx.scan('repair_requests'))==len(tx.scan('jobs'))==1
                assert LifecycleBudgetService.snapshot_in(tx,request.lifecycle_id)['lineage']['reserved']['repair_attempts']==1
                assert len([r for r in tx.scan('outbox') if r['body']['kind']=='job_ready'])==1
            else:
                predecessor='run/a' if operation=='promote' else request.failed_run_ref
                new=tx.get('runs',first['run_ref'])['body']
                assert tx.get('runs',predecessor)['body']['successor_run_id']==new['run_id']
                assert new['supersedes_run_id']==predecessor and new['graph_ref']=='g:2'
                assert tx.get('dispatch_gates',predecessor)['body']['open'] is False
                assert len([r for r in tx.scan('runs') if r['body'].get('supersedes_run_id')==predecessor])==1
                assert tx.get('run_bundle_bindings',new['run_id'])['body']['bundle_ref']==request.worker_bundle_ref
    finally:
        if process.is_alive():process.kill();process.join(10)
        parent.close()


def repair_worker_child(dsn,job,cut,pipe,effect_path):
    from sandbox_graph.runtime.worker import RemedyJobHandler
    from sandbox_graph.domain.models import NodeResult
    repo=CutRepository(dsn,cut,pipe)
    async def action(context):
        # Independent durable side-effect ledger; a second invocation cannot
        # overwrite the first effect and silently appear idempotent.
        with open(effect_path,'xb') as stream:
            stream.write(b'original-operation\n');stream.flush();os.fsync(stream.fileno())
        if cut=='effect_before_receipt':repo.barrier()
        return NodeResult(execution_status='succeeded',operation_refs=('op/a',))
    asyncio.run(RemedyJobHandler(repo,lambda token,activation:{'N01':action},lease_seconds=3,segment_seconds=15)(job))
    pipe.send('unexpected_completion')


@pytest.mark.parametrize('cut',['effect_before_receipt','write:repair_outcomes',
    'before_commit:repair_outcomes','after_commit:repair_outcomes'])
def test_repair_effect_receipt_outcome_settlement_and_outbox_crash_cuts(postgres,tmp_path,cut):
    import time
    from tests.orchestration.unit.test_repair_execution import modern_job,closed_operation
    from sandbox_graph.runtime.worker import RemedyJobHandler
    repo,dsn=postgres
    with repo.transaction() as tx:now=float(tx.now)
    request,order,job=modern_job(repo,now=now);closed_operation(repo)
    effect=tmp_path/'independent-effect.log'
    context=multiprocessing.get_context('spawn');parent,remote=context.Pipe()
    process=context.Process(target=repair_worker_child,args=(dsn,job,cut,remote,str(effect)))
    process.start();remote.close()
    try:
        assert parent.poll(30),'worker did not reach requested effect/outcome cut'
        assert parent.recv()==cut
        os.kill(process.pid,signal.SIGKILL);process.join(10)
        assert process.exitcode==-signal.SIGKILL and effect.read_bytes()==b'original-operation\n'
        committed=cut=='after_commit:repair_outcomes'
        with repo.transaction() as tx:
            assert bool(tx.get('repair_outcomes',order['order_ref']))==committed
            assert bool(tx.get('budget_settlements','repair-budget:repair/a'))==committed
            assert bool(tx.get('outbox','repair-outcome:'+order['order_ref']))==committed
        # Expiry is elapsed real time, never a forged database clock/fence.
        time.sleep(3.2)
        async def forbidden(context):raise AssertionError('original action must not be submitted twice')
        handler=RemedyJobHandler(repo,lambda token,activation:{'N01':forbidden},lease_seconds=10,segment_seconds=15)
        result=asyncio.run(handler(job))
        assert result['execution_status']==('blocked' if cut=='effect_before_receipt' else 'succeeded')
        assert asyncio.run(handler(job))==result
        assert effect.read_bytes()==b'original-operation\n'
        with repo.transaction() as tx:
            assert len(tx.scan('repair_orders'))==len(tx.scan('repair_outcomes'))==len(tx.scan('budget_settlements'))==1
            assert LifecycleBudgetService.snapshot_in(tx,request.lifecycle_id)['lineage']['spent']['repair_attempts']==1
            if cut=='effect_before_receipt':assert tx.scan('repair_isolations')
    finally:
        if process.is_alive():process.kill();process.join(10)
        parent.close()
