"""ST-18/19: independent PostgreSQL connections; never a memory fallback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from sandbox_graph.persistence.repository import Conflict, UnitOfWork
from sandbox_graph.services.revisions import RevisionService

pytestmark=pytest.mark.postgres


@pytest.mark.parametrize('first',['submission','cutoff'])
def test_cutoff_races_submission(postgres,monkeypatch,first):
    from tests.orchestration.unit.test_runner_protocol import service
    repo,_=postgres
    operations,runner,op,token=service(repo)
    with repo.transaction() as tx:
        op=op.model_copy(update={'deadline':tx.now+60})
    entered,release=Event(),Event()
    original=UnitOfWork.put
    def barrier(tx,kind,key,body,*args,**kwargs):
        result=original(tx,kind,key,body,*args,**kwargs)
        selected=(first=='submission' and kind=='operations' and body.get('phase')=='submission_started'
            or first=='cutoff' and kind=='run_stop_requests')
        if selected:
            entered.set()
            assert release.wait(10),'second connection never released commit barrier'
        return result
    monkeypatch.setattr(UnitOfWork,'put',barrier)
    def submit(): return asyncio.run(operations.submit(op,token))
    def cutoff():
        return RevisionService(repo).activate_revision(request_id='cutoff',predecessor_run_id='r',
            expected_revision=0,candidate_plan_hash='p2',graph_ref='g2',disposition_policy='drain')
    first_fn,second_fn=(submit,cutoff) if first=='submission' else (cutoff,submit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(first_fn)
        assert entered.wait(10)
        b=pool.submit(second_fn)
        release.set();a.result(timeout=15)
        if first=='cutoff':
            with pytest.raises(Conflict,match='gate|superseded'): b.result(timeout=15)
            assert runner.effects=={}
        else:
            b.result(timeout=15)
            assert set(runner.effects)=={'op'}
            assert asyncio.run(operations.submit(op,token)).state=='completed'
            assert len(runner.effects)==1


def seed_stop(repo):
    from tests.orchestration.unit.test_lifecycle_service import setup_lifecycle
    from sandbox_graph.domain.agent_lifecycle import RepairPolicy
    from sandbox_graph.domain.models import RunRecord,NodeResult,ErrorRecord
    from sandbox_graph.graphs.executor import NodeExecutor
    from sandbox_graph.services.lifecycle_stop import LifecycleStopService
    from sandbox_graph.runtime.worker import Worker,WorkerPolicy
    service,old=setup_lifecycle(repo)
    with repo.transaction() as tx:
        tx.release_thread(old)
        now=tx.now
        body=tx.get('repair_policies','policy/a')['body']
        body['deadline']=now+300
        body['safety_budget']['deadline']=now+600
        tx.put('groups','r:g',{'run_id':'r','group_id':'g','plan_ref':'p'})
    service.register_policy('policy/pg',RepairPolicy.model_validate_json(__import__('json').dumps(body)))
    RevisionService(repo).create(RunRecord(run_id='r',root_thread_id='root',graph_ref='g1',
        active_plan_ref='p',contract_ref='c',auth_ref='a',lineage_id='r'))
    with repo.transaction() as tx: token=tx.acquire_thread('source','node',60)
    async def fail(context):
        return NodeResult(execution_status='failed',error=ErrorRecord(code='INPUT_MISSING',
            observed_at=now,effect_certainty='none'))
    _,ref=asyncio.run(NodeExecutor(repo,{'E1':fail},token).execute('E1',{'thread_id':'source',
        'scope':'g','run_ref':'r','plan_ref':'p','graph_ref':'g1','activation_id':'source','input_refs':{}}))
    record=service.observe_failure(ref,policy_ref='policy/pg',token=token)
    with repo.transaction() as tx: tx.release_thread(token)
    worker=Worker(repo,{},policy=WorkerPolicy(claim_seconds=30,job_seconds=20,poll_seconds=1))
    return worker,record


@pytest.mark.parametrize('first',['proof','late_effect'])
def test_proof_races_late_effect(postgres,monkeypatch,first):
    from sandbox_graph.services.lifecycle_stop import LifecycleStopService
    from sandbox_graph.services.lifecycle_disposition import DispositionVersions
    repo,_=postgres;worker,record=seed_stop(repo)
    with repo.transaction() as tx:
        LifecycleStopService.request_in(tx,request_id='stop',run_ref='r',plan_ref='p',
            graph_ref='g1',reason='requested',policy='cancel')
        target=tx.scan('lifecycle_stop_requests')[0]['key']
    entered,release=Event(),Event();original=UnitOfWork.put
    def barrier(tx,kind,key,body,*args,**kwargs):
        result=original(tx,kind,key,body,*args,**kwargs)
        if (first=='proof' and kind=='lifecycle_stop_proofs' or
                first=='late_effect' and kind=='resources' and key=='late'):
            entered.set();assert release.wait(10)
        return result
    monkeypatch.setattr(UnitOfWork,'put',barrier)
    def close(): return asyncio.run(worker.run_once())
    def late():
        with repo.transaction() as tx:
            tx.put('resources','late',{'run_id':'r','scope':'g','status':'active'})
    a_fn,b_fn=(close,late) if first=='proof' else (late,close)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(a_fn);assert entered.wait(10)
        b=pool.submit(b_fn);release.set();a.result(timeout=15);b.result(timeout=15)
    with repo.transaction() as tx:
        assert tx.get('lifecycle_stop_status',target)['body']['status']!='closed'
        for proof in tx.scan('lifecycle_stop_proofs'):
            with pytest.raises(Conflict,match='revision|inventory'):
                DispositionVersions.verify_stop_in(tx,proof['body'])
