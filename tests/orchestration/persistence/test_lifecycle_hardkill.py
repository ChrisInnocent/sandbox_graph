"""ALI-04: kill a real PostgreSQL writer on both sides of the commit boundary.

The dedicated postgres fixture owns and removes an isolated schema. No default
database, in-memory fallback, or mocked process termination is used here.
"""
import multiprocessing
import os
import signal
import pytest
from sandbox_graph.domain.agent_lifecycle import RepairPolicy
from sandbox_graph.domain.models import RunRecord,NodeResult
from sandbox_graph.persistence.repository import PostgresRepository
from sandbox_graph.services.agent_lifecycle import LifecycleService
from sandbox_graph.services.lifecycle_budgets import LifecycleBudgetService
from tests.orchestration.unit.test_lifecycle_models import policy_data
from tests.orchestration.unit.test_lifecycle_budgets import LIMITS,SAFETY

pytestmark=pytest.mark.postgres


def _write(repo,life,token):
    with repo.transaction() as tx:
        LifecycleBudgetService.reserve_in(tx,request_id='original-allowance',lifecycle_id=life.key.lifecycle_id,
            input_ref=life.source_receipt,amounts={'diagnosis_calls':1})
        result=LifecycleService.record_decision_in(tx,life.key.lifecycle_id,request_id='original-decision',
            expected_revision=life.revision,route='needs_input',evidence_refs=(life.source_receipt,),token=token)
        yield result


def _child(dsn,life,token,cut,pipe):
    from contextlib import contextmanager
    repo=PostgresRepository(dsn)
    with contextmanager(_write)(repo,life,token):
        if cut=='before_commit':pipe.send('before_commit');pipe.recv()
    pipe.send('after_commit');pipe.recv()


def _seed(repo):
    with repo.transaction() as tx:now=float(tx.now)
    data=policy_data();data.update(deadline=now+120,safety_budget=data['safety_budget']|{'deadline':now+180})
    service=LifecycleService(repo);service.register_policy('policy/kill',RepairPolicy.model_validate(data))
    with repo.transaction() as tx:
        token=tx.acquire_thread('thread/kill','parent',120)
        run=RunRecord(run_id='run/kill',root_thread_id=token.thread_id,graph_ref='g:1',contract_ref='contract',
            active_plan_ref='plan',auth_ref='auth',lineage_id='lineage/kill')
        tx.put('runs',run.run_id,run.model_dump(mode='json'))
        identity={'run_ref':run.run_id,'scope':run.run_id,'node_id':'R1','thread_id':token.thread_id,
            'activation_id':'activation/kill','plan_ref':'plan','graph_ref':'g:1','input_refs':{}}
        tx.put('receipts','receipt/kill',{'identity':identity,'result':NodeResult(execution_status='failed').model_dump(mode='json')})
        tx.put('activations','activation/kill',{'status':'complete','record':{'activation_id':'activation/kill',
            'run_id':run.run_id,'scope':run.run_id,'plan_ref':'plan','graph_ref':'g:1'},'result':{'result_refs':{'R1':'receipt/kill'}}})
    life=service.observe_failure('receipt/kill',policy_ref='policy/kill',token=token)
    with repo.transaction() as tx:
        LifecycleBudgetService.initialize_in(tx,lifecycle_id=life.key.lifecycle_id,lineage_budget_ref=life.key.lineage_budget_ref,
            limits=LIMITS,deadline=now+120,safety_limits=SAFETY,safety_deadline=now+180,cost_unit='microUSD')
    return life,token


@pytest.mark.parametrize('cut',['before_commit','after_commit'])
def test_budget_decision_outbox_survive_original_writer_hardkill(postgres,cut):
    from contextlib import contextmanager
    repo,dsn=postgres;life,token=_seed(repo)
    context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
    process=context.Process(target=_child,args=(dsn,life,token,cut,child));process.start();child.close()
    try:
        assert parent.poll(30),'writer did not reach the requested crash cut'
        assert parent.recv()==cut
        os.kill(process.pid,signal.SIGKILL);process.join(10)
        assert process.exitcode==-signal.SIGKILL
        with repo.transaction() as tx:
            committed=cut=='after_commit'
            assert bool(tx.get('lifecycle_decision_requests','original-decision'))==committed
            assert bool(tx.get('budget_reservations','original-allowance'))==committed
            assert len([r for r in tx.scan('outbox') if r['body']['kind']=='decision_ready'])==int(committed)
        # Replay retains request IDs, consumes one allowance and queues one intent.
        with contextmanager(_write)(repo,life,token):pass
        with contextmanager(_write)(repo,life,token):pass
        with repo.transaction() as tx:
            assert len(tx.scan('lifecycle_decisions'))==1
            assert len([r for r in tx.scan('outbox') if r['body']['kind']=='decision_ready'])==1
            assert LifecycleBudgetService.snapshot_in(tx,life.key.lifecycle_id)['lineage']['reserved']['diagnosis_calls']==1
    finally:
        if process.is_alive():process.kill();process.join(10)
        parent.close()
